import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from yayashare.gui import Window
from yayashare.storage import State


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
