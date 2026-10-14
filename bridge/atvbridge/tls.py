"""Minimal asyncio TLS server transport built on pyOpenSSL memory BIOs.

Python's stdlib ssl cannot accept arbitrary self-signed client certificates
(there is no verify callback), which the Android TV Remote protocol requires.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from cryptography import x509
from OpenSSL import SSL, crypto

LOG = logging.getLogger(__name__)

# verify(conn, cert) -> accept?
VerifyFn = Callable[["TLSConnection", x509.Certificate], bool]


def make_context(key, cert: x509.Certificate, verify: Callable[[SSL.Connection, x509.Certificate], bool]) -> SSL.Context:
    ctx = SSL.Context(SSL.TLS_SERVER_METHOD)
    ctx.set_min_proto_version(SSL.TLS1_2_VERSION)
    ctx.use_privatekey(key)
    ctx.use_certificate(cert)

    def _cb(conn: SSL.Connection, x509obj: crypto.X509, errno: int, depth: int, ok: int) -> bool:
        if depth != 0:
            return True
        return verify(conn, x509obj.to_cryptography())

    ctx.set_verify(SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT, _cb)
    return ctx


class TLSConnection(asyncio.Protocol):
    """Subclass and implement `select_context`, `on_ready`, `on_data`, `on_closed`."""

    def __init__(self) -> None:
        self.transport: asyncio.Transport | None = None
        self.peer_ip: str = ""
        self.peer_cert: x509.Certificate | None = None
        self._ssl: SSL.Connection | None = None
        self._ready = False
        self._closed = False

    # --- hooks -----------------------------------------------------------------
    def select_context(self) -> SSL.Context:
        raise NotImplementedError

    def on_ready(self) -> None:
        pass

    def on_data(self, data: bytes) -> None:
        pass

    def on_closed(self) -> None:
        pass

    # --- asyncio.Protocol ----------------------------------------------------------
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        peer = transport.get_extra_info("peername")
        self.peer_ip = peer[0] if peer else ""
        if self.peer_ip.startswith("::ffff:"):
            self.peer_ip = self.peer_ip[7:]
        self._ssl = SSL.Connection(self.select_context(), None)
        self._ssl.set_app_data(self)
        self._ssl.set_accept_state()

    def data_received(self, data: bytes) -> None:
        assert self._ssl is not None
        self._ssl.bio_write(data)
        if not self._ready:
            try:
                self._ssl.do_handshake()
            except SSL.WantReadError:
                self._flush()
                return
            except SSL.Error as exc:
                LOG.debug("TLS handshake with %s failed: %s", self.peer_ip, exc)
                self._flush()
                self.close()
                return
            self._ready = True
            self._flush()
            peer = self._ssl.get_peer_certificate()
            self.peer_cert = peer.to_cryptography() if peer is not None else None
            self.on_ready()
        while not self._closed:
            try:
                chunk = self._ssl.recv(65536)
            except SSL.WantReadError:
                break
            except SSL.ZeroReturnError:
                self.close()
                return
            except SSL.Error as exc:
                LOG.debug("TLS error from %s: %s", self.peer_ip, exc)
                self._flush()
                self.close()
                return
            if not chunk:
                break
            self.on_data(chunk)
        self._flush()

    def connection_lost(self, exc: Exception | None) -> None:
        if not self._closed:
            self._closed = True
            self.on_closed()

    # --- helpers -------------------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self._closed or self._ssl is None or not self._ready:
            return
        try:
            self._ssl.sendall(data)
        except SSL.Error as exc:
            LOG.debug("TLS write to %s failed: %s", self.peer_ip, exc)
            self.close()
            return
        self._flush()

    def _flush(self) -> None:
        if self._ssl is None or self.transport is None:
            return
        while True:
            try:
                out = self._ssl.bio_read(65536)
            except SSL.WantReadError:
                break
            if not out:
                break
            self.transport.write(out)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ssl is not None and self._ready:
            try:
                self._ssl.shutdown()
                self._flush()
            except SSL.Error:
                pass
        if self.transport is not None:
            self.transport.close()
        self.on_closed()


class ProtobufFraming:
    """Varint length-delimited protobuf framing used by both ports."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        msgs: list[bytes] = []
        while True:
            length, pos, shift = 0, 0, 0
            while True:
                if pos >= len(self._buf):
                    return msgs
                b = self._buf[pos]
                length |= (b & 0x7F) << shift
                pos += 1
                if not b & 0x80:
                    break
                shift += 7
                if shift > 35:
                    raise ValueError("bad varint")
            if len(self._buf) < pos + length:
                return msgs
            msgs.append(bytes(self._buf[pos : pos + length]))
            del self._buf[: pos + length]

    @staticmethod
    def encode(payload: bytes) -> bytes:
        out = bytearray()
        n = len(payload)
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        return bytes(out) + payload
