import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from yayashare.gui import Window
from yayashare.storage import State
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QImage
import uuid


def test_window_history_and_clipboard(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(State(tmp_path), port=0, discover=False)
    try:
        window.show()
        app.processEvents()
        window.state.record("text", "R9000P", "hello <script> & 喵")
        window.refresh()
        assert window.history_list.count() == 1
        assert window.detail.toPlainText() == "hello <script> & 喵"
        app.clipboard().setText("test clipboard")
        window.clip_btn.click()
        assert window.editor.toPlainText() == "test clipboard"
        window.send_btn.click()
        assert "先选择" in window.status.text()
    finally:
        window.close()
        app.processEvents()


def test_automatic_clipboard_ui_gate_mime_and_remote_echo(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = Window(State(tmp_path), port=0, discover=False)
    try:
        assert not window.sync_toggle.isChecked()
        assert not window.windowIcon().isNull()
        app.clipboard().setText("old secret")
        window.sync_toggle.setChecked(True)
        window.poll_clipboard()
        assert window.service.clipboard.last_text == "old secret"
        assert window.clipboard_pending is None
        mime = QMimeData()
        mime.setText("not plain clipboard")
        mime.setUrls([QUrl.fromLocalFile("C:/test.txt")])
        app.clipboard().setMimeData(mime)
        assert window.clipboard_text() is None
        app.clipboard().setImage(QImage(10, 10, QImage.Format.Format_RGB32))
        assert window.clipboard_text() is None
        peer = {"id": uuid.uuid4().hex, "name": "test"}
        window.state.trust(peer)
        window.service.clipboard.receive(peer["id"], uuid.uuid4().hex, "remote\ntext")
        window.poll_clipboard()
        assert app.clipboard().text() == "remote\ntext"
        assert window.clipboard_job is None
        assert window.clipboard_pending is None
        window.sync_toggle.setChecked(False)
        assert not window.service.clipboard.enabled
    finally:
        window.close()
        app.processEvents()


def test_phone_portal_opt_in_share_and_close(tmp_path, monkeypatch):
    from yayashare import web_dialog
    from yayashare.web_portal import WebPortal
    app = QApplication.instance() or QApplication([])
    window = Window(State(tmp_path), port=0, discover=False)
    monkeypatch.setattr(web_dialog, 'WebPortal', lambda state, addresses, **kw:
                        WebPortal(state, addresses, host='127.0.0.1', port=0, **kw))
    dialog = web_dialog.PortalDialog(window)
    try:
        assert dialog.portal is None and not dialog.start.isEnabled()
        assert not dialog.add_text.isEnabled()
        dialog.consent.setChecked(True)
        dialog.start.click()
        portal = dialog.portal
        assert portal.active() and dialog.link.text().startswith('http://')
        assert not dialog.qr.pixmap().isNull()
        dialog.text.setPlainText('电脑发给手机 🐾')
        dialog.add_text.click()
        assert portal.listing()[0]['text'] == '电脑发给手机 🐾'
        assert not window.state.history()
        dialog.reject()  # Escape must revoke as well as the window close button.
        assert not portal.active() and not portal.spool.exists()
        assert dialog.portal is None
    finally:
        dialog.close()
        window.close()
        app.processEvents()
