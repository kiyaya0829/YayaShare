import hashlib
from pathlib import Path
import threading
import time
import uuid

import pytest

from test_protocol import pair
from yayashare.clipboard import ClipboardSync, MAX_TEXT
from yayashare.folders import manifest, portable_path, validate_manifest
from yayashare.protocol import read_json, response, send_json
from yayashare.security import pinned_socket


def enabled():
    sync = ClipboardSync()
    sync.configure(True, "existing secret")
    return sync


def test_clipboard_default_off_and_enable_baseline():
    sync = ClipboardSync()
    assert sync.local_change("private") is None
    sync.configure(True, "private")
    assert sync.local_change("private") is None
    assert sync.local_change("new copy")["text"] == "new copy"
    sync.configure(False)
    assert sync.local_change("secret") is None


def test_clipboard_echo_and_duplicate_ids():
    a, b = enabled(), enabled()
    update = a.local_change("你好\nMac → Windows")
    writes = []
    for _ in range(4):
        b.receive("a", **update)
    b.apply_pending({"a"}, writes.append)
    assert writes == [update["text"]]
    assert b.local_change(writes[-1]) is None
    a.receive("b", **update)
    assert not a.apply_pending({"b"}, writes.append)
    reverse = b.local_change("Windows → Mac")
    a.receive("b", **reverse)
    a.apply_pending({"b"}, writes.append)
    assert a.local_change(writes[-1]) is None


def test_clipboard_simultaneous_updates_do_not_pingpong():
    a, b = enabled(), enabled()
    ua, ub = a.local_change("a copy"), b.local_change("b copy")
    a.receive("b", **ub)
    b.receive("a", **ua)
    for sync, pid in [(a, "b"), (b, "a")]:
        out = []
        sync.apply_pending({pid}, out.append)
        assert sync.local_change(out[-1]) is None


def test_clipboard_revocation_switch_and_queue_bounds():
    sync = enabled()
    sync.receive("revoked", uuid.uuid4().hex, "do not apply")
    assert not sync.apply_pending(set(), lambda _: pytest.fail("revoked"))
    for i in range(600):
        sync.receive("a", uuid.uuid4().hex, str(i))
    assert len(sync.seen) == 512 and len(sync.incoming) == 64
    sync.configure(False)
    assert not sync.incoming
    with pytest.raises(ValueError, match="未开启"):
        sync.receive("a", uuid.uuid4().hex, "off")


@pytest.mark.parametrize("text", [None, 12, "a" * (MAX_TEXT + 1), "bad\x00text"], ids=["nontext", "number", "oversize", "nul"])
def test_clipboard_invalid_text(text):
    sync = enabled()
    assert sync.local_change(text) is None
    with pytest.raises(ValueError):
        sync.receive("a", uuid.uuid4().hex, text)


def test_clipboard_concurrent_duplicate_delivery():
    sync = enabled()
    uid = uuid.uuid4().hex
    threads = [threading.Thread(target=sync.receive, args=("a", uid, "hello")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out = []
    sync.apply_pending({"a"}, out.append)
    assert out == ["hello"]


def test_clipboard_tls_bidirectional_gate_and_no_history(pair):
    a, b, peer = pair
    a.clipboard.configure(True)
    update = a.clipboard.local_change("auto copy")
    with pytest.raises(ValueError, match="未开启"):
        a.send_clipboard(peer, update, a.clipboard.generation)
    b.clipboard.configure(True)
    a.send_clipboard(peer, update, a.clipboard.generation)
    a.send_clipboard(peer, update, a.clipboard.generation)
    output = []
    b.clipboard.apply_pending(b.state.peers(), output.append)
    assert output == ["auto copy"]
    assert b.clipboard.local_change(output[0]) is None
    reverse = b.clipboard.local_change("return copy")
    b.send_clipboard(b.state.peers()[a.info["id"]], reverse, b.clipboard.generation)
    a.clipboard.apply_pending(a.state.peers(), output.append)
    assert output[-1] == "return copy"
    assert not a.state.history() and not b.state.history()
    a.send(peer, text="manual text")
    assert b.state.history()[0]["content"] == "manual text"
    assert not b.clipboard.incoming


def test_clipboard_stale_generation_and_untrusted(pair):
    a, b, peer = pair
    a.clipboard.configure(True)
    b.clipboard.configure(True)
    update = a.clipboard.local_change("pending")
    generation = a.clipboard.generation
    a.clipboard.configure(False)
    a.clipboard.configure(True)
    a.send_clipboard(peer, update, generation)
    assert not b.clipboard.incoming
    b.state.forget(a.info["id"])
    with pytest.raises(ValueError, match="信任"):
        a.send_clipboard(peer, update, a.clipboard.generation)


@pytest.mark.parametrize("extra", [{"mime": "image/png"}, {"update_id": "invalid"}], ids=["nontext-mime", "bad-id"])
def test_clipboard_malformed_wire(pair, extra):
    a, b, peer = pair
    b.clipboard.configure(True)
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, {"version": 1, "op": "text", "purpose": "clipboard", "mime": "text/plain",
                         "update_id": uuid.uuid4().hex, "text": "bad", "id": a.info["id"], "token": peer["token"], **extra})
        assert not read_json(conn)["ok"]
    assert not b.clipboard.incoming


def snapshot(root):
    return {p.relative_to(root).as_posix(): None if p.is_dir() else p.read_bytes() for p in root.rglob("*")}


def test_folder_bidirectional_structure_empty_and_duplicate(pair, tmp_path):
    a, b, peer = pair
    folder = tmp_path / "SC2008 中文"
    (folder / "code" / "empty").mkdir(parents=True)
    (folder / "报告.txt").write_text("你好", encoding="utf-8")
    (folder / "code" / "a.bin").write_bytes(b"abc\x00" * 100000)
    (folder / "code" / "zero").touch()
    progress = []
    a.send(peer, path=folder, progress=progress.append)
    a.send(peer, path=folder)
    targets = [Path(e["content"]) for e in b.state.history()]
    assert targets[0] != targets[1]
    assert all(snapshot(p) == snapshot(folder) for p in targets)
    assert progress[-1] == 100
    b.send(b.state.peers()[a.info["id"]], path=targets[0])
    assert snapshot(Path(a.state.history()[0]["content"])) == snapshot(folder)
    empty = tmp_path / "empty"
    empty.mkdir()
    a.send(peer, path=empty)
    assert list(Path(b.state.history()[0]["content"]).iterdir()) == []


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/windows", "C:\\windows", "a\\b", "a//b", "a/./b", "a/../b", "//server/share"])
def test_folder_rejects_unsafe_portable_paths(path):
    with pytest.raises(ValueError):
        portable_path(path)


@pytest.mark.parametrize("names", [["Foo", "foo"], ["é", "e\u0301"], ["a?", "a*"], ["CON", "_CON"]])
def test_folder_cross_platform_collisions(names):
    with pytest.raises(ValueError, match="重名"):
        validate_manifest([{"path": n, "kind": "directory"} for n in names])


def test_folder_portable_reserved_names_and_parent_validation():
    assert portable_path("code/CON.txt") == "code/_CON.txt"
    with pytest.raises(ValueError, match="父目录"):
        validate_manifest([{"path": "missing/child", "kind": "directory"}])


def folder_request(a, peer, entries):
    return {"version": 1, "op": "folder", "id": a.info["id"], "token": peer["token"], "name": "test", "entries": entries}


def test_folder_limits_and_parent_case():
    from yayashare.folders import MAX_ENTRIES, MAX_TOTAL
    with pytest.raises(ValueError):
        validate_manifest([{}] * (MAX_ENTRIES + 1))
    with pytest.raises(ValueError):
        portable_path("a/" * 33 + "end")
    with pytest.raises(ValueError):
        validate_manifest([{"path": "large", "kind": "file", "size": MAX_TOTAL + 1, "sha256": "0" * 64}])
    with pytest.raises(ValueError):
        validate_manifest([{"path": str(n), "kind": "file", "size": MAX_TOTAL, "sha256": "0" * 64} for n in range(2)])
    with pytest.raises(ValueError, match="父目录"):
        validate_manifest([{"path": "Foo", "kind": "directory"}, {"path": "foo/child", "kind": "directory"}])


def test_folder_walk_error_is_not_silent(tmp_path, monkeypatch):
    import os
    def denied(*args, **kwargs):
        kwargs["onerror"](PermissionError("denied"))
    monkeypatch.setattr(os, "walk", denied)
    with pytest.raises(PermissionError):
        manifest(tmp_path)


def test_folder_hash_disconnect_and_traversal_cleanup(pair):
    a, b, peer = pair
    entry = {"path": "a.bin", "kind": "file", "size": 4, "sha256": "0" * 64}
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, folder_request(a, peer, [entry]))
        response(conn)
        conn.sendall(b"test")
        assert not read_json(conn)["ok"]
    assert not list(b.state.inbox.iterdir())
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, folder_request(a, peer, [{**entry, "path": "../escape"}]))
        assert not read_json(conn)["ok"]
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, folder_request(a, peer, [entry]))
        response(conn)
        conn.sendall(b"t")
    deadline = time.monotonic() + 3
    while list(b.state.inbox.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not list(b.state.inbox.iterdir())
    assert not b.state.history()


def test_folder_symlink_rejected(tmp_path):
    root = tmp_path / "folder"
    root.mkdir()
    try:
        (root / "link").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable; macOS exercises this test")
    with pytest.raises(ValueError, match="链接"):
        manifest(root)
