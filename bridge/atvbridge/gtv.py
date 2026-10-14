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

from .certs import DEFAULT_EXPONENT, EASY_CODE, ServerIdentity, fingerprint
from .tls import ProtobufFraming, TLSConnection, make_context

if TYPE_CHECKING:
    from .appletv import AppleTV
    from .store import Store

LOG = logging.getLogger(__name__)

PAIRING_PORT = int(os.environ.get("ATVB_PAIRING_PORT", 6467))
REMOTE_PORT = int(os.environ.get("ATVB_REMOTE_PORT", 6466))

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
        # Fingerprints of phones whose first pairing connection we dropped (see PairingSession).
        self.bounced: set[str] = set()
        # Current pairing (shown on the dashboard): {"code", "client", "ip", "started"}
        self.active_pairing: dict[str, Any] | None = None
        self.sessions: set[RemoteSession] = set()
        self._servers: list[asyncio.base_events.Server] = []
        self._zc: AsyncZeroconf | None = None
        self._services: list[ServiceInfo] = []
        self.on_change: Callable[[], None] = lambda: None
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
        dash_port = int(os.environ.get("ATVB_WEB_PORT", 8090))
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

    # --- Apple TV -> phones ---------------------------------------------------------
    def _on_appletv_change(self, state: dict[str, Any]) -> None:
        for s in list(self.sessions):
            s.push_state()

    def _on_keyboard(self, focused: bool) -> None:
        for s in list(self.sessions):
            s.push_keyboard(focused)


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
        self._ime_counter = 0
        self._field_counter = 0
        self._press: _Press | None = None

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
            msg = RemoteMessage()
            try:
                msg.ParseFromString(raw)
            except Exception:  # noqa: BLE001
                continue
            self.handle(msg)

    def handle(self, msg: RemoteMessage) -> None:
        if not (msg.HasField("remote_ping_request") or msg.HasField("remote_ping_response")):
            LOG.debug("Remote message from %s: %s", self.peer_ip, str(msg).replace("\n", " "))
        if msg.HasField("remote_configure"):
            reply = RemoteMessage()
            reply.remote_set_active.SetInParent()
            self.send(reply)
        elif msg.HasField("remote_set_active"):
            self.active = True
            self.push_state(initial=True)
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
        elif msg.HasField("remote_ime_batch_edit"):
            edits = msg.remote_ime_batch_edit.edit_info
            if edits:
                self._run("text_set", text=edits[-1].text_field_status.value)
        else:
            LOG.debug("Unhandled remote message: %s", msg)

    def _key(self, code: int, direction: int) -> None:
        name = RemoteKeyCode.Name(code)
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
        v.volume_muted = False
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

    def push_keyboard(self, focused: bool) -> None:
        if not self.active or not focused:
            return
        self._ime_counter += 1
        self._field_counter += 1
        msg = RemoteMessage()
        status = msg.remote_ime_show_request.remote_text_field_status
        status.counter_field = self._field_counter
        status.value = ""
        status.start = 0
        status.end = 0
        self.send(msg)


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
