"""Desktop controls for the optional phone portal."""
import time

import qrcode
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QVBoxLayout)

from .web_portal import WebPortal


def qr_image(url):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = 5
    image = QImage(len(matrix) * scale, len(matrix) * scale, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(x * scale, y * scale, scale, scale, QColor("black"))
    painter.end()
    return image


class PortalDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.portal = None
        self.job = None
        self.setWindowTitle("YayaShare · 手机网页入口")
        self.resize(590, 740)
        layout = QVBoxLayout(self)
        info = QLabel("手机与电脑连接同一局域网，扫码后在 Safari 或其他浏览器打开。\n关闭此窗口会停止入口；最长开放 30 分钟，仅供一台手机授权。")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.consent = QCheckBox("我了解：网页 HTTP 未加密，只在可信网络传普通内容")
        layout.addWidget(self.consent)
        self.address = QComboBox()
        addresses = sorted({a.toString() for a in QNetworkInterface.allAddresses()
                            if a.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not a.isLoopback()})
        for value in addresses:
            self.address.addItem(value)
        if not addresses:
            self.address.addItem("127.0.0.1（仅本机测试，无局域网地址）", "127.0.0.1")
        row = QHBoxLayout()
        row.addWidget(QLabel("本机地址"))
        row.addWidget(self.address, 1)
        self.start = QPushButton("开启手机入口")
        self.start.setEnabled(False)
        self.start.clicked.connect(self.toggle)
        self.consent.toggled.connect(lambda checked: self.start.setEnabled(not self.job and (checked or self.portal is not None)))
        row.addWidget(self.start)
        layout.addLayout(row)
        self.qr = QLabel("开启后显示二维码")
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.qr)
        self.link = QLineEdit()
        self.link.setReadOnly(True)
        self.link.setPlaceholderText("临时连接地址")
        layout.addWidget(self.link)
        copy = QPushButton("复制临时连接地址")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.link.text()) if self.link.text() else None)
        layout.addWidget(copy)
        self.notice = QLabel("默认关闭。校园网设备隔离会阻止连接；可以换个人热点。")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        layout.addWidget(QLabel("主动分享给手机（不会开放原有接收历史）"))
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("粘贴需要发给手机的文字…")
        self.text.setMaximumHeight(85)
        layout.addWidget(self.text)
        actions = QHBoxLayout()
        self.add_text = QPushButton("分享这段文字")
        self.add_text.clicked.connect(self.share_text)
        actions.addWidget(self.add_text)
        self.add_file = QPushButton("分享文件 · 最大 256 MiB")
        self.add_file.clicked.connect(self.share_file)
        actions.addWidget(self.add_file)
        layout.addLayout(actions)
        self.items = QListWidget()
        self.items.setMaximumHeight(90)
        layout.addWidget(self.items)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.address.currentIndexChanged.connect(self.update_link)
        self.refresh()

    def toggle(self):
        if self.portal:
            self.stop()
            return
        try:
            addresses = [self.address.itemData(i) or self.address.itemText(i) for i in range(self.address.count())]
            self.portal = WebPortal(self.owner.state, addresses, event=self.owner.bridge.event.emit)
            self.update_link()
            self.start.setText("关闭入口 / 撤销连接")
            self.notice.setText("请用手机相机扫码；浏览器若提示连接未加密，这是本次局域网 HTTP 入口。")
        except OSError:
            self.notice.setText("无法开启端口 45875，可能已有入口在运行。请关闭另一个实例后重试。")
        self.refresh()

    def update_link(self):
        if self.portal and self.portal.invite:
            address = self.address.currentData() or self.address.currentText()
            url = self.portal.url(address)
            self.link.setText(url)
            self.qr.setPixmap(QPixmap.fromImage(qr_image(url)))

    def stop(self):
        if self.portal:
            self.portal.stop()
            self.portal = None
        self.link.clear()
        self.qr.clear()
        self.qr.setText("入口已关闭，旧二维码和手机连接已失效")
        self.start.setText("开启手机入口")
        self.start.setEnabled(self.consent.isChecked())
        self.notice.setText("已关闭。已接收内容留在电脑，临时分享副本已清理。")
        self.refresh()

    def refresh(self):
        if self.portal and not self.portal.active() and not self.job:
            self.stop()
            self.notice.setText("30 分钟已到，入口已自动关闭。需要继续时重新开启并扫码。")
            return
        self.add_text.setEnabled(bool(self.portal) and not self.job)
        self.add_file.setEnabled(bool(self.portal) and not self.job)
        self.address.setEnabled(not self.portal or bool(self.portal.invite))
        self.items.clear()
        if self.portal:
            left = max(0, int(self.portal.expires - time.monotonic()))
            self.setWindowTitle(f"手机网页入口 · 剩余 {left // 60:02d}:{left % 60:02d}")
            if self.portal.session:
                self.link.clear()
                self.qr.clear()
                self.qr.setText("手机已授权连接\n请保留手机页面；重新连接需要关闭后再开启入口")
            for item in self.portal.listing():
                self.items.addItem(item["name"] if item["kind"] == "file" else item["text"][:50].replace("\n", " "))

    def share_text(self):
        try:
            self.portal.share_text(self.text.toPlainText())
            self.text.clear()
            self.notice.setText("已放入分享列表，手机刷新后可见。")
        except ValueError as exc:
            self.notice.setText(str(exc))
        self.refresh()

    def share_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要分享给手机的文件")
        if not path:
            return
        from .gui import Job
        portal = self.portal
        self.job = Job(lambda _: portal.share_file(path), self)
        self.job.result.connect(lambda error: self.notice.setText("未完成：" + error if error else "文件副本已准备好，手机刷新后可下载。"))
        self.job.finished.connect(self.finished_file)
        self.start.setEnabled(False)
        self.notice.setText("正在准备文件副本…")
        self.job.start()
        self.refresh()

    def finished_file(self):
        self.job.deleteLater()
        self.job = None
        self.start.setEnabled(True)
        self.refresh()

    def done(self, result):
        if self.job:
            self.notice.setText("正在准备文件，请稍候再关闭。")
            return
        self.stop()
        self.timer.stop()
        super().done(result)

    def closeEvent(self, event):
        if self.job:
            event.ignore()
            return
        self.stop()
        self.timer.stop()
        event.accept()
