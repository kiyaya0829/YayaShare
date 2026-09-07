"""Small length-prefixed JSON protocol over pinned TLS; streamed file bodies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import threading
import time
import uuid

from .security import Invitation, certificate, pinned_socket
from .storage import safe_name

PORT = 45873
MAX_FRAME = 512 * 1024
MAX_TEXT = 64 * 1024
MAX_FILE = 2 * 1024**3
CHUNK = 256 * 1024


def read_exact(conn, size):
    chunks = bytearray()
    while len(chunks) < size:
        block = conn.recv(min(size - len(chunks), CHUNK))
        if not block:
            raise ConnectionError("连接中断，内容未完整接收")
        chunks.extend(block)
    return bytes(chunks)


def send_json(conn, value):
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("消息过大")
    conn.sendall(struct.pack("!I", len(data)) + data)


def read_json(conn):
    size = struct.unpack("!I", read_exact(conn, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("消息长度超限")
    value = json.loads(read_exact(conn, size))
    if not isinstance(value, dict):
        raise ValueError("无效消息")
    return value


def response(conn):
    result = read_json(conn)
    if not result.get("ok"):
        raise ValueError(result.get("error", "请求失败"))
    return result


def identity(value):
    if not isinstance(value.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", value["id"]):
        raise ValueError("无效设备身份")
    name = value.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
        raise ValueError("无效设备名")
    fp = value.get("fingerprint")
    if not isinstance(fp, str) or not re.fullmatch(r"[a-f0-9]{64}", fp):
        raise ValueError("无效设备证书")
    port = value.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("无效端口")


class Service:
    def __init__(self, state, host="0.0.0.0", port=PORT, event=None):
        self.state = state
        self.host, self.port = host, port
        self.event = event or (lambda *_: None)
        self.context, self.fingerprint = certificate(state.root)
        self.invitation = Invitation(self.fingerprint)
        self.stop_event = threading.Event()
        self.slots = threading.BoundedSemaphore(8)
        self.threads = []
        self.connections = set()
        self.lock = threading.Lock()
        self.listener = None

    @property
    def info(self):
        return {**self.state.identity, "fingerprint": self.fingerprint, "port": self.port}

    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind((self.host, self.port))
            self.listener.listen(8)
        except Exception:
            self.listener.close()
            raise
        self.listener.settimeout(0.5)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop_event.is_set():
            try:
                conn, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                conn.close()
                continue
            with self.lock:
                self.connections.add(conn)
                self.threads = [t for t in self.threads if t.is_alive()]
                thread = threading.Thread(target=self._handle, args=(conn, addr[0]), daemon=True)
                self.threads.append(thread)
                thread.start()

    def _handle(self, raw, host):
        conn = raw
        try:
            raw.settimeout(10)
            conn = self.context.wrap_socket(raw, server_side=True)
            with self.lock:
                self.connections.discard(raw)
                self.connections.add(conn)
            conn.settimeout(30)
            request = read_json(conn)
            if request.get("version") != 1:
                raise ValueError("版本不兼容，请更新两台设备")
            op = request.get("op")
            if op == "pair":
                identity(request)
                if request["id"] == self.state.identity["id"]:
                    raise ValueError("不能与自己配对")
                secret = request.get("secret", "")
                if not isinstance(secret, str) or len(secret) != 32:
                    raise ValueError("无效配对码")
                self.invitation.consume(secret)
                token = secrets.token_hex(32)
                peer = {key: request[key] for key in ("id", "name", "fingerprint", "port")}
                peer.update(host=host, token=token)
                self.state.trust(peer)
                send_json(conn, {"ok": True, **self.info, "token": token})
                self.event("paired", peer["name"])
                return
            peer_id = request.get("id")
            token = request.get("token")
            if not isinstance(peer_id, str) or not isinstance(token, str) or len(token) != 64:
                raise ValueError("请先配对")
            peer = self.state.peers().get(peer_id)
            if not peer or not secrets.compare_digest(peer["token"], token):
                raise ValueError("设备未受信任，请重新配对")
            if op == "text":
                text = request.get("text")
                if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TEXT:
                    raise ValueError("文本为空或超过 64 KiB")
                self.state.record("text", peer["name"], text)
            elif op == "file":
                self._receive_file(conn, request, peer)
            else:
                raise ValueError("未知请求")
            send_json(conn, {"ok": True})
            self.event("received", peer["name"])
        except (OSError, ValueError, KeyError, TypeError, ConnectionError) as exc:
            try:
                send_json(conn, {"ok": False, "error": str(exc)[:200]})
            except (OSError, ValueError):
                pass
        finally:
            conn.close()
            with self.lock:
                self.connections.discard(raw)
                self.connections.discard(conn)
            self.slots.release()

    def _receive_file(self, conn, request, peer):
        name = safe_name(request.get("name"))
        size = request.get("size")
        digest = request.get("sha256")
        if type(size) is not int or not 0 <= size <= MAX_FILE:
            raise ValueError("文件大小必须在 0–2 GiB 之间")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("无效文件校验值")
        if shutil.disk_usage(self.state.inbox).free < size + 16 * 1024**2:
            raise ValueError("接收磁盘空间不足")
        # A private random subdirectory prevents collisions, symlink following,
        # overwrites and Windows long absolute paths from sender-controlled input.
        folder = self.state.inbox / uuid.uuid4().hex[:12]
        folder.mkdir(mode=0o700)
        partial = folder / ".partial"
        target = folder / name
        complete = False
        try:
            with open(partial, "xb") as out:
                send_json(conn, {"ok": True, "ready": True})
                remaining = size
                sha = hashlib.sha256()
                deadline = time.monotonic() + 3600
                while remaining:
                    if time.monotonic() > deadline or self.stop_event.is_set():
                        raise TimeoutError("传输已超时或停止")
                    block = read_exact(conn, min(CHUNK, remaining))
                    out.write(block)
                    sha.update(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
            if sha.hexdigest() != digest:
                raise ValueError("文件校验失败，请重试")
            os.replace(partial, target)
            self.state.record("file", peer["name"], str(target))
            complete = True
        finally:
            if not complete:
                partial.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                folder.rmdir()

    def pair(self, host, port, code):
        fingerprint, secret = Invitation.parse(code)
        with pinned_socket(host, port, fingerprint) as conn:
            send_json(conn, {"version": 1, "op": "pair", **self.info, "secret": secret})
            peer = response(conn)
            identity(peer)
            if peer["fingerprint"] != fingerprint or not re.fullmatch(r"[a-f0-9]{64}", peer.get("token", "")):
                raise ValueError("配对响应无效")
            peer = {key: peer[key] for key in ("id", "name", "fingerprint", "port", "token")}
            peer["host"] = host
            self.state.trust(peer)
            self.event("paired", peer["name"])
            return peer

    def send(self, peer, *, text=None, path=None, progress=None):
        request = {"version": 1, "id": self.state.identity["id"], "token": peer["token"]}
        if path is None:
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TEXT:
                raise ValueError("文本为空或超过 64 KiB")
            with pinned_socket(peer["host"], peer["port"], peer["fingerprint"]) as conn:
                send_json(conn, {**request, "op": "text", "text": text})
                response(conn)
            return
        path = Path(path)
        with open(path, "rb") as source:
            import stat
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_FILE:
                raise ValueError("请选择不超过 2 GiB 的普通文件")
            sha = hashlib.sha256()
            while block := source.read(CHUNK):
                sha.update(block)
            source.seek(0)
            with pinned_socket(peer["host"], peer["port"], peer["fingerprint"]) as conn:
                send_json(conn, {**request, "op": "file", "name": path.name,
                                 "size": meta.st_size, "sha256": sha.hexdigest()})
                response(conn)
                sent = 0
                while sent < meta.st_size:
                    block = source.read(min(CHUNK, meta.st_size - sent))
                    if not block:
                        raise ValueError("文件在传输时被修改，请重试")
                    conn.sendall(block)
                    sent += len(block)
                    if progress:
                        progress(int(sent * 100 / max(meta.st_size, 1)))
                response(conn)

    def stop(self):
        self.stop_event.set()
        if self.listener:
            self.listener.close()
            self.thread.join(timeout=2)
        with self.lock:
            connections = list(self.connections)
            threads = list(self.threads)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        for thread in threads:
            thread.join(timeout=2)
