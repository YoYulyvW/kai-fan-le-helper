#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开饭了助手 - Windows 置顶工具（多设备版）
- 自动读取剪贴板
- 发现局域网内所有运行"开饭了"的手机
- 支持切换目标设备 / 广播到所有设备
- 发送剧名到手机 /submit
- 历史记录（本地 JSON 保存）
"""

import sys
import os
import re
import json
import socket
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from PySide2.QtCore import Qt, QTimer, QThread, Signal, Slot, QPoint
from PySide2.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QBrush, QLinearGradient
)
from PySide2.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QSystemTrayIcon, QMenu, QAction, QLineEdit, QListWidget, QListWidgetItem
)

# ============================================================
# 配置
# ============================================================
PORT = 8848
SCAN_TIMEOUT = 0.4
SCAN_MAX_WORKERS = 100
SCAN_INTERVAL = 8
SCAN_HARD_TIMEOUT = 20
HEARTBEAT_INTERVAL = 15
CLIPBOARD_DEBOUNCE = 400
SEND_TIMEOUT = 8
SEND_FALLBACK_MS = 12000
MAX_HISTORY = 50

# 窗口尺寸
WIN_WIDTH = 560
WIN_HEIGHT = 44

# 历史记录文件
HISTORY_FILE = os.path.join(
    os.path.expanduser("~"), ".kai_fan_le_helper_history.json"
)

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
        tokens = s.split()
        if not tokens:
            return None
        collected = []
        for tok in reversed(tokens):
            if TitleParser._is_noise(tok):
                break
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
            return TitleParser._clean(before)
        for tag in tags:
            if tag and tag not in CATEGORY_TAGS:
                return TitleParser._clean(tag)
        if before:
            return TitleParser._clean(before)
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
        if len(tok) <= 2 and tok.isalpha():
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
                        self.items = data[:MAX_HISTORY]
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
    """返回 (ip, device_name) 或 None"""
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


def scan_network(port=PORT):
    """扫描整个网段，返回 [(ip, name), ...]"""
    local_ip = get_local_ip()
    if not local_ip:
        return []

    prefix = '.'.join(local_ip.split('.')[:3]) + '.'
    ips = [f"{prefix}{i}" for i in range(1, 255)]

    found = []
    with ThreadPoolExecutor(max_workers=SCAN_MAX_WORKERS) as ex:
        futures = {ex.submit(check_ip, ip, port): ip for ip in ips}
        for fut in as_completed(futures):
            result = fut.result()
            if result:
                found.append(result)

    # 按 IP 排序，稳定顺序
    found.sort(key=lambda x: tuple(int(p) for p in x[0].split('.')))
    return found


def ping_phone(ip, port=PORT, timeout=2):
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


def send_to_phone(ip, text, port=PORT, timeout=SEND_TIMEOUT):
    url = f"http://{ip}:{port}/submit"
    body = json.dumps({"text": text}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    req.add_header('User-Agent', 'KaiFanLe-Helper/1.0')

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode('utf-8')
            return json.loads(data)
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ============================================================
# 扫描线程
# ============================================================
class DiscoveryWorker(QThread):
    finished_scan = Signal(list)

    def __init__(self, port=PORT):
        super().__init__()
        self.port = port

    def run(self):
        try:
            result = scan_network(self.port)
        except Exception:
            result = []
        self.finished_scan.emit(result)


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
    device_selected = Signal(int)  # 索引

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(360)
        self.devices = []       # [(ip, name), ...]
        self.current_index = 0
        self._build()

    def _build(self):
        container = QWidget(self)
        container.setObjectName("container")
        container.setStyleSheet("""
            #container {
                background: rgba(28, 28, 30, 0.98);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.12);
            }
            QLabel { background: transparent; }
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
                color: #FFFFFF;
                font-size: 13px;
            }
            QListWidget::item {
                padding: 10px 14px;
                border-radius: 6px;
                margin: 2px 6px;
            }
            QListWidget::item:selected {
                background: #6366F1;
            }
            QListWidget::item:hover {
                background: rgba(255, 255, 255, 0.08);
            }
        """)
        self.container = container

        self.title = QLabel("📱 选择目标设备")
        self.title.setStyleSheet(
            "color: #FFFFFF; font-size: 13px; font-weight: 600;"
            "padding: 4px 4px;"
        )

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._on_double_click)

        close_btn = QPushButton("关闭")
        close_btn.setFixedHeight(26)
        close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.08);
                color: #FFFFFF;
                border: none; border-radius: 8px;
                padding: 0 14px; font-size: 12px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.15); }
        """)
        close_btn.clicked.connect(self.hide)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addStretch()
        bottom.addWidget(close_btn)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        layout.addWidget(self.title)
        layout.addWidget(self.list, 1)
        layout.addLayout(bottom)
        container.setLayout(layout)

        # 动态高度
        self._relayout()

    def _relayout(self):
        n = len(self.devices) if self.devices else 1
        h = 60 + min(n, 6) * 44 + 40
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
            mark = " ✅" if i == current_index else ""
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
        self.setFixedWidth(520)
        self.history = history
        self._build()

    def _build(self):
        container = QWidget(self)
        container.setObjectName("container")
        container.setStyleSheet("""
            #container {
                background: rgba(28, 28, 30, 0.98);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.12);
            }
            QListWidget {
                background: transparent;
                border: none;
                outline: none;
                color: #FFFFFF;
                font-size: 13px;
            }
            QListWidget::item {
                padding: 8px 14px;
                border-radius: 6px;
                margin: 2px 6px;
            }
            QListWidget::item:selected {
                background: #6366F1;
            }
            QListWidget::item:hover {
                background: rgba(255, 255, 255, 0.08);
            }
        """)
        container.setGeometry(0, 0, 520, 320)
        self.container = container

        title = QLabel("📋 历史记录（双击填入）")
        title.setStyleSheet(
            "color: #FFFFFF; font-size: 13px; font-weight: 600;"
            "padding: 4px 4px;"
        )

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._on_double_click)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(6)
        bottom.addStretch()

        clear_btn = QPushButton("清空")
        clear_btn.setFixedHeight(26)
        clear_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 59, 48, 0.2);
                color: #FF453A;
                border: none; border-radius: 8px;
                padding: 0 14px; font-size: 12px;
            }
            QPushButton:hover { background: rgba(255, 59, 48, 0.3); }
        """)
        clear_btn.clicked.connect(self._on_clear)

        close_btn = QPushButton("关闭")
        close_btn.setFixedHeight(26)
        close_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.08);
                color: #FFFFFF;
                border: none; border-radius: 8px;
                padding: 0 14px; font-size: 12px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.15); }
        """)
        close_btn.clicked.connect(self.hide)

        bottom.addWidget(clear_btn)
        bottom.addWidget(close_btn)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        layout.addWidget(title)
        layout.addWidget(self.list, 1)
        layout.addLayout(bottom)
        container.setLayout(layout)

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

        # ✅ 多设备支持
        self.devices = []       # [(ip, name), ...]
        self.current_index = 0  # 当前目标设备索引

        self.last_clipboard = ""
        self._drag_pos = None
        self._discovering = False
        self._quitting = False
        self._send_fallback = None
        self._scan_hard_timeout = None

        self.history = History()

        self.setup_ui()
        self.setup_clipboard()
        self.setup_tray()
        self.setup_panels()
        self.setup_timers()

        self.position_top_right()
        QTimer.singleShot(500, self.start_discovery)

    # ---------- 当前设备辅助 ----------
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

    # ---------- UI ----------
    def setup_ui(self):
        container = QWidget(self)
        container.setObjectName("container")
        container.setStyleSheet("""
            #container {
                background: rgba(28, 28, 30, 0.96);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.12);
            }
            QLabel { background: transparent; }
            QPushButton { outline: none; }
            QLineEdit {
                background: rgba(255, 255, 255, 0.08);
                border: none;
                border-radius: 8px;
                color: #FFFFFF;
                font-size: 13px;
                padding: 0 10px;
                selection-background-color: #6366F1;
            }
            QLineEdit:focus {
                background: rgba(255, 255, 255, 0.12);
            }
        """)
        container.setGeometry(0, 0, WIN_WIDTH, WIN_HEIGHT)
        self.container = container

        # 状态点
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #FF9500; font-size: 11px;")
        self.status_dot.setFixedWidth(12)
        self.status_dot.setAlignment(Qt.AlignCenter)

        # 状态文字（可点击 → 打开设备面板）
        self.status_text = QLabel("扫描中")
        self.status_text.setStyleSheet(
            "color: #8E8E93; font-size: 11px;")
        self.status_text.setFixedWidth(150)
        self.status_text.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status_text.setCursor(Qt.PointingHandCursor)
        self.status_text.mousePressEvent = self._on_status_clicked

        # 输入框
        self.input = QLineEdit()
        self.input.setPlaceholderText("等待剪贴板...")
        self.input.setFixedHeight(30)
        self.input.setMinimumWidth(140)
        self.input.returnPressed.connect(self.on_send)

        # 历史按钮
        self.history_btn = QPushButton("📋")
        self.history_btn.setFixedSize(30, 30)
        self.history_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.08);
                color: #FFFFFF;
                border: none; border-radius: 8px;
                font-size: 14px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.15); }
            QPushButton:pressed { background: rgba(255, 255, 255, 0.2); }
        """)
        self.history_btn.clicked.connect(self.toggle_history)

        # 发送按钮
        self.send_btn = QPushButton("发送")
        self.send_btn.setFixedSize(46, 30)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: #34C759; color: white;
                border: none; border-radius: 8px;
                font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: #30D158; }
            QPushButton:pressed { background: #28A745; }
            QPushButton:disabled { background: #3A3A3C; color: #8E8E93; }
        """)
        self.send_btn.clicked.connect(self.on_send)
        self.send_btn.setEnabled(False)

        # 关闭按钮
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setStyleSheet("""
            QPushButton {
                background: transparent; color: #8E8E93;
                border: none; border-radius: 6px;
                font-size: 11px; font-weight: 600;
            }
            QPushButton:hover {
                background: rgba(255, 255, 255, 0.1); color: #FFFFFF;
            }
        """)
        self.close_btn.clicked.connect(self.hide)

        row = QHBoxLayout()
        row.setContentsMargins(8, 7, 6, 7)
        row.setSpacing(4)
        row.addWidget(self.status_dot)
        row.addWidget(self.status_text)
        row.addWidget(self.input, 1)
        row.addWidget(self.history_btn)
        row.addWidget(self.send_btn)
        row.addWidget(self.close_btn)
        container.setLayout(row)

    def position_top_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.right() - self.width() - 20
        y = screen.top() + 20
        self.move(x, y)

    def _on_status_clicked(self, event):
        """点击状态文字 → 打开设备面板"""
        if event.button() == Qt.LeftButton:
            self.toggle_device_panel()

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
        pos = self.mapToGlobal(QPoint(0, self.height() + 6))
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
        pos = self.mapToGlobal(QPoint(0, self.height() + 6))
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

        title, is_fast = TitleParser.parse(text)
        if title:
            display = title + (" - 极速" if is_fast else "")
            self.input.setText(display)
            self.send_btn.setEnabled(self.current_ip is not None)

    # ---------- 托盘 ----------
    def setup_tray(self):
        self.tray = QSystemTrayIcon(create_icon(), self)
        self.tray.setToolTip("开饭了助手")

        menu = QMenu()
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)

        rediscover_action = QAction("重新扫描", self)
        rediscover_action.triggered.connect(self.start_discovery)
        menu.addAction(rediscover_action)

        device_action = QAction("选择设备", self)
        device_action.triggered.connect(self._show_device_from_tray)
        menu.addAction(device_action)

        history_action = QAction("历史记录", self)
        history_action.triggered.connect(self._show_history_from_tray)
        menu.addAction(history_action)

        menu.addSeparator()

        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

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

        self._rediscover_timer = QTimer(self)
        self._rediscover_timer.timeout.connect(self.on_rediscover)
        self._rediscover_timer.start(SCAN_INTERVAL * 1000)

    def on_heartbeat(self):
        ip = self.current_ip
        if not ip:
            return

        def do_ping():
            ok = ping_phone(ip)
            if not ok:
                QTimer.singleShot(0, lambda: self._on_device_offline(ip))
        threading.Thread(target=do_ping, daemon=True).start()

    def _on_device_offline(self, ip):
        """当前设备离线 → 从列表移除"""
        old_len = len(self.devices)
        self.devices = [d for d in self.devices if d[0] != ip]
        if len(self.devices) != old_len:
            # 索引修正
            if self.current_index >= len(self.devices):
                self.current_index = max(0, len(self.devices) - 1)
            self._update_status()
            self.send_btn.setEnabled(
                self.current_ip is not None and bool(self.input.text().strip()))
            if self.device_panel.isVisible():
                self.device_panel.refresh(self.devices, self.current_index)

    def on_rediscover(self):
        if self._discovering:
            return
        self.start_discovery()

    # ---------- 扫描 ----------
    def start_discovery(self):
        if self._discovering:
            return
        self._discovering = True
        self.set_status_text("扫描中", "#FF9500")
        self.send_btn.setEnabled(False)

        if self._scan_hard_timeout is not None:
            self._scan_hard_timeout.stop()
        self._scan_hard_timeout = QTimer(self)
        self._scan_hard_timeout.setSingleShot(True)
        self._scan_hard_timeout.timeout.connect(self._force_reset_scan)
        self._scan_hard_timeout.start(SCAN_HARD_TIMEOUT * 1000)

        self._worker = DiscoveryWorker(PORT)
        self._worker.finished_scan.connect(self.on_discovery_finished)
        self._worker.start()

    def _force_reset_scan(self):
        if self._discovering:
            print("[Scan] 硬超时，强制重置")
            self._discovering = False
            self._update_status()

    @Slot(list)
    def on_discovery_finished(self, ips):
        if self._scan_hard_timeout is not None:
            self._scan_hard_timeout.stop()
            self._scan_hard_timeout = None

        self._discovering = False

        # ✅ 更新设备列表
        old_current_ip = self.current_ip
        self.devices = ips  # [(ip, name), ...]

        # 尽量保持当前设备不变
        if old_current_ip:
            for i, (ip, _) in enumerate(self.devices):
                if ip == old_current_ip:
                    self.current_index = i
                    break
            else:
                # 当前设备消失
                self.current_index = 0
        else:
            self.current_index = 0

        self._update_status()

        if self.current_ip and self.input.text().strip():
            self.send_btn.setEnabled(True)
        else:
            self.send_btn.setEnabled(False)

        if self.device_panel.isVisible():
            self.device_panel.refresh(self.devices, self.current_index)

    def _update_status(self):
        """根据设备数量和当前选择更新状态"""
        n = len(self.devices)
        if n == 0:
            self.set_status_text("未找到", "#FF3B30")
            self.status_text.setToolTip("")
        elif n == 1:
            name = self.current_name or "手机"
            display = f"已连接 {name}"
            if len(display) > 17:
                display = display[:17] + "…"
            self.set_status_text(display, "#34C759")
            self.status_text.setToolTip(
                f"{name}\nIP: {self.current_ip}\n\n点击切换设备")
        else:
            name = self.current_name or "手机"
            display = f"{name}（共{n}台）"
            if len(display) > 17:
                display = display[:17] + "…"
            self.set_status_text(display, "#34C759")
            self.status_text.setToolTip(
                f"当前: {name}\nIP: {self.current_ip}\n共 {n} 台设备\n\n点击切换设备")

    def set_status_text(self, text, color):
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 11px;")
        self.status_text.setText(text)
        if color == "#FF3B30":
            self.status_text.setStyleSheet("color: #FF3B30; font-size: 11px;")
        elif color == "#34C759":
            self.status_text.setStyleSheet("color: #34C759; font-size: 11px;")
        else:
            self.status_text.setStyleSheet("color: #8E8E93; font-size: 11px;")

    def _restore_status(self):
        self._update_status()

    # ---------- 发送 ----------
    def on_send(self):
        text = self.input.text().strip()
        if not text:
            self._flash("无内容", "#FF9500")
            return
        ip = self.current_ip
        if not ip:
            self._flash("未连接", "#FF3B30")
            return

        self.send_btn.setEnabled(False)
        self.send_btn.setText("...")

        if self._send_fallback is not None:
            self._send_fallback.stop()
        self._send_fallback = QTimer(self)
        self._send_fallback.setSingleShot(True)
        self._send_fallback.timeout.connect(self._force_recover_button)
        self._send_fallback.start(SEND_FALLBACK_MS)

        def do_send():
            try:
                result = send_to_phone(ip, text)
            except Exception as e:
                result = {"ok": False, "message": str(e)}
            QTimer.singleShot(0, lambda: self._on_send_result(text, result))

        threading.Thread(target=do_send, daemon=True).start()

    def _force_recover_button(self):
        if self.send_btn.text() == "...":
            self.send_btn.setEnabled(self.current_ip is not None)
            self.send_btn.setText("发送")

    def _on_send_result(self, sent_text, result):
        if self._send_fallback is not None:
            self._send_fallback.stop()
            self._send_fallback = None

        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")

        try:
            ok = bool(result.get("ok"))
        except Exception:
            ok = False

        if ok:
            self._flash("已发送", "#34C759")
            title, _ = TitleParser.parse(sent_text)
            if title:
                self.history.add(sent_text, title)
            self.input.clear()
            if self.history_panel.isVisible():
                self.history_panel.refresh()
        else:
            msg = result.get("message", "失败")
            if len(msg) > 6:
                msg = "失败"
            self._flash(msg, "#FF3B30")

    def _flash(self, text, color):
        self.set_status_text(text, color)
        QTimer.singleShot(1500, self._restore_status)


# ============================================================
# main
# ============================================================
def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
