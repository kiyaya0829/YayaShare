from pathlib import Path
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import uuid

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDropEvent, QImage
from PySide6.QtWidgets import QApplication

from test_protocol import pair
from yayashare import protocol
from yayashare.clipboard import MAX_TEXT
from yayashare.discovery import peer_at
from yayashare.gui import Window
from yayashare.protocol import Service, read_json, response, send_json
from yayashare.security import pinned_socket
from yayashare.storage import State
from yayashare.web_portal import WebPortal


def screenshot():
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor("purple"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return image, bytes(buffer.data())


def test_image_and_large_text_round_trip(pair):
    a, b, peer = pair
    _, png = screenshot()
    a.clipboard.configure(True, images=True)
    b.clipboard.configure(True)
    image_update = a.clipboard.local_change(None, png)
    with pytest.raises(ValueError, match="未开启"):
        a.send_clipboard(peer, image_update, a.clipboard.generation)
    b.clipboard.configure(True, images=True)
    for _ in range(2):
        a.send_clipboard(peer, image_update, a.clipboard.generation)
    received = []
    assert b.clipboard.apply_pending(b.state.peers(), received.append)
    assert received == [png]
    assert b.clipboard.local_change(None, png) is None
    text = "\r\n\x01" * (MAX_TEXT // 3) + "xx"
    assert len(text.encode()) == MAX_TEXT
    a.send_clipboard(peer, a.clipboard.local_change(text), a.clipboard.generation)
    assert b.clipboard.apply_pending(b.state.peers(), received.append)
    assert received[-1] == text
    assert not a.state.history() and not b.state.history()


def test_image_gui_readback_and_preferences(tmp_path):
    app = QApplication.instance() or QApplication([])
    state = State(tmp_path)
    state.configure(clipboard_sync=True, clipboard_images=True)
    window = Window(State(tmp_path), port=0, discover=False, tray_enabled=False)
    try:
        assert window.sync_toggle.isChecked() and window.image_toggle.isChecked()
        source_id = uuid.uuid4().hex
        window.state.trust({"id": source_id, "name": "peer", "host": "127.0.0.1",
                            "port": 1, "fingerprint": "0" * 64, "token": "0" * 64})
        image, png = screenshot()
        window.service.clipboard.receive_image(source_id, uuid.uuid4().hex, png)
        window.poll_clipboard()
        window.poll_clipboard()
        assert app.clipboard().image().pixelColor(0, 0) == image.pixelColor(0, 0)
        assert window.clipboard_pending is None and window.clipboard_job is None
        window.copy_private_text("private invitation")
        window.poll_clipboard()
        assert window.clipboard_job is None
        window.image_toggle.setChecked(False)
        assert State(tmp_path).settings == {"clipboard_sync": True, "clipboard_images": False}
    finally:
        window.close()


def test_drop_sends_all_items_to_selected_trusted_peer(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = Window(State(tmp_path), port=0, discover=False, tray_enabled=False)
    try:
        peer = {"id": uuid.uuid4().hex, "name": "trusted", "host": "127.0.0.1", "port": 1,
                "fingerprint": "0" * 64, "token": "0" * 64}
        window.state.trust(peer)
        window.refresh()
        calls = []
        monkeypatch.setattr(window, "send_paths", lambda selected, paths: calls.append((selected, paths)))
        paths = [(tmp_path / name).as_posix() for name in ("file.txt", "a folder")]
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        drop = QDropEvent(QPointF(1, 1), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        assert window.eventFilter(window.editor.viewport(), drop)
        assert calls == [(peer, paths)] and drop.isAccepted()
    finally:
        window.close()
        app.processEvents()


def test_second_instance_notifies_the_first(tmp_path):
    ready = tmp_path / "ready"
    woke = tmp_path / "woke"
    state = tmp_path / "profile"
    code = """
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from yayashare.__main__ import main
from yayashare.gui import Window

ready, woke = map(Path, sys.argv[1:3])
del sys.argv[1:3]
old_init = Window.__init__
old_activate = Window.activate_window

def start(self, *args, **kwargs):
    old_init(self, *args, **kwargs)
    QTimer.singleShot(0, ready.touch)

def activate(self):
    old_activate(self)
    woke.touch()
    self.quit()

Window.__init__ = start
Window.activate_window = activate
sys.exit(main())
"""
    args = ["--no-discovery", "--port", "0", "--data-dir", str(state), "--minimized"]
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    process = subprocess.Popen([sys.executable, "-c", code, str(ready), str(woke), *args], env=env)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "first instance failed to start"
        subprocess.run([sys.executable, "-m", "yayashare", *args], env=env, check=True, timeout=10)
        assert process.wait(timeout=10) == 0
        assert woke.exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_tailnet_route_order_and_legacy_address(monkeypatch, tmp_path):
    service = Service(State(tmp_path))
    fingerprint = "f" * 64
    old = {"host": "192.168.1.2", "port": 1234, "fingerprint": fingerprint}
    peer = peer_at(old, "100.80.1.2", 2345)
    peer = peer_at(peer, "192.168.1.3", 3456)
    calls = []
    result = object()

    def connect(host, port, pin, timeout):
        calls.append((host, port, pin, timeout))
        if host == "100.80.1.2":
            return result
        raise TimeoutError()

    monkeypatch.setattr(protocol, "pinned_socket", connect)
    assert service.connect(peer) is result
    assert calls == [("192.168.1.3", 3456, fingerprint, 2), ("100.80.1.2", 2345, fingerprint, 4)]
    calls.clear()
    assert service.connect({**old, "host": "100.80.1.2"}) is result
    assert calls == [("100.80.1.2", 1234, fingerprint, 4)]
    calls.clear()
    assert service.connect({**peer, "lan_seen": time.monotonic() - 13}) is result
    assert calls == [("100.80.1.2", 2345, fingerprint, 4)]


def test_capability_negotiation_learns_tailnet_and_reuses_one_connection(pair, monkeypatch):
    a, b, peer = pair
    monkeypatch.setattr(protocol, "tailscale_address", lambda: "100.80.1.2")
    a.capabilities.clear()
    a.clipboard.configure(True)
    b.clipboard.configure(True)
    calls = []

    def connect(*args, **kwargs):
        calls.append(args)
        return pinned_socket(*args, **kwargs)

    monkeypatch.setattr(protocol, "pinned_socket", connect)
    for text in ("first", "second"):
        a.send_clipboard(peer, a.clipboard.local_change(text), a.clipboard.generation)
    assert len(calls) == 2
    assert a.state.peers()[peer["id"]]["tailscale_host"] == "100.80.1.2"
    assert b.state.peers()[a.info["id"]]["tailscale_host"] == "100.80.1.2"
    assert a.state.peers()[peer["id"]]["lan_host"] == "127.0.0.1"


def test_legacy_clipboard_file_and_folder_fallback(pair, monkeypatch, tmp_path):
    a, b, peer = pair
    monkeypatch.setattr(protocol, "CAPABILITIES", ("folder", "clipboard-text-v1"))
    a.capabilities.clear()
    a.clipboard.configure(True)
    b.clipboard.configure(True)
    a.send_clipboard(peer, a.clipboard.local_change("legacy"), a.clipboard.generation)
    assert list(b.clipboard.incoming)[0][1] == "legacy"
    with pytest.raises(ValueError, match="版本"):
        update = a.clipboard.local_change("a" * MAX_TEXT)
        a.send_clipboard(peer, update, a.clipboard.generation)
    folder = tmp_path / "folder"
    folder.mkdir()
    source = folder / "doc.txt"
    source.write_text("legacy data", encoding="utf-8")
    a.send(peer, path=source)
    a.send(peer, path=folder)
    assert (b.state.inbox / "doc.txt").read_text() == "legacy data"
    assert (b.state.inbox / "folder/doc.txt").read_text() == "legacy data"


def test_flat_receive_numbering_and_cancellation(pair, tmp_path):
    a, b, peer = pair
    inbox = tmp_path / "Custom Inbox"
    b.state.configure(receive_directory=str(inbox))
    assert State(b.state.root).inbox == inbox
    (inbox / "REPORT.txt").write_bytes(b"existing")
    source = tmp_path / "report.txt"
    source.write_bytes(b"new" * protocol.CHUNK)
    cancel = threading.Event()
    with pytest.raises(InterruptedError, match="已取消"):
        a.send(peer, path=source, cancel=cancel, progress=lambda _: cancel.set())
    deadline = time.monotonic() + 3
    while (inbox / ".staging").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sorted(p.name for p in inbox.iterdir()) == ["REPORT.txt"]
    assert not b.state.history()
    a.send(peer, path=source)
    a.send(peer, path=source)
    assert (inbox / "REPORT.txt").read_bytes() == b"existing"
    assert (inbox / "report (2).txt").read_bytes() == source.read_bytes()
    assert (inbox / "report (3).txt").read_bytes() == source.read_bytes()
    assert not (inbox / ".staging").exists()


def test_stream_trailer_mismatch_leaves_inbox_empty(pair):
    a, b, peer = pair
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, {"version": 1, "op": "file", "name": "file", "size": 3,
                         "stream": 2, "id": a.info["id"], "token": peer["token"]})
        response(conn)
        conn.sendall(b"abc")
        send_json(conn, {"sha256": "0" * 64})
        assert read_json(conn)["ok"] is False
    assert not list(b.state.inbox.iterdir())


@pytest.mark.parametrize("confirmed", [True, False])
def test_sas_pairs_only_when_both_confirm(tmp_path, confirmed):
    codes = []

    def server_confirm(name, code):
        codes.append(code)
        return confirmed

    a = Service(State(tmp_path / "a"), host="127.0.0.1", port=0)
    b = Service(State(tmp_path / "b"), host="127.0.0.1", port=0, confirm_pairing=server_confirm)
    a.start()
    b.start()
    try:
        with pytest.raises(ValueError, match="先在接收设备"):
            a.pair_sas("127.0.0.1", b.port, lambda *_: True)
        b.allow_pairing()
        def client_confirm(name, code):
            codes.append(code)
            return True

        if confirmed:
            peer = a.pair_sas("127.0.0.1", b.port, client_confirm)
            a.send(peer, text="paired using SAS")
            assert b.state.history()[0]["content"] == "paired using SAS"
            assert Path(b.state.path).exists()
        else:
            with pytest.raises(ValueError, match="取消了配对"):
                a.pair_sas("127.0.0.1", b.port, client_confirm)
            assert not a.state.peers() and not b.state.peers()
        assert len(codes) == 2 and codes[0] == codes[1]
        assert len(codes[0]) == 6 and codes[0].isdigit()
    finally:
        a.stop()
        b.stop()


def test_https_portal_checks_origin_and_uses_secure_cookie(tmp_path):
    portal = WebPortal(State(tmp_path), ["127.0.0.1"], host="127.0.0.1", port=0, https=True)
    context = ssl.create_default_context(cafile=str(portal.spool / "certificate.pem"))
    conn = http.client.HTTPSConnection("127.0.0.1", portal.port, context=context)
    try:
        origin = f"https://127.0.0.1:{portal.port}"
        assert portal.url("127.0.0.1").startswith(origin)
        body = json.dumps({"token": portal.invite})
        headers = {"Content-Type": "application/json", "X-YayaShare": "1"}
        conn.request("POST", "/api/connect", body, {**headers, "Origin": origin.replace("https:", "http:")})
        result = conn.getresponse()
        assert result.status == 403
        result.read()
        conn.request("POST", "/api/connect", body, {**headers, "Origin": origin})
        result = conn.getresponse()
        assert result.status == 200 and "; Secure" in result.getheader("Set-Cookie")
        result.read()
    finally:
        conn.close()
        portal.stop()
    assert not portal.spool.exists()
