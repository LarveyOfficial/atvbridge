"""Server side of the Android TV Remote v2 protocol (what a Google TV runs).

Port 6467: pairing ("polo") protocol.  Port 6466: remote control protocol.
Both are TLS with mutual (self-signed) certificates and varint-framed protobufs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import time
from typing import TYPE_CHECKING, Any, Callable

from androidtvremote2.polo_pb2 import Options, OuterMessage
from androidtvremote2.remotemessage_pb2 import RemoteDirection, RemoteKeyCode, RemoteMessage
from cryptography.hazmat.primitives import serialization
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from . import ime, voice
from .certs import DEFAULT_EXPONENT, EASY_CODE, ServerIdentity, fingerprint
from .tls import ProtobufFraming, TLSConnection, make_context

if TYPE_CHECKING:
    from .appletv import AppleTV
    from .store import Store

LOG = logging.getLogger(__name__)

PAIRING_PORT = int(os.environ.get("ATVB_PAIRING_PORT") or 6467)
REMOTE_PORT = int(os.environ.get("ATVB_REMOTE_PORT") or 6466)

# PING | KEY | IME | VOICE | POWER | VOLUME | APP_LINK (623, as a real Google TV announces).
SERVER_FEATURES = 1 | 2 | 4 | 8 | 32 | 64 | 512

# Android key code name -> Apple TV command
KEY_MAP = {
    "KEYCODE_DPAD_UP": "up",
    "KEYCODE_DPAD_DOWN": "down",
    "KEYCODE_DPAD_LEFT": "left",
    "KEYCODE_DPAD_RIGHT": "right",
    "KEYCODE_DPAD_CENTER": "select",
    "KEYCODE_ENTER": "select",
    "KEYCODE_NUMPAD_ENTER": "select",
    "KEYCODE_BUTTON_A": "select",
    "KEYCODE_BACK": "menu",
    "KEYCODE_ESCAPE": "menu",
    "KEYCODE_HOME": "home",
    "KEYCODE_MENU": "control_center",
    "KEYCODE_SETTINGS": "control_center",
    "KEYCODE_GUIDE": "top_menu",
    "KEYCODE_MEDIA_PLAY_PAUSE": "play_pause",
    "KEYCODE_MEDIA_PLAY": "play",
    "KEYCODE_MEDIA_PAUSE": "pause",
    "KEYCODE_MEDIA_STOP": "stop",
    "KEYCODE_MEDIA_NEXT": "next",
    "KEYCODE_MEDIA_PREVIOUS": "previous",
    "KEYCODE_MEDIA_FAST_FORWARD": "skip_forward",
    "KEYCODE_MEDIA_REWIND": "skip_backward",
    "KEYCODE_VOLUME_UP": "volume_up",
    "KEYCODE_VOLUME_DOWN": "volume_down",
    "KEYCODE_VOLUME_MUTE": "mute",
    "KEYCODE_MUTE": "mute",
    "KEYCODE_POWER": "power_toggle",
    "KEYCODE_TV_POWER": "power_toggle",
    "KEYCODE_SLEEP": "power_off",
    "KEYCODE_WAKEUP": "power_on",
    "KEYCODE_CHANNEL_UP": "channel_up",
    "KEYCODE_CHANNEL_DOWN": "channel_down",
}
# Keys the phone sends as press (START_LONG) ... release (END_LONG). A quick press is a
# tap; holding auto-repeats arrows and long-presses select/menu/home.
REPEATING = {"up", "down", "left", "right"}
LONG_PRESSABLE = {"select", "menu", "home"}
HOLD_DELAY = 0.5        # seconds before a press counts as held
REPEAT_INTERVAL = 0.15  # arrow auto-repeat rate while held
MAX_HOLD = 10.0         # stop repeating if the release never arrives

# Voice audio is 16-bit mono PCM. Google's app sends field 31 as raw bytes at 16 kHz;
# androidtvremote2 sends a RemoteVoicePayload message at 8 kHz.
VOICE_PAYLOAD_TAG = bytes([0xFA, 0x01])  # field 31, wire type 2
VOICE_RATE_RAW = 16000
VOICE_RATE_STRUCTURED = 8000
# The Google TV voice assistant; reported in remote_voice_begin like a real TV.
VOICE_PACKAGE = "com.google.android.katniss"
VOICE_MAX_SECONDS = 20.0  # end a voice session if the phone never sends remote_voice_end

# Level reported to phones when the Apple TV can only step volume up/down.
RELATIVE_VOLUME_PLACEHOLDER = 50
MAX_VOLUME_STEPS = 5


class _Press:
    def __init__(self, cmd: str, timer: asyncio.TimerHandle) -> None:
        self.cmd = cmd
        self.timer = timer
        self.held = False
        self.repeat: asyncio.Task | None = None

    def cancel(self) -> None:
        self.timer.cancel()
        if self.repeat:
            self.repeat.cancel()


def _status_msg(status: int = OuterMessage.Status.STATUS_OK) -> OuterMessage:
    msg = OuterMessage()
    msg.protocol_version = 2
    msg.status = status  # type: ignore[assignment]
    return msg


class GoogleTVServer:
    def __init__(self, store: Store, appletv: AppleTV) -> None:
        self.store = store
        self.appletv = appletv
        self.identity = ServerIdentity(store.data_dir, self._cert_cn())
        # Client certs seen on the remote port from unpaired phones, by IP.
        self.pending: dict[str, Any] = {}
        # The Apple TV's focused text field, as announced to phones (see ime.py).
        self.ime_field_id = 0      # 0 = no text field focused
        self._ime_field_seq = 0
        self.ime_text = ""
        self.ime_cursor = (0, 0)
        self.ime_text_counter = 0
        self._text_target: str | None = None
        self._text_task: asyncio.Task | None = None
        # Fingerprints of phones whose first pairing connection we dropped (see PairingSession).
        self.bounced: set[str] = set()
        # Current pairing (shown on the dashboard): {"code", "client", "ip", "started"}
        self.active_pairing: dict[str, Any] | None = None
        self.sessions: set[RemoteSession] = set()
        self._servers: list[asyncio.base_events.Server] = []
        self._zc: AsyncZeroconf | None = None
        self._services: list[ServiceInfo] = []
        self.on_change: Callable[[], None] = lambda: None
        # Voice sessions: begin(phone ip), each audio chunk(pcm, sample_rate) as it arrives,
        # then on_voice(all pcm, sample_rate, phone ip) when the session ends.
        self.on_voice_begin: Callable[[str], None] = lambda phone: None
        self.on_voice_chunk: Callable[[bytes, int], None] = lambda pcm, rate: None
        self.on_voice: Callable[[bytes, int, str], None] = self._save_voice
        appletv.on_change(self._on_appletv_change)
        appletv.on_keyboard(self._on_keyboard)

    def _cert_cn(self) -> str:
        atv = self.store.appletv or {}
        mac = atv.get("mac") or "00:00:00:00:00:00"
        return f"atvremote/appletv/appletv/{self.store.display_name()}/{mac}"

    # --- TLS contexts --------------------------------------------------------------
    def context_for(self, e: int, verify):  # noqa: ANN001
        key, cert = self.identity.get(e)
        return make_context(key, cert, verify)

    def exponent_for_pairing(self, ip: str) -> tuple[int, str | None]:
        """Pick the server key for a pairing connection from `ip`."""
        cert = self.pending.get(ip)
        if self.store.data.get("easy_pairing", True) and cert is not None:
            e = self.identity.find_exponent_for_code(cert, EASY_CODE)
            if e:
                return e, fingerprint(cert)
        return DEFAULT_EXPONENT, None

    def exponent_for_remote(self, ip: str) -> int:
        match = self.store.phone_for_ip(ip)
        return match[1].get("server_e", DEFAULT_EXPONENT) if match else DEFAULT_EXPONENT

    # --- lifecycle ------------------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._servers.append(await loop.create_server(lambda: PairingSession(self), "0.0.0.0", PAIRING_PORT))
        self._servers.append(await loop.create_server(lambda: RemoteSession(self), "0.0.0.0", REMOTE_PORT))
        LOG.info("Google TV remote server listening on %s (remote) and %s (pairing)", REMOTE_PORT, PAIRING_PORT)
        await self.advertise()

    async def advertise(self) -> None:
        """(Re)announce the bridge on the network as a Google TV."""
        if self._zc is None:
            self._zc = AsyncZeroconf()
        for info in self._services:
            await self._zc.async_unregister_service(info)
        self._services.clear()
        ip = host_ip()
        name = self.store.display_name()
        atv = self.store.appletv or {}
        mac = atv.get("mac") or "00:00:00:00:00:00"
        self._services.append(ServiceInfo(
            "_androidtvremote2._tcp.local.",
            f"{name}._androidtvremote2._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=REMOTE_PORT,
            properties={"bt": mac},
            server=f"atvbridge-{mac.replace(':', '').lower()}.local.",
        ))
        dash_port = int(os.environ.get("ATVB_WEB_PORT") or 8090)
        self._services.append(ServiceInfo(
            "_atvbridge._tcp.local.",
            f"{name}._atvbridge._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=dash_port,
            properties={"name": name, "path": "/ws"},
            server=f"atvbridge-{mac.replace(':', '').lower()}.local.",
        ))
        for info in self._services:
            await self._zc.async_register_service(info, allow_name_change=True, strict=False)
        LOG.info("Advertising '%s' on %s", name, ip)

    async def stop(self) -> None:
        for s in self._servers:
            s.close()
        if self._zc:
            for info in self._services:
                await self._zc.async_unregister_service(info)
            await self._zc.async_close()

    def _save_voice(self, pcm: bytes, rate: int, phone: str) -> None:
        voice.save_wav(pcm, rate, self.store.data_dir / "voice" / "last.wav")

    # --- Apple TV -> phones ---------------------------------------------------------
    def _on_appletv_change(self, state: dict[str, Any]) -> None:
        for s in list(self.sessions):
            s.push_state()

    def _on_keyboard(self, focused: bool, text: str | None) -> None:
        if focused:
            self._ime_field_seq += 1
            self.ime_field_id = self._ime_field_seq
            self.ime_text = text or ""
            self.ime_cursor = (len(self.ime_text),) * 2  # type: ignore[assignment]
            self.ime_text_counter += 1
        else:
            self.ime_field_id = 0
        for s in list(self.sessions):
            s.push_keyboard()

    def ime_start_frames(self) -> list[bytes]:
        """StartInput + ShowHide describing the current field, as a Google TV sends them."""
        app = (self.appletv.state.get("app") or {}).get("id") or ""
        if not self.ime_field_id:
            return [ime.start_input(0, app), ime.show_hide(False)]
        return [
            ime.start_input(self.ime_field_id, app, self.ime_text_counter, self.ime_text, hint="Search"),
            ime.show_hide(True),
        ]

    def apply_phone_edit(self, session: RemoteSession, batch: ime.BatchEdit) -> None:
        if not self.ime_field_id or batch.field_id != self.ime_field_id:
            LOG.debug("Ignoring edit for field %s (current %s)", batch.field_id, self.ime_field_id)
            return
        text, cursor = ime.apply_edits(self.ime_text, self.ime_cursor, batch.edits)
        for e in batch.edits:
            if e.kind == "action":
                LOG.info("Phone pressed the keyboard's action key (%s)", e.action)
        if text == self.ime_text and cursor == self.ime_cursor:
            return
        self.ime_cursor = cursor
        if text != self.ime_text:
            self.ime_text = text
            self.ime_text_counter += 1
            self._sync_text(text)
        for s in list(self.sessions):
            s.send_raw(ime.text_update(self.ime_text_counter, text, *cursor, own_edit=s is session))

    def _sync_text(self, text: str) -> None:
        """Send the field's text to the Apple TV; fast typing collapses to the latest text."""
        self._text_target = text
        if self._text_task is None or self._text_task.done():
            self._text_task = asyncio.get_event_loop().create_task(self._text_worker())

    async def _text_worker(self) -> None:
        sent = None
        while self._text_target != sent:
            sent = self._text_target
            try:
                await self.appletv.command("type_text", text=sent)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Typing on the Apple TV failed: %s", exc)
                return


class PairingSession(TLSConnection):
    def __init__(self, server: GoogleTVServer) -> None:
        super().__init__()
        self.server = server
        self.framing = ProtobufFraming()
        self.server_e = DEFAULT_EXPONENT
        self.expected_fp: str | None = None
        self.secret: bytes | None = None
        self.client_name = ""
        self.last_step = "connected"

    def select_context(self):  # noqa: ANN201
        self.server_e, self.expected_fp = self.server.exponent_for_pairing(self.peer_ip)
        return self.server.context_for(self.server_e, lambda conn, cert: True)

    def on_ready(self) -> None:
        LOG.info("Pairing connection from %s", self.peer_ip)
        if self.peer_cert is None:
            return
        # Remember the cert so a retry from this phone can use the easy code.
        self.server.pending[self.peer_ip] = self.peer_cert
        fp = fingerprint(self.peer_cert)
        if (self.server.store.data.get("easy_pairing", True) and fp != self.expected_fp
                and fp not in self.server.bounced):
            # Phones that go straight to the pairing port reach us before we know their cert,
            # so this connection's key can't make 000000 valid. Drop it once; the retry can.
            self.server.bounced.add(fp)
            LOG.info("New phone at %s; closing so its retry gets code %s", self.peer_ip, EASY_CODE)
            self.close()

    def send(self, msg: OuterMessage) -> None:
        self.write(ProtobufFraming.encode(msg.SerializeToString()))

    def on_data(self, data: bytes) -> None:
        for raw in self.framing.feed(data):
            msg = OuterMessage()
            msg.ParseFromString(raw)
            self.handle(msg)

    def handle(self, msg: OuterMessage) -> None:
        LOG.debug("Pairing message from %s: %s", self.peer_ip, str(msg).replace("\n", " "))
        if msg.status != OuterMessage.Status.STATUS_OK:
            LOG.info("Phone %s aborted pairing (status %s, last step: %s)",
                     self.peer_ip, OuterMessage.Status.Name(msg.status), self.last_step)
            self.close()
            return
        reply = _status_msg()
        if msg.HasField("pairing_request"):
            self.last_step = "pairing_request"
            self.client_name = msg.pairing_request.client_name
            reply.pairing_request_ack.server_name = self.server.store.display_name()
        elif msg.HasField("options"):
            self.last_step = "options"
            # The phone picks a configuration from the intersection of our output encodings
            # with its input encodings (Polo OptionsMessage.getBestConfiguration), so the
            # server must offer the code as *output*: we display it, the phone types it.
            # Input is offered too, for clients that match the other way round.
            for encodings in (reply.options.output_encodings, reply.options.input_encodings):
                enc = encodings.add()
                enc.type = Options.Encoding.ENCODING_TYPE_HEXADECIMAL
                enc.symbol_length = 6
            reply.options.preferred_role = Options.RoleType.ROLE_TYPE_OUTPUT
        elif msg.HasField("configuration"):
            self.last_step = "configuration"
            self._start_code()
            reply.configuration_ack.SetInParent()
        elif msg.HasField("secret"):
            self.last_step = "secret"
            if self.secret is not None and secrets.compare_digest(msg.secret.secret, self.secret):
                reply.secret_ack.secret = self.secret
                self._paired()
            else:
                LOG.info("Wrong pairing code from %s", self.peer_ip)
                self.send(_status_msg(OuterMessage.Status.STATUS_BAD_SECRET))
                return
        else:
            LOG.debug("Unhandled pairing message: %s", msg)
            self.send(_status_msg(OuterMessage.Status.STATUS_ERROR))
            return
        self.send(reply)

    def _start_code(self) -> None:
        assert self.peer_cert is not None
        fp = fingerprint(self.peer_cert)
        if self.expected_fp == fp:
            nonce = bytes.fromhex(EASY_CODE[2:])
        else:
            nonce = secrets.token_bytes(2)
        code, self.secret = self.server.identity.pairing_code_for(self.peer_cert, self.server_e, nonce)
        self.server.active_pairing = {
            "code": code,
            "client": self.client_name or "Android device",
            "ip": self.peer_ip,
            "started": time.time(),
            "easy": code == EASY_CODE,
        }
        LOG.info("Pairing code for %s (%s): %s", self.client_name, self.peer_ip, code)
        self.server.on_change()

    def _paired(self) -> None:
        assert self.peer_cert is not None
        fp = fingerprint(self.peer_cert)
        pem = self.peer_cert.public_bytes(serialization.Encoding.PEM).decode()
        self.server.store.add_phone(fp, self.client_name, pem, self.server_e, self.peer_ip)
        self.server.pending.pop(self.peer_ip, None)
        self.server.active_pairing = None
        self.server.on_change()
        LOG.info("Paired phone '%s' (%s)", self.client_name, self.peer_ip)

    def on_closed(self) -> None:
        ap = self.server.active_pairing
        if ap and ap.get("ip") == self.peer_ip:
            self.server.active_pairing = None
            self.server.on_change()


class RemoteSession(TLSConnection):
    def __init__(self, server: GoogleTVServer) -> None:
        super().__init__()
        self.server = server
        self.framing = ProtobufFraming()
        self.fp: str | None = None
        self.active = False
        self._ping_task: asyncio.Task | None = None
        self._last_rx = time.monotonic()
        self._ping_n = 0
        self._press: _Press | None = None
        self._voice_id: int | None = None
        self._voice_pcm = bytearray()
        self._voice_rate = VOICE_RATE_STRUCTURED
        self._voice_timeout: asyncio.TimerHandle | None = None

    def select_context(self):  # noqa: ANN201
        return self.server.context_for(self.server.exponent_for_remote(self.peer_ip), self._verify)

    def _verify(self, conn, cert) -> bool:  # noqa: ANN001
        fp = fingerprint(cert)
        if fp in self.server.store.phones:
            return True
        # Unknown phone: remember its cert (for easy pairing) and refuse,
        # which tells the phone it needs to pair first.
        self.server.pending[self.peer_ip] = cert
        LOG.info("Unpaired phone at %s; asking it to pair", self.peer_ip)
        return False

    def send(self, msg: RemoteMessage) -> None:
        self.write(ProtobufFraming.encode(msg.SerializeToString()))

    def send_raw(self, frame: bytes) -> None:
        self.write(ProtobufFraming.encode(frame))

    def on_ready(self) -> None:
        assert self.peer_cert is not None
        self.fp = fingerprint(self.peer_cert)
        self.server.store.touch_phone(self.fp, self.peer_ip)
        self.server.sessions.add(self)
        LOG.info("Remote connected: %s (%s)", self.server.store.phones[self.fp]["name"], self.peer_ip)
        msg = RemoteMessage()
        msg.remote_configure.code1 = SERVER_FEATURES
        info = msg.remote_configure.device_info
        info.model = "Apple TV"
        info.vendor = "Apple"
        # Identify as Google's own remote service (values from a real Google TV), in case
        # the phone app gates features on it.
        info.unknown1 = 1
        info.unknown2 = "14"  # Android version
        info.package_name = "com.google.android.tv.remote.service"
        info.app_version = "6.1.683155326"
        self.send(msg)
        self._ping_task = asyncio.get_event_loop().create_task(self._pinger())
        self.server.on_change()

    def on_closed(self) -> None:
        if self in self.server.sessions:
            self.server.sessions.discard(self)
            self.server.on_change()
        if self._ping_task:
            self._ping_task.cancel()
        self._cancel_press()
        if self._voice_timeout:
            self._voice_timeout.cancel()


    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - self._last_rx > 20:
                LOG.info("Remote at %s timed out", self.peer_ip)
                self.close()
                return
            self._ping_n += 1
            msg = RemoteMessage()
            msg.remote_ping_request.val1 = self._ping_n
            self.send(msg)

    def on_data(self, data: bytes) -> None:
        self._last_rx = time.monotonic()
        for raw in self.framing.feed(data):
            if self._voice_id is not None and raw.startswith(VOICE_PAYLOAD_TAG) and self._raw_voice(raw):
                continue
            if ime.top_field(raw) == ime.BATCH_EDIT:
                try:
                    batch = ime.parse_batch_edit(raw)
                except ValueError as exc:
                    LOG.debug("Bad keyboard edit from %s: %s", self.peer_ip, exc)
                    continue
                LOG.debug("Keyboard edit from %s: %s", self.peer_ip, batch)
                self.server.apply_phone_edit(self, batch)
                continue
            msg = RemoteMessage()
            try:
                msg.ParseFromString(raw)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Unparseable %d-byte message from %s (%s): %s", len(raw), self.peer_ip, exc, raw[:64].hex())
                continue
            self.handle(msg)

    def handle(self, msg: RemoteMessage) -> None:
        if not (msg.HasField("remote_ping_request") or msg.HasField("remote_ping_response")
                or msg.HasField("remote_voice_payload")):
            LOG.debug("Remote message from %s: %s", self.peer_ip, str(msg).replace("\n", " "))
        if msg.HasField("remote_configure"):
            reply = RemoteMessage()
            reply.remote_set_active.SetInParent()
            self.send(reply)
        elif msg.HasField("remote_set_active"):
            self.active = True
            self.push_state(initial=True)
            if self.server.ime_field_id:
                self.push_keyboard()
        elif msg.HasField("remote_ping_response"):
            pass
        elif msg.HasField("remote_ping_request"):
            reply = RemoteMessage()
            reply.remote_ping_response.val1 = msg.remote_ping_request.val1
            self.send(reply)
        elif msg.HasField("remote_key_inject"):
            self._key(msg.remote_key_inject.key_code, msg.remote_key_inject.direction)
        elif msg.HasField("remote_adjust_volume_level"):
            pass  # carries no direction; volume keys arrive as KEYCODE_VOLUME_* instead
        elif msg.HasField("remote_set_volume_level"):
            self._set_volume(msg.remote_set_volume_level.volume_level)
        elif msg.HasField("remote_app_link_launch_request"):
            self._run("launch", app=msg.remote_app_link_launch_request.app_link)
        elif msg.HasField("remote_voice_begin"):
            pass  # the phone echoing our session id: it starts streaming next
        elif msg.HasField("remote_voice_payload"):
            payload = msg.remote_voice_payload
            if payload.session_id == self._voice_id:
                self._voice_pcm += payload.samples
                self._voice_rate = VOICE_RATE_STRUCTURED
                self._voice_chunk(payload.samples, VOICE_RATE_STRUCTURED)
            else:
                LOG.debug("Voice payload for session %s (current %s), %d bytes",
                          payload.session_id, self._voice_id, len(payload.samples))
        elif msg.HasField("remote_voice_end"):
            if msg.remote_voice_end.session_id == self._voice_id:
                self._end_voice()
        elif msg.HasField("remote_ime_show_request"):
            pass  # phone asking to show/hide the TV keyboard (field 22); nothing to do
        else:
            LOG.debug("Unhandled remote message: %s", msg)

    def _key(self, code: int, direction: int) -> None:
        name = RemoteKeyCode.Name(code)
        if name in ("KEYCODE_SEARCH", "KEYCODE_ASSIST", "KEYCODE_VOICE_ASSIST"):
            if direction != RemoteDirection.END_LONG:
                self._start_voice()
            return
        cmd = KEY_MAP.get(name)
        if cmd is None:
            LOG.debug("No mapping for %s", name)
            return
        if direction == RemoteDirection.END_LONG:
            self._release(cmd)
        elif direction == RemoteDirection.START_LONG and cmd in REPEATING | LONG_PRESSABLE:
            # A new press while another is down cancels the earlier one. This is how swipes on
            # the phone's touchpad arrive: select down, then a direction, then select up.
            self._cancel_press()
            loop = asyncio.get_event_loop()
            self._press = _Press(cmd, loop.call_later(HOLD_DELAY, self._held))
        else:
            self._cancel_press()
            self._run(cmd)

    def _held(self) -> None:
        press = self._press
        if press is None:
            return
        press.held = True
        if press.cmd in REPEATING:
            press.repeat = asyncio.get_event_loop().create_task(self._repeat(press.cmd))
        else:
            self._run(press.cmd, hold=True)

    async def _repeat(self, cmd: str) -> None:
        deadline = time.monotonic() + MAX_HOLD
        while time.monotonic() < deadline:
            self._run(cmd)
            await asyncio.sleep(REPEAT_INTERVAL)

    def _release(self, cmd: str) -> None:
        press = self._press
        if press is None or press.cmd != cmd:
            return  # e.g. select released after a swipe already cancelled it
        self._press = None
        press.cancel()
        if not press.held:
            self._run(cmd)

    def _cancel_press(self) -> None:
        if self._press:
            self._press.cancel()
            self._press = None

    # --- voice ------------------------------------------------------------------------
    def _raw_voice(self, raw: bytes) -> bool:
        """Take audio from a field-31 frame that holds raw PCM rather than the
        RemoteVoicePayload message our proto declares (Google's app does this)."""
        length, pos, shift = 0, len(VOICE_PAYLOAD_TAG), 0
        while pos < len(raw):
            b = raw[pos]
            length |= (b & 0x7F) << shift
            pos += 1
            if not b & 0x80:
                break
            shift += 7
        if pos + length != len(raw):
            return False
        body = raw[pos:]
        try:
            structured = RemoteMessage.FromString(raw).remote_voice_payload
            if structured.session_id == self._voice_id and structured.samples:
                return False  # a real RemoteVoicePayload: let handle() take it
        except Exception:  # noqa: BLE001
            pass
        self._voice_pcm += body
        self._voice_rate = VOICE_RATE_RAW
        self._voice_chunk(body, VOICE_RATE_RAW)
        return True

    def _voice_chunk(self, pcm: bytes, rate: int) -> None:
        try:
            self.server.on_voice_chunk(pcm, rate)
        except Exception:  # noqa: BLE001
            LOG.exception("Voice chunk handler failed")

    def _start_voice(self) -> None:
        if self._voice_id is not None:
            self._end_voice()  # mic pressed again: finish the current utterance
            return
        self._voice_id = secrets.randbelow(2**31 - 1) + 1
        self._voice_pcm = bytearray()
        self._voice_timeout = asyncio.get_event_loop().call_later(VOICE_MAX_SECONDS, self._end_voice)
        msg = RemoteMessage()
        msg.remote_voice_begin.session_id = self._voice_id
        msg.remote_voice_begin.package_name = VOICE_PACKAGE
        self.send(msg)
        LOG.info("Voice session %s started by %s", self._voice_id, self.peer_ip)
        try:
            self.server.on_voice_begin(self.peer_ip)
        except Exception:  # noqa: BLE001
            LOG.exception("Voice begin handler failed")

    def _end_voice(self) -> None:
        if self._voice_id is None:
            return
        session_id, self._voice_id = self._voice_id, None
        if self._voice_timeout:
            self._voice_timeout.cancel()
            self._voice_timeout = None
        pcm = voice.trim_padding(bytes(self._voice_pcm))
        rate = self._voice_rate
        self._voice_pcm = bytearray()
        self._voice_rate = VOICE_RATE_STRUCTURED
        msg = RemoteMessage()
        msg.remote_voice_end.session_id = session_id
        self.send(msg)
        duration, rms = voice.stats(pcm, rate)
        LOG.info("Voice session %s ended: %.1f s of audio at %d Hz, level %.3f", session_id, duration, rate, rms)
        if pcm:
            try:
                self.server.on_voice(pcm, rate, self.peer_ip)
            except Exception:  # noqa: BLE001
                LOG.exception("Voice handler failed")

    def _run(self, cmd: str, **kwargs: Any) -> None:
        async def go() -> None:
            try:
                await self.server.appletv.command(cmd, **kwargs)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Command %s failed: %s", cmd, exc)

        LOG.debug("%s -> %s %s", self.peer_ip, cmd, kwargs or "")
        asyncio.get_event_loop().create_task(go())

    # --- Apple TV state -> phone ----------------------------------------------------
    def push_state(self, initial: bool = False) -> None:
        if not self.active:
            return
        st = self.server.appletv.state
        app = (st.get("app") or {}).get("id") or ""
        msg = RemoteMessage()
        msg.remote_ime_key_inject.app_info.app_package = app
        self.send(msg)
        self._push_volume()
        msg = RemoteMessage()
        msg.remote_start.started = st.get("power") != "off"
        self.send(msg)

    def _push_volume(self) -> None:
        """Report volume like a real Google TV does on connect; phones may rely on it to
        route their volume buttons to the TV. Relative-only Apple TVs get a placeholder."""
        st = self.server.appletv.state
        vol = st.get("volume")
        if vol is None and st.get("volume_control") == "relative":
            vol = RELATIVE_VOLUME_PLACEHOLDER
        if vol is None:
            return
        msg = RemoteMessage()
        v = msg.remote_set_volume_level
        # Meaning of unknown1/2/4/5 is undocumented; 1s plus a level is what the Google TV
        # app was seen accepting (its volume buttons only send keys after a real-TV-like
        # remote_configure, see on_ready).
        v.unknown1 = v.unknown2 = v.unknown4 = v.unknown5 = 1
        v.player_model = self.server.store.display_name()
        v.volume_max = 100
        v.volume_level = int(vol)
        v.volume_muted = bool(st.get("muted"))
        self.send(msg)

    def _set_volume(self, level: int) -> None:
        st = self.server.appletv.state
        if st.get("volume_control") != "relative":
            self._run("set_volume", level=level)
            return
        # Phone dragged a volume slider, but the Apple TV can only step: turn the change
        # relative to the placeholder into up/down presses, then re-centre the slider.
        steps = max(-MAX_VOLUME_STEPS, min(MAX_VOLUME_STEPS, level - RELATIVE_VOLUME_PLACEHOLDER))
        for _ in range(abs(steps)):
            self._run("volume_up" if steps > 0 else "volume_down")
        self._push_volume()

    def push_keyboard(self) -> None:
        """Tell the phone the Apple TV's text field opened/closed, so it offers to type."""
        if not self.active:
            return
        for frame in self.server.ime_start_frames():
            self.send_raw(frame)
        if self.server.ime_field_id:
            LOG.info("Told %s a text field is focused (text %r)", self.peer_ip, self.server.ime_text)

def host_ip() -> str:
    """LAN IP to advertise. Override with ATVB_HOST_IP if detection is wrong."""
    if os.environ.get("ATVB_HOST_IP"):
        return os.environ["ATVB_HOST_IP"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
