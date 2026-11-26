"""Small length-prefixed JSON protocol over pinned TLS; streamed file bodies."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
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

from .security import (Invitation, certificate, certificate_proof, pairing_socket, pinned_socket,
                       verify_certificate_proof)
from .storage import safe_name
from .clipboard import ClipboardSync, LEGACY_TEXT, MAX_IMAGE, MAX_TEXT as MAX_CLIP_TEXT, valid_image, valid_text
from .discovery import is_tailnet, peer_at, tailscale_address
from .folders import is_link, receive_folder, send_folder

PORT = 45873
MAX_FRAME = 512 * 1024
MAX_TEXT = 64 * 1024
MAX_FILE = 2 * 1024**3
CHUNK = 256 * 1024
CAPABILITIES = ("folder", "clipboard-text-v1", "clipboard-text-v2", "clipboard-image-v1",
                "capabilities-continue", "stream-v2")


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("已取消传输")


def file_hash(source, cancel):
    sha = hashlib.sha256()
    while block := source.read(CHUNK):
        check_cancel(cancel)
        sha.update(block)
    return sha.hexdigest()


def transfer_digest(conn, expected, stream):
    return read_json(conn).get("sha256") if stream else expected


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


def sas_value(message, key):
    value = message.get(key)
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("无效配对请求")
    return value


def sas_transcript(conn, initiator, receiver, first, second):
    binding = conn.get_channel_binding()
    if not binding:
        raise ValueError("无法验证配对连接")
    fields = ["YayaShare SAS v1", binding.hex(), first, second]
    for peer in (initiator, receiver):
        fields.extend(peer[key] for key in ("id", "fingerprint"))
    return "\0".join(fields).encode("ascii")


def sas_code(transcript):
    digest = hashlib.sha256(transcript).digest()
    return f"{int.from_bytes(digest, 'big') % 1_000_000:06d}"


class Service:
    def __init__(self, state, host="0.0.0.0", port=PORT, event=None, confirm_pairing=None):
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
        self.clipboard = ClipboardSync()
        self.capabilities = {}
        self.confirm_pairing = confirm_pairing
        self.pairing_until = 0

    def allow_pairing(self, seconds=120):
        with self.lock:
            self.pairing_until = time.monotonic() + seconds

    @property
    def info(self):
        return {**self.state.identity, "fingerprint": self.fingerprint, "port": self.port}

    def _endpoint_offer(self):
        host = tailscale_address()
        return {"tailscale_host": host, "tailscale_port": self.port} if host else {}

    @staticmethod
    def _offered_endpoints(message, default_port):
        host = message.get("tailscale_host")
        port = message.get("tailscale_port", default_port)
        if is_tailnet(host) and type(port) is int and 1 <= port <= 65535:
            return {"tailscale_host": host, "tailscale_port": port}
        return {}

    def connect(self, peer):
        route = []
        if peer.get("lan_host") and 0 <= time.monotonic() - peer.get("lan_seen", 0) < 12:
            route.append((peer["lan_host"], peer.get("lan_port", peer["port"]), 2))
        tail = peer.get("tailscale_host")
        if not tail and is_tailnet(peer.get("host")):
            tail = peer["host"]
        if tail:
            route.append((tail, peer.get("tailscale_port", peer["port"]), 4))
        if peer.get("host"):
            route.append((peer["host"], peer["port"], 2))
        if peer.get("lan_host"):
            route.append((peer["lan_host"], peer.get("lan_port", peer["port"]), 2))
        tried = set()
        last = ValueError("设备没有可用地址")
        for host, port, timeout in route:
            if (host, port) in tried:
                continue
            tried.add((host, port))
            try:
                conn = pinned_socket(host, port, peer["fingerprint"], timeout=timeout)
            except (OSError, ValueError) as exc:
                last = exc
                continue
            if host == peer.get("lan_host") and "token" in peer and "id" in peer:
                try:
                    self.state.learn_endpoints(peer, lan_host=host, lan_port=port)
                except OSError:
                    pass
            return conn
        raise last

    def _cache_capabilities(self, peer, message):
        caps = message.get("capabilities")
        if not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps):
            return frozenset()
        caps = frozenset(caps)
        with self.lock:
            self.capabilities[peer["id"]] = (peer["fingerprint"], peer["token"], time.monotonic(), caps)
        return caps

    def _cached_capabilities(self, peer):
        with self.lock:
            cached = self.capabilities.get(peer["id"])
        if cached and cached[:2] == (peer["fingerprint"], peer["token"]) and time.monotonic() - cached[2] < 60:
            return cached[3]
        return None

    def _probe_capabilities(self, conn, peer, auth, continue_on_connection):
        send_json(conn, {**auth, "op": "capabilities", "continue": continue_on_connection,
                         **self._endpoint_offer()})
        result = read_json(conn)
        if not result.get("ok"):
            if result.get("error") != "未知请求":
                raise ValueError(result.get("error", "能力协商失败"))
            result = {"capabilities": []}
        caps = self._cache_capabilities(peer, result)
        endpoints = self._offered_endpoints(result, peer["port"])
        self.state.learn_endpoints(peer, **endpoints)
        return {**peer, **endpoints}, caps

    def _transfer_capabilities(self, peer, auth):
        caps = self._cached_capabilities(peer)
        if caps is not None:
            return peer, caps
        with self.connect(peer) as conn:
            return self._probe_capabilities(conn, peer, auth, False)

    @contextmanager
    def _negotiated(self, peer, auth):
        caps = self._cached_capabilities(peer)
        if caps is not None:
            with self.connect(peer) as conn:
                yield conn, caps
            return
        with self.connect(peer) as conn:
            peer, caps = self._probe_capabilities(conn, peer, auth, True)
            if "capabilities-continue" in caps:
                yield conn, caps
                return
        with self.connect(peer) as conn:
            yield conn, caps

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
            if op == "pair-sas":
                self._receive_sas(conn, host, request)
                return
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
                peer.update(host=host, token=token, **self._offered_endpoints(request, request["port"]))
                peer = peer_at(peer, host, peer["port"])
                self.state.trust(peer)
                self._cache_capabilities(peer, request)
                send_json(conn, {"ok": True, **self.info, **self._endpoint_offer(),
                                 "token": token, "capabilities": list(CAPABILITIES)})
                self.event("paired", peer["name"])
                return
            peer_id = request.get("id")
            token = request.get("token")
            if not isinstance(peer_id, str) or not isinstance(token, str) or len(token) != 64:
                raise ValueError("请先配对")
            peer = self.state.peers().get(peer_id)
            if not peer or not secrets.compare_digest(peer["token"], token):
                raise ValueError("设备未受信任，请重新配对")
            if op == "capabilities":
                self.state.learn_endpoints(peer, **self._offered_endpoints(request, peer["port"]))
                send_json(conn, {"ok": True, **self._endpoint_offer(), "capabilities": list(CAPABILITIES)})
                if request.get("continue") is not True:
                    return
                conn.settimeout(30)
                request = read_json(conn)
                conn.settimeout(30)
                follow_token = request.get("token")
                if (request.get("version") != 1 or request.get("id") != peer_id
                        or not isinstance(follow_token, str) or not follow_token.isascii()
                        or not secrets.compare_digest(follow_token, token)):
                    raise ValueError("设备未受信任")
                peer = self.state.peers().get(peer_id)
                if not peer or not secrets.compare_digest(peer["token"], token):
                    raise ValueError("设备未受信任")
                op = request.get("op")
            if op == "clipboard":
                self._receive_clipboard(conn, request, peer_id)
                send_json(conn, {"ok": True})
                return
            if op == "text":
                text = request.get("text")
                if request.get("purpose") == "clipboard":
                    if request.get("mime") != "text/plain":
                        raise ValueError("只支持纯文本剪贴板")
                    if not valid_text(text) or len(text.encode("utf-8")) > LEGACY_TEXT:
                        raise ValueError("旧版剪贴板文本不得超过 64 KiB")
                    self.clipboard.receive(peer_id, request.get("update_id"), text)
                    send_json(conn, {"ok": True})
                    return
                if "purpose" in request:
                    raise ValueError("未知文本用途")
                if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TEXT:
                    raise ValueError("文本为空或超过 64 KiB")
                self.state.record("text", peer["name"], text)
            elif op == "file":
                self._receive_file(conn, request, peer)
            elif op == "folder":
                receive_folder(self, conn, request, peer)
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

    def _receive_clipboard(self, conn, request, peer_id):
        mime, size, digest = request.get("mime"), request.get("size"), request.get("sha256")
        limit = {"text/plain": MAX_CLIP_TEXT, "image/png": MAX_IMAGE}.get(mime)
        if limit is None or type(size) is not int or not 0 <= size <= limit:
            raise ValueError("剪贴板内容超限或类型无效")
        with self.clipboard.lock:
            if not self.clipboard.enabled or (mime == "image/png" and not self.clipboard.images_enabled):
                raise ValueError("对方未开启该类型的剪贴板同步")
            generation = self.clipboard.generation
        send_json(conn, {"ok": True, "ready": True})
        body = read_exact(conn, size)
        sha = hashlib.sha256(body).hexdigest()
        if sha != digest:
            raise ValueError("剪贴板校验失败")
        with self.clipboard.lock:
            if generation != self.clipboard.generation:
                raise ValueError("剪贴板同步设置已变更")
            if mime == "image/png":
                self.clipboard.receive_image(peer_id, request.get("update_id"), body)
            else:
                self.clipboard.receive(peer_id, request.get("update_id"), body.decode("utf-8"))

    def _receive_file(self, conn, request, peer):
        name = safe_name(request.get("name"))
        size = request.get("size")
        digest = request.get("sha256")
        if type(size) is not int or not 0 <= size <= MAX_FILE:
            raise ValueError("文件大小必须在 0–2 GiB 之间")
        stream = request.get("stream") == 2
        if not stream and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("无效文件校验值")
        inbox = self.state.inbox
        if shutil.disk_usage(inbox).free < size + 16 * 1024**2:
            raise ValueError("接收磁盘空间不足")
        with self.state.staging(inbox) as folder:
            partial = folder / ".partial"
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
            if sha.hexdigest() != transfer_digest(conn, digest, stream):
                raise ValueError("文件校验失败，请重试")
            self.state.publish(partial, inbox, name, "file", peer["name"])

    def pair(self, host, port, code):
        fingerprint, secret = Invitation.parse(code)
        with pinned_socket(host, port, fingerprint) as conn:
            send_json(conn, {"version": 1, "op": "pair", **self.info, **self._endpoint_offer(),
                             "capabilities": list(CAPABILITIES), "secret": secret})
            result = response(conn)
            identity(result)
            if result["fingerprint"] != fingerprint or not re.fullmatch(r"[a-f0-9]{64}", result.get("token", "")):
                raise ValueError("配对响应无效")
            peer = {key: result[key] for key in ("id", "name", "fingerprint", "port", "token")}
            peer.update(self._offered_endpoints(result, peer["port"]))
            peer["host"] = host
            peer = peer_at(peer, host, port)
            self.state.trust(peer)
            self._cache_capabilities(peer, result)
            self.event("paired", peer["name"])
            return peer

    def _pairing_info(self):
        return {**self.info, **self._endpoint_offer(), "capabilities": list(CAPABILITIES)}

    def _trust_sas(self, message, host, port, token):
        peer = {key: message[key] for key in ("id", "name", "fingerprint", "port")}
        peer.update(host=host, token=token, **self._offered_endpoints(message, message["port"]))
        peer = peer_at(peer, host, port)
        self.state.trust(peer)
        self._cache_capabilities(peer, message)
        self.event("paired", peer["name"])
        return peer

    def _receive_sas(self, conn, host, request):
        identity(request)
        if request["id"] == self.info["id"]:
            raise ValueError("不能与自己配对")
        commitment = sas_value(request, "commitment")
        with self.lock:
            if not self.confirm_pairing or time.monotonic() >= self.pairing_until:
                raise ValueError("请先在接收设备开启接收六位码配对")
            self.pairing_until = 0
        own = self._pairing_info()
        nonce = secrets.token_hex(32)
        send_json(conn, {"ok": True, **own, "nonce": nonce})
        conn.settimeout(10)
        proof = read_json(conn)
        first = sas_value(proof, "nonce")
        if not secrets.compare_digest(hashlib.sha256(bytes.fromhex(first)).hexdigest(), commitment):
            raise ValueError("配对承诺校验失败")
        transcript = sas_transcript(conn, request, own, first, nonce)
        verify_certificate_proof(proof, request["fingerprint"], transcript)
        send_json(conn, {"ok": True})
        conn.settimeout(65)
        if not self.confirm_pairing(request["name"], sas_code(transcript)):
            raise ValueError("接收设备取消了配对")
        if read_json(conn).get("confirmed") is not True:
            raise ValueError("发起设备取消了配对")
        token = secrets.token_hex(32)
        self._trust_sas(request, host, request["port"], token)
        send_json(conn, {"ok": True, "token": token})

    def pair_sas(self, host, port, confirm):
        with pairing_socket(host, port) as conn:
            own = self._pairing_info()
            nonce = secrets.token_hex(32)
            commitment = hashlib.sha256(bytes.fromhex(nonce)).hexdigest()
            send_json(conn, {"version": 1, "op": "pair-sas", **own, "commitment": commitment})
            remote = response(conn)
            identity(remote)
            actual = hashlib.sha256(conn.getpeercert(binary_form=True)).hexdigest()
            if remote["id"] == own["id"] or not secrets.compare_digest(remote["fingerprint"], actual):
                raise ValueError("配对设备身份无效")
            transcript = sas_transcript(conn, own, remote, nonce, sas_value(remote, "nonce"))
            send_json(conn, {"nonce": nonce, **certificate_proof(self.state.root, transcript)})
            response(conn)
            conn.settimeout(65)
            confirmed = confirm(remote["name"], sas_code(transcript))
            send_json(conn, {"confirmed": bool(confirmed)})
            if not confirmed:
                raise ValueError("已取消配对")
            token = sas_value(response(conn), "token")
            return self._trust_sas(remote, host, port, token)

    def send(self, peer, *, text=None, path=None, progress=None, cancel=None):
        check_cancel(cancel)
        request = {"version": 1, "id": self.state.identity["id"], "token": peer["token"]}
        if path is None:
            if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TEXT:
                raise ValueError("文本为空或超过 64 KiB")
            with self.connect(peer) as conn:
                send_json(conn, {**request, "op": "text", "text": text})
                response(conn)
            return
        path = Path(path)
        if is_link(path):
            raise ValueError("请选择普通文件或文件夹，不支持链接")
        peer, caps = self._transfer_capabilities(peer, request)
        stream = "stream-v2" in caps
        if path.is_dir():
            return send_folder(self, peer, path, progress, stream, cancel)
        with open(path, "rb") as source:
            import stat
            meta = os.fstat(source.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_FILE:
                raise ValueError("请选择不超过 2 GiB 的普通文件")
            if stream:
                integrity = {"stream": 2}
            else:
                integrity = {"sha256": file_hash(source, cancel)}
                source.seek(0)
            check_cancel(cancel)
            with self.connect(peer) as conn:
                send_json(conn, {**request, "op": "file", "name": path.name,
                                 "size": meta.st_size, **integrity})
                response(conn)
                sent = 0
                sha = hashlib.sha256()
                while sent < meta.st_size:
                    check_cancel(cancel)
                    block = source.read(min(CHUNK, meta.st_size - sent))
                    if not block:
                        raise ValueError("文件在传输时被修改，请重试")
                    conn.sendall(block)
                    if stream:
                        sha.update(block)
                    sent += len(block)
                    if progress:
                        progress(int(sent * 100 / max(meta.st_size, 1)))
                if stream:
                    check_cancel(cancel)
                    current = os.fstat(source.fileno())
                    if (current.st_size, current.st_mtime_ns) != (meta.st_size, meta.st_mtime_ns):
                        raise ValueError("文件在传输时被修改，请重试")
                    send_json(conn, {"sha256": sha.hexdigest()})
                response(conn)
                if progress:
                    progress(100)

    def send_clipboard(self, peer, update, generation):
        image, text = update.get("image"), update.get("text")
        if not (valid_image(image) if image is not None else valid_text(text)):
            raise ValueError("无效剪贴板内容")
        body = image if image is not None else text.encode("utf-8")
        uid = update["update_id"]
        auth = {"version": 1, "id": self.state.identity["id"], "token": peer["token"]}
        mime, capability = ("image/png", "clipboard-image-v1") if image is not None else ("text/plain", "clipboard-text-v2")
        with self._negotiated(peer, auth) as (conn, caps):
            legacy_frame = image is None and len(body) <= LEGACY_TEXT and "clipboard-text-v1" in caps
            with self.clipboard.lock:
                current = self.state.peers().get(peer["id"])
                if (not self.clipboard.enabled or self.clipboard.generation != generation
                        or not current or current["token"] != peer["token"]):
                    return
                if legacy_frame:
                    send_json(conn, {**auth, "op": "text", "purpose": "clipboard",
                                     "mime": mime, "update_id": uid, "text": text})
                elif capability in caps:
                    send_json(conn, {**auth, "op": "clipboard", "mime": mime, "update_id": uid,
                                     "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
                else:
                    raise ValueError("对方版本不支持此剪贴板内容，请更新")
            if not legacy_frame:
                response(conn)
                for offset in range(0, len(body), CHUNK):
                    with self.clipboard.lock:
                        current = self.state.peers().get(peer["id"])
                        if (not current or current["token"] != peer["token"]
                                or generation != self.clipboard.generation):
                            return
                    conn.sendall(body[offset:offset + CHUNK])
            response(conn)

    def stop(self):
        self.clipboard.configure(False)
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
