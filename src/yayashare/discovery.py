"""IPv4 UDP discovery. Advertisements are hints, never trust credentials."""
import json
import socket
import threading
import time

DISCOVERY_PORT = 45874


def parse_advertisement(data, host):
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("app") != "YayaShare" or value.get("version") != 1:
        raise ValueError("Unknown service")
    from .protocol import identity
    identity(value)
    return {key: value[key] for key in ("id", "name", "fingerprint", "port")} | {"host": host, "seen": time.monotonic()}


class Discovery:
    def __init__(self, info):
        self.info = info
        self.lock = threading.Lock()
        self.devices = {}
        self.stopped = threading.Event()
        self.error = ""
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.sock.bind(("", DISCOVERY_PORT))
        except OSError as exc:
            self.error = str(exc)
            self.sock.close()
            self.sock = None
            return
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        last = 0
        payload = json.dumps({"app": "YayaShare", "version": 1, **self.info}).encode()
        while not self.stopped.is_set():
            if time.monotonic() - last > 3:
                try:
                    self.sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
                except OSError as exc:
                    self.error = str(exc)
                last = time.monotonic()
            try:
                data, addr = self.sock.recvfrom(2048)
                peer = parse_advertisement(data, addr[0])
                if peer["id"] != self.info["id"]:
                    with self.lock:
                        # Bound memory even on a hostile broadcast network.
                        if len(self.devices) < 100 or peer["id"] in self.devices:
                            self.devices[peer["id"]] = peer
            except socket.timeout:
                pass
            except (ValueError, UnicodeError, KeyError, TypeError):
                pass
            except OSError:
                break
            with self.lock:
                self.devices = {k: v for k, v in self.devices.items() if time.monotonic() - v["seen"] < 12}

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.devices.items()}

    def stop(self):
        self.stopped.set()
        if self.sock:
            self.sock.close()
            self.thread.join(timeout=2)
