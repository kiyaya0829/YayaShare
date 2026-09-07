"""Local state and safe file naming; remote paths are never used as paths."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
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
        self.inbox = self.root / "Received"
        self.inbox.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "state.json"
        self.lock = threading.RLock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"id": uuid.uuid4().hex, "name": socket.gethostname()[:80], "peers": {}, "history": []}
            self.save()

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

    def record(self, kind, sender, content):
        with self.lock:
            self.data["history"].insert(0, {"kind": kind, "sender": sender, "content": content,
                                         "time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            del self.data["history"][100:]
            self.save()

    def history(self):
        with self.lock:
            return copy.deepcopy(self.data["history"])
