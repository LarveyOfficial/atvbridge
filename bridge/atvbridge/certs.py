"""Server TLS identity for the Android TV Remote v2 protocol.

Pairing codes are 6 hex chars: the first byte must equal the first byte of
SHA-256(client_n, client_e, server_n, server_e, code[1:3]). The phone checks this
itself, so a fixed code only works if the server's public key is chosen to match
that particular phone. We keep one RSA modulus and vary the public exponent,
which makes finding a match for a given phone take milliseconds.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DEFAULT_EXPONENT = 65537
# Keep exponents below 0x100000 so their byte encoding matches every client
# implementation we know of (and stays well within TLS library limits).
MAX_EXPONENT = 0x100000
EASY_CODE = "000000"


def _int_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def pairing_hash(client_n: int, client_e: int, server_n: int, server_e: int, nonce: bytes) -> bytes:
    h = hashlib.sha256()
    h.update(_int_bytes(client_n))
    h.update(_int_bytes(client_e))
    h.update(_int_bytes(server_n))
    h.update(_int_bytes(server_e))
    h.update(nonce)
    return h.digest()


def public_numbers(cert: x509.Certificate) -> tuple[int, int]:
    nums = cert.public_key().public_numbers()  # type: ignore[union-attr]
    return nums.n, nums.e  # type: ignore[union-attr]


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


class ServerIdentity:
    """Holds the base RSA key and derives certificates for different exponents."""

    def __init__(self, data_dir: Path, common_name: str) -> None:
        self._path = data_dir / "server_key.pem"
        self.common_name = common_name
        self._key = self._load_or_create()
        nums = self._key.private_numbers()
        self._p, self._q = nums.p, nums.q
        self.n = self._p * self._q
        self._phi = (self._p - 1) * (self._q - 1)
        self._cache: dict[int, tuple[rsa.RSAPrivateKey, x509.Certificate]] = {}

    def _load_or_create(self) -> rsa.RSAPrivateKey:
        if self._path.exists():
            key = serialization.load_pem_private_key(self._path.read_bytes(), password=None)
            assert isinstance(key, rsa.RSAPrivateKey)
            return key
        key = rsa.generate_private_key(public_exponent=DEFAULT_EXPONENT, key_size=2048)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return key

    def find_exponent_for_code(self, client_cert: x509.Certificate, code: str = EASY_CODE) -> int | None:
        """Return the smallest exponent that makes `code` valid for this client."""
        client_n, client_e = public_numbers(client_cert)
        target = int(code[:2], 16)
        nonce = bytes.fromhex(code[2:])
        for e in range(DEFAULT_EXPONENT, MAX_EXPONENT, 2):
            if math.gcd(e, self._phi) != 1:
                continue
            if pairing_hash(client_n, client_e, self.n, e, nonce)[0] == target:
                return e
        return None

    def get(self, e: int = DEFAULT_EXPONENT) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
        if e not in self._cache:
            key = self._key if e == DEFAULT_EXPONENT else self._derive(e)
            self._cache[e] = (key, self._make_cert(key))
        return self._cache[e]

    def _derive(self, e: int) -> rsa.RSAPrivateKey:
        p, q = self._p, self._q
        d = pow(e, -1, self._phi)
        return rsa.RSAPrivateNumbers(
            p, q, d,
            rsa.rsa_crt_dmp1(d, p),
            rsa.rsa_crt_dmq1(d, q),
            rsa.rsa_crt_iqmp(p, q),
            rsa.RSAPublicNumbers(e, self.n),
        ).private_key()

    def _make_cert(self, key: rsa.RSAPrivateKey) -> x509.Certificate:
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.common_name)])
        # Fixed validity start so certs for the same exponent are stable across restarts.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(self.n % (2**63) + 1)
            .not_valid_before(start)
            .not_valid_after(start + timedelta(days=30 * 365))
            .sign(key, hashes.SHA256())
        )

    def pairing_code_for(self, client_cert: x509.Certificate, server_e: int, nonce: bytes) -> tuple[str, bytes]:
        """Compute (code, secret) for a nonce. Code = hex(secret[0]) + hex(nonce)."""
        client_n, client_e = public_numbers(client_cert)
        secret = pairing_hash(client_n, client_e, self.n, server_e, nonce)
        return f"{secret[0]:02X}{nonce.hex().upper()}", secret
