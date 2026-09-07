import hashlib
import json
from pathlib import Path
import socket
import struct
import time

import pytest

from yayashare.discovery import parse_advertisement
from yayashare.protocol import MAX_FILE, MAX_FRAME, MAX_TEXT, Service, read_json, response, send_json
from yayashare.security import Invitation, pinned_socket
from yayashare.storage import State, safe_name


@pytest.fixture
def pair(tmp_path):
    a = Service(State(tmp_path / "a"), host="127.0.0.1", port=0)
    b = Service(State(tmp_path / "b"), host="127.0.0.1", port=0)
    a.start()
    b.start()
    try:
        peer = a.pair("127.0.0.1", b.port, b.invitation.create())
        yield a, b, peer
    finally:
        a.stop()
        b.stop()


@pytest.mark.parametrize("value,expected", [
    ("../../secret.txt", "secret.txt"), ("C:\\Users\\someone\\a.txt", "a.txt"),
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("LPT9.pdf", "_LPT9.pdf"),
    ("a:b?.txt", "a_b_.txt"), ("..", "received"), ("bad\x00name", "bad_name"),
    (" 你好.txt. ", "你好.txt"), ("/etc/passwd", "passwd"),
])
def test_safe_names(value, expected):
    assert safe_name(value) == expected


def test_unicode_length():
    assert len(safe_name("喵" * 200).encode()) <= 160


def test_bidirectional_text_and_persistence(pair):
    a, b, peer = pair
    a.send(peer, text="你好，R9000P！🐾")
    assert b.state.history()[0]["content"] == "你好，R9000P！🐾"
    b.send(b.state.peers()[a.state.identity["id"]], text="Mac 已收到")
    assert a.state.history()[0]["content"] == "Mac 已收到"
    restored = State(a.state.root)
    assert restored.peers() == a.state.peers()
    assert restored.history() == a.state.history()


@pytest.mark.parametrize("data", [b"", b"abc\x00" * 200000], ids=["empty", "binary-800KB"])
def test_file_stream_and_duplicate_names(pair, tmp_path, data):
    a, b, peer = pair
    source = tmp_path / "中文 example.bin"
    source.write_bytes(data)
    a.send(peer, path=source)
    a.send(peer, path=source)
    paths = [Path(x["content"]) for x in b.state.history()]
    assert len(set(paths)) == 2
    assert all(p.read_bytes() == data for p in paths)
    assert all(p.is_relative_to(b.state.inbox) for p in paths)


def test_pairing_code_single_use_expiration_and_pin(pair):
    a, b, _ = pair
    code = b.invitation.create()
    fp, secret = Invitation.parse(code)
    assert fp == b.fingerprint
    b.invitation.consume(secret)
    with pytest.raises(ValueError):
        b.invitation.consume(secret)
    code = b.invitation.create()
    b.invitation.expires = time.monotonic() - 1
    with pytest.raises(ValueError, match="过期"):
        a.pair("127.0.0.1", b.port, code)
    with pytest.raises(ValueError, match="证书"):
        pinned_socket("127.0.0.1", b.port, "0" * 64)
    with pytest.raises(ValueError):
        Invitation.parse("123456")


def test_wrong_secret(pair):
    a, b, _ = pair
    b.invitation.create()
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, {"version": 1, "op": "pair", **a.info, "secret": "0" * 32})
        with pytest.raises(ValueError, match="配对码"):
            response(conn)


def test_untrusted_and_revocation(pair):
    a, b, peer = pair
    with pytest.raises(ValueError):
        a.send({**peer, "token": "0" * 64}, text="forbidden")
    b.state.forget(a.state.identity["id"])
    with pytest.raises(ValueError, match="信任"):
        a.send(peer, text="forbidden")
    assert not b.state.history()


def test_oversized_text(pair):
    a, b, peer = pair
    with pytest.raises(ValueError):
        a.send(peer, text="a" * (MAX_TEXT + 1))
    assert not b.state.history()


def test_bad_frames(pair):
    _, b, _ = pair
    for data in [struct.pack("!I", MAX_FRAME + 1), struct.pack("!I", 2) + b"[]"]:
        with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
            conn.sendall(data)
            assert read_json(conn)["ok"] is False


def file_request(a, peer, **extra):
    return {"version": 1, "op": "file", "id": a.state.identity["id"], "token": peer["token"],
            "name": "../../escape.txt", "size": 4, "sha256": hashlib.sha256(b"test").hexdigest(), **extra}


def test_traversal_stays_inside_inbox(pair):
    a, b, peer = pair
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, file_request(a, peer))
        response(conn)
        conn.sendall(b"test")
        response(conn)
    target = Path(b.state.history()[0]["content"])
    assert target.is_relative_to(b.state.inbox)
    assert target.name == "escape.txt"
    assert target.read_bytes() == b"test"


def test_bad_hash_and_interruption_cleanup(pair):
    a, b, peer = pair
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, file_request(a, peer, sha256="0" * 64))
        response(conn)
        conn.sendall(b"test")
        with pytest.raises(ValueError, match="校验"):
            response(conn)
    assert list(b.state.inbox.iterdir()) == []
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, file_request(a, peer))
        response(conn)
        conn.sendall(b"t")
    deadline = time.monotonic() + 3
    while list(b.state.inbox.iterdir()) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert list(b.state.inbox.iterdir()) == []
    assert not b.state.history()


@pytest.mark.parametrize("size", [-1, True, MAX_FILE + 1, "4"])
def test_invalid_file_sizes(pair, size):
    a, b, peer = pair
    with pinned_socket("127.0.0.1", b.port, b.fingerprint) as conn:
        send_json(conn, file_request(a, peer, size=size))
        assert not read_json(conn)["ok"]
    assert list(b.state.inbox.iterdir()) == []


def test_discovery_validation(pair):
    a, _, _ = pair
    packet = json.dumps({"app": "YayaShare", "version": 1, **a.info}).encode()
    assert parse_advertisement(packet, "192.168.1.4")["host"] == "192.168.1.4"
    with pytest.raises(ValueError):
        parse_advertisement(b'{"app":"other"}', "192.168.1.4")


def test_history_bounded(tmp_path):
    state = State(tmp_path)
    for i in range(105):
        state.record("text", "test", str(i))
    assert len(state.history()) == 100
    assert state.history()[0]["content"] == "104"
