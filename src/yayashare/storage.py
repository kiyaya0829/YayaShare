"""Local state and safe file naming; remote paths are never used as paths."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import threading
import unicodedata
import uuid
from datetime import datetime, timezone


def data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "YayaShare"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/YayaShare"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "YayaShare"


def safe_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("无效文件名")
    value = unicodedata.normalize("NFC", value.replace("\\", "/").split("/")[-1])
    value = "".join("_" if unicodedata.category(c).startswith("C") else c for c in value)
    value = re.sub(r'[<>:"/\\|?*]', "_", value).strip(" .")
    if not value:
        value = "received"
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}:
        value = "_" + value
    # Limit UTF-8 bytes too: macOS and Windows count names differently.
    while len(value.encode("utf-8")) > 160:
        value = value[:-1]
    return value.rstrip(" .") or "received"


class State:
    def __init__(self, root: Path | None = None):
        self.root = (root or data_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.default_inbox = self.root / "Received"
        self.default_inbox.mkdir(exist_ok=True, mode=0o700)
        self.inbox = self.default_inbox
        self.path = self.root / "state.json"
        self.lock = threading.RLock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"id": uuid.uuid4().hex, "name": socket.gethostname()[:80], "peers": {}, "history": []}
            self.save()
        directory = self.settings.get("receive_directory")
        if isinstance(directory, str) and directory:
            try:
                self.inbox = self._receive_directory(directory)
            except (OSError, ValueError):
                pass

    @property
    def settings(self):
        with self.lock:
            return dict(self.data.get("settings", {}))

    def configure(self, **settings):
        with self.lock:
            inbox = self.inbox
            if "receive_directory" in settings:
                directory = settings["receive_directory"]
                inbox = self._receive_directory(directory) if directory else self.default_inbox
                settings["receive_directory"] = str(inbox) if directory else ""
            previous = self.data.get("settings", {})
            self.data["settings"] = {**previous, **settings}
            try:
                self.save()
            except OSError:
                self.data["settings"] = previous
                raise
            self.inbox = inbox

    @staticmethod
    def _receive_directory(directory):
        path = Path(directory).expanduser()
        if not path.is_absolute():
            raise ValueError("接收目录必须是绝对路径")
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def staging(self, inbox):
        from .folders import is_link
        parent = inbox / ".staging"
        batch = parent / uuid.uuid4().hex
        with self.lock:
            if is_link(inbox) or is_link(parent):
                raise ValueError("接收目录不能是链接")
            parent.mkdir(exist_ok=True, mode=0o700)
            if os.name == "nt":
                import ctypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                attributes = kernel.GetFileAttributesW(str(parent))
                if attributes != -1:
                    kernel.SetFileAttributesW(str(parent), attributes | 2)
            batch.mkdir(mode=0o700)
        try:
            yield batch
        finally:
            with self.lock:
                shutil.rmtree(batch)
                try:
                    parent.rmdir()
                except OSError:
                    pass

    def publish(self, source, inbox, name, kind, sender):
        stem, suffix = (Path(name).stem, Path(name).suffix) if kind == "file" else (name, "")
        with self.lock:
            occupied = {unicodedata.normalize("NFC", p.name).casefold() for p in inbox.iterdir()}
            target = inbox / name
            index = 2
            while unicodedata.normalize("NFC", target.name).casefold() in occupied:
                target = inbox / f"{stem} ({index}){suffix}"
                index += 1
            if kind == "file" and os.name != "nt":
                os.link(source, target)
            else:
                source.rename(target)
            try:
                self.record(kind, sender, str(target))
            except OSError:
                if kind == "file" and os.name != "nt":
                    target.unlink()
                else:
                    target.rename(source)
                raise

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                os.chmod(tmp, 0o600)
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)

    @property
    def identity(self):
        return {"id": self.data["id"], "name": self.data["name"]}

    def peers(self):
        with self.lock:
            return copy.deepcopy(self.data["peers"])

    def trust(self, peer):
        with self.lock:
            self.data["peers"][peer["id"]] = dict(peer)
            self.save()

    def forget(self, peer_id):
        with self.lock:
            self.data["peers"].pop(peer_id, None)
            self.save()

    def learn_endpoints(self, peer, **endpoints):
        with self.lock:
            current = self.data["peers"].get(peer["id"])
            if not current or any(current[k] != peer[k] for k in ("token", "fingerprint")):
                return
            if any(current.get(key) != value for key, value in endpoints.items()):
                self.data["peers"][peer["id"]] = {**current, **endpoints}
                try:
                    self.save()
                except OSError:
                    self.data["peers"][peer["id"]] = current
                    raise

    def record(self, kind, sender, content):
        with self.lock:
            previous = self.data["history"]
            entry = {"kind": kind, "sender": sender, "content": content,
                     "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.data["history"] = [entry, *previous[:99]]
            try:
                self.save()
            except OSError:
                self.data["history"] = previous
                raise

    def history(self):
        with self.lock:
            return copy.deepcopy(self.data["history"])
