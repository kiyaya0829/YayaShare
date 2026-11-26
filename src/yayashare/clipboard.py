"""Session-only clipboard state; no clipboard contents are persisted."""
from collections import OrderedDict, deque
import hashlib
import threading
import uuid

LEGACY_TEXT = 64 * 1024
MAX_TEXT = 512 * 1024
MAX_IMAGE = 16 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def valid_text(text):
    if not isinstance(text, str) or "\x00" in text:
        return False
    try:
        return len(text.encode("utf-8")) <= MAX_TEXT
    except UnicodeError:
        return False


def valid_image(image):
    return isinstance(image, bytes) and image.startswith(PNG_SIGNATURE) and len(image) <= MAX_IMAGE


class ClipboardSync:
    def __init__(self):
        self.lock = threading.RLock()
        self.enabled = False
        self.generation = 0
        self.last_text = None
        self.last_image_sha256 = None
        self.images_enabled = False
        self.seen = OrderedDict()
        self.incoming = deque(maxlen=64)

    def configure(self, enabled, baseline=None, *, images=False, image=None):
        with self.lock:
            self.enabled = bool(enabled)
            self.generation += 1
            self.images_enabled = images
            self.baseline(baseline, image)
            self.incoming.clear()

    def baseline(self, text, image=None):
        self.last_text = text
        self.last_image_sha256 = hashlib.sha256(image).hexdigest() if image else None

    def _remember(self, update_id):
        self.seen[update_id] = None
        while len(self.seen) > 512:
            self.seen.popitem(last=False)

    def local_change(self, text, image=None):
        with self.lock:
            digest = hashlib.sha256(image).hexdigest() if image else None
            if (text, digest) == (self.last_text, self.last_image_sha256):
                return None
            self.last_text, self.last_image_sha256 = text, digest
            if not self.enabled:
                return None
            if self.images_enabled and valid_image(image):
                update = {"image": image}
            elif valid_text(text):
                update = {"text": text}
            else:
                return None
            update["update_id"] = uuid.uuid4().hex
            self._remember(update["update_id"])
            return update

    def receive(self, peer_id, update_id, text):
        if not valid_text(text):
            raise ValueError("剪贴板必须是最多 512 KiB 的纯文本")
        self._receive(peer_id, update_id, text)

    def receive_image(self, peer_id, update_id, image):
        if not valid_image(image):
            raise ValueError("无效 PNG 剪贴板图片")
        self._receive(peer_id, update_id, image)

    def _receive(self, peer_id, update_id, content):
        if not isinstance(update_id, str) or len(update_id) != 32:
            raise ValueError("无效剪贴板更新 ID")
        try:
            if uuid.UUID(hex=update_id).hex != update_id:
                raise ValueError()
        except ValueError:
            raise ValueError("无效剪贴板更新 ID") from None
        with self.lock:
            if not self.enabled:
                raise ValueError("对方未开启 Clipboard Sync")
            if update_id in self.seen:
                return
            if isinstance(content, bytes):
                if not self.images_enabled:
                    raise ValueError("对方未开启图片同步")
                self.incoming = deque(
                    ((pid, item) for pid, item in self.incoming if not isinstance(item, bytes)),
                    maxlen=64)
            self._remember(update_id)
            self.incoming.append((peer_id, content))

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
                    if isinstance(text, bytes):
                        self.baseline(None, text)
                    else:
                        self.baseline(text)
                    write(text)
                    applied = True
            return applied
