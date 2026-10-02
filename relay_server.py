# -*- coding: utf-8 -*-
"""开饭了 · 中继服务器（Relay Server）

在 Windows 助手进程内嵌一个轻量 HTTP/WebSocket 中继，让不同网络下的设备也能互推数据。
接口规范见 docs/RELAY_SERVER.md。当前实现覆盖 P0 + P4 + P5：

    POST /relay/register   设备注册，下发 relayToken
    POST /relay/send       发送消息（在线直投 / 离线入队）
    GET  /relay/poll       长轮询拉取
    POST /relay/ack        确认收到
    GET  /relay/devices    设备列表
    GET  /relay/health     健康检查
    WS   /relay/ws         WebSocket 实时通道（P4）
    SQLite 持久化（P5，可选 db_path）
    端到端加密透传（P5，中继只见密文，e2ee 字段）

设计要点：
- 纯标准库（含自实现 WebSocket 握手与帧编解码），不依赖 PySide2
- 每设备一把 Condition：poll 线程 wait()，新消息 notify_all() 唤醒
- WebSocket 会话用 socketpair 作为唤醒通道，实现毫秒级实时推送
- delivered = 发送时目标是否有活跃 poll 或 ws 会话
- poll/ws 投递的消息进入 inflight，ack 时才真正清除
- 投递后超过 retry_interval_sec 仍未 ack 视为失败，自动回队重试；
  重试次数超过 retry_max 则丢弃（见配置）
"""

import base64
import hashlib
import json
import os
import secrets
import select
import socket
import sqlite3
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


VERSION = "1.1.0"

DEFAULT_RELAY_CONFIG = {
    "enabled": False,
    "port": 8860,
    # 内置默认公网地址（可在 settings.json 的 relay.public_url 覆盖）
    "public_url": "https://relay.lyvw.top",
    "require_token": True,
    "master_token": "",
    "offline_ttl_hours": 24,
    "max_queue_per_device": 50,
    "log_level": "info",
    "db_path": "",          # 空 = 纯内存；非空 = SQLite 持久化
    "ws_enabled": True,     # 是否开放 /relay/ws
    "retry_max": 3,         # 投递未确认的最大重试次数（超过则丢弃；0=不重试）
    "retry_interval_sec": 30,  # 投递后多久未 ack 视为失败并重试（秒）
    "device_ttl_sec": 120,  # 设备无活动超过此时长则清理（秒）
}

MAX_BODY_BYTES = 256 * 1024      # 单请求体积上限
POLL_TIMEOUT_MIN = 1
POLL_TIMEOUT_MAX = 30
POLL_TIMEOUT_DEFAULT = 25
ONLINE_WINDOW_SEC = 60           # 最近 60s 有活动即视为在线
REGISTER_RATE = (3, 60)          # 同 deviceId 60s 内最多注册 3 次
SEND_RATE = (30, 60)             # 单设备 60s 内最多发 30 条
DEFAULT_TTL_HOURS = 24
RETRY_SWEEP_INTERVAL = 5         # 后台重试扫描间隔（秒）
DEVICE_TTL_SEC = 120             # 设备无活动超过此时长则清理（秒）
DEVICE_SWEEP_INTERVAL = 30       # 设备清理扫描间隔（秒）

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_FRAME = 4 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,
    device_name   TEXT,
    platform      TEXT,
    push_mode     TEXT,
    version       TEXT,
    token         TEXT,
    registered_at REAL,
    last_seen     REAL
);
CREATE INDEX IF NOT EXISTS idx_devices_token ON devices(token);

CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,
    to_id        TEXT NOT NULL,
    from_id      TEXT,
    from_name    TEXT,
    type         TEXT,
    payload      TEXT,
    received_at  REAL,
    ttl_hours    INTEGER,
    retries      INTEGER DEFAULT 0,
    e2ee         INTEGER DEFAULT 0,
    state        TEXT DEFAULT 'queued'
);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_id);
"""


def _iso_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================
# WebSocket 帧工具（RFC 6455 基础子集：text / ping / pong / close）
# ============================================================
def _ws_accept_key(client_key):
    return base64.b64encode(
        hashlib.sha1((client_key + WS_GUID).encode()).digest()).decode()


def _ws_build_frame(opcode, payload):
    b1 = 0x80 | (opcode & 0x0F)   # FIN=1
    n = len(payload)
    if n < 126:
        header = struct.pack(">BB", b1, n)
    elif n < 65536:
        header = struct.pack(">BBH", b1, 126, n)
    else:
        header = struct.pack(">BBQ", b1, 127, n)
    return header + payload


def _ws_recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except Exception:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _ws_read_frame(sock):
    """读一帧，返回 (opcode, payload)；连接关闭/出错返回 None。"""
    hdr = _ws_recv_exact(sock, 2)
    if hdr is None:
        return None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = (b2 >> 7) & 1
    plen = b2 & 0x7F
    if plen == 126:
        ext = _ws_recv_exact(sock, 2)
        if ext is None:
            return None
        plen = struct.unpack(">H", ext)[0]
    elif plen == 127:
        ext = _ws_recv_exact(sock, 8)
        if ext is None:
            return None
        plen = struct.unpack(">Q", ext)[0]
    if plen > WS_MAX_FRAME:
        return None
    mask = b""
    if masked:
        mask = _ws_recv_exact(sock, 4)
        if mask is None:
            return None
    if plen:
        payload = _ws_recv_exact(sock, plen)
        if payload is None:
            return None
        if masked:
            ba = bytearray(payload)
            for i in range(plen):
                ba[i] ^= mask[i % 4]
            payload = bytes(ba)
    else:
        payload = b""
    return opcode, payload


# ============================================================
# 限流
# ============================================================
class _RateLimiter(object):
    """滑动窗口限流：window 秒内最多 limit 次。"""

    def __init__(self, limit, window):
        self._limit = int(limit)
        self._window = float(window)
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.time()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = deque()
                self._hits[key] = q
            while q and now - q[0] > self._window:
                q.popleft()
            if len(q) >= self._limit:
                return False
            q.append(now)
            return True


# ============================================================
# 数据模型
# ============================================================
class RelayMessage(object):
    """一条待投递的中继消息。"""

    __slots__ = ("message_id", "from_id", "from_name", "type",
                 "payload", "received_at", "ttl_hours", "retries", "e2ee",
                 "delivered_at")

    def __init__(self, from_id, from_name, mtype, payload, ttl_hours,
                 e2ee=False, message_id=None):
        self.message_id = message_id or ("msg_" + secrets.token_hex(8))
        self.from_id = from_id
        self.from_name = from_name
        self.type = mtype
        self.payload = payload
        self.received_at = time.time()
        self.ttl_hours = ttl_hours
        self.retries = 0
        self.e2ee = bool(e2ee)
        self.delivered_at = None    # 进入 inflight 的时刻（用于重试判定）

    def to_dict(self):
        return {
            "messageId": self.message_id,
            "from": self.from_id,
            "fromName": self.from_name,
            "type": self.type,
            "receivedAt": _iso_utc(self.received_at),
            "e2ee": self.e2ee,
            "payload": self.payload,
        }


class RelayDevice(object):
    """设备注册表条目 + 离线队列 + 活跃会话。"""

    def __init__(self, device_id, device_name, platform, push_mode,
                 version, max_queue, token=None, registered_at=None):
        self.device_id = device_id
        self.device_name = device_name
        self.platform = platform
        self.push_mode = push_mode
        self.version = version
        self.token = token or ("rt_" + secrets.token_urlsafe(24))
        self.registered_at = registered_at or time.time()
        self.last_seen = time.time()
        self.queue = deque(maxlen=max_queue)
        self.inflight = {}         # message_id -> RelayMessage，已投递待确认
        self.cond = threading.Condition()
        self.pollers = 0           # 正在等待的长轮询数量
        self.ws_sessions = set()   # 活跃的 WebSocket 会话

    @property
    def online(self):
        if self.ws_sessions:
            return True
        return (time.time() - self.last_seen) <= ONLINE_WINDOW_SEC

    def to_dict(self):
        return {
            "deviceId": self.device_id,
            "deviceName": self.device_name,
            "platform": self.platform,
            "online": self.online,
            "lastSeen": _iso_utc(self.last_seen),
            "ws": len(self.ws_sessions),
        }


# ============================================================
# 存储：设备注册表 + 离线队列 + 限流 + SQLite 持久化
# ============================================================
class RelayStore(object):
    def __init__(self, config):
        self.cfg = config
        self._devices = {}
        self._tokens = {}
        self._lock = threading.RLock()
        self._db = None
        self._db_lock = threading.RLock()
        self._reg_limiter = _RateLimiter(*REGISTER_RATE)
        self._send_limiter = _RateLimiter(*SEND_RATE)
        self._stopping = False
        self._retry_thread = None
        self.started_at = time.time()
        self._init_db()

    # ---------- SQLite ----------
    def _init_db(self):
        path = (self.cfg.get("db_path") or "").strip()
        if not path:
            return
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            db = sqlite3.connect(path, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(_SCHEMA)
            db.commit()
            self._db = db
            self._load_from_db()
        except Exception:
            self._db = None

    @property
    def db_enabled(self):
        return self._db is not None

    def _db_exec(self, sql, params=()):
        db = self._db
        if db is None:
            return
        try:
            with self._db_lock:
                db.execute(sql, params)
                db.commit()
        except Exception:
            pass

    def _load_from_db(self):
        db = self._db
        if db is None:
            return
        try:
            with self._db_lock:
                dev_rows = db.execute("SELECT * FROM devices").fetchall()
        except Exception:
            return
        try:
            max_queue = int(self.cfg.get("max_queue_per_device") or 50)
        except Exception:
            max_queue = 50
        max_queue = max(1, max_queue)

        for row in dev_rows:
            try:
                dev = RelayDevice(
                    row["device_id"], row["device_name"] or row["device_id"],
                    row["platform"] or "unknown", row["push_mode"] or "poll",
                    row["version"] or "", max_queue,
                    token=row["token"],
                    registered_at=row["registered_at"] or time.time())
                # 恢复真实 last_seen，否则重启后所有设备会被误判为在线
                try:
                    if row["last_seen"]:
                        dev.last_seen = float(row["last_seen"])
                except Exception:
                    pass
                self._devices[dev.device_id] = dev
                if dev.token:
                    self._tokens[dev.token] = dev.device_id
            except Exception:
                continue

        try:
            with self._db_lock:
                msg_rows = db.execute(
                    "SELECT * FROM messages "
                    "WHERE state IN ('queued','inflight') "
                    "ORDER BY received_at ASC").fetchall()
        except Exception:
            return

        for row in msg_rows:
            dev = self._devices.get(row["to_id"])
            if dev is None:
                continue
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except Exception:
                payload = {}
            msg = RelayMessage(
                row["from_id"] or "", row["from_name"] or "",
                row["type"] or "push", payload,
                int(row["ttl_hours"] or DEFAULT_TTL_HOURS),
                e2ee=bool(row["e2ee"]),
                message_id=row["message_id"])
            msg.received_at = row["received_at"] or time.time()
            msg.retries = int(row["retries"] or 0)
            # 无论 queued 还是 inflight，重启后一律回到队列头，
            # 交由客户端按 messageId 去重（避免丢消息）
            dev.queue.append(msg)

    def _db_upsert_device(self, dev):
        self._db_exec(
            "INSERT OR REPLACE INTO devices "
            "(device_id, device_name, platform, push_mode, version, "
            " token, registered_at, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dev.device_id, dev.device_name, dev.platform, dev.push_mode,
             dev.version, dev.token, dev.registered_at, dev.last_seen))

    def _db_insert_message(self, to_id, msg):
        try:
            payload_json = json.dumps(msg.payload, ensure_ascii=False)
        except Exception:
            payload_json = "{}"
        self._db_exec(
            "INSERT OR REPLACE INTO messages "
            "(message_id, to_id, from_id, from_name, type, payload, "
            " received_at, ttl_hours, retries, e2ee, state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (msg.message_id, to_id, msg.from_id, msg.from_name, msg.type,
             payload_json, msg.received_at, int(msg.ttl_hours),
             int(msg.retries), 1 if msg.e2ee else 0, "queued"))

    def _db_mark_inflight(self, ids):
        if not ids:
            return
        db = self._db
        if db is None:
            return
        try:
            with self._db_lock:
                db.executemany(
                    "UPDATE messages SET state='inflight' WHERE message_id=?",
                    [(i,) for i in ids])
                db.commit()
        except Exception:
            pass

    def _db_requeue(self, pairs):
        # pairs: [(message_id, retries), ...]
        if not pairs:
            return
        db = self._db
        if db is None:
            return
        try:
            with self._db_lock:
                db.executemany(
                    "UPDATE messages SET state='queued', retries=? "
                    "WHERE message_id=?",
                    [(int(r), mid) for mid, r in pairs])
                db.commit()
        except Exception:
            pass

    def _db_delete_device(self, device_id):
        if not device_id:
            return
        self._db_exec("DELETE FROM devices WHERE device_id=?", (device_id,))
        self._db_exec("DELETE FROM messages WHERE to_id=?", (device_id,))

    def _db_delete_messages(self, ids):
        if not ids:
            return
        db = self._db
        if db is None:
            return
        try:
            with self._db_lock:
                db.executemany(
                    "DELETE FROM messages WHERE message_id=?",
                    [(i,) for i in ids])
                db.commit()
        except Exception:
            pass

    def close(self):
        db = self._db
        self._db = None
        if db is not None:
            try:
                with self._db_lock:
                    db.commit()
                    db.close()
            except Exception:
                pass

    # ---------- 查询 ----------
    def device_by_token(self, token):
        if not token:
            return None
        with self._lock:
            did = self._tokens.get(token)
            dev = self._devices.get(did) if did else None
        # 任何携带有效 token 的请求都算一次活动，刷新 last_seen，
        # 避免纯发送方设备（从不 poll）被定期清理误删
        if dev is not None:
            dev.last_seen = time.time()
        return dev

    def is_master(self, token):
        mt = (self.cfg.get("master_token") or "").strip()
        return bool(mt) and bool(token) and secrets.compare_digest(mt, token)

    def list_devices(self):
        with self._lock:
            devs = list(self._devices.values())
        return [d.to_dict() for d in devs]

    def remove_device(self, device_id):
        """注销设备：从内存与数据库移除，并唤醒其上的等待者。"""
        if not device_id:
            return False
        with self._lock:
            dev = self._devices.pop(device_id, None)
            if dev is None:
                return False
            self._tokens.pop(dev.token, None)
        try:
            with dev.cond:
                sessions = list(dev.ws_sessions)
                dev.cond.notify_all()
            for s in sessions:
                try:
                    s.wake()
                except Exception:
                    pass
        except Exception:
            pass
        self._db_delete_device(device_id)
        return True

    def _device_sweep(self):
        """清理长时间无活动的设备（防止崩溃/断网后永久残留）。"""
        try:
            ttl = float(self.cfg.get("device_ttl_sec") or DEVICE_TTL_SEC)
        except Exception:
            ttl = float(DEVICE_TTL_SEC)
        now = time.time()
        with self._lock:
            stale = [d.device_id for d in self._devices.values()
                     if not d.ws_sessions and (now - d.last_seen) > ttl]
        for did in stale:
            self.remove_device(did)
        return stale

    def stats(self):
        with self._lock:
            devs = list(self._devices.values())
        queued = 0
        inflight = 0
        ws = 0
        for d in devs:
            with d.cond:
                queued += len(d.queue)
                inflight += len(d.inflight)
                ws += len(d.ws_sessions)
        return {
            "ok": True,
            "version": VERSION,
            "uptime": int(time.time() - self.started_at),
            "devices": len(devs),
            "queued": queued,
            "inflight": inflight,
            "ws": ws,
            "db": self.db_enabled,
            "ws_enabled": bool(self.cfg.get("ws_enabled", True)),
        }

    # ---------- 注册 ----------
    def register(self, body):
        device_id = str(body.get("deviceId") or "").strip()
        if not device_id:
            return 400, {"ok": False, "error": "deviceId required"}
        if not self._reg_limiter.allow(device_id):
            return 429, {"ok": False, "error": "too many registrations"}

        try:
            max_queue = int(self.cfg.get("max_queue_per_device") or 50)
        except Exception:
            max_queue = 50
        max_queue = max(1, max_queue)

        with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                dev = RelayDevice(
                    device_id,
                    str(body.get("deviceName") or device_id),
                    str(body.get("platform") or "unknown"),
                    str(body.get("pushMode") or "poll"),
                    str(body.get("version") or ""),
                    max_queue,
                )
                self._devices[device_id] = dev
            else:
                # 重复注册：更新资料并轮换 token（旧 token 立即失效）
                self._tokens.pop(dev.token, None)
                dev.device_name = str(body.get("deviceName") or dev.device_name)
                dev.platform = str(body.get("platform") or dev.platform)
                dev.push_mode = str(body.get("pushMode") or dev.push_mode)
                dev.version = str(body.get("version") or dev.version)
                dev.token = "rt_" + secrets.token_urlsafe(24)
                dev.last_seen = time.time()
            self._tokens[dev.token] = device_id
        self._db_upsert_device(dev)

        return 200, {
            "ok": True,
            "relayToken": dev.token,
            "publicUrl": self.cfg.get("public_url") or "",
            "ttlSeconds": 86400,
        }

    # ---------- 发送 ----------
    def send(self, sender, to_id, mtype, payload, ttl_hours, e2ee=False):
        if not self._send_limiter.allow(sender.device_id):
            return 429, {"ok": False, "error": "send rate limited"}
        with self._lock:
            target = self._devices.get(to_id)
        if target is None:
            return 404, {"ok": False, "error": "target not registered"}

        msg = RelayMessage(sender.device_id, sender.device_name,
                           mtype, payload, ttl_hours, e2ee=e2ee)
        with target.cond:
            sessions = list(target.ws_sessions)
            delivered = target.pollers > 0 or len(sessions) > 0
            target.queue.append(msg)
            target.cond.notify_all()
            queue_size = len(target.queue)

        self._db_insert_message(target.device_id, msg)
        for s in sessions:
            try:
                s.wake()
            except Exception:
                pass

        return 200, {
            "ok": True,
            "messageId": msg.message_id,
            "delivered": delivered,
            "queueSize": 0 if delivered else queue_size,
        }

    # ---------- 拉取 ----------
    def poll(self, device, timeout):
        deadline = time.time() + timeout
        with device.cond:
            device.last_seen = time.time()
            if not device.queue and not self._stopping:
                device.pollers += 1
                try:
                    remain = deadline - time.time()
                    if remain > 0:
                        device.cond.wait(remain)
                finally:
                    device.pollers -= 1
        device.last_seen = time.time()
        return self.drain(device)

    def drain(self, device):
        """把队列中的消息移入 inflight 并返回（供 poll / ws 使用）。"""
        try:
            ttl = float(self.cfg.get("offline_ttl_hours")
                        or DEFAULT_TTL_HOURS) * 3600.0
        except Exception:
            ttl = DEFAULT_TTL_HOURS * 3600.0
        now = time.time()
        out = []
        with device.cond:
            while device.queue:
                msg = device.queue.popleft()
                if now - msg.received_at > ttl:
                    continue          # 过期消息直接丢弃
                msg.delivered_at = now
                device.inflight[msg.message_id] = msg
                out.append(msg)
            # 清理长期未 ack 的 inflight，避免内存堆积
            stale = [mid for mid, m in device.inflight.items()
                     if now - m.received_at > ttl]
            for mid in stale:
                device.inflight.pop(mid, None)
        if out:
            self._db_mark_inflight([m.message_id for m in out])
        if stale:
            self._db_delete_messages(stale)
        return out

    # ---------- 确认 ----------
    def ack(self, device, message_ids):
        ids = set(message_ids or [])
        if not ids:
            return 0
        n = 0
        confirmed = []
        with device.cond:
            for mid in ids:
                if device.inflight.pop(mid, None) is not None:
                    n += 1
                    confirmed.append(mid)
        self._db_delete_messages(confirmed)
        return n

    # ---------- 投递失败自动重试 ----------
    def start_maintenance(self):
        if self._retry_thread is not None and self._retry_thread.is_alive():
            return
        self._retry_thread = threading.Thread(
            target=self._retry_loop, name="RelayRetry", daemon=True)
        self._retry_thread.start()

    def _retry_loop(self):
        tick = 0
        dev_tick = 0
        while not self._stopping:
            time.sleep(1.0)
            tick += 1
            dev_tick += 1
            if tick >= RETRY_SWEEP_INTERVAL:
                tick = 0
                try:
                    self._retry_sweep()
                except Exception:
                    pass
            if dev_tick >= DEVICE_SWEEP_INTERVAL:
                dev_tick = 0
                try:
                    self._device_sweep()
                except Exception:
                    pass

    def _retry_sweep(self):
        """把投递后长期未 ack 的消息回队重试；超过上限则丢弃。"""
        try:
            interval = float(self.cfg.get("retry_interval_sec") or 30)
        except Exception:
            interval = 30.0
        interval = max(1.0, interval)
        try:
            rmax = int(self.cfg.get("retry_max") or 0)
        except Exception:
            rmax = 0
        now = time.time()
        with self._lock:
            devs = list(self._devices.values())
        for dev in devs:
            requeue = []
            drop = []
            sessions = []
            with dev.cond:
                for mid, msg in list(dev.inflight.items()):
                    if msg.delivered_at is None:
                        continue
                    if now - msg.delivered_at < interval:
                        continue
                    dev.inflight.pop(mid, None)
                    if msg.retries >= rmax:
                        drop.append(mid)      # 重试耗尽，丢弃
                        continue
                    msg.retries += 1
                    msg.delivered_at = None
                    dev.queue.append(msg)
                    requeue.append((mid, msg.retries))
                if requeue:
                    sessions = list(dev.ws_sessions)
                    dev.cond.notify_all()
            if requeue:
                self._db_requeue(requeue)
            if drop:
                self._db_delete_messages(drop)
            for s in sessions:
                try:
                    s.wake()
                except Exception:
                    pass

    # ---------- 关停 ----------
    def wake_all(self):
        self._stopping = True
        with self._lock:
            devs = list(self._devices.values())
        for d in devs:
            with d.cond:
                sessions = list(d.ws_sessions)
                d.cond.notify_all()
            for s in sessions:
                try:
                    s.wake()
                except Exception:
                    pass


# ============================================================
# WebSocket 会话
# ============================================================
class _WSSession(object):
    """一个 WebSocket 会话，run() 阻塞直到连接断开。"""

    def __init__(self, conn, device, store):
        self.conn = conn
        self.device = device
        self.store = store
        self._send_lock = threading.Lock()
        self._running = True
        self._wake_r = None
        self._wake_w = None
        try:
            self._wake_r, self._wake_w = socket.socketpair()
        except Exception:
            self._wake_r = self._wake_w = None

    def wake(self):
        w = self._wake_w
        if w is None:
            return
        try:
            w.send(b"\x01")
        except Exception:
            pass

    def send_json(self, obj):
        if not self._running:
            return
        try:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            return
        frame = _ws_build_frame(0x1, data)
        with self._send_lock:
            if not self._running:
                return
            try:
                self.conn.sendall(frame)
            except Exception:
                self._running = False

    def close(self):
        self._running = False
        for s in (self._wake_r, self._wake_w):
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
        self._wake_r = self._wake_w = None
        try:
            self.conn.close()
        except Exception:
            pass

    def run(self):
        device = self.device
        store = self.store
        with device.cond:
            device.ws_sessions.add(self)
            device.last_seen = time.time()
        try:
            while self._running and not store._stopping:
                watch = [self.conn]
                if self._wake_r is not None:
                    watch.append(self._wake_r)
                try:
                    ready, _, _ = select.select(watch, [], [], 25.0)
                except Exception:
                    break

                if self._wake_r is not None and self._wake_r in ready:
                    try:
                        self._wake_r.recv(4096)
                    except Exception:
                        pass

                if self.conn in ready:
                    frame = _ws_read_frame(self.conn)
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == 0x8:      # close
                        break
                    if opcode == 0x9:      # ping
                        with self._send_lock:
                            try:
                                self.conn.sendall(
                                    _ws_build_frame(0xA, payload))
                            except Exception:
                                break
                    elif opcode == 0xA:    # pong
                        pass
                    elif opcode == 0x1:    # text
                        try:
                            obj = json.loads(payload.decode("utf-8"))
                        except Exception:
                            obj = {}
                        if isinstance(obj, dict) and obj.get("type") == "ping":
                            self.send_json({"type": "pong"})

                # 推送队列中的消息（移入 inflight）
                msgs = store.drain(device)
                for m in msgs:
                    self.send_json(m.to_dict())

                if not self._running:
                    break
                with device.cond:
                    device.last_seen = time.time()
        finally:
            with device.cond:
                device.ws_sessions.discard(self)
            self.close()


# ============================================================
# HTTP Handler
# ============================================================
class _RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KaiFanLe-Relay/" + VERSION

    # ---------- 基础设施 ----------
    def log_message(self, fmt, *args):
        # --windowed 打包后 stderr 可能不可用，这里保持静默
        return

    def _send_json(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            code = 500
            body = b'{"ok":false,"error":"encode failed"}'
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass   # 客户端提前断开（如长轮询超时）时忽略

    def _read_json(self):
        """读请求体，返回 (dict, error_code)。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}, None
        if length > MAX_BODY_BYTES:
            return None, 413
        try:
            raw = self.rfile.read(length)
        except Exception:
            return None, 400
        if not raw:
            return {}, None
        try:
            obj = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            return None, 400
        if not isinstance(obj, dict):
            return None, 400
        return obj, None

    def _bearer(self):
        hdr = self.headers.get("Authorization") or ""
        if hdr[:7].lower() == "bearer ":
            return hdr[7:].strip()
        return ""

    def _auth_device(self):
        return self.server.store.device_by_token(self._bearer())

    def _unauthorized(self):
        self._send_json(401, {"ok": False, "error": "unauthorized"})

    # ---------- WebSocket ----------
    def _handle_ws(self, parsed):
        store = self.server.store
        if not store.cfg.get("ws_enabled", True):
            self._send_json(404, {"ok": False, "error": "ws disabled"})
            return
        upgrade = (self.headers.get("Upgrade") or "").strip().lower()
        if upgrade != "websocket":
            self._send_json(
                400, {"ok": False, "error": "expected websocket upgrade"})
            return
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        if not key:
            self._send_json(
                400, {"ok": False, "error": "missing Sec-WebSocket-Key"})
            return
        token = (parse_qs(parsed.query).get("token") or [""])[0]
        dev = store.device_by_token(token)
        if dev is None:
            self._unauthorized()
            return

        accept = _ws_accept_key(key)
        try:
            self.wfile.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
            self.wfile.flush()
        except Exception:
            return

        self.close_connection = True   # 会话结束后由本 handler 关闭连接
        session = _WSSession(self.connection, dev, store)
        try:
            session.run()
        except Exception:
            pass

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        store = self.server.store

        if path == "/relay/ws":
            self._handle_ws(parsed)
            return

        if path == "/relay/health":
            self._send_json(200, store.stats())
            return

        if path == "/relay/poll":
            dev = self._auth_device()
            if dev is None:
                self._unauthorized()
                return
            try:
                timeout = int((parse_qs(parsed.query).get("timeout")
                               or [POLL_TIMEOUT_DEFAULT])[0])
            except Exception:
                timeout = POLL_TIMEOUT_DEFAULT
            timeout = max(POLL_TIMEOUT_MIN, min(POLL_TIMEOUT_MAX, timeout))
            msgs = store.poll(dev, timeout)
            self._send_json(200, {
                "ok": True,
                "messages": [m.to_dict() for m in msgs],
            })
            return

        if path == "/relay/devices":
            token = self._bearer()
            if store.device_by_token(token) is None and not store.is_master(token):
                self._unauthorized()
                return
            self._send_json(200, {"ok": True, "devices": store.list_devices()})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        store = self.server.store

        if path == "/relay/register":
            body, err = self._read_json()
            if err:
                self._send_json(err, {"ok": False, "error": "bad request"})
                return
            mt = (store.cfg.get("master_token") or "").strip()
            if mt and bool(store.cfg.get("require_token", True)):
                if not secrets.compare_digest(mt, self._bearer()):
                    self._send_json(
                        401, {"ok": False, "error": "bad master token"})
                    return
            code, resp = store.register(body)
            self._send_json(code, resp)
            return

        if path == "/relay/send":
            dev = self._auth_device()
            if dev is None:
                self._unauthorized()
                return
            body, err = self._read_json()
            if err == 413:
                self._send_json(
                    413, {"ok": False, "error": "payload too large"})
                return
            if err:
                self._send_json(err, {"ok": False, "error": "bad request"})
                return
            to_id = str(body.get("to") or "").strip()
            if not to_id:
                self._send_json(400, {"ok": False, "error": "to required"})
                return
            try:
                ttl_hours = int(body.get("ttlHours")
                                or store.cfg.get("offline_ttl_hours")
                                or DEFAULT_TTL_HOURS)
            except Exception:
                ttl_hours = DEFAULT_TTL_HOURS
            code, resp = store.send(
                dev, to_id, str(body.get("type") or "push"),
                body.get("payload") or {}, ttl_hours,
                e2ee=bool(body.get("e2ee", False)))
            self._send_json(code, resp)
            return

        if path == "/relay/unregister":
            token = self._bearer()
            dev = store.device_by_token(token)
            master = store.is_master(token)
            if not master and dev is None:
                self._unauthorized()
                return
            body, err = self._read_json()
            if err:
                self._send_json(err, {"ok": False, "error": "bad request"})
                return
            target = ""
            if isinstance(body, dict):
                target = str(body.get("deviceId") or "").strip()
            if target and not master and (dev is None or dev.device_id != target):
                self._send_json(403, {"ok": False, "error": "forbidden"})
                return
            if target:
                store.remove_device(target)
            elif dev is not None:
                store.remove_device(dev.device_id)
            # 幂等：无论是否存在都返回 ok
            self._send_json(200, {"ok": True})
            return

        if path == "/relay/ack":
            dev = self._auth_device()
            if dev is None:
                self._unauthorized()
                return
            body, err = self._read_json()
            if err:
                self._send_json(err, {"ok": False, "error": "bad request"})
                return
            ids = body.get("messageIds") or []
            if not isinstance(ids, list):
                ids = []
            self._send_json(200, {
                "ok": True,
                "acked": store.ack(dev, [str(x) for x in ids]),
            })
            return

        self._send_json(404, {"ok": False, "error": "not found"})


# ============================================================
# 门面：RelayServer
# ============================================================
class RelayServer(object):
    """中继服务门面：start() / stop() / is_running()。

    不继承 QThread，避免依赖 Qt；内部用 daemon 线程跑 ThreadingHTTPServer。
    """

    def __init__(self, config=None, logger=None):
        cfg = dict(DEFAULT_RELAY_CONFIG)
        if isinstance(config, dict):
            for k, v in config.items():
                if k in cfg:
                    cfg[k] = v
        self.cfg = cfg
        self.store = RelayStore(cfg)
        self._logger = logger
        self._httpd = None
        self._thread = None

    @property
    def port(self):
        try:
            return int(self.cfg.get("port") or DEFAULT_RELAY_CONFIG["port"])
        except Exception:
            return DEFAULT_RELAY_CONFIG["port"]

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return True
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _RelayHandler)
        except Exception as e:
            self._log("[Relay] ❌ 监听 :%s 失败: %s" % (self.port, e))
            return False
        httpd.daemon_threads = True
        httpd.store = self.store
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.3},
            name="RelayServer", daemon=True)
        self._thread.start()
        try:
            self.store.start_maintenance()
        except Exception:
            pass
        self._log("[Relay] ✅ 中继服务已启动 :%s (ws=%s, db=%s)" % (
            self.port, bool(self.cfg.get("ws_enabled", True)),
            self.store.db_enabled))
        return True

    def stop(self):
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                self.store.wake_all()     # 唤醒阻塞中的长轮询与 ws 会话
            except Exception:
                pass
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        t = self._thread
        self._thread = None
        if t is not None and t is not threading.current_thread():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        try:
            self.store.close()
        except Exception:
            pass
        self._log("[Relay] ⏹ 中继服务已停止")

    # ---------- 地址 ----------
    def public_url(self):
        return (self.cfg.get("public_url") or "").strip()

    def local_url(self):
        return "http://127.0.0.1:%d" % self.port

    def _log(self, msg):
        fn = self._logger
        if fn is not None:
            try:
                fn(msg)
                return
            except Exception:
                pass
        try:
            print(msg)
        except Exception:
            pass
