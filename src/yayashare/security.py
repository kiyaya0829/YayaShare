"""TLS certificates and out-of-band certificate-pinned invitations."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import os
import secrets
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def certificate(root):
    cert_path, key_path = root / "certificate.pem", root / "private.pem"
    if not cert_path.exists() or not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "YayaShare")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
                .sign(key, hashes.SHA256()))
        with open(key_path, "wb") as f:
            os.chmod(key_path, 0o600)
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context, fingerprint


def pinned_socket(host, port, fingerprint):
    import socket
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    # Self-signed peer identity is verified below, before any application data.
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    raw = socket.create_connection((host, port), timeout=10)
    try:
        conn = context.wrap_socket(raw, server_hostname="YayaShare")
        actual = hashlib.sha256(conn.getpeercert(binary_form=True)).hexdigest()
        if not secrets.compare_digest(actual, fingerprint):
            conn.close()
            raise ValueError("设备证书不匹配，请核对配对码或重新配对")
        conn.settimeout(30)
        return conn
    except Exception:
        raw.close()
        raise


class Invitation:
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.lock = threading.Lock()
        self.secret = ""
        self.expires = 0

    def create(self):
        with self.lock:
            self.secret = secrets.token_hex(16)
            self.expires = time.monotonic() + 300
            return base64.urlsafe_b64encode(bytes.fromhex(self.fingerprint + self.secret)).decode("ascii")

    def consume(self, secret):
        with self.lock:
            if not self.secret or time.monotonic() > self.expires or not secrets.compare_digest(self.secret, secret):
                raise ValueError("配对码错误、已使用或已过期")
            self.secret = ""
            self.expires = 0

    @staticmethod
    def parse(code):
        try:
            raw = base64.b64decode("".join(code.split()), altchars=b"-_", validate=True)
            if len(raw) != 48:
                raise ValueError()
            return raw[:32].hex(), raw[32:].hex()
        except Exception as exc:
            raise ValueError("请粘贴完整的 64 位配对码") from exc
