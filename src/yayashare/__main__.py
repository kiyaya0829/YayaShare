import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="YayaShare LAN desktop sharing")
    parser.add_argument("--data-dir", type=Path, help="Use an isolated local state directory")
    parser.add_argument("--port", type=int, default=45873)
    parser.add_argument("--no-discovery", action="store_true")
    parser.add_argument("--smoke-test", action="store_true", help="Launch UI briefly and exit")
    args = parser.parse_args()
    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .gui import Window
    from .storage import State
    app = QApplication(sys.argv[:1])
    app.setApplicationName("YayaShare")
    app.setOrganizationName("YayaShare")
    app.setStyle("Fusion")
    try:
        state = State(args.data_dir)
        lock = QLockFile(str(state.root / "application.lock"))
        if not lock.tryLock(0):
            raise RuntimeError("YayaShare 已在运行，请使用已打开的窗口。")
        window = Window(state, port=args.port, discover=not args.no_discovery)
    except Exception as exc:
        if args.smoke_test:
            print(str(exc), file=sys.stderr)
        else:
            QMessageBox.critical(None, "YayaShare 无法启动", str(exc))
        return 1
    window.show()
    if args.smoke_test:
        QTimer.singleShot(1200, window.close)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
