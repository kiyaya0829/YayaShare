import argparse
import hashlib
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="YayaShare LAN desktop sharing")
    parser.add_argument("--data-dir", type=Path, help="Use an isolated local state directory")
    parser.add_argument("--port", type=int, default=45873)
    parser.add_argument("--no-discovery", action="store_true")
    parser.add_argument("--minimized", action="store_true", help="Start in the system tray")
    parser.add_argument("--smoke-test", action="store_true", help="Launch UI briefly and exit")
    args = parser.parse_args()
    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .gui import Window
    from .storage import State, data_dir
    app = QApplication(sys.argv[:1])
    app.setApplicationName("YayaShare")
    app.setOrganizationName("YayaShare")
    from . import __version__
    app.setApplicationVersion(__version__)
    from PySide6.QtGui import QIcon
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets/icon.png")))
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YayaShare.Desktop.1")
    app.setStyle("Fusion")
    try:
        root = (args.data_dir or data_dir()).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = QLockFile(str(root / "application.lock"))
        lock.setStaleLockTime(0)
        server_name = "YayaShare-" + hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:32]
        if not lock.tryLock(0):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                client = QLocalSocket()
                client.connectToServer(server_name)
                if client.waitForConnected(200):
                    return 0
                time.sleep(0.05)
            return 1
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(server_name)
        if not server.listen(server_name):
            raise RuntimeError(server.errorString())
        state = State(root)
        window = Window(state, port=args.port, discover=not args.no_discovery,
                        tray_enabled=not args.smoke_test)
        def wake():
            while server.hasPendingConnections():
                client = server.nextPendingConnection()
                client.abort()
                client.deleteLater()
                window.activate_window()
        server.newConnection.connect(wake)
    except Exception as exc:
        if args.smoke_test:
            print(str(exc), file=sys.stderr)
        else:
            QMessageBox.critical(None, "YayaShare 无法启动", str(exc))
        return 1
    if window.tray:
        app.setQuitOnLastWindowClosed(False)
    if not (args.minimized and window.tray):
        window.show()
    if args.smoke_test:
        # Verify that a packaged app includes the phone page and QR dependency.
        from .web_portal import WebPortal
        from .web_dialog import qr_image
        import http.client
        portal = WebPortal(state, ["127.0.0.1"], host="127.0.0.1", port=0)
        try:
            assert not qr_image(portal.url("127.0.0.1")).isNull()
            for path in ("/", "/app.js", "/style.css"):
                conn = http.client.HTTPConnection("127.0.0.1", portal.port, timeout=5)
                conn.request("GET", path)
                response = conn.getresponse()
                assert response.status == 200 and response.read()
                conn.close()
        finally:
            portal.stop()
        if app.windowIcon().isNull() or window.windowIcon().isNull():
            window.close()
            return 1
        QTimer.singleShot(1200, window.close)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
