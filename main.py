#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开饭了助手 - Windows 置顶工具
- 方案 C：复制链接 / 窗口激活时触发扫描
- 方案 D：监听手机端 UDP 广播，毫秒级发现
- 防火墙规则由 init_firewall.bat 一次性配置
"""

import sys
import os
import re
import json
import random
import socket
import threading
import http.client
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from PySide2.QtCore import (
    Qt, QTimer, QThread, Signal, Slot, QPoint, QProcess
)
from PySide2.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QBrush, QLinearGradient,
    QFontMetrics, QKeySequence
)
from PySide2.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QSystemTrayIcon, QMenu, QAction, QLineEdit, QListWidget, QListWidgetItem,
    QSizePolicy, QFrame, QShortcut
)

# ============================================================
# 全局热键（keyboard 库；未安装时降级为窗口内热键）
# ============================================================
try:
    import keyboard as _keyboard
    HAS_GLOBAL_HOTKEY = True
except Exception:
    _keyboard = None
    HAS_GLOBAL_HOTKEY = False

# ============================================================
# 高 DPI 自适应（必须在创建 QApplication 之前设置）
# ============================================================
if hasattr(Qt, "AA_EnableHighDpiScaling"):
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)

if hasattr(Qt, "AA_UseHighDpiPixmaps"):
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

# ============================================================
# 全局 UI 缩放（根据屏幕分辨率动态调整）
# ============================================================
UI_SCALE = 1.0
SCALE_MULTIPLIER = 1.0

SETTINGS_FILE = os.path.join(
    os.path.expanduser("~"), ".kai_fan_le_helper_settings.json"
)


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def compute_ui_scale():
    app = QApplication.instance()
    screen = app.primaryScreen() if app else None
    if screen is None:
        return 1.0
    geo = screen.availableGeometry()
    w, h = geo.width(), geo.height()
    if w <= 0 or h <= 0:
        return 1.0
    res_scale = min(w / 1920.0, h / 1080.0)
    return max(1.0, min(res_scale, 2.0))


def sc(value):
    return max(1, int(round(value * UI_SCALE * SCALE_MULTIPLIER)))


# ============================================================
# 配置
# ============================================================
PORT = 8848
BROADCAST_PORT = 8849
HANDSHAKE_PORT = 8850   # TCP 主动握手端口（手机打开时主动连本机）
SCAN_TIMEOUT = 0.3
SCAN_MAX_WORKERS = 128
HEARTBEAT_INTERVAL = 6
HEARTBEAT_TIMEOUT = 1.5
CLIPBOARD_DEBOUNCE = 400
SEND_TIMEOUT = 4
MAX_HISTORY = 50

SCAN_BACKOFF_SEQUENCE = [5, 5, 5, 5, 5, 5, 10, 15, 30, 60]
IDLE_SCAN_INTERVAL = 3   # 无设备时的重扫间隔（秒）
KNOWN_IPS_MAX = 10       # 最多记住多少个曾连上的 IP

WIN_WIDTH = 380
WIN_HEIGHT = 44

INPUT_WIDTH = 150
STATUS_MIN_W = 56
STATUS_MAX_W = 110

THEME_POLL_INTERVAL = 2000

DEFAULT_HOTKEY = "F1"
HOTKEY_OPTIONS = [f"F{i}" for i in range(1, 13)]

HISTORY_FILE = os.path.join(
    os.path.expanduser("~"), ".kai_fan_le_helper_history.json"
)

CONN_ERROR_KEYWORDS = [
    "connection", "refused", "timed out", "timeout",
    "unreachable", "reset", "aborted", "broken pipe",
    "10061", "10060", "10054", "10053"
]

DOUYIN_HINTS = [
    "v.douyin.com",
    "douyin.com",
    "iesdouyin.com",
    "复制打开抖音",
    "复制此链接，打开dou音",
    "复制此链接，打开抖音",
    "打开dou音搜索",
    "打开抖音搜索",
    "抖音搜索",
    "dou音搜索",
]


# ============================================================
# 随机中文姓名（贴近 2020 年代真实取名习惯）
# ============================================================
# 常见姓氏，按人口比例加权（越常见重复越多）
SURNAME_POOL = (
    "王" * 7 + "李" * 7 + "张" * 7 + "刘" * 5 + "陈" * 5 +
    "杨" * 3 + "黄" * 3 + "赵" * 2 + "吴" * 2 + "周" * 2 +
    "徐" * 2 + "孙" * 2 + "马" * 2 + "朱" * 2 + "胡" * 2 +
    "郭" * 2 + "何" * 2 + "高" * 2 + "林" * 2 + "罗" * 2 +
    "郑" + "梁" + "谢" + "宋" + "唐" + "许" + "韩" + "冯" + "邓" + "曹" +
    "彭" + "曾" + "肖" + "田" + "董" + "袁" + "潘" + "于" + "蒋" + "蔡" +
    "余" + "杜" + "叶" + "程" + "苏" + "魏" + "吕" + "丁" + "任" + "沈" +
    "姚" + "卢" + "姜" + "崔" + "钟" + "谭" + "陆" + "汪" + "范" + "金" +
    "石" + "廖" + "贾" + "夏" + "韦" + "傅" + "方" + "白" + "邹" + "孟" +
    "熊" + "秦" + "邱" + "江" + "尹" + "薛" + "闫" + "段" + "雷" + "侯" +
    "龙" + "史" + "陶" + "黎" + "贺" + "顾" + "毛" + "郝" + "龚" + "邵"
)

# 男孩常用双字名（近年热门）
MALE_NAMES = [
    "宇轩", "浩宇", "子轩", "浩然", "俊杰", "宇航", "沐辰", "奕辰",
    "子墨", "泽宇", "一鸣", "天佑", "明轩", "睿轩", "昱辰", "昊然",
    "承泽", "思远", "梓豪", "睿泽", "俊熙", "铭泽", "皓轩", "星辰",
    "锦程", "亦辰", "亦泽", "书豪", "柏宇", "博文", "梓轩", "昊宇",
    "嘉豪", "子豪", "俊宇", "逸辰", "泽楷", "予安", "予泽", "景行",
    "致远", "锦泽", "沐阳", "宇宸", "瑞霖", "泽睿", "皓宇", "思齐",
]

# 女孩常用双字名（近年热门）
FEMALE_NAMES = [
    "欣怡", "梓涵", "诗涵", "雨桐", "语汐", "若曦", "可馨", "思彤",
    "嘉怡", "梦琪", "紫萱", "依诺", "一诺", "芷晴", "悦涵", "语桐",
    "诗琪", "晨曦", "若彤", "梦瑶", "佳怡", "雨欣", "诗蕊", "语嫣",
    "晓彤", "语晨", "恬欣", "依涵", "梓萱", "若涵", "汐月", "悦昕",
    "语诺", "沐妍", "书瑶", "婉清", "楚涵", "思妍", "瑾萱", "语乔",
    "思琪", "可欣", "雅涵", "雨萱", "诗妍", "语昕", "念安", "知微",
]

# 单字名（较传统的取名方式）
SINGLE_GIVEN = [
    "伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "军", "洋",
    "勇", "艳", "杰", "娟", "涛", "明", "超", "霞", "平", "刚",
    "华", "文", "玉", "建", "国", "志", "海", "峰", "鹏", "浩",
    "宇", "轩", "涵", "欣", "怡", "佳", "琪", "诺", "宸", "泽",
]


def generate_name():
    surname = random.choice(SURNAME_POOL)
    if random.random() < 0.85:
        # 双字名为现代主流
        pool = MALE_NAMES if random.random() < 0.5 else FEMALE_NAMES
        given = random.choice(pool)
    else:
        given = random.choice(SINGLE_GIVEN)
    return surname + given


# ============================================================
# 剪贴板过滤
# ============================================================
_FILE_EXT_PATTERN = re.compile(
    r'\.(apk|exe|zip|rar|7z|tar|gz|bz2|xz|'
    r'png|jpg|jpeg|gif|bmp|webp|svg|ico|'
    r'mp4|avi|mov|mkv|flv|wmv|webm|'
    r'mp3|wav|flac|aac|ogg|m4a|'
    r'txt|doc|docx|xls|xlsx|ppt|pptx|pdf|'
    r'py|js|ts|java|cpp|c|h|hpp|cs|go|rs|rb|php|'
    r'json|xml|yaml|yml|toml|ini|cfg|conf|log|md|'
    r'html|htm|css|scss|less|sql|sh|bat|ps1'
    r')$',
    re.IGNORECASE
)


def is_noise_clipboard(text):
    if not text:
        return True
    s = text.strip()
    if not s:
        return True
    if s.lower().startswith('file:'):
        return True
    if re.match(r'^[A-Za-z]:[\\/]', s):
        return True
    if s.startswith('\\\\'):
        return True
    if s.startswith('/') and not s.startswith('//'):
        return True
    if ' ' not in s and '\n' not in s and _FILE_EXT_PATTERN.search(s):
        return True
    return False


def looks_like_douyin_share(text):
    if not text:
        return False
    s = text.lower()
    for h in DOUYIN_HINTS:
        if h.lower() in s:
            return True
    return False


# ============================================================
# 主题管理
# ============================================================
class ThemeManager:
    mode = "auto"
    current = "dark"

    @classmethod
    def detect_system(cls):
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if value == 1 else "dark"
        except Exception:
            return "light"

    @classmethod
    def apply_mode(cls):
        if cls.mode == "auto":
            cls.current = cls.detect_system()
        else:
            cls.current = cls.mode
        return cls.current

    @classmethod
    def colors(cls):
        return cls.palette(cls.current)

    @classmethod
    def palette(cls, mode):
        if mode == "dark":
            return {
                "bg":           "rgba(28, 28, 30, 0.97)",
                "bg_solid":     "#1c1c1e",
                "border":       "rgba(255, 255, 255, 0.14)",
                "text":         "#FFFFFF",
                "text_sub":     "#8E8E93",
                "text_dim":     "#6B6B70",
                "input_bg":     "rgba(255, 255, 255, 0.08)",
                "input_focus":  "rgba(255, 255, 255, 0.14)",
                "hover":        "rgba(255, 255, 255, 0.10)",
                "hover_strong": "rgba(255, 255, 255, 0.18)",
                "list_sel":     "#6366F1",
                "list_hover":   "rgba(255, 255, 255, 0.08)",
                "close_color":  "#8E8E93",
                "close_hover":  "#FFFFFF",
                "danger_bg":    "rgba(255, 59, 48, 0.20)",
                "danger_text":  "#FF453A",
            }
        else:
            return {
                "bg":           "rgba(255, 255, 255, 0.98)",
                "bg_solid":     "#f2f2f2",
                "border":       "rgba(0, 0, 0, 0.10)",
                "text":         "#1C1C1E",
                "text_sub":     "#6E6E73",
                "text_dim":     "#AEAEB2",
                "input_bg":     "rgba(0, 0, 0, 0.05)",
                "input_focus":  "rgba(0, 0, 0, 0.09)",
                "hover":        "rgba(0, 0, 0, 0.05)",
                "hover_strong": "rgba(0, 0, 0, 0.10)",
                "list_sel":     "#6366F1",
                "list_hover":   "rgba(0, 0, 0, 0.05)",
                "close_color":  "#8E8E93",
                "close_hover":  "#1C1C1E",
                "danger_bg":    "rgba(255, 59, 48, 0.12)",
                "danger_text":  "#FF3B30",
            }


# ============================================================
# 剧名解析
# ============================================================
CATEGORY_TAGS = {
    "漫剧", "AI漫剧", "AI", "ai", "AI动画", "ai漫剧", "AI动漫",
    "好剧推荐", "短剧", "短剧推荐", "追剧", "追剧推荐",
    "抖音", "视频", "电影", "动漫", "电视剧", "影视",
    "推荐", "日常", "热播", "新剧", "剧", "漫",
    "动漫推荐", "推荐短剧", "影视剪辑", "剪辑",
    "douyin", "Douyin", "抖音短剧",
}

PREFIX_TOKENS = [
    "复制打开抖音，看看",
    "复制打开抖音看看",
    "复制此链接，打开Dou音搜索，直接观看视频",
    "复制此链接，打开抖音搜索，直接观看视频",
    "复制此链接，打开Dou音搜索",
    "复制此链接，打开抖音搜索",
    "复制此链接，打开Dou音",
    "复制此链接，打开抖音",
    "打开Dou音搜索",
    "打开抖音搜索",
]

CUT_TOKENS = [
    "复制此链接", "打开Dou音", "直接观看视频",
    "复制链接", "打开抖音", "看看TA的视频",
    "打开Dou音搜索",
]


class TitleParser:
    @staticmethod
    def parse(text):
        if not text:
            return None, False
        s = text.strip()
        s = s.replace('＃', '#').replace('：', ':')
        s = re.sub(r'https?://\S+', '', s, flags=re.IGNORECASE)
        s = re.sub(r'【[^】]*】', ' ', s)
        for p in PREFIX_TOKENS:
            s = s.replace(p, ' ')
        s = s.strip()
        s = re.sub(r'^[\d.]+\s+', '', s)
        s = s.strip()
        if not s:
            return None, False
        if '#' in s:
            title = TitleParser._from_hashtag(s)
        else:
            title = TitleParser._from_plain(s)
        if not title:
            return None, False
        is_fast = False
        m = re.search(r'\s*[-~－\u2010-\u2015]\s*极速\s*$', title)
        if m:
            base = title[:m.start()].strip()
            if base:
                title = base
                is_fast = True
        return title, is_fast

    @staticmethod
    def _from_plain(s):
        return TitleParser._extract_from_text(s)

    @staticmethod
    def _extract_from_text(s):
        tokens = s.split()
        if not tokens:
            return None
        collected = []
        for tok in reversed(tokens):
            if TitleParser._is_noise(tok):
                continue
            collected.insert(0, tok)
        candidate = ' '.join(collected).strip()
        if not candidate:
            return None
        return TitleParser._clean(candidate)

    @staticmethod
    def _from_hashtag(s):
        parts = s.split('#')
        before = parts[0].strip()
        tags = [p.strip() for p in parts[1:]]

        if before and before not in CATEGORY_TAGS:
            cleaned = TitleParser._clean(before)
            if cleaned:
                return cleaned

        for tag in tags:
            if not tag:
                continue
            if tag in CATEGORY_TAGS:
                continue
            cleaned = TitleParser._extract_from_text(tag)
            if cleaned:
                return cleaned

        if before:
            cleaned = TitleParser._clean(before)
            if cleaned:
                return cleaned
        return None

    @staticmethod
    def _is_noise(tok):
        if not tok:
            return True
        if all(c.isdigit() or c == '.' for c in tok):
            return True
        if re.match(r'^\d{1,2}/\d{1,2}$', tok):
            return True
        if re.match(r'^:?\d{1,2}(am|pm|AM|PM)$', tok):
            return True
        if '@' in tok and re.match(r'^[A-Za-z]?@[A-Za-z0-9._]+$', tok):
            return True
        if re.match(r'^[A-Za-z]{1,5}:?/?$', tok):
            return True
        return False

    @staticmethod
    def _clean(s):
        for tok in CUT_TOKENS:
            idx = s.find(tok)
            if idx >= 0:
                s = s[:idx]
        return s.strip(' \t\n!！。.,，、;；:：/\\@#')


# ============================================================
# 历史记录
# ============================================================
class History:
    def __init__(self):
        self.items = []
        self.load()

    def load(self):
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.items = [
                            it for it in data[:MAX_HISTORY]
                            if isinstance(it, dict)
                            and 'text' in it and 'title' in it
                        ]
        except Exception:
            self.items = []

    def save(self):
        try:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.items[:MAX_HISTORY], f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, text, title):
        self.items = [it for it in self.items if it.get("title") != title]
        self.items.insert(0, {
            "text": text,
            "title": title,
            "time": datetime.now().strftime("%m-%d %H:%M"),
        })
        self.items = self.items[:MAX_HISTORY]
        self.save()

    def clear(self):
        self.items = []
        self.save()


# ============================================================
# 网络工具
# ============================================================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def check_ip(ip, port=PORT, timeout=SCAN_TIMEOUT):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(b"GET /ping HTTP/1.0\r\nHost: " + ip.encode() + b"\r\n\r\n")
        data = s.recv(4096)
        s.close()

        if b'"ok"' not in data and b'200 OK' not in data:
            return None

        device_name = "开饭了"
        if b'\r\n\r\n' in data:
            try:
                body = data.split(b'\r\n\r\n', 1)[1]
                info = json.loads(body.decode('utf-8', errors='ignore'))
                device_name = info.get("device") or info.get("model") or "开饭了"
            except Exception:
                pass
        return (ip, device_name)
    except Exception:
        return None


def scan_network(port=PORT, priority_ips=None):
    local_ip = get_local_ip()
    if not local_ip:
        return []

    # 先快速探测"曾连上过的 IP"，命中就立即返回，避免整网段扫描
    if priority_ips:
        hits = []
        with ThreadPoolExecutor(max_workers=min(len(priority_ips), 16)) as ex:
            futures = {ex.submit(check_ip, ip, port, 0.5): ip
                       for ip in priority_ips}
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    hits.append(result)
        if hits:
            hits.sort(key=lambda x: tuple(int(p) for p in x[0].split('.')))
            return hits

    prefix = '.'.join(local_ip.split('.')[:3]) + '.'
    ips = [f"{prefix}{i}" for i in range(1, 255)]

    found = []
    with ThreadPoolExecutor(max_workers=SCAN_MAX_WORKERS) as ex:
        futures = {ex.submit(check_ip, ip, port): ip for ip in ips}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                found.append(result)

    found.sort(key=lambda x: tuple(int(p) for p in x[0].split('.')))
    return found


def ping_phone(ip, port=PORT, timeout=HEARTBEAT_TIMEOUT):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(b"GET /ping HTTP/1.0\r\nHost: " + ip.encode() + b"\r\n\r\n")
        data = s.recv(4096)
        s.close()
        return b'"ok"' in data or b'200 OK' in data
    except Exception:
        return False


# 直接用 http.client，完全绕开系统代理与 urllib（PyInstaller 打包后
# urllib 的代理处理可能失效，且系统代理会让局域网请求延迟 8-10 秒）
def send_to_phone(ip, text, port=PORT, timeout=SEND_TIMEOUT):
    body = json.dumps({"text": text}, ensure_ascii=False).encode('utf-8')
    conn = None
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        conn.request(
            "POST", "/submit", body=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "KaiFanLe-Helper/1.0",
                "Content-Length": str(len(body)),
                "Connection": "close",
            },
        )
        resp = conn.getresponse()
        data = resp.read().decode('utf-8')
        return json.loads(data)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# 扫描线程
# ============================================================
class DiscoveryWorker(QThread):
    finished_scan = Signal(list, int)

    def __init__(self, port=PORT, worker_id=0, priority_ips=None):
        super().__init__()
        self.port = port
        self.worker_id = worker_id
        self.priority_ips = priority_ips or []

    def run(self):
        try:
            result = scan_network(self.port, self.priority_ips)
        except Exception:
            result = []
        self.finished_scan.emit(result, self.worker_id)


# ============================================================
# UDP 广播监听线程
# ============================================================
class BroadcastListener(QThread):
    device_announced = Signal(str, int)

    def __init__(self, listen_port=BROADCAST_PORT):
        super().__init__()
        self.listen_port = listen_port
        self._running = True
        self._sock = None

    def stop(self):
        self._running = False
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.bind(('0.0.0.0', self.listen_port))
            except Exception as e:
                print(f"[Broadcast] ❌ 绑定 {self.listen_port} 失败: {e}")
                return
            sock.settimeout(1.0)
            self._sock = sock
            print(f"[Broadcast] ✅ 已监听 UDP :{self.listen_port}")
        except Exception as e:
            print(f"[Broadcast] ❌ 创建 socket 失败: {e}")
            return

        while self._running:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                break

            try:
                msg = json.loads(data.decode('utf-8', errors='ignore'))
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue
            if msg.get('magic') != 'KFL':
                continue
            if msg.get('action') != 'hello':
                continue

            try:
                port = int(msg.get('port') or PORT)
            except Exception:
                port = PORT

            ip = addr[0]
            print(f"[Broadcast] 📥 收到广播 from {ip}:{port}")
            if ip and ip != '0.0.0.0':
                self.device_announced.emit(ip, port)

        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass


# ============================================================
# TCP 主动握手监听（手机打开/回前台时主动连本机，不依赖 UDP 广播）
# ============================================================
class HandshakeListener(QThread):
    handshake_received = Signal(str, str)  # ip, device_name

    def __init__(self, listen_port=HANDSHAKE_PORT):
        super().__init__()
        self.listen_port = listen_port
        self._running = True
        self._srv = None

    def stop(self):
        self._running = False
        try:
            if self._srv is not None:
                self._srv.close()
        except Exception:
            pass

    def run(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('0.0.0.0', self.listen_port))
            srv.listen(5)
            srv.settimeout(1.0)
            self._srv = srv
            print(f"[Handshake] ✅ 已监听 TCP :{self.listen_port}")
        except Exception as e:
            print(f"[Handshake] ❌ 监听失败: {e}")
            return

        while self._running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            try:
                conn.settimeout(2.0)
                data = conn.recv(4096)
                if b'\r\n\r\n' in data:
                    body = data.split(b'\r\n\r\n', 1)[1]
                else:
                    body = data
                try:
                    msg = json.loads(body.decode('utf-8', errors='ignore'))
                except Exception:
                    msg = {}

                if msg.get('magic') == 'KFL' and msg.get('action') == 'hello':
                    ip = addr[0]
                    name = msg.get('device') or '手机'
                    self.handshake_received.emit(ip, name)
                    resp = json.dumps({"ok": True}).encode('utf-8')
                    conn.sendall(
                        b'HTTP/1.1 200 OK\r\n'
                        b'Content-Type: application/json\r\n'
                        b'Content-Length: ' + str(len(resp)).encode() + b'\r\n'
                        b'Connection: close\r\n\r\n' + resp)
                else:
                    conn.sendall(
                        b'HTTP/1.1 400 Bad Request\r\n'
                        b'Content-Length: 0\r\n'
                        b'Connection: close\r\n\r\n')
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


# ============================================================
# 图标
# ============================================================
def create_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    gradient = QLinearGradient(0, 0, 64, 64)
    gradient.setColorAt(0, QColor("#60A5FA"))
    gradient.setColorAt(1, QColor("#6366F1"))
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)

    painter.setPen(QColor("white"))
    font = QFont("Microsoft YaHei", 30)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "饭")
    painter.end()
    return QIcon(pixmap)


# ============================================================
# 设备选择面板
# ============================================================
class DevicePanel(QWidget):
    device_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setFixedWidth(WIN_WIDTH)
        self.devices = []
        self.current_index = 0
        self._build()
        self.apply_theme()

    def _build(self):
        self.container = QWidget(self)
        self.container.setObjectName("container")
        self.container.setAttribute(Qt.WA_StyledBackground, True)

        self.title = QLabel("📱 选择目标设备")
        self.title.setObjectName("title")

        self.list = QListWidget()
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setAutoFillBackground(False)
        self.list.viewport().setAutoFillBackground(False)
        self.list.itemDoubleClicked.connect(self._on_double_click)

        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("closeBtn")
        self.close_btn.setFixedHeight(sc(26))
        self.close_btn.clicked.connect(self.hide)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addStretch()
        bottom.addWidget(self.close_btn)

        layout = QVBoxLayout()
        layout.setContentsMargins(sc(12), sc(10), sc(12), sc(10))
        layout.setSpacing(sc(6))
        layout.addWidget(self.title)
        layout.addWidget(self.list, 1)
        layout.addLayout(bottom)
        self.container.setLayout(layout)

        self._relayout()

    def showEvent(self, event):
        self.apply_theme()
        super().showEvent(event)

    def apply_theme(self):
        c = ThemeManager.colors()
        self.container.setStyleSheet(f"""
            #container {{
                background: {c['bg_solid']};
                border-radius: {sc(12)}px;
                border: 1px solid {c['border']};
            }}
            QLabel#title {{
                color: {c['text']};
                font-size: {sc(13)}px; font-weight: 600;
                padding: {sc(4)}px;
                background: transparent;
            }}
            QListWidget {{
                background: transparent;
                border: none;
                outline: none;
                color: {c['text']};
                font-size: {sc(13)}px;
            }}
            QListWidget::item {{
                padding: {sc(10)}px {sc(14)}px;
                border-radius: {sc(6)}px;
                margin: {sc(2)}px {sc(6)}px;
            }}
            QListWidget::item:selected {{
                background: {c['list_sel']};
                color: #FFFFFF;
            }}
            QListWidget::item:hover {{
                background: {c['list_hover']};
            }}
            QPushButton#closeBtn {{
                background: {c['input_bg']};
                color: {c['text']};
                border: none; border-radius: {sc(8)}px;
                padding: 0 {sc(14)}px; font-size: {sc(12)}px;
            }}
            QPushButton#closeBtn:hover {{
                background: {c['hover_strong']};
            }}
        """)

    def _relayout(self):
        n = len(self.devices) if self.devices else 1
        h = sc(60) + min(n, 6) * sc(44) + sc(40)
        self.setFixedHeight(h)
        self.container.setGeometry(0, 0, self.width(), h)

    def refresh(self, devices, current_index):
        self.devices = devices
        self.current_index = current_index
        self.list.clear()

        if not devices:
            it = QListWidgetItem("(未发现任何设备)")
            it.setFlags(Qt.NoItemFlags)
            self.list.addItem(it)
            self._relayout()
            return

        for i, (ip, name) in enumerate(devices):
            mark = "  ✅" if i == current_index else ""
            text = f"{name}{mark}\n{ip}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, i)
            if i == current_index:
                item.setSelected(True)
            self.list.addItem(item)

        self._relayout()

    def _on_double_click(self, item):
        idx = item.data(Qt.UserRole)
        if idx is not None:
            self.device_selected.emit(int(idx))
            self.hide()


# ============================================================
# 历史记录弹窗
# ============================================================
class HistoryPanel(QWidget):
    item_selected = Signal(str)

    def __init__(self, history, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setFixedWidth(WIN_WIDTH)
        self.history = history
        self._build()
        self.apply_theme()

    def _build(self):
        self.container = QWidget(self)
        self.container.setObjectName("container")
        self.container.setAttribute(Qt.WA_StyledBackground, True)

        self.title = QLabel("📋 历史记录（双击填入）")
        self.title.setObjectName("title")

        self.list = QListWidget()
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setAutoFillBackground(False)
        self.list.viewport().setAutoFillBackground(False)
        self.list.itemDoubleClicked.connect(self._on_double_click)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.setFixedHeight(sc(26))
        self.clear_btn.clicked.connect(self._on_clear)

        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("closeBtn")
        self.close_btn.setFixedHeight(sc(26))
        self.close_btn.clicked.connect(self.hide)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(sc(6))
        bottom.addStretch()
        bottom.addWidget(self.clear_btn)
        bottom.addWidget(self.close_btn)

        layout = QVBoxLayout()
        layout.setContentsMargins(sc(12), sc(10), sc(12), sc(10))
        layout.setSpacing(sc(6))
        layout.addWidget(self.title)
        layout.addWidget(self.list, 1)
        layout.addLayout(bottom)
        self.container.setLayout(layout)
        self.container.setGeometry(0, 0, WIN_WIDTH, sc(320))

    def showEvent(self, event):
        self.apply_theme()
        super().showEvent(event)

    def apply_theme(self):
        c = ThemeManager.colors()
        self.container.setStyleSheet(f"""
            #container {{
                background: {c['bg_solid']};
                border-radius: {sc(12)}px;
                border: 1px solid {c['border']};
            }}
            QLabel#title {{
                color: {c['text']};
                font-size: {sc(13)}px; font-weight: 600;
                padding: {sc(4)}px;
                background: transparent;
            }}
            QListWidget {{
                background: transparent;
                border: none;
                outline: none;
                color: {c['text']};
                font-size: {sc(13)}px;
            }}
            QListWidget::item {{
                padding: {sc(8)}px {sc(14)}px;
                border-radius: {sc(6)}px;
                margin: {sc(2)}px {sc(6)}px;
            }}
            QListWidget::item:selected {{
                background: {c['list_sel']};
                color: #FFFFFF;
            }}
            QListWidget::item:hover {{
                background: {c['list_hover']};
            }}
            QPushButton#closeBtn {{
                background: {c['input_bg']};
                color: {c['text']};
                border: none; border-radius: {sc(8)}px;
                padding: 0 {sc(14)}px; font-size: {sc(12)}px;
            }}
            QPushButton#closeBtn:hover {{
                background: {c['hover_strong']};
            }}
            QPushButton#clearBtn {{
                background: {c['danger_bg']};
                color: {c['danger_text']};
                border: none; border-radius: {sc(8)}px;
                padding: 0 {sc(14)}px; font-size: {sc(12)}px;
            }}
            QPushButton#clearBtn:hover {{
                background: {c['danger_bg']};
            }}
        """)

    def refresh(self):
        self.list.clear()
        if not self.history.items:
            it = QListWidgetItem("(暂无历史记录)")
            it.setFlags(Qt.NoItemFlags)
            self.list.addItem(it)
            return
        for it in self.history.items:
            text = f"{it['title']}"
            if it.get('time'):
                text += f"  ·  {it['time']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, it['text'])
            self.list.addItem(item)

    def _on_double_click(self, item):
        text = item.data(Qt.UserRole)
        if text:
            self.item_selected.emit(text)
            self.hide()

    def _on_clear(self):
        self.history.clear()
        self.refresh()


# ============================================================
# 主窗口
# ============================================================
class MainWindow(QWidget):
    devices_offline_signal = Signal(list)
    send_result_signal = Signal(str, str, dict)
    broadcast_hit_signal = Signal(str, int)
    global_hotkey_signal = Signal()

    SCALE_OPTIONS = [
        ("自动", 1.0),
        ("1.25x", 1.25),
        ("1.5x", 1.5),
        ("1.75x", 1.75),
        ("2.0x", 2.0),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("开饭了助手")
        self.setFixedSize(WIN_WIDTH, WIN_HEIGHT)
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint |
            Qt.FramelessWindowHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowIcon(create_icon())

        self.devices = []
        self.current_index = 0

        self.last_clipboard = ""
        self._drag_pos = None
        self._discovering = False
        self._scan_silent = False
        self._quitting = False
        self._scan_hard_timeout = None

        self._scan_backoff_idx = 0
        self._scan_id = 0

        settings = load_settings()
        self._auto_scan = bool(settings.get("auto_scan", True))
        self._hotkey = str(settings.get("hotkey", DEFAULT_HOTKEY))
        self._hotkey_enabled = bool(settings.get("hotkey_enabled", True))
        self._show_name_btn = bool(settings.get("show_name_btn", True))

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._restore_status)

        self._send_guard_timer = QTimer(self)
        self._send_guard_timer.setSingleShot(True)
        self._send_guard_timer.timeout.connect(self._recover_send_btn)

        self._recent_broadcast = {}

        self.history = History()

        self.devices_offline_signal.connect(self._on_devices_offline)
        self.send_result_signal.connect(self._on_send_result)
        self.broadcast_hit_signal.connect(self._on_broadcast_hit)
        self.global_hotkey_signal.connect(self._on_global_hotkey)

        self.setup_ui()
        self.setup_clipboard()
        self.setup_tray()
        self.setup_panels()
        self.setup_timers()
        self.setup_hotkey()

        self._broadcast_listener = BroadcastListener(BROADCAST_PORT)
        self._broadcast_listener.device_announced.connect(
            self.broadcast_hit_signal)
        self._broadcast_listener.start()

        self._handshake_listener = HandshakeListener(HANDSHAKE_PORT)
        self._handshake_listener.handshake_received.connect(
            self._on_handshake_received)
        self._handshake_listener.start()

        ThemeManager.apply_mode()
        self.apply_theme()

        self.position_top_right()
        if self._auto_scan:
            QTimer.singleShot(500, lambda: self.start_discovery(silent=False))

    @property
    def current_device(self):
        if 0 <= self.current_index < len(self.devices):
            return self.devices[self.current_index]
        return None

    @property
    def current_ip(self):
        d = self.current_device
        return d[0] if d else None

    @property
    def current_name(self):
        d = self.current_device
        return d[1] if d else None

    # ---------- 已知 IP（优先扫描） ----------
    def _known_ips(self):
        try:
            return list(load_settings().get("known_ips", []))[:KNOWN_IPS_MAX]
        except Exception:
            return []

    def _remember_ip(self, ip):
        if not ip:
            return
        try:
            settings = load_settings()
            ips = [x for x in settings.get("known_ips", []) if x != ip]
            ips.insert(0, ip)
            settings["known_ips"] = ips[:KNOWN_IPS_MAX]
            save_settings(settings)
        except Exception:
            pass

    # ---------- 退避辅助 ----------
    def _reset_backoff(self):
        self._scan_backoff_idx = 0

    def _advance_backoff(self):
        if self._scan_backoff_idx < len(SCAN_BACKOFF_SEQUENCE) - 1:
            self._scan_backoff_idx += 1

    def _current_backoff_seconds(self):
        return SCAN_BACKOFF_SEQUENCE[self._scan_backoff_idx]

    # ---------- UI ----------
    def setup_ui(self):
        self.container = QWidget(self)
        self.container.setObjectName("container")
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.container.setGeometry(0, 0, WIN_WIDTH, WIN_HEIGHT)

        self.status_box = QWidget()
        self.status_box.setFixedWidth(STATUS_MIN_W)
        self.status_box.setCursor(Qt.PointingHandCursor)
        self.status_box.mousePressEvent = self._on_status_clicked

        self.status_line1 = QLabel("● 扫描中")
        self.status_line1.setFixedHeight(sc(14))
        self.status_line1.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status_line1.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.status_line2 = QLabel("")
        self.status_line2.setFixedHeight(sc(14))
        self.status_line2.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status_line2.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.status_line2.setVisible(False)

        svb = QVBoxLayout(self.status_box)
        svb.setContentsMargins(0, 0, 0, 0)
        svb.setSpacing(0)
        svb.addStretch(1)
        svb.addWidget(self.status_line1)
        svb.addWidget(self.status_line2)
        svb.addStretch(1)

        self.input = QLineEdit()
        self.input.setPlaceholderText("等待剪贴板...")
        self.input.setFixedHeight(sc(28))
        self.input.setFixedWidth(INPUT_WIDTH)
        self.input.returnPressed.connect(self.on_send)

        self.history_btn = QPushButton("📋")
        self.history_btn.setFixedSize(sc(28), sc(28))
        self.history_btn.clicked.connect(self.toggle_history)

        self.name_btn = QPushButton("🎲")
        self.name_btn.setFixedSize(sc(28), sc(28))
        self.name_btn.setToolTip("生成名字并复制")
        self.name_btn.clicked.connect(self.generate_and_copy_name)
        self.name_btn.setVisible(self._show_name_btn)

        self.send_btn = QPushButton("发送")
        self.send_btn.setFixedSize(sc(48), sc(28))
        self.send_btn.setStyleSheet(f"""
            QPushButton {{
                background: #34C759; color: white;
                border: none; border-radius: {sc(8)}px;
                font-size: {sc(12)}px; font-weight: 600;
            }}
            QPushButton:hover {{ background: #30D158; }}
            QPushButton:pressed {{ background: #28A745; }}
            QPushButton:disabled {{ background: #AEAEB2; color: #FFFFFF; }}
        """)
        self.send_btn.clicked.connect(self.on_send)
        self.send_btn.setEnabled(False)

        self.input.textChanged.connect(self._update_send_btn_state)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(sc(22), sc(22))
        self.close_btn.clicked.connect(self.hide)

        row = QHBoxLayout()
        row.setContentsMargins(sc(8), sc(6), sc(6), sc(6))
        row.setSpacing(sc(4))
        row.addWidget(self.status_box)
        row.addWidget(self.input)
        row.addWidget(self.history_btn)
        row.addWidget(self.name_btn)
        row.addWidget(self.send_btn)
        row.addWidget(self.close_btn)
        self.container.setLayout(row)

    def apply_theme(self):
        c = ThemeManager.colors()

        self.container.setStyleSheet(f"""
            #container {{
                background: {c['bg']};
                border-radius: {sc(12)}px;
                border: 1px solid {c['border']};
            }}
            QLabel {{ background: transparent; }}
            QLineEdit {{
                background: {c['input_bg']};
                border: none;
                border-radius: {sc(8)}px;
                color: {c['text']};
                font-size: {sc(13)}px;
                padding: 0 {sc(10)}px;
                selection-background-color: {c['list_sel']};
            }}
            QLineEdit:focus {{
                background: {c['input_focus']};
            }}
        """)

        self._update_status()

        self.history_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c['input_bg']};
                color: {c['text']};
                border: none; border-radius: {sc(8)}px;
                font-size: {sc(14)}px;
            }}
            QPushButton:hover {{ background: {c['hover_strong']}; }}
            QPushButton:pressed {{ background: {c['hover']}; }}
        """)

        self.name_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c['input_bg']};
                color: {c['text']};
                border: none; border-radius: {sc(8)}px;
                font-size: {sc(14)}px;
            }}
            QPushButton:hover {{ background: {c['hover_strong']}; }}
            QPushButton:pressed {{ background: {c['hover']}; }}
        """)

        self.close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {c['close_color']};
                border: none; border-radius: {sc(6)}px;
                font-size: {sc(11)}px; font-weight: 600;
            }}
            QPushButton:hover {{
                background: {c['hover']};
                color: {c['close_hover']};
            }}
        """)

        if hasattr(self, 'device_panel'):
            self.device_panel.apply_theme()
        if hasattr(self, 'history_panel'):
            self.history_panel.apply_theme()

    def position_top_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.right() - self.width() - sc(20)
        y = screen.top() + sc(20)
        self.move(x, y)

    def _on_status_clicked(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._discovering:
            return
        if self.current_ip:
            self.toggle_device_panel()
        else:
            self._trigger_immediate_scan()

    def _trigger_immediate_scan(self):
        if self._quitting:
            return
        if hasattr(self, '_scan_timer'):
            self._scan_timer.stop()
        self._reset_backoff()
        if self._discovering:
            return
        self.start_discovery(silent=False)

    # ---------- TCP 握手命中 ----------
    def _on_handshake_received(self, ip, name):
        self._remember_ip(ip)
        for i, (dip, _) in enumerate(self.devices):
            if dip == ip:
                self.current_index = i
                self._reset_backoff()
                self._update_status()
                self.send_btn.setEnabled(
                    self.current_ip is not None
                    and bool(self.input.text().strip()))
                return

        self.devices.append((ip, name))
        self.devices.sort(
            key=lambda x: tuple(int(p) for p in x[0].split('.')))
        for i, (dip, _) in enumerate(self.devices):
            if dip == ip:
                self.current_index = i
                break

        self._reset_backoff()
        self._update_status()
        self.send_btn.setEnabled(
            self.current_ip is not None
            and bool(self.input.text().strip()))

        if self.device_panel.isVisible():
            self.device_panel.refresh(self.devices, self.current_index)

        if self._auto_scan and not self.current_ip and not self._discovering:
            self._trigger_immediate_scan()

    # ---------- 广播命中 ----------
    def _on_broadcast_hit(self, ip, port):
        now = datetime.now().timestamp()
        last = self._recent_broadcast.get(ip, 0)
        if now - last < 1.0:
            return
        self._recent_broadcast[ip] = now

        for i, (dip, _) in enumerate(self.devices):
            if dip == ip:
                self._reset_backoff()
                return

        def do_ping():
            result = check_ip(ip, port=port, timeout=1.0)
            if result:
                QTimer.singleShot(
                    0, lambda: self._add_device_from_broadcast(result))

        threading.Thread(target=do_ping, daemon=True).start()

    def _add_device_from_broadcast(self, result):
        ip, name = result
        self._remember_ip(ip)

        for i, (dip, _) in enumerate(self.devices):
            if dip == ip:
                self._reset_backoff()
                return

        self.devices.append((ip, name))
        self.devices.sort(
            key=lambda x: tuple(int(p) for p in x[0].split('.')))

        for i, (dip, _) in enumerate(self.devices):
            if dip == ip:
                self.current_index = i
                break

        self._reset_backoff()
        self._update_status()
        self.send_btn.setEnabled(
            self.current_ip is not None
            and bool(self.input.text().strip()))

        if self.device_panel.isVisible():
            self.device_panel.refresh(self.devices, self.current_index)

    # ---------- 面板 ----------
    def setup_panels(self):
        self.device_panel = DevicePanel()
        self.device_panel.device_selected.connect(self._on_device_selected)

        self.history_panel = HistoryPanel(self.history)
        self.history_panel.item_selected.connect(self._on_history_picked)

    def toggle_device_panel(self):
        if self.device_panel.isVisible():
            self.device_panel.hide()
            return
        self.device_panel.refresh(self.devices, self.current_index)
        pos = self.mapToGlobal(QPoint(0, self.height() + sc(6)))
        self.device_panel.move(pos)
        self.device_panel.show()

    def _on_device_selected(self, index):
        if 0 <= index < len(self.devices):
            self.current_index = index
            self._update_status()
            self.send_btn.setEnabled(bool(self.input.text().strip()))

    def toggle_history(self):
        if self.history_panel.isVisible():
            self.history_panel.hide()
            return
        self.history_panel.refresh()
        pos = self.mapToGlobal(QPoint(0, self.height() + sc(6)))
        self.history_panel.move(pos)
        self.history_panel.show()

    def _on_history_picked(self, text):
        title, is_fast = TitleParser.parse(text)
        if title:
            self.input.setText(title + (" - 极速" if is_fast else ""))
        else:
            self.input.setText(text)
        self.send_btn.setEnabled(self.current_ip is not None)

    # ---------- 拖动 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    # ---------- 窗口激活触发扫描 ----------
    def showEvent(self, event):
        super().showEvent(event)
        if self._auto_scan and not self.current_ip and not self._quitting:
            QTimer.singleShot(50, self._trigger_immediate_scan)

    # ---------- 剪贴板 ----------
    def setup_clipboard(self):
        self._clip_timer = QTimer(self)
        self._clip_timer.setSingleShot(True)
        self._clip_timer.timeout.connect(self.on_clipboard_debounced)
        QApplication.clipboard().dataChanged.connect(self.on_clipboard_changed)

    def on_clipboard_changed(self):
        self._clip_timer.start(CLIPBOARD_DEBOUNCE)

    def on_clipboard_debounced(self):
        try:
            text = QApplication.clipboard().text() or ""
        except Exception:
            return
        text = text.strip()
        if not text or text == self.last_clipboard:
            return
        self.last_clipboard = text

        if is_noise_clipboard(text):
            return
        if not looks_like_douyin_share(text):
            return

        title, is_fast = TitleParser.parse(text)
        if title:
            display = title + (" - 极速" if is_fast else "")
            self.input.setText(display)
            self.send_btn.setEnabled(self.current_ip is not None)

            self.history.add(text, title)
            if self.history_panel.isVisible():
                self.history_panel.refresh()

            if self._auto_scan and not self.current_ip and not self._discovering:
                self._trigger_immediate_scan()

    # ---------- 托盘 ----------
    def setup_tray(self):
        self.tray = QSystemTrayIcon(create_icon(), self)
        self.tray.setToolTip("开饭了助手")

        menu = QMenu()

        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)

        rediscover_action = QAction("重新扫描", self)
        rediscover_action.triggered.connect(self._trigger_immediate_scan)
        menu.addAction(rediscover_action)

        self.auto_scan_action = QAction("自动扫描", self, checkable=True)
        self.auto_scan_action.setChecked(self._auto_scan)
        self.auto_scan_action.triggered.connect(self._toggle_auto_scan)
        menu.addAction(self.auto_scan_action)

        device_action = QAction("选择设备", self)
        device_action.triggered.connect(self._show_device_from_tray)
        menu.addAction(device_action)

        history_action = QAction("历史记录", self)
        history_action.triggered.connect(self._show_history_from_tray)
        menu.addAction(history_action)

        name_action = QAction("生成名字并复制", self)
        name_action.triggered.connect(self.generate_and_copy_name)
        menu.addAction(name_action)

        self.name_btn_action = QAction("显示生成名字按钮", self, checkable=True)
        self.name_btn_action.setChecked(self._show_name_btn)
        self.name_btn_action.triggered.connect(self._toggle_name_btn)
        menu.addAction(self.name_btn_action)

        menu.addSeparator()

        scale_menu = menu.addMenu("缩放")
        for label, mult in self.SCALE_OPTIONS:
            act = QAction(label, self, checkable=True)
            act.setChecked(abs(SCALE_MULTIPLIER - mult) < 1e-6)
            act.triggered.connect(
                lambda checked=False, m=mult: self._set_scale(m))
            scale_menu.addAction(act)

        menu.addSeparator()

        theme_menu = menu.addMenu("主题")

        self.theme_auto_action = QAction("自动（跟随系统）", self, checkable=True)
        self.theme_auto_action.triggered.connect(
            lambda: self._set_theme("auto"))
        theme_menu.addAction(self.theme_auto_action)

        self.theme_light_action = QAction("浅色", self, checkable=True)
        self.theme_light_action.triggered.connect(
            lambda: self._set_theme("light"))
        theme_menu.addAction(self.theme_light_action)

        self.theme_dark_action = QAction("深色", self, checkable=True)
        self.theme_dark_action.triggered.connect(
            lambda: self._set_theme("dark"))
        theme_menu.addAction(self.theme_dark_action)

        self._update_theme_menu_checks()

        menu.addSeparator()

        hotkey_menu = menu.addMenu("热键")

        self.hotkey_enabled_action = QAction("启用热键", self, checkable=True)
        self.hotkey_enabled_action.setChecked(self._hotkey_enabled)
        self.hotkey_enabled_action.triggered.connect(self._toggle_hotkey_enabled)
        hotkey_menu.addAction(self.hotkey_enabled_action)

        hotkey_menu.addSeparator()

        self._hotkey_actions = {}
        for key in HOTKEY_OPTIONS:
            act = QAction(key, self, checkable=True)
            act.setChecked(key == self._hotkey)
            act.triggered.connect(
                lambda checked=False, k=key: self._set_hotkey(k))
            hotkey_menu.addAction(act)
            self._hotkey_actions[key] = act

        menu.addSeparator()

        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def _toggle_auto_scan(self, checked):
        self._auto_scan = bool(checked)
        settings = load_settings()
        settings["auto_scan"] = self._auto_scan
        save_settings(settings)
        if self._auto_scan:
            # 开启时立即扫描一次
            self._reset_backoff()
            self._trigger_immediate_scan()
        else:
            # 关闭时停止定时扫描
            if hasattr(self, '_scan_timer'):
                self._scan_timer.stop()

    def _set_scale(self, mult):
        if abs(SCALE_MULTIPLIER - mult) < 1e-6:
            return
        settings = load_settings()
        settings["scale_multiplier"] = mult
        save_settings(settings)
        self._restart_app()

    def _restart_app(self):
        try:
            if getattr(sys, 'frozen', False):
                args = sys.argv[1:]
            else:
                args = sys.argv
            QProcess.startDetached(sys.executable, args)
        except Exception:
            pass
        self.quit_app()

    def generate_and_copy_name(self):
        name = generate_name()
        try:
            QApplication.clipboard().setText(name)
            self.last_clipboard = name
        except Exception:
            pass
        self._flash(f"已复制 {name}", "#34C759")

    # ---------- 热键 ----------
    def setup_hotkey(self):
        self._hotkey_handle = None
        self._hotkey_shortcut = None
        if not HAS_GLOBAL_HOTKEY:
            # 未安装 keyboard 时降级为窗口内热键
            self._hotkey_shortcut = QShortcut(QKeySequence(self._hotkey), self)
            self._hotkey_shortcut.setContext(Qt.WindowShortcut)
            self._hotkey_shortcut.activated.connect(self._on_global_hotkey)
        self._apply_hotkey_enabled()

    def _apply_hotkey_enabled(self):
        if self._hotkey_enabled:
            if HAS_GLOBAL_HOTKEY:
                self._register_global_hotkey()
            elif self._hotkey_shortcut is not None:
                self._hotkey_shortcut.setEnabled(True)
        else:
            self._unregister_hotkey()
            if self._hotkey_shortcut is not None:
                self._hotkey_shortcut.setEnabled(False)

    def _toggle_hotkey_enabled(self, checked):
        self._hotkey_enabled = bool(checked)
        settings = load_settings()
        settings["hotkey_enabled"] = self._hotkey_enabled
        save_settings(settings)
        self._apply_hotkey_enabled()

    def _register_global_hotkey(self):
        if self._hotkey_handle is not None:
            try:
                _keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None
        try:
            self._hotkey_handle = _keyboard.add_hotkey(
                self._hotkey.lower(), self._emit_global_hotkey)
        except Exception:
            self._hotkey_handle = None

    def _unregister_hotkey(self):
        if self._hotkey_handle is not None:
            try:
                _keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None

    def _emit_global_hotkey(self):
        # keyboard 回调运行在后台线程，用信号切回主线程
        self.global_hotkey_signal.emit()

    def _on_global_hotkey(self):
        name = generate_name()
        try:
            QApplication.clipboard().setText(name)
            self.last_clipboard = name
        except Exception:
            pass

        if self.input.hasFocus():
            # 助手输入框聚焦：直接填入输入框
            self.input.setText(name)
            self.input.selectAll()
            self.input.setFocus()
            self._update_send_btn_state()
            self._flash(f"已填入 {name}", "#34C759")
        else:
            # 其它程序聚焦：模拟 Ctrl+V 把名字粘贴进前台窗口
            self._paste_to_foreground(name)

    def _paste_to_foreground(self, name):
        if not HAS_GLOBAL_HOTKEY:
            self._flash(f"已复制 {name}", "#34C759")
            return
        self._flash(f"已粘贴 {name}", "#34C759")

        def do_paste():
            try:
                _keyboard.send('ctrl+v')
            except Exception:
                pass

        # 稍延迟，确保剪贴板就绪、前台窗口稳定
        QTimer.singleShot(40, do_paste)

    def _is_window_focused(self):
        # Frameless/Qt.Tool 置顶窗口在 Windows 下通常带 WS_EX_NOACTIVATE，
        # 从不获得系统键盘焦点，Qt 的 hasFocus/isActiveWindow 永远为假。
        # 因此以"鼠标是否在窗口矩形内"为主判据（Win32 像素坐标，最可靠），
        # 并叠加"前台窗口属于本进程"的判断。
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            # 1) 鼠标在窗口矩形内
            hwnd = int(self.winId())
            pt = wintypes.POINT()
            rect = wintypes.RECT()
            if user32.GetCursorPos(ctypes.byref(pt)) and                     user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                if (rect.left <= pt.x <= rect.right and
                        rect.top <= pt.y <= rect.bottom):
                    return True

            # 2) 前台窗口属于本进程
            import os
            fg = user32.GetForegroundWindow()
            if fg:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                if pid.value == os.getpid():
                    return True
        except Exception:
            pass

        # 3) Qt 焦点兜底
        try:
            if self.input.hasFocus() or self.isActiveWindow():
                return True
        except Exception:
            pass
        return False

    def _log_hotkey_diag(self):
        try:
            import ctypes
            from PySide2.QtGui import QCursor
            try:
                fg = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                fg = 0
            try:
                my = int(self.winId())
            except Exception:
                my = 0
            line = (
                f"hasFocus={self.input.hasFocus()} "
                f"isActive={self.isActiveWindow()} "
                f"visible={self.isVisible()} "
                f"fg={fg} my={my} match={fg == my} "
                f"cursorIn={self.geometry().contains(QCursor.pos())} "
                f"focusW={QApplication.focusWidget()}\n"
            )
            with open(os.path.join(os.path.expanduser("~"),
                                   ".kfl_hotkey_diag.txt"),
                      "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _set_hotkey(self, key):
        if key == self._hotkey:
            return
        self._hotkey = key
        settings = load_settings()
        settings["hotkey"] = key
        save_settings(settings)
        if HAS_GLOBAL_HOTKEY:
            self._unregister_hotkey()
            self._apply_hotkey_enabled()
        elif self._hotkey_shortcut is not None:
            self._hotkey_shortcut.setKey(QKeySequence(key))
        self._update_hotkey_menu_checks()

    def _update_hotkey_menu_checks(self):
        for k, act in self._hotkey_actions.items():
            act.setChecked(k == self._hotkey)

    def _toggle_name_btn(self, checked):
        self._show_name_btn = bool(checked)
        self.name_btn.setVisible(self._show_name_btn)
        settings = load_settings()
        settings["show_name_btn"] = self._show_name_btn
        save_settings(settings)

    def _set_theme(self, mode):
        ThemeManager.mode = mode
        ThemeManager.apply_mode()
        self.apply_theme()
        self._update_theme_menu_checks()

    def _update_theme_menu_checks(self):
        m = ThemeManager.mode
        self.theme_auto_action.setChecked(m == "auto")
        self.theme_light_action.setChecked(m == "light")
        self.theme_dark_action.setChecked(m == "dark")

    def _show_device_from_tray(self):
        self.show_window()
        QTimer.singleShot(100, self.toggle_device_panel)

    def _show_history_from_tray(self):
        self.show_window()
        QTimer.singleShot(100, self.toggle_history)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_window()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._quitting = True
        self._unregister_hotkey()
        try:
            self._broadcast_listener.stop()
            self._broadcast_listener.wait(1000)
        except Exception:
            pass
        try:
            self._handshake_listener.stop()
            self._handshake_listener.wait(1000)
        except Exception:
            pass
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if self._quitting:
            event.accept()
        else:
            event.ignore()
            self.hide()

    # ---------- 定时器 ----------
    def setup_timers(self):
        self._hb_timer = QTimer(self)
        self._hb_timer.timeout.connect(self.on_heartbeat)
        self._hb_timer.start(HEARTBEAT_INTERVAL * 1000)

        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(self._run_scheduled_scan)

        self._theme_poll_timer = QTimer(self)
        self._theme_poll_timer.timeout.connect(self._poll_system_theme)
        self._theme_poll_timer.start(THEME_POLL_INTERVAL)

    def _poll_system_theme(self):
        if ThemeManager.mode != "auto":
            return
        old = ThemeManager.current
        new = ThemeManager.detect_system()
        if new != old:
            ThemeManager.current = new
            self.apply_theme()

    def _schedule_scan(self, delay_ms):
        if self._quitting:
            return
        self._scan_timer.start(delay_ms)

    def _run_scheduled_scan(self):
        if self._quitting:
            return
        if not self._auto_scan:
            return
        if self.current_ip:
            return
        # 无设备时每 IDLE_SCAN_INTERVAL 秒重扫一次
        self.start_discovery(silent=False)

    # ---------- 心跳 ----------
    def on_heartbeat(self):
        if not self.devices:
            return

        snapshot = list(self.devices)

        def do_ping_all():
            offline = []
            for ip, _ in snapshot:
                if not ping_phone(ip, timeout=HEARTBEAT_TIMEOUT):
                    offline.append(ip)
            if offline:
                self.devices_offline_signal.emit(offline)

        threading.Thread(target=do_ping_all, daemon=True).start()

    def _on_devices_offline(self, ips):
        if not ips:
            return
        ips_set = set(ips)
        old_len = len(self.devices)
        removed_any = any(d[0] in ips_set for d in self.devices)

        if not removed_any:
            return

        current_removed = (self.current_ip in ips_set) if self.current_ip else False

        self.devices = [d for d in self.devices if d[0] not in ips_set]

        if len(self.devices) != old_len:
            if self.current_index >= len(self.devices):
                self.current_index = max(0, len(self.devices) - 1)
            elif current_removed:
                self.current_index = 0

            self._update_status()
            self.send_btn.setEnabled(
                self.current_ip is not None
                and bool(self.input.text().strip()))

            if self.device_panel.isVisible():
                self.device_panel.refresh(
                    self.devices, self.current_index)

            if not self.devices:
                self._schedule_scan(IDLE_SCAN_INTERVAL * 1000)

    # ---------- 扫描 ----------
    def start_discovery(self, silent=False):
        if self._discovering:
            return
        self._discovering = True
        self._scan_silent = silent
        self._scan_id += 1
        wid = self._scan_id

        if not silent and not self.current_ip:
            self.set_line1("● 扫描中", "#FF9500")
            self.status_line2.setVisible(False)
            self._fit_status_width()
            self.send_btn.setEnabled(False)

        if self._scan_hard_timeout is not None:
            self._scan_hard_timeout.stop()
        self._scan_hard_timeout = QTimer(self)
        self._scan_hard_timeout.setSingleShot(True)
        self._scan_hard_timeout.timeout.connect(self._force_reset_scan)
        self._scan_hard_timeout.start(20000)

        self._worker = DiscoveryWorker(PORT, wid, priority_ips=self._known_ips())
        self._worker.finished_scan.connect(self.on_discovery_finished)
        self._worker.start()

    def _force_reset_scan(self):
        if self._discovering:
            self._discovering = False
            self._scan_id += 1
            if not self._scan_silent:
                self._update_status()
            if not self.devices:
                self._schedule_scan(IDLE_SCAN_INTERVAL * 1000)

    @Slot(list, int)
    def on_discovery_finished(self, ips, worker_id):
        if worker_id != self._scan_id:
            return

        if self._scan_hard_timeout is not None:
            self._scan_hard_timeout.stop()
            self._scan_hard_timeout = None

        was_silent = self._scan_silent
        self._discovering = False
        self._scan_silent = False

        old_current_ip = self.current_ip
        self.devices = ips

        if old_current_ip:
            for i, (ip, _) in enumerate(self.devices):
                if ip == old_current_ip:
                    self.current_index = i
                    break
            else:
                self.current_index = 0
        else:
            self.current_index = 0

        if not was_silent or old_current_ip != self.current_ip:
            self._update_status()

        self.send_btn.setEnabled(
            self.current_ip is not None and bool(self.input.text().strip()))

        if self.device_panel.isVisible():
            self.device_panel.refresh(self.devices, self.current_index)

        if self.devices:
            self._reset_backoff()
            for (dip, _) in self.devices:
                self._remember_ip(dip)
        else:
            self._schedule_scan(IDLE_SCAN_INTERVAL * 1000)

    def _update_status(self):
        c = ThemeManager.colors()

        if self._discovering:
            self.set_line1("● 扫描中", "#FF9500")
            self.status_line2.setVisible(False)
            self._fit_status_width()
            self.status_box.setToolTip("正在扫描局域网...")
            return

        n = len(self.devices)
        if n == 0:
            self.set_line1("● 未找到", "#FF3B30")
            self.status_line2.setVisible(False)
            self._fit_status_width()
            self.status_box.setToolTip(
                "未发现局域网内的手机\n点击立即重新扫描")
            return

        name = self.current_name or "手机"

        if n == 1:
            line1 = "已连接"
        else:
            line1 = f"已连接 ({n})"

        fm2 = QFontMetrics(self.status_line2.font())
        elided = fm2.elidedText(name, Qt.ElideRight, STATUS_MAX_W - sc(4))

        self.set_line1(f"● {line1}", "#34C759")
        self.set_line2(elided)
        self.status_line2.setVisible(True)
        self._fit_status_width()

        tip = f"{name}\nIP: {self.current_ip}"
        if n > 1:
            tip += f"\n\n共 {n} 台设备在线\n点击切换"
        else:
            tip += "\n\n点击切换设备"
        self.status_box.setToolTip(tip)

    def _fit_status_width(self):
        fm1 = QFontMetrics(self.status_line1.font())
        w1 = fm1.horizontalAdvance(self.status_line1.text())
        if self.status_line2.isVisible():
            fm2 = QFontMetrics(self.status_line2.font())
            w2 = fm2.horizontalAdvance(self.status_line2.text())
            target = max(w1, w2) + sc(4)
        else:
            target = w1 + sc(4)
        target = max(STATUS_MIN_W, min(target, STATUS_MAX_W))
        if self.status_box.width() != target:
            self.status_box.setFixedWidth(target)

    def set_line1(self, text, color):
        self.status_line1.setText(text)
        self.status_line1.setStyleSheet(
            f"color: {color}; font-size: {sc(11)}px; background: transparent;"
        )

    def set_line2(self, text, color=None):
        if color is None:
            color = ThemeManager.colors()['text_sub']
        self.status_line2.setText(text)
        self.status_line2.setStyleSheet(
            f"color: {color}; font-size: {sc(11)}px; background: transparent;"
        )

    def _restore_status(self):
        self._update_status()

    # ---------- 发送 ----------
    def _update_send_btn_state(self, *args):
        self.send_btn.setEnabled(
            self.current_ip is not None
            and bool(self.input.text().strip()))

    def on_send(self):
        text = self.input.text().strip()
        if not text:
            self._flash("无内容", "#FF9500")
            return
        ip = self.current_ip
        if not ip:
            self._flash("未连接", "#FF3B30")
            return

        self.input.clear()
        self.send_btn.setEnabled(False)

        self._send_guard_timer.start((SEND_TIMEOUT + 2) * 1000)

        def do_send():
            try:
                result = send_to_phone(ip, text)
            except Exception as e:
                result = {"ok": False, "message": str(e)}
            self.send_result_signal.emit(ip, text, result)

        threading.Thread(target=do_send, daemon=True).start()

    def _recover_send_btn(self):
        if not self.send_btn.isEnabled():
            self.send_btn.setEnabled(
                self.current_ip is not None
                and bool(self.input.text().strip()))

    def _on_send_result(self, ip, sent_text, result):
        self._send_guard_timer.stop()

        try:
            ok = bool(result.get("ok"))
        except Exception:
            ok = False

        if ok:
            self._flash("已发送", "#34C759")
            return

        raw_msg = str(result.get("message", ""))

        # 记录失败详情，便于诊断
        try:
            log_path = os.path.join(
                os.path.expanduser("~"), ".kfl_send_log.txt")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("%s\tip=%s\ttext=%r\tresult=%r\n" % (
                    datetime.now().isoformat(), ip, sent_text, result))
        except Exception:
            pass

        msg = raw_msg.lower()
        is_conn_error = any(k in msg for k in CONN_ERROR_KEYWORDS)

        if is_conn_error and ip:
            self._flash("连接已断开", "#FF3B30")
            self._on_devices_offline([ip])
        else:
            self._flash(raw_msg[:16] if raw_msg else "失败", "#FF3B30")

        self.send_btn.setEnabled(
            self.current_ip is not None
            and bool(self.input.text().strip()))

    def _flash(self, text, color):
        self.set_line1(f"● {text}", color)
        self.status_line2.setVisible(False)
        self._fit_status_width()
        self._flash_timer.start(1500)


# ============================================================
# main
# ============================================================
def main():
    global UI_SCALE, SCALE_MULTIPLIER
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    settings = load_settings()
    try:
        SCALE_MULTIPLIER = float(settings.get("scale_multiplier", 1.0))
    except (TypeError, ValueError):
        SCALE_MULTIPLIER = 1.0
    SCALE_MULTIPLIER = max(0.5, min(SCALE_MULTIPLIER, 3.0))

    UI_SCALE = compute_ui_scale()

    global WIN_WIDTH, WIN_HEIGHT, INPUT_WIDTH, STATUS_MIN_W, STATUS_MAX_W
    WIN_WIDTH = sc(430)
    WIN_HEIGHT = sc(44)
    INPUT_WIDTH = sc(150)
    STATUS_MIN_W = sc(56)
    STATUS_MAX_W = sc(110)

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
