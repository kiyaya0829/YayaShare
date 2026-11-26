"""TLS certificates and out-of-band certificate-pinned invitations."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import os
import secrets
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def certificate(root, *, addresses=()):
    cert_path, key_path = root / "certificate.pem", root / "private.pem"
    if not cert_path.exists() or not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                        "YayaShare Phone Portal" if addresses else "YayaShare")])
        now = datetime.now(timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=1 if addresses else 3650)))
        if addresses:
            builder = (builder.add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address(address)) for address in sorted(addresses)
            ]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False, key_agreement=False,
                    key_cert_sign=True, crl_sign=False, encipher_only=False, decipher_only=False),
                    critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
        cert = builder.sign(key, hashes.SHA256())
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


def _client_socket(host, port, timeout):
    import socket
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    # Self-signed peer identity is verified below, before any application data.
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("连接已超时")
        raw.settimeout(remaining)
        conn = context.wrap_socket(raw, server_hostname="YayaShare")
        conn.settimeout(30)
        return conn
    except Exception:
        raw.close()
        raise


def pinned_socket(host, port, fingerprint, timeout=10):
    conn = _client_socket(host, port, timeout)
    actual = hashlib.sha256(conn.getpeercert(binary_form=True)).hexdigest()
    if not secrets.compare_digest(actual, fingerprint):
        conn.close()
        raise ValueError("设备证书不匹配，请核对配对码或重新配对")
    return conn


def pairing_socket(host, port):
    return _client_socket(host, port, 4)


def certificate_proof(root, transcript):
    key = serialization.load_pem_private_key((root / "private.pem").read_bytes(), None)
    cert = x509.load_pem_x509_certificate((root / "certificate.pem").read_bytes())
    return {"certificate": cert.public_bytes(serialization.Encoding.DER).hex(),
            "signature": key.sign(transcript, ec.ECDSA(hashes.SHA256())).hex()}


def verify_certificate_proof(message, fingerprint, transcript):
    from cryptography.exceptions import InvalidSignature
    try:
        der = bytes.fromhex(message["certificate"])
        signature = bytes.fromhex(message["signature"])
        if len(der) > 4096 or len(signature) > 128:
            raise ValueError()
        cert = x509.load_der_x509_certificate(der)
        public = cert.public_key()
        if (not isinstance(public, ec.EllipticCurvePublicKey)
                or not isinstance(public.curve, ec.SECP256R1)
                or not secrets.compare_digest(cert.fingerprint(hashes.SHA256()).hex(), fingerprint)):
            raise ValueError()
        public.verify(signature, transcript, ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature, KeyError, TypeError) as exc:
        raise ValueError("配对证书证明无效") from exc


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
