"""Session-only clipboard state; no clipboard contents are persisted."""
from collections import OrderedDict, deque
import threading
import uuid

MAX_TEXT = 64 * 1024


def valid_text(text):
    return isinstance(text, str) and "\x00" not in text and len(text.encode("utf-8")) <= MAX_TEXT


class ClipboardSync:
    def __init__(self):
        self.lock = threading.RLock()
        self.enabled = False
        self.generation = 0
        self.last_text = None
        self.seen = OrderedDict()
        self.incoming = deque(maxlen=64)

    def configure(self, enabled, baseline=None):
        with self.lock:
            self.enabled = bool(enabled)
            self.generation += 1
            self.last_text = baseline
            self.incoming.clear()

    def _remember(self, update_id):
        self.seen[update_id] = None
        while len(self.seen) > 512:
            self.seen.popitem(last=False)

    def local_change(self, text):
        with self.lock:
            if text == self.last_text:
                return None
            self.last_text = text
            if not self.enabled or not valid_text(text):
                return None
            update = {"update_id": uuid.uuid4().hex, "text": text}
            self._remember(update["update_id"])
            return update

    def receive(self, peer_id, update_id, text):
        if not isinstance(update_id, str) or len(update_id) != 32:
            raise ValueError("无效剪贴板更新 ID")
        try:
            if uuid.UUID(hex=update_id).hex != update_id:
                raise ValueError()
        except ValueError:
            raise ValueError("无效剪贴板更新 ID") from None
        if not valid_text(text):
            raise ValueError("剪贴板必须是最多 64 KiB 的纯文本")
        with self.lock:
            if not self.enabled:
                raise ValueError("对方未开启 Clipboard Sync")
            if update_id in self.seen:
                return
            self._remember(update_id)
            self.incoming.append((peer_id, text))

    def apply_pending(self, trusted_ids, write):
        # Called only on the GUI thread. Mark before setText (which can emit
        # dataChanged synchronously), then read-back normalization is done by UI.
        with self.lock:
            if not self.enabled:
                self.incoming.clear()
                return False
            applied = False
            while self.incoming:
                peer_id, text = self.incoming.popleft()
                if peer_id in trusted_ids:
                    self.last_text = text
                    write(text)
                    applied = True
            return applied
