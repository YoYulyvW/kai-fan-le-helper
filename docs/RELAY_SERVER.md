# 开饭了 · 中继服务器（Relay）设计与接口规范

> 目标：让两台手机（或手机↔电脑）在**不同网络**下也能互推数据，摆脱局域网限制。
> 方案：在 **kai-fan-le-helper（Windows 助手）** 内置一个轻量中继服务，配合**内网穿透**暴露到公网。
> 版本：v1.0 · 2026-10-02

---

## 一、为什么集成到 helper

| 理由 | 说明 |
|------|------|
| 已有常驻进程 | helper 开机自启、托盘常驻，天生适合跑长连接服务 |
| 已有网络基础 | `main.py` 已有 `BroadcastListener` / `HandshakeListener` 线程模型，可复用 |
| 已有设备概念 | 已有设备发现、IP 记忆、心跳机制 |
| 免额外服务器成本 | 家用电脑 + 内网穿透 = 0 云服务器成本 |
| 数据可控 | 中继只是转发，不落库；隐私优于第三方 |

---

## 二、整体架构

```
┌─────────────┐        ┌──────────────────────────────┐        ┌─────────────┐
│  手机 A      │        │   kai-fan-le-helper (Windows) │        │  手机 B      │
│  (发送方)    │        │                              │        │  (接收方)    │
│             │        │  ┌────────────────────────┐  │        │             │
│  POST /send ├───────►│  │  RelayServer (新增)     │  │◄───────┤ GET /poll   │
│             │  公网   │  │  :8860                 │  │  公网   │             │
│             │        │  │  - 设备注册表            │  │        │             │
│             │        │  │  - 离线消息队列          │  │        │             │
│             │        │  │  - 长轮询 / WebSocket    │  │        │             │
│             │        │  └────────────────────────┘  │        │             │
│             │        │            ▲                  │        │             │
│             │        │            │ 内网穿透          │        │             │
│             │        │      (frp / CF Tunnel)        │        │             │
└─────────────┘        └──────────────────────────────┘        └─────────────┘
                                    ▲
                                    │
                          公网入口: relay.example.com:443
```

**数据流：**
1. 手机启动 → 向中继 `POST /register` 注册，拿到 `relayToken`
2. 发送方 `POST /send`（带目标 deviceId + PushPayload）
3. 中继查注册表：在线则直接投递；离线则入队
4. 接收方 `GET /poll` 长轮询拉取，或 WebSocket 实时接收
5. 接收方 `POST /ack` 确认，中继出队

---

## 三、端口与配置

| 项 | 值 | 说明 |
|----|-----|------|
| **中继监听端口** | `8860` | 新增，与现有 8848/8849/8850 不冲突 |
| 默认绑定 | `0.0.0.0:8860` | 局域网可直接访问 |
| 公网暴露 | 由内网穿透决定 | 见第六节 |
| 配置文件 | `.kai_fan_le_helper_settings.json` | 复用现有设置文件，新增 `relay` 段 |

### 新增配置项

```json
{
  "relay": {
    "enabled": true,
    "port": 8860,
    "public_url": "https://relay.example.com",
    "require_token": true,
    "master_token": "管理员预共享密钥（可选）",
    "offline_ttl_hours": 24,
    "max_queue_per_device": 50,
    "log_level": "info"
  }
}
```

---

## 四、HTTP 接口规范

> 所有请求/响应均为 `application/json; charset=utf-8`
> 鉴权：除 `/register` 外，均需 `Authorization: Bearer <relayToken>`

### 4.1 设备注册

```http
POST /relay/register
Content-Type: application/json

{
  "deviceId": "uuid-xxxx",          // 手机唯一 ID（已有 DeviceDiscovery.selfDeviceId）
  "deviceName": "iPhone 15 Pro",     // 展示名
  "platform": "ios",                 // ios | android | windows
  "pushMode": "poll",                // poll | ws
  "version": "1.2.0"
}
```

**响应 200：**
```json
{
  "ok": true,
  "relayToken": "rt_xxxxxxxxxxxx",   // 后续所有请求携带
  "publicUrl": "https://relay.example.com",
  "ttlSeconds": 86400
}
```

**错误：**
- `400` 参数缺失
- `429` 注册过于频繁（同 deviceId 60s 内限 3 次）

---

### 4.2 发送消息

```http
POST /relay/send
Authorization: Bearer rt_xxxxxxxxxxxx
Content-Type: application/json

{
  "to": "uuid-yyyy",                 // 目标 deviceId
  "type": "push",                    // push | title | text（预留）
  "payload": {                       // 复用现有 PushPayload 结构
    "version": 1,
    "sender": "Xiaomi 14",
    "senderId": "uuid-xxxx",
    "date": "2026-10-02",
    "dramas": [{ "title": "都市之最强赘婿", "isFast": false }],
    "records": [{ "title": "都市之最强赘婿", "platform": "橙子建站",
                  "isFast": false, "count": 2, "updatedAt": "..." }]
  },
  "ttlHours": 24                     // 可选，覆盖默认
}
```

**响应 200：**
```json
{
  "ok": true,
  "messageId": "msg_xxxxxxxx",
  "delivered": true,                 // true=在线已投递；false=已入队
  "queueSize": 0                     // delivered=false 时为离线队列长度
}
```

**错误：**
- `401` token 无效
- `404` 目标设备未注册过
- `413` payload 过大（限制 256 KB）
- `429` 发送频率超限（单设备 30 条/分钟）

---

### 4.3 长轮询拉取

```http
GET /relay/poll?timeout=25
Authorization: Bearer rt_xxxxxxxxxxxx
```

- `timeout`：等待秒数，1~30，默认 25
- 有消息立即返回；无消息等到 timeout 返回空

**响应 200（有消息）：**
```json
{
  "ok": true,
  "messages": [
    {
      "messageId": "msg_xxxxxxxx",
      "from": "uuid-xxxx",
      "fromName": "Xiaomi 14",
      "type": "push",
      "receivedAt": "2026-10-02T09:41:23Z",
      "payload": { ... PushPayload ... }
    }
  ]
}
```

**响应 200（超时无消息）：**
```json
{ "ok": true, "messages": [] }
```

---

### 4.4 确认收到

```http
POST /relay/ack
Authorization: Bearer rt_xxxxxxxxxxxx
Content-Type: application/json

{
  "messageIds": ["msg_xxxxxxxx", "msg_yyyyyyyy"]
}
```

**响应 200：**
```json
{ "ok": true, "acked": 2 }
```

---

### 4.5 WebSocket 实时通道（可选，推荐）

```
WS /relay/ws?token=rt_xxxxxxxxxxxx
```

- 连接成功后服务端推送 JSON 帧，格式同 `/poll` 的 `messages[i]`
- 客户端发送 `{"type":"ping"}` 保活，服务端回 `{"type":"pong"}`
- 断线自动重连（指数退避：1s/2s/4s/8s，上限 30s）

---

### 4.6 设备列表（调试用）

```http
GET /relay/devices
Authorization: Bearer rt_xxxxxxxxxxxx
```

**响应 200：**
```json
{
  "ok": true,
  "devices": [
    { "deviceId": "uuid-xxxx", "deviceName": "Xiaomi 14",
      "platform": "android", "online": true, "lastSeen": "...", "ws": 0 }
  ]
}
```

- `online`：最近 60 秒有活动（poll / ws / 任意带 token 的请求）为 `true`，否则 `false`
- `lastSeen`：最后一次活动时间（UTC）
- `ws`：该设备当前活跃的 WebSocket 会话数

---

### 4.7 注销设备

```http
POST /relay/unregister
Authorization: Bearer rt_xxxxxxxxxxxx
Content-Type: application/json

{ }
```

- 不带 body（或空对象）：注销 **token 对应的设备自己**
- 带 `{"deviceId": "uuid-yyyy"}`：仅当持有 `master_token` 时可注销**任意设备**
- 幂等：设备不存在也返回 `{"ok": true}`

---

### 4.8 健康检查

```http
GET /relay/health
```

```json
{ "ok": true, "version": "1.1.0", "uptime": 3600, "devices": 3,
  "queued": 5, "inflight": 0, "ws": 0, "db": true, "ws_enabled": true }
```

> 设备清理：后台每 30 秒扫描，**无活动超过 `device_ttl_sec`（默认 120 秒）**的设备自动删除。

---

## 五、数据模型

### 5.1 设备注册表（内存）

```python
class RelayDevice:
    device_id: str          # 唯一 ID
    device_name: str
    platform: str           # ios / android / windows
    token: str              # relayToken
    last_seen: float        # 时间戳
    online: bool            # 最近 60s 内有 poll/ws 活动
    push_mode: str          # poll / ws
    queue: deque[RelayMessage]   # 离线队列（最多 50 条）
```

### 5.2 消息

```python
class RelayMessage:
    message_id: str
    from_id: str
    from_name: str
    type: str               # push / title / text
    payload: dict
    received_at: float
    ttl_hours: int          # 默认 24
    retries: int            # 投递失败重试次数
```

### 5.3 持久化（可选）

- **默认**：纯内存，helper 重启后队列丢失（可接受，因为推送是即时性数据）
- **增强**：落盘到 `~/.kai_fan_le_helper_relay.db`（SQLite），重启恢复队列

---

## 六、内网穿透方案

helper 在本机监听 `0.0.0.0:8860`，


用户自行部署穿透，自行填入 `https://relay.yourdomain.com` →  到 helper 的 `public_url`。

### 与 helper 集成

helper 托盘菜单新增：
- **启动中继服务**（开关）
- **复制中继地址**（把 `public_url` 复制到剪贴板，供手机扫码/粘贴）
- **生成配对二维码**（二维码内容：`kaifanle://relay?url=...&token=...`，手机扫码一键配置）

---

## 七、代码落点（helper 项目）

### 新增文件

| 文件 | 作用 |
|------|------|
| `src/relay/__init__.py` | 模块入口（若走 Rust 重构则对应 `src/relay/mod.rs`） |
| `src/relay/server.py` | HTTP/WS 服务，基于 `http.server.ThreadingHTTPServer` 或 `aiohttp` |
| `src/relay/store.py` | 设备注册表 + 离线队列 |
| `src/relay/models.py` | RelayDevice / RelayMessage |
| `src/relay/client.py` | 手机端 SDK 参考实现（供 App 侧对照） |

> 若继续用 Python `main.py` 单文件形态，可先内联为 `RelayServer(QThread)` 类。

### 修改文件

| 文件 | 改动 |
|------|------|
| `main.py` | 新增 `RelayServer` 类；`MainWindow.__init__` 里按配置启动；托盘菜单加中继开关 |
| `.kai_fan_le_helper_settings.json` | 新增 `relay` 段（见第三节） |
| `README.md` | 补中继使用说明 |

### 与现有线程模型的关系

```python
# 现有
self._broadcast_listener = BroadcastListener(BROADCAST_PORT)
self._handshake_listener = HandshakeListener(HANDSHAKE_PORT)

# 新增（同款 QThread 模式）
self._relay_server = RelayServer(port=relay_cfg["port"])
self._relay_server.start()
```

---

## 八、手机端接入（App 侧改动）

### iOS / Android 共同改动

1. **设置页新增**：「中继服务器」区块
   - 服务器地址（`public_url`）
   - 注册状态（已注册 / 未注册）
   - 连接状态（在线 / 离线）
   - 待拉取消息数
2. **启动时**：若启用中继 → `POST /relay/register` → 持久化 `relayToken`
3. **后台保活**：
   - Android：`PushForegroundService` 内跑长轮询（或 WebSocket）
   - iOS：TrollStore 真后台插件 + 长轮询
4. **收到消息 → 弹本地通知**（见 `推送通知需求.md`）
5. **点击通知 → 询问弹窗** → 确认后打开 ImportSheet

### 发送侧

- 推送设备选择器里，局域网设备 + 中继设备**合并展示**
- 设备来源标记：「局域网」/「中继」
- 选中后走对应通道：局域网直连 `POST /push`，中继走 `POST /relay/send`

---

## 九、安全

| 项 | 措施 |
|----|------|
| 传输加密 | 内网穿透自带 HTTPS（Cloudflare Tunnel / frp + TLS） |
| 设备鉴权 | `relayToken`（注册时下发，UUID v4 + HMAC 签名） |
| 防重放 | 消息带 `messageId` + `receivedAt`，接收方去重 |
| 频率限制 | 单设备 30 条/分钟；注册 3 次/分钟 |
| 体积限制 | payload ≤ 256 KB |
| 端到端加密（可选） | 沿用 `ShareCode` 的 PBKDF2 + HMAC-SHA256，中继只见密文 |
| 主令牌 | `master_token` 用于远程管理（查看设备列表 / 清空队列） |

---

## 十、与现有局域网协议的关系

| 通道 | 触发 | 优点 | 缺点 |
|------|------|------|------|
| **局域网直连**（现有） | 同 WiFi | 零延迟、不走公网 | 必须同网 |
| **中继**（新增） | 任意网络 | 跨网、可离线 | 依赖公网 + 穿透 |

**优先级策略**：App 发送时先探测目标是否在同局域网（已有 `/ping`），命中则走直连，否则走中继。接收方**两个通道同时监听**，谁先到用谁（去重）。

---

## 十一、实施阶段

| 阶段 | 内容 | 产出 |
|------|------|------|
| **P0** | helper 内实现 `RelayServer`，仅 `/register` + `/send` + `/poll` | 本机 `curl` 可测通 |
| **P1** | 接入 Cloudflare Tunnel，暴露公网 | 手机可访问 |
| **P2** | Android 接入中继 + 前台服务 + 通知 | 跨网推送跑通 |
| **P3** | iOS 接入中继 + 真后台 + 通知 + 询问弹窗 | 双端完整 |
| **P4** | WebSocket 替代长轮询，降低延迟 | 体验优化 |
| **P5** | 端到端加密 + SQLite 持久化 | 生产级 |

---

## 十二、接口速查表

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/relay/register` | 否 | 注册设备，拿 token |
| POST | `/relay/send` | 是 | 发送消息 |
| GET | `/relay/poll` | 是 | 长轮询拉取 |
| POST | `/relay/ack` | 是 | 确认收到 |
| POST | `/relay/unregister` | 是 | 注销本设备（或指定 deviceId） |
| WS | `/relay/ws` | 是 | 实时通道 |
| GET | `/relay/devices` | 是 | 设备列表（含 `online`） |
| GET | `/relay/health` | 否 | 健康检查 |

---

*文档随实现更新。接口若有变更，以 helper 项目内 `src/relay/` 实际代码为准。*
