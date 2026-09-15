#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
开饭了助手 - Windows 置顶工具（精简版）
- 自动读取剪贴板
- 发现局域网内运行"开饭了"的手机
- 发送剧名到手机 /submit
"""

import sys
import re
import json
import socket
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from PySide2.QtCore import Qt, QTimer, QThread, Signal, Slot
from PySide2.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QFont, QBrush, QLinearGradient
)
from PySide2.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QSystemTrayIcon, QMenu, QAction, QLineEdit
)

# ============================================================
# 配置
# ============================================================
PORT = 8848
SCAN_TIMEOUT = 0.4
SCAN_MAX_WORKERS = 100
SCAN_INTERVAL = 30
HEARTBEAT_INTERVAL = 15
CLIPBOARD_DEBOUNCE = 400
SEND_TIMEOUT = 15

# 窗口尺寸
WIN_WIDTH = 360
WIN_HEIGHT = 44

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
        data = s.recv(1024)
        s.close()
        if b'"ok"' in data and b'"app"' in data:
            return ip
        if b'200 OK' in data:
            return ip
    except Exception:
        pass
    return None


def scan_network(port=PORT):
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
    return found


def ping_phone(ip, port=PORT, timeout=2):
    return check_ip(ip, port, timeout) is not None


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

        self.device_ip = None
        self.last_clipboard = ""
        self._drag_pos = None
        self._discovering = False
        self._quitting = False

        self.setup_ui()
        self.setup_clipboard()
        self.setup_tray()
        self.setup_timers()

        self.position_top_right()
        QTimer.singleShot(500, self.start_discovery)

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
        self.status_dot.setFixedWidth(14)
        self.status_dot.setAlignment(Qt.AlignCenter)

        # 输入框（能显示约 15 个汉字）
        self.input = QLineEdit()
        self.input.setPlaceholderText("等待剪贴板...")
        self.input.setFixedHeight(30)
        self.input.setMinimumWidth(150)
        self.input.returnPressed.connect(self.on_send)

        # 发送按钮
        self.send_btn = QPushButton("发送")
        self.send_btn.setFixedSize(48, 30)
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
        row.setSpacing(5)
        row.addWidget(self.status_dot)
        row.addWidget(self.input, 1)   # 输入框弹性拉伸
        row.addWidget(self.send_btn)
        row.addWidget(self.close_btn)
        container.setLayout(row)

    def position_top_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.right() - self.width() - 20
        y = screen.top() + 20
        self.move(x, y)

    # ---------- 拖动 ----------
    # 只有状态点区域和边距可以拖动（输入框和按钮会拦截鼠标事件）
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
            self.send_btn.setEnabled(self.device_ip is not None)
        # 提取失败就不覆盖输入框（保留用户手动输入的内容）

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

        menu.addSeparator()

        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

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
        if not self.device_ip:
            return

        def do_ping():
            ok = ping_phone(self.device_ip)
            if not ok:
                QTimer.singleShot(0, self._on_lost_connection)
        threading.Thread(target=do_ping, daemon=True).start()

    def _on_lost_connection(self):
        if not self.device_ip:
            return
        self.device_ip = None
        self.set_status_color("#FF3B30")
        self.send_btn.setEnabled(False)
        QTimer.singleShot(1000, self.start_discovery)

    def on_rediscover(self):
        if self.device_ip or self._discovering:
            return
        self.start_discovery()

    # ---------- 扫描 ----------
    def start_discovery(self):
        if self._discovering:
            return
        self._discovering = True
        self.set_status_color("#FF9500")
        self.send_btn.setEnabled(False)

        self._worker = DiscoveryWorker(PORT)
        self._worker.finished_scan.connect(self.on_discovery_finished)
        self._worker.start()

    @Slot(list)
    def on_discovery_finished(self, ips):
        self._discovering = False
        if ips:
            self.device_ip = ips[0]
            self.set_status_color("#34C759")
            if self.input.text().strip():
                self.send_btn.setEnabled(True)
        else:
            self.device_ip = None
            self.set_status_color("#FF3B30")
            self.send_btn.setEnabled(False)

    def set_status_color(self, color):
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 11px;")

    # ---------- 发送 ----------
    def on_send(self):
        text = self.input.text().strip()
        if not text:
            self.flash_status("#FF9500")
            return
        if not self.device_ip:
            self.flash_status("#FF3B30")
            return

        def do_send():
            result = send_to_phone(self.device_ip, text)
            QTimer.singleShot(0, lambda: self._on_send_result(result))

        self.send_btn.setEnabled(False)
        self.send_btn.setText("...")
        threading.Thread(target=do_send, daemon=True).start()

    def _on_send_result(self, result):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")

        if result.get("ok"):
            self.flash_status("#34C759")
            self.input.clear()
        else:
            self.flash_status("#FF3B30")

    def flash_status(self, color):
        self.set_status_color(color)

        def restore():
            if self.device_ip:
                self.set_status_color("#34C759")
            else:
                self.set_status_color("#FF3B30")

        QTimer.singleShot(2000, restore)


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