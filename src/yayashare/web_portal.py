"""Opt-in, short-lived LAN HTTP portal. This is NOT the desktop TLS protocol.

Only explicit exports are readable; incoming files use private random batches.
The GUI must obtain acknowledgment of HTTP's lack of transport encryption.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import threading
import time
from urllib.parse import quote, unquote, urlsplit
import uuid

from .storage import safe_name

WEB_PORT = 45875
MAX_UPLOAD = 256 * 1024**2
MAX_TOTAL = 512 * 1024**2
MAX_ITEMS = 50
MAX_TEXT = 64 * 1024
CHUNK = 256 * 1024
ASSETS = Path(__file__).parent / "web"


class PortalError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 8

    def __init__(self, address, portal):
        self.portal = portal
        self.slots = threading.BoundedSemaphore(8)
        self.connections = set()
        self.conn_lock = threading.Lock()
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            request.close()
            return
        request.settimeout(15)
        with self.conn_lock:
            self.connections.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self.conn_lock:
                self.connections.discard(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.conn_lock:
                self.connections.discard(request)
            self.slots.release()

    def handle_error(self, request, client_address):
        # Do not log request bodies, cookies, pairing links or local paths.
        pass


class WebPortal:
    def __init__(self, state, addresses, *, host="0.0.0.0", port=WEB_PORT, lifetime=1800, event=None):
        self.state = state
        self.lock = threading.RLock()
        self.event = event or (lambda *_: None)
        self.addresses = {str(ipaddress.IPv4Address(a)) for a in addresses} | {"127.0.0.1"}
        self.stopped = threading.Event()
        self.expires = time.monotonic() + lifetime
        self.invite = secrets.token_urlsafe(24)
        self.session = ""
        self.items = {}
        self.received_bytes = 0
        self.received_count = 0
        self.export_bytes = 0
        self.spool = state.root / (".web-export-" + uuid.uuid4().hex)
        self.spool.mkdir(mode=0o700)
        try:
            self.server = LimitedServer((host, port), self)
        except Exception:
            self.spool.rmdir()
            raise
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def active(self):
        return not self.stopped.is_set() and time.monotonic() < self.expires

    def url(self, address):
        if address not in self.addresses:
            raise ValueError("未知本机地址")
        return f"http://{address}:{self.port}/#{self.invite}"

    def authorize(self, value):
        with self.lock:
            if not self.active() or not self.invite or not isinstance(value, str) or not value.isascii() or not secrets.compare_digest(value, self.invite):
                raise PortalError(403, "二维码已使用、已过期或不正确，请在电脑重新开启入口。")
            self.invite = ""
            self.session = secrets.token_urlsafe(32)
            return self.session

    def authenticated(self, cookie):
        try:
            values = SimpleCookie(cookie or "")
            value = values["yaya_web"].value
        except (KeyError, ValueError, CookieError):
            return False
        with self.lock:
            return self.active() and bool(self.session) and value.isascii() and secrets.compare_digest(value, self.session)

    def share_text(self, text):
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TEXT:
            raise ValueError("文字为空或超过 64 KiB")
        with self.lock:
            self._export_check(0)
            item = {"id": uuid.uuid4().hex, "kind": "text", "text": text, "name": "电脑分享的文字"}
            self.items[item["id"]] = item
            return item["id"]

    def _export_check(self, size):
        if not self.active():
            raise ValueError("手机入口已关闭或过期")
        if len(self.items) >= MAX_ITEMS or self.export_bytes + size > MAX_TOTAL:
            raise ValueError("本次分享已达 50 项或 512 MiB，请重新开启入口")

    def share_file(self, path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError("请选择普通文件")
        item_id = uuid.uuid4().hex
        target = self.spool / item_id
        try:
            with path.open("rb") as source:
                meta = os.fstat(source.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_UPLOAD:
                    raise ValueError("请选择最大 256 MiB 的普通文件")
                with self.lock:
                    self._export_check(meta.st_size)
                    # Reserve a slot/quota while the snapshot is being copied.
                    self.export_bytes += meta.st_size
                    self.items[item_id] = {"pending": True}
                with target.open("xb") as out:
                    remaining = meta.st_size
                    while remaining:
                        if not self.active():
                            raise ValueError("手机入口已关闭或过期")
                        block = source.read(min(CHUNK, remaining))
                        if not block:
                            raise ValueError("文件已变化，请重新选择")
                        out.write(block)
                        remaining -= len(block)
                    if source.read(1) or os.fstat(source.fileno()).st_mtime_ns != meta.st_mtime_ns:
                        raise ValueError("文件已变化，请重新选择")
            with self.lock:
                if not self.active():
                    raise ValueError("手机入口已关闭或过期")
                self.items[item_id] = {"id": item_id, "kind": "file", "name": safe_name(path.name),
                                       "size": meta.st_size, "path": target}
            return item_id
        except Exception:
            target.unlink(missing_ok=True)
            with self.lock:
                if self.items.pop(item_id, None) is not None:
                    self.export_bytes -= meta.st_size
            raise

    def listing(self):
        with self.lock:
            return [{k: v for k, v in item.items() if k != "path"}
                    for item in self.items.values() if not item.get("pending")]

    def reserve_receive(self, size):
        with self.lock:
            if not self.active():
                raise PortalError(403, "入口已过期，请在电脑重新开启")
            if self.received_count >= MAX_ITEMS or self.received_bytes + size > MAX_TOTAL:
                raise PortalError(413, "本次接收已达 50 项或 512 MiB，请重新开启入口")
            self.received_count += 1
            self.received_bytes += size

    def release_receive(self, size):
        with self.lock:
            self.received_count -= 1
            self.received_bytes -= size

    def stop(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        with self.lock:
            self.invite = self.session = ""
        with self.server.conn_lock:
            for conn in list(self.server.connections):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        with self.lock:
            self.items.clear()
        # Windows may still have a download handle open; the handler retries cleanup.
        shutil.rmtree(self.spool, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "YayaShare"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    @property
    def portal(self):
        return self.server.portal

    def _reply(self, status, data=b"", mime="application/json; charset=utf-8", cookie=None, extra=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self._headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Connection", "close")

    def _guard(self, write=False):
        hosts = {f"{a}:{self.portal.port}" for a in self.portal.addresses}
        host = self.headers.get("Host", "")
        if len(self.headers.get_all("Host", [])) != 1 or host not in hosts:
            raise PortalError(403, "请使用电脑显示的 IP 地址打开")
        if not self.portal.active():
            raise PortalError(403, "手机入口已关闭或过期，请在电脑重新开启")
        origin = self.headers.get("Origin")
        if (write or origin) and origin != "http://" + host:
            raise PortalError(403, "不允许跨站请求")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            raise PortalError(403, "不允许跨站请求")
        if write and self.headers.get("X-YayaShare") != "1":
            raise PortalError(403, "请使用 YayaShare 网页操作")

    def _length(self, maximum):
        values = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
            raise PortalError(400, "无效请求长度")
        size = int(values[0])
        if size > maximum:
            raise PortalError(413, "内容超过大小限制")
        return size

    def _read(self, size):
        data = bytearray()
        while len(data) < size:
            if not self.portal.active():
                raise PortalError(403, "入口已关闭或过期")
            block = self.rfile.read1(min(CHUNK, size - len(data)))
            if not block:
                raise PortalError(400, "传输中断，内容未保存")
            data.extend(block)
        return bytes(data)

    def _json(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise PortalError(415, "需要 JSON 内容")
        value = json.loads(self._read(self._length(512 * 1024)))
        if not isinstance(value, dict):
            raise PortalError(400, "无效消息")
        return value

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def _dispatch(self, write):
        try:
            self._guard(write)
            path = urlsplit(self.path).path
            if not write and path in ("/", "/app.js", "/style.css", "/icon.png"):
                names = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path == "/icon.png":
                    return self._reply(200, (ASSETS.parent / "assets/icon.png").read_bytes(), "image/png")
                name, mime = names[path]
                return self._reply(200, (ASSETS / name).read_bytes(), mime)
            if write and path == "/api/connect":
                session = self.portal.authorize(self._json().get("token"))
                seconds = max(0, int(self.portal.expires - time.monotonic()))
                return self._reply(200, {"ok": True}, cookie=f"yaya_web={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age={seconds}")
            if not self.portal.authenticated(self.headers.get("Cookie")):
                raise PortalError(401, "请扫描电脑上的二维码连接；已使用的链接不能再次授权。")
            if not write and path == "/api/items":
                return self._reply(200, {"name": self.portal.state.identity["name"], "items": self.portal.listing(),
                                          "remaining": max(0, int(self.portal.expires - time.monotonic()))})
            if not write and path.startswith("/download/"):
                return self._download(path.removeprefix("/download/"))
            if write and path == "/api/text":
                text = self._json().get("text")
                if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_TEXT:
                    raise PortalError(413, "文字为空或超过 64 KiB")
                self.portal.reserve_receive(len(text.encode("utf-8")))
                try:
                    with self.portal.lock:
                        if not self.portal.active():
                            raise PortalError(403, "入口已关闭")
                        self.portal.state.record("text", "手机网页", text)
                except Exception:
                    self.portal.release_receive(len(text.encode("utf-8")))
                    raise
                self.portal.event("received", "手机网页")
                return self._reply(200, {"ok": True})
            if write and path == "/api/file":
                return self._upload()
            raise PortalError(404, "内容不存在")
        except PortalError as exc:
            self._error(exc.status, exc.message)
        except (ValueError, UnicodeError):
            self._error(400, "无效请求内容")
        except OSError:
            self._error(503, "连接中断或磁盘不可用，请重试")
        finally:
            if self.portal.stopped.is_set():
                shutil.rmtree(self.portal.spool, ignore_errors=True)

    def _error(self, status, message):
        try:
            self._reply(status, {"error": message})
        except OSError:
            pass

    def _upload(self):
        size = self._length(MAX_UPLOAD)
        raw_name = self.headers.get("X-Filename", "")
        if len(raw_name) > 4096:
            raise PortalError(400, "文件名过长")
        name = safe_name(unquote(raw_name, errors="strict"))
        if shutil.disk_usage(self.portal.state.inbox).free < size + 16 * 1024**2:
            raise PortalError(507, "电脑磁盘空间不足")
        self.portal.reserve_receive(size)
        folder = self.portal.state.inbox / uuid.uuid4().hex[:12]
        complete = False
        try:
            folder.mkdir(mode=0o700)
            partial, target = folder / ".partial", folder / name
            with partial.open("xb") as out:
                remaining = size
                while remaining:
                    if not self.portal.active():
                        raise PortalError(403, "入口已关闭或过期，未完成文件已清理")
                    block = self._read(min(CHUNK, remaining))
                    out.write(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
            with self.portal.lock:
                if not self.portal.active():
                    raise PortalError(403, "入口已关闭")
                os.replace(partial, target)
                self.portal.state.record("file", "手机网页", str(target))
                complete = True
            self.portal.event("received", "手机网页")
            self._reply(200, {"ok": True, "name": name})
        finally:
            if not complete:
                self.portal.release_receive(size)
                shutil.rmtree(folder, ignore_errors=True)

    def _download(self, item_id):
        with self.portal.lock:
            item = dict(self.portal.items.get(item_id, {}))
        if item.get("kind") != "file":
            raise PortalError(404, "文件不存在")
        with item["path"].open("rb") as source:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(item["size"]))
            self.send_header("Content-Disposition", "attachment; filename=download; filename*=UTF-8''" + quote(item["name"], safe=""))
            self._headers()
            self.end_headers()
            try:
                while block := source.read(CHUNK):
                    if not self.portal.active():
                        return  # A short Content-Length response fails closed in the browser.
                    self.wfile.write(block)
            except OSError:
                # Headers were sent: never append an error page to file bytes.
                self.close_connection = True
