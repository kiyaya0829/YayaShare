"""Qt Widgets UI. Network and hashing work never run on the GUI thread."""
from pathlib import Path
import ipaddress
import threading
import time

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QImageReader
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QVBoxLayout, QWidget, QCheckBox, QMenu, QSystemTrayIcon)

from .clipboard import MAX_IMAGE
from .discovery import Discovery, peer_at
from .protocol import PORT, Service
from .storage import State


class Bridge(QObject):
    event = Signal(str, str)
    pairing = Signal(str, str, object, object)


class Job(QThread):
    result = Signal(str)
    progress = Signal(int)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel_event = threading.Event()

    def run(self):
        try:
            self.fn(self.progress.emit)
            self.result.emit("")
        except Exception as exc:
            self.result.emit(str(exc))


class Window(QMainWindow):
    def __init__(self, state=None, port=PORT, discover=True, tray_enabled=True):
        super().__init__()
        self.state = state or State()
        self.bridge = Bridge()
        self.bridge.event.connect(self.network_event)
        self.bridge.pairing.connect(self.show_pairing_confirmation)
        self.service = Service(self.state, port=port, event=self.bridge.event.emit,
                               confirm_pairing=self.confirm_pairing)
        self.service.start()
        self.discovery = Discovery(self.service.info)
        if discover:
            self.discovery.start()
        self.job = None
        self.clipboard_job = None
        self.clipboard_pending = None
        self.applying_clipboard = False
        self.cached_image = QImage()
        self.cached_png = None
        self.last_clipboard_image = None
        self.quitting = False
        self.tray = None
        self.last_history = None
        self.last_devices = None
        self.setWindowTitle("YayaShare · 局域网共享")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets/icon.png")))
        self.resize(940, 680)
        self.setMinimumSize(740, 560)
        self.setAcceptDrops(True)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        title = QLabel("YayaShare")
        title.setStyleSheet("font-size: 30px; font-weight: 700; color: #bca7ff;")
        layout.addWidget(title)
        self.local = QLabel(f"{self.state.identity['name']}  ·  同一 Wi-Fi，轻松互传  ·  端口 {self.service.port}")
        self.local.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.local)
        row = QHBoxLayout()
        self.devices = QComboBox()
        self.devices.setMinimumWidth(220)
        row.addWidget(self.devices, 1)
        self.pair_btn = QPushButton("配对 / 手动连接")
        self.pair_btn.clicked.connect(self.pair_dialog)
        row.addWidget(self.pair_btn)
        self.invite_btn = QPushButton("生成我的配对码")
        self.invite_btn.clicked.connect(self.show_invitation)
        row.addWidget(self.invite_btn)
        sas = QPushButton("接收六位码配对")
        sas.clicked.connect(self.allow_pairing)
        row.addWidget(sas)
        self.forget_btn = QPushButton("取消信任")
        self.forget_btn.clicked.connect(self.forget)
        row.addWidget(self.forget_btn)
        layout.addLayout(row)
        self.web_btn = QPushButton("手机网页 · 扫码收发文字和文件")
        self.web_btn.clicked.connect(self.show_web_portal)
        layout.addWidget(self.web_btn)
        self.sync_toggle = QCheckBox("Clipboard Sync · 自动同步到所有已配对设备")
        self.sync_toggle.setToolTip("两边都需开启。设置会保留，仅同步开启后新复制的内容。")
        layout.addWidget(self.sync_toggle)
        self.image_toggle = QCheckBox("同步截图和剪贴板图片")
        layout.addWidget(self.image_toggle)
        self.sync_status = QLabel("剪贴板同步已关闭")
        self.sync_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.sync_status)
        split = QSplitter()
        compose = QWidget()
        left = QVBoxLayout(compose)
        left.setContentsMargins(0, 0, 8, 0)
        left.addWidget(QLabel("发送文字"))
        self.editor = QPlainTextEdit()
        self.editor.viewport().installEventFilter(self)
        self.editor.setPlaceholderText("这里的文字在点击发送时共享。\n自动剪贴板同步由上方开关控制。")
        left.addWidget(self.editor)
        actions = QHBoxLayout()
        self.clip_btn = QPushButton("读取剪贴板")
        self.clip_btn.clicked.connect(lambda: self.editor.setPlainText(QApplication.clipboard().text()))
        actions.addWidget(self.clip_btn)
        self.send_btn = QPushButton("发送文字")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.send_text)
        actions.addWidget(self.send_btn)
        left.addLayout(actions)
        self.file_btn = QPushButton("选择文件并发送 · 最大 2 GiB")
        self.file_btn.clicked.connect(self.send_file)
        left.addWidget(self.file_btn)
        self.folder_btn = QPushButton("选择文件夹并发送 · 总计最大 2 GiB")
        self.folder_btn.clicked.connect(self.send_folder)
        left.addWidget(self.folder_btn)
        split.addWidget(compose)
        history_panel = QWidget()
        right = QVBoxLayout(history_panel)
        right.setContentsMargins(8, 0, 0, 0)
        right.addWidget(QLabel("最近接收 · 保留 100 条"))
        self.history_list = QListWidget()
        self.history_list.currentRowChanged.connect(self.preview)
        right.addWidget(self.history_list, 2)
        self.detail = QPlainTextEdit()
        self.detail.viewport().installEventFilter(self)
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择记录查看全文或文件位置")
        right.addWidget(self.detail, 1)
        buttons = QHBoxLayout()
        copy = QPushButton("复制内容")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.detail.toPlainText()))
        buttons.addWidget(copy)
        folder = QPushButton("打开接收文件夹")
        folder.clicked.connect(self.open_inbox)
        buttons.addWidget(folder)
        change_folder = QPushButton("更改接收目录")
        change_folder.clicked.connect(self.choose_inbox)
        buttons.addWidget(change_folder)
        right.addLayout(buttons)
        split.addWidget(history_panel)
        split.setSizes([430, 430])
        layout.addWidget(split, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.cancel_btn = QPushButton("取消传输")
        self.cancel_btn.clicked.connect(self.cancel_transfer)
        self.cancel_btn.hide()
        layout.addWidget(self.cancel_btn)
        self.status = QLabel("准备就绪。先在另一台设备生成配对码，再在这里配对。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setStyleSheet("""
            QWidget { background: #171923; color: #e8e8f1; font-size: 13px; }
            QPlainTextEdit, QListWidget, QComboBox, QLineEdit {
                background: #222533; border: 1px solid #3c4053; border-radius: 8px; padding: 9px; }
            QPushButton { background: #303448; border: 1px solid #444961; border-radius: 7px; padding: 9px 12px; }
            QPushButton:hover { background: #434962; }
            QPushButton:disabled { color: #777b8d; }
            QPushButton#primary { background: #7155bd; border-color: #9273df; }
            QListWidget::item:selected { background: #493966; }
            QProgressBar { border: 0; background: #303448; text-align: center; }
            QProgressBar::chunk { background: #9575de; }
        """)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.clipboard_timer = QTimer(self)
        self.clipboard_timer.timeout.connect(self.poll_clipboard)
        self.clipboard_timer.start(400)
        QApplication.clipboard().dataChanged.connect(self.poll_clipboard)
        settings = self.state.settings
        self.sync_toggle.setChecked(settings.get("clipboard_sync") is True)
        self.image_toggle.setChecked(settings.get("clipboard_images") is True)
        self.set_clipboard_options()
        self.sync_toggle.toggled.connect(self.configure_clipboard)
        self.image_toggle.toggled.connect(self.configure_clipboard)
        if tray_enabled and QSystemTrayIcon.isSystemTrayAvailable():
            self.create_tray()
        self.refresh()

    def create_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("YayaShare")
        menu = QMenu(self)
        menu.addAction("打开主窗口", self.activate_window)
        toggle = menu.addAction("Clipboard Sync")
        toggle.setCheckable(True)
        toggle.setChecked(self.sync_toggle.isChecked())
        toggle.toggled.connect(self.sync_toggle.setChecked)
        self.sync_toggle.toggled.connect(toggle.setChecked)
        menu.addAction("手机网页入口", self.tray_web_portal)
        menu.addAction("打开接收文件夹", self.open_inbox)
        menu.addAction("彻底退出", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.activate_window()

    def activate_window(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def tray_web_portal(self):
        self.activate_window()
        self.show_web_portal()

    def open_inbox(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.state.inbox)))

    def choose_inbox(self):
        directory = QFileDialog.getExistingDirectory(self, "选择接收保存目录", str(self.state.inbox))
        if directory:
            try:
                self.state.configure(receive_directory=directory)
                self.status.setText(f"接收目录已设为 {self.state.inbox}")
            except (OSError, ValueError) as exc:
                self.status.setText(f"无法更改接收目录：{exc}")

    def accepts_paths(self, event):
        urls = event.mimeData().urls()
        return bool(urls) and all(url.isLocalFile() for url in urls)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop) and self.accepts_paths(event):
            if kind == QEvent.Type.Drop:
                self.dropEvent(event)
            else:
                self.dragEnterEvent(event)
            return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if not self.job and self.accepts_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        if self.job or not paths or not all(paths):
            return
        peer = self.trusted()
        if peer:
            event.acceptProposedAction()
            self.send_paths(peer, paths)

    def clipboard_text(self):
        mime = QApplication.clipboard().mimeData()
        if mime is None or not mime.hasText() or mime.hasUrls() or mime.hasImage():
            return None
        return mime.text()

    def show_web_portal(self):
        from .web_dialog import PortalDialog
        dialog = PortalDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def clipboard_image(self):
        if not self.image_toggle.isChecked():
            return None
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None or not mime.hasImage() or mime.hasUrls():
            self.cached_image = QImage()
            self.cached_png = None
            return None
        image = clipboard.image()
        if image.isNull() or image.width() * image.height() > 24_000_000:
            self.cached_image = QImage()
            self.cached_png = None
            return None
        if image == self.cached_image:
            return self.cached_png
        self.cached_image = image
        self.cached_png = None
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if image.save(buffer, "PNG") and buffer.size() <= MAX_IMAGE:
            self.cached_png = bytes(buffer.data())
        return self.cached_png

    def write_clipboard(self, content):
        clipboard = QApplication.clipboard()
        if isinstance(content, str):
            clipboard.setText(content)
            return
        buffer = QBuffer()
        buffer.setData(QByteArray(content))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer, b"png")
        size = reader.size()
        if size.isValid() and 0 < size.width() * size.height() <= 24_000_000:
            image = reader.read()
            if not image.isNull():
                clipboard.setImage(image)

    def copy_private_text(self, text):
        self.applying_clipboard = True
        try:
            QApplication.clipboard().setText(text)
            self.service.clipboard.baseline(self.clipboard_text())
            self.last_clipboard_image = None
        finally:
            self.applying_clipboard = False

    def configure_clipboard(self, _checked):
        try:
            self.state.configure(clipboard_sync=self.sync_toggle.isChecked(),
                                 clipboard_images=self.image_toggle.isChecked())
        except OSError as exc:
            self.status.setText(f"无法保存剪贴板设置：{exc}")
        self.set_clipboard_options()

    def set_clipboard_options(self):
        enabled = self.sync_toggle.isChecked()
        if not enabled or not self.image_toggle.isChecked():
            self.cached_image = QImage()
            self.cached_png = None
        self.last_clipboard_image = self.clipboard_image() if enabled else None
        self.service.clipboard.configure(enabled, self.clipboard_text(),
                                         images=self.image_toggle.isChecked(),
                                         image=self.last_clipboard_image)
        self.clipboard_pending = None
        self.sync_status.setText("剪贴板同步已开启（两边均需开启）" if enabled else "剪贴板同步已关闭")

    def poll_clipboard(self):
        if self.applying_clipboard or not self.service.clipboard.enabled:
            return
        sync = self.service.clipboard
        self.applying_clipboard = True
        try:
            if sync.apply_pending(self.state.peers(), self.write_clipboard):
                # OS clipboard may normalize line endings. Baseline the read-back
                # so the resulting dataChanged/poll never turns into a new update.
                self.last_clipboard_image = self.clipboard_image()
                sync.baseline(self.clipboard_text(), self.last_clipboard_image)
                self.clipboard_pending = None
                self.sync_status.setText("已接收远端剪贴板，可以直接粘贴")
            text, image = self.clipboard_text(), self.clipboard_image()
            if text != sync.last_text or image is not self.last_clipboard_image:
                self.last_clipboard_image = image
                update = sync.local_change(text, image)
                if update:
                    self.clipboard_pending = (update, sync.generation)
        finally:
            self.applying_clipboard = False
        if self.clipboard_job or not self.clipboard_pending:
            return
        update, generation = self.clipboard_pending
        self.clipboard_pending = None
        peers = self.state.peers()
        found = self.discovery.snapshot()
        for pid, peer in peers.items():
            if pid in found and peer.get("fingerprint") == found[pid]["fingerprint"]:
                online = found[pid]
                peers[pid] = peer_at(peer, online["host"], online["port"], online["seen"])
        if not peers:
            self.sync_status.setText("剪贴板同步等待配对设备")
            return
        def send(_):
            errors = []
            for peer in peers.values():
                if not sync.enabled or sync.generation != generation:
                    break
                try:
                    self.service.send_clipboard(peer, update, generation)
                except Exception as exc:
                    errors.append(f"{peer['name']}: {exc}")
            if errors:
                raise ValueError("；".join(errors))
        self.clipboard_job = Job(send, self)
        self.clipboard_job.result.connect(self.clipboard_result)
        self.clipboard_job.finished.connect(self.clipboard_finished)
        self.clipboard_job.start()

    def clipboard_result(self, error):
        if self.sync_toggle.isChecked():
            self.sync_status.setText("同步未完成：" + error if error else "新剪贴板已发送")

    def clipboard_finished(self):
        self.clipboard_job.deleteLater()
        self.clipboard_job = None

    def refresh(self):
        known = self.state.peers()
        found = self.discovery.snapshot()
        devices = []
        for pid, peer in known.items():
            peer = dict(peer)
            online = found.get(pid) if found.get(pid, {}).get("fingerprint") == peer.get("fingerprint") else None
            if online:
                # Never replace stored fingerprint/token with untrusted UDP values.
                peer = peer_at(peer, online["host"], online["port"], online["seen"])
            devices.append((f"{peer['name']} · 已信任{' · 在线' if online else ''}", peer))
        for pid, peer in found.items():
            if pid not in known:
                devices.append((f"{peer['name']} · 未配对", peer))
        signature = [(label, {k: v for k, v in p.items() if k not in ("seen", "lan_seen")}) for label, p in devices]
        if signature != self.last_devices:
            selected = self.devices.currentData()
            self.devices.clear()
            for label, peer in devices:
                self.devices.addItem(label, peer)
                if selected and selected["id"] == peer["id"]:
                    self.devices.setCurrentIndex(self.devices.count() - 1)
            if not devices:
                self.devices.addItem("正在寻找设备… 可手动输入 IP", None)
            self.last_devices = signature
        history = self.state.history()
        if history != self.last_history:
            self.last_history = history
            self.history_list.clear()
            for entry in history:
                content = Path(entry["content"]).name if entry["kind"] in ("file", "folder") else entry["content"]
                kind = {"file": "文件", "folder": "文件夹", "text": "文字"}.get(entry["kind"], entry["kind"])
                label = f"{kind} · {entry['sender']}\n{content[:70].replace(chr(10), ' ')}"
                item = QListWidgetItem(label)
                item.setToolTip(entry["time"])
                self.history_list.addItem(item)
            if history:
                self.history_list.setCurrentRow(0)
        if self.discovery.error and not self.job:
            self.status.setText("自动发现暂不可用，请手动输入局域网 IP。" + self.discovery.error)

    def preview(self, row):
        if self.last_history and 0 <= row < len(self.last_history):
            self.detail.setPlainText(self.last_history[row]["content"])

    def network_event(self, kind, name):
        if kind == "paired":
            self.last_devices = None
        self.status.setText(f"{'配对成功' if kind == 'paired' else '已收到内容'}：{name}")
        self.refresh()

    def run_job(self, fn, message, *, cancellable=False):
        if self.job:
            return
        self.status.setText(message)
        self.progress.setValue(0)
        self.progress.show()
        for button in (self.pair_btn, self.send_btn, self.file_btn, self.folder_btn, self.forget_btn):
            button.setEnabled(False)
        self.job = Job(fn, self)
        self.cancel_btn.setVisible(cancellable)
        self.cancel_btn.setEnabled(cancellable)
        self.job.progress.connect(self.progress.setValue)
        self.job.result.connect(self.job_result)
        self.job.finished.connect(self.job_finished)
        self.job.start()

    def cancel_transfer(self):
        if self.job:
            self.job.cancel_event.set()
            self.cancel_btn.setEnabled(False)
            self.status.setText("正在取消传输…")

    def job_result(self, error):
        if error:
            self.status.setText("未完成：" + error)
        else:
            self.status.setText("操作成功，对方已确认接收。")

    def job_finished(self):
        self.job.deleteLater()
        self.job = None
        self.progress.hide()
        self.cancel_btn.hide()
        for button in (self.pair_btn, self.send_btn, self.file_btn, self.folder_btn, self.forget_btn):
            button.setEnabled(True)
        self.refresh()

    def trusted(self):
        peer = self.devices.currentData()
        if not peer or "token" not in peer:
            self.status.setText("请先选择已信任的设备，或点击配对。")
            return None
        peer = self.state.peers().get(peer["id"])
        if not peer:
            return None
        online = self.discovery.snapshot().get(peer["id"])
        if online and online["fingerprint"] == peer["fingerprint"]:
            return peer_at(peer, online["host"], online["port"], online["seen"])
        return peer

    def send_paths(self, peer, paths):
        def send(progress):
            for index, path in enumerate(paths):
                self.service.send(peer, path=path, cancel=self.job.cancel_event,
                                  progress=lambda value: progress((index * 100 + value) // len(paths)))
                progress((index + 1) * 100 // len(paths))
        self.run_job(send, "正在发送文件或文件夹…", cancellable=True)

    def send_text(self):
        peer = self.trusted()
        if peer:
            text = self.editor.toPlainText()
            self.run_job(lambda _: self.service.send(peer, text=text), "正在发送文字…")

    def send_file(self):
        peer = self.trusted()
        if not peer:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if path:
            self.send_paths(peer, [path])

    def send_folder(self):
        peer = self.trusted()
        if peer:
            path = QFileDialog.getExistingDirectory(self, "选择要发送的整个文件夹")
            if path:
                self.send_paths(peer, [path])

    def show_invitation(self):
        from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
        addresses = [a.toString() for a in QNetworkInterface.allAddresses()
                     if a.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not a.isLoopback()]
        code = self.service.invitation.create()
        dialog = QDialog(self)
        dialog.setWindowTitle("我的一次性配对码")
        layout = QVBoxLayout(dialog)
        label = QLabel("在另一台设备点击「配对」，输入本机 IP 和下面的配对码。\n"
                       f"本机 IPv4：{', '.join(addresses) or '未找到局域网地址'}   端口：{self.service.port}\n"
                       "配对码包含证书指纹，5 分钟内有效，成功使用一次即失效。\n仅通过你信任的渠道传给自己的另一台设备。")
        label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(label)
        field = QLineEdit(code)
        field.setReadOnly(True)
        field.setMinimumWidth(560)
        layout.addWidget(field)
        copy = QPushButton("复制配对码")
        copy.clicked.connect(lambda: self.copy_private_text(code))
        layout.addWidget(copy)
        remaining = QLabel()
        layout.addWidget(remaining)
        timer = QTimer(dialog)
        timer.timeout.connect(lambda: remaining.setText(f"剩余 {max(0, int(self.service.invitation.expires - time.monotonic()))} 秒（关闭窗口仍可使用）"))
        timer.start(1000)
        dialog.exec()

    def allow_pairing(self):
        self.service.allow_pairing()
        self.status.setText("接收配对已开启两分钟。在另一台设备发起配对，然后核对两边的六位数字。")

    def confirm_pairing(self, name, code):
        done = threading.Event()
        accepted = []
        self.bridge.pairing.emit(name, code, done, accepted)
        deadline = time.monotonic() + 60
        while not done.wait(0.2):
            if self.service.stop_event.is_set() or time.monotonic() >= deadline:
                done.set()
                return False
        return bool(accepted)

    def show_pairing_confirmation(self, name, code, done, accepted):
        if done.is_set():
            return
        self.activate_window()
        dialog = QMessageBox(QMessageBox.Icon.Question, "核对六位配对码",
                             f"设备：{name}\n\n{code}\n\n请确认另一台设备显示完全相同的数字。",
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        QTimer.singleShot(59000, dialog.reject)
        if dialog.exec() == QMessageBox.StandardButton.Yes and not done.is_set():
            accepted.append(True)
        done.set()
        dialog.deleteLater()

    def pair_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("配对 / 手动连接")
        layout = QFormLayout(dialog)
        current = self.devices.currentData() or {}
        discovered = self.discovery.snapshot().get(current.get("id"))
        preferred = discovered if discovered and discovered["fingerprint"] == current.get("fingerprint") else current
        host = QLineEdit(preferred.get("host", ""))
        host.setPlaceholderText("另一台电脑的局域网或 Tailscale IPv4")
        port = QLineEdit(str(preferred.get("port", PORT)))
        code = QLineEdit()
        code.setPlaceholderText("可选。留空发起六位码配对，已信任设备留空则更新 IP")
        code.setMinimumWidth(500)
        layout.addRow("对方 IP", host)
        layout.addRow("端口", port)
        layout.addRow("配对码", code)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            address = str(ipaddress.IPv4Address(host.text().strip()))
            number = int(port.text())
            if not 1 <= number <= 65535:
                raise ValueError()
        except ValueError:
            self.status.setText("请输入有效的 IPv4 地址和端口（1–65535）。")
            return
        invitation = code.text().strip()
        if not invitation and "token" in current:
            stored = self.state.peers().get(current["id"])
            if not stored:
                return
            stored.update(host=address, port=number)
            self.state.trust(peer_at(stored, address, number))
            self.last_devices = None
            self.refresh()
            self.status.setText("已更新地址；发送时仍会核对已信任证书。")
        elif invitation:
            self.run_job(lambda _: self.service.pair(address, number, invitation), "正在安全配对…")
        else:
            self.run_job(lambda _: self.service.pair_sas(address, number, self.confirm_pairing),
                         "正在发起六位码配对…")

    def forget(self):
        peer = self.trusted()
        if peer:
            self.state.forget(peer["id"])
            self.last_devices = None
            self.refresh()
            self.status.setText("已取消信任，对方不能再发送内容。重新连接需重新配对。")

    def quit(self):
        self.quitting = True
        if self.close():
            QApplication.quit()

    def closeEvent(self, event):
        if self.tray and not self.quitting:
            self.hide()
            event.ignore()
            return
        if self.job or self.clipboard_job:
            self.activate_window()
            self.quitting = False
            self.status.setText("正在传输，请等待完成后关闭窗口。")
            event.ignore()
            return
        self.timer.stop()
        self.clipboard_timer.stop()
        QApplication.clipboard().dataChanged.disconnect(self.poll_clipboard)
        if self.tray:
            self.tray.hide()
        self.discovery.stop()
        self.service.stop()
        event.accept()
