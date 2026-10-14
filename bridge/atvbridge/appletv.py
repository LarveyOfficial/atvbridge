"""Apple TV connection via pyatv (Companion + AirPlay/MRP)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import pyatv
from pyatv.const import (
    DeviceState, FeatureName, FeatureState, InputAction, KeyboardFocusState, PowerState, Protocol,
)
from pyatv.interface import (
    AudioListener,
    DeviceListener,
    KeyboardListener,
    PowerListener,
    PushListener,
)

from . import rti
from .store import Store

LOG = logging.getLogger(__name__)

PROTOCOLS = {"companion": Protocol.Companion, "airplay": Protocol.AirPlay}
HID_MUTE = (0x0C, 0xE2)  # USB HID consumer page: Mute


class AppleTV(DeviceListener, PushListener, PowerListener, AudioListener, KeyboardListener):
    """Keeps a live connection to the Apple TV and exposes a simple state dict."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.atv: pyatv.interface.AppleTV | None = None
        self.loop = asyncio.get_event_loop()
        self.state: dict[str, Any] = {"connected": False}
        self.artwork: tuple[bytes, str] | None = None
        self._artwork_id: str | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        # cb(focused, current text of the focused field or None)
        self._keyboard_listeners: list[Callable[[bool, str | None], None]] = []
        self._task: asyncio.Task | None = None
        self._lost = asyncio.Event()
        self.keyboard_focused = False
        self._focus_changed = asyncio.Event()
        # Text input session for the focused field: (session uuid, text we know is in it).
        self._ti_session: bytes | None = None
        self._ti_text: str | None = None
        self._apps: dict[str, str] = {}
        self._apps_at = 0.0

    # --- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        pending = self._close()
        if pending:
            await asyncio.wait(pending, timeout=5)

    async def restart(self) -> None:
        await self.stop()
        self.start()

    async def _run(self) -> None:
        delay = 2
        while True:
            cfg = self.store.appletv
            if not cfg or not cfg.get("credentials"):
                self._set(connected=False, error="Not configured")
                await asyncio.sleep(5)
                continue
            try:
                await self._connect(cfg)
                delay = 2
                self._lost.clear()
                await self._lost.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Apple TV connection failed: %s", exc)
                self._set(connected=False, error=str(exc))
            pending = self._close()
            if pending:
                await asyncio.wait(pending, timeout=5)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _connect(self, cfg: dict[str, Any]) -> None:
        confs = await pyatv.scan(self.loop, hosts=[cfg["address"]], timeout=5)
        if not confs:
            raise ConnectionError(f"Apple TV not found at {cfg['address']}")
        conf = confs[0]
        for name, creds in cfg["credentials"].items():
            if creds and name in PROTOCOLS:
                conf.set_credentials(PROTOCOLS[name], creds)
        self.atv = await pyatv.connect(conf, self.loop)
        self.atv.listener = self
        self.atv.push_updater.listener = self
        self.atv.push_updater.start()
        self.atv.power.listener = self
        self.atv.audio.listener = self
        try:
            self.atv.keyboard.listener = self
        except Exception:  # noqa: BLE001
            pass
        try:
            self.keyboard_focused = self.atv.keyboard.text_focus_state == KeyboardFocusState.Focused
        except Exception:  # noqa: BLE001
            self.keyboard_focused = False
        LOG.info("Connected to Apple TV %s (%s)", conf.name, conf.address)
        self._set(connected=True, error=None, name=conf.name, power=self._power_str(),
                  volume_control=self._volume_control(), volume=None)
        await self._refresh_now_playing()

    def _close(self) -> set[asyncio.Task]:
        pending: set[asyncio.Task] = set()
        if self.atv:
            try:
                self.atv.push_updater.stop()
                pending = self.atv.close() or set()
            except Exception:  # noqa: BLE001
                pass
        self.atv = None
        self._set(connected=False)
        return pending

    # --- listeners -----------------------------------------------------------------
    def connection_lost(self, exception: Exception) -> None:
        LOG.warning("Apple TV connection lost: %s", exception)
        self._lost.set()

    def connection_closed(self) -> None:
        self._lost.set()

    def playstatus_update(self, updater, playstatus) -> None:  # noqa: ANN001
        self.loop.create_task(self._refresh_now_playing(playstatus))

    def playstatus_error(self, updater, exception: Exception) -> None:  # noqa: ANN001
        LOG.debug("Push update error: %s", exception)

    def powerstate_update(self, old_state: PowerState, new_state: PowerState) -> None:
        self._set(power=self._power_str())

    def volume_update(self, old_level: float, new_level: float) -> None:
        if self.state.get("volume_control") == "absolute":
            self._set(volume=new_level)

    def volume_device_update(self, output_device, old_level: float, new_level: float) -> None:  # noqa: ANN001
        pass

    def outputdevices_update(self, old_devices, new_devices) -> None:  # noqa: ANN001
        pass

    def focusstate_update(self, old_state: KeyboardFocusState, new_state: KeyboardFocusState) -> None:
        focused = new_state == KeyboardFocusState.Focused
        self.keyboard_focused = focused
        self._focus_changed.set()
        LOG.info("Apple TV keyboard %s", "focused" if focused else "unfocused")
        self.loop.create_task(self._notify_keyboard(focused))

    async def _notify_keyboard(self, focused: bool) -> None:
        text = None
        self._ti_session = self._ti_text = None
        if focused and self.atv:
            try:
                text = await self._start_text_session()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Starting text input session failed: %s", exc)
        for cb in list(self._keyboard_listeners):
            try:
                cb(focused, text)
            except Exception:  # noqa: BLE001
                LOG.exception("keyboard listener failed")

    # --- state -------------------------------------------------------------------
    def on_change(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(cb)

    def on_keyboard(self, cb: Callable[[bool, str | None], None]) -> None:
        self._keyboard_listeners.append(cb)

    def _set(self, **kwargs: Any) -> None:
        self.state.update(kwargs)
        for cb in list(self._listeners):
            try:
                cb(self.state)
            except Exception:  # noqa: BLE001
                LOG.exception("state listener failed")

    def _volume_control(self) -> str | None:
        """"absolute" if the level can be read and set, "relative" for up/down steps only
        (typical: Apple TV driving the TV over HDMI-CEC/IR), None if not controllable."""
        assert self.atv is not None
        features = self.atv.features
        if all(features.in_state(FeatureState.Available, f) for f in (FeatureName.Volume, FeatureName.SetVolume)):
            return "absolute"
        if features.in_state(FeatureState.Available, FeatureName.VolumeUp):
            return "relative"
        return None

    def _power_str(self) -> str:
        try:
            return "on" if self.atv and self.atv.power.power_state == PowerState.On else "off"
        except Exception:  # noqa: BLE001
            return "unknown"

    async def _refresh_now_playing(self, playing=None) -> None:  # noqa: ANN001
        if not self.atv:
            return
        try:
            if playing is None:
                playing = await self.atv.metadata.playing()
            app = None
            try:
                a = self.atv.metadata.app
                app = {"name": a.name, "id": a.identifier} if a else None
            except Exception:  # noqa: BLE001
                pass
            state = playing.device_state
            now_playing = {
                "title": playing.title,
                "artist": playing.artist,
                "album": playing.album,
                "series": getattr(playing, "series_name", None),
                "state": state.name.lower() if state else "idle",
                "position": playing.position,
                "total": playing.total_time,
                "media_type": playing.media_type.name.lower() if playing.media_type else None,
                "updated_at": time.time(),
                "artwork_id": None,
            }
            art_id = self.atv.metadata.artwork_id
            if art_id and art_id != self._artwork_id:
                art = await self.atv.metadata.artwork(width=512, height=512)
                self.artwork = (art.bytes, art.mimetype) if art else None
                self._artwork_id = art_id if art else None
            elif not art_id:
                self.artwork, self._artwork_id = None, None
            now_playing["artwork_id"] = self._artwork_id
            active = state not in (None, DeviceState.Idle, DeviceState.Stopped)
            self._set(now_playing=now_playing if active else None, app=app, power=self._power_str())
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Failed to refresh now playing: %s", exc)

    async def app_names(self) -> dict[str, str]:
        """Installed apps as {display name: bundle id}, cached for 10 minutes."""
        if not self.atv:
            raise ConnectionError("Apple TV not connected")
        if not self._apps or time.monotonic() - self._apps_at > 600:
            self._apps = {a.name: a.identifier for a in await self.atv.apps.app_list() if a.name}
            self._apps_at = time.monotonic()
        return self._apps

    async def wait_keyboard_focus(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.keyboard_focused:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._focus_changed.clear()
            try:
                await asyncio.wait_for(self._focus_changed.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        return True

    def _companion_api(self):  # noqa: ANN202
        assert self.atv is not None
        keyboard = getattr(self.atv.keyboard, "_interfaces", {}).get(Protocol.Companion)
        api = getattr(keyboard, "api", None)
        if api is None:
            raise NotImplementedError("Companion text input is not available")
        return api

    async def _start_text_session(self) -> str:
        """Open (or join) the focused field's text input session; returns its current text."""
        from pyatv.protocols.companion import keyed_archiver

        response = await self._companion_api()._text_input_start()
        ti_data = response.get("_c", {}).get("_tiD")
        if ti_data is None:
            raise ConnectionError("no text input session")
        session, text = keyed_archiver.read_archive_properties(
            ti_data, ["sessionUUID"], ["documentState", "docSt", "contextBeforeInput"],
        )
        self._ti_session = bytes(session)
        self._ti_text = text or ""
        return self._ti_text

    async def type_text(self, text: str) -> None:
        """Make the focused field read `text`, sending only the difference (backspaces +
        new characters) instead of clearing and re-typing it like text_set does."""
        if self._ti_session is None or self._ti_text is None:
            await self._start_text_session()
        assert self._ti_session is not None and self._ti_text is not None
        delete, insert = rti.diff(self._ti_text, text)
        if not delete and not insert:
            return
        await self._companion_api()._send_event(
            "_tiC", {"_tiV": 1, "_tiD": rti.text_operation_payload(self._ti_session, insert, delete)},
        )
        self._ti_text = text

    async def _mrp_hid(self, usage_page: int, usage: int) -> None:
        """Press a HID key over MRP. pyatv only exposes a fixed set of keys (no mute), so this
        reaches into its MRP remote control; fails cleanly if pyatv's internals change."""
        from pyatv.protocols.mrp import messages

        assert self.atv is not None
        mrp = getattr(self.atv.remote_control, "_interfaces", {}).get(Protocol.MRP)
        protocol = getattr(mrp, "protocol", None)
        if protocol is None:
            raise NotImplementedError("MRP HID keys are not available")
        await protocol.send(messages.send_hid_event(usage_page, usage, True))
        await protocol.send(messages.send_hid_event(usage_page, usage, False))

    # --- commands -------------------------------------------------------------------
    async def command(self, name: str, hold: bool = False, **kwargs: Any) -> None:
        if not self.atv:
            raise ConnectionError("Apple TV not connected")
        rc = self.atv.remote_control
        action = InputAction.Hold if hold else InputAction.SingleTap
        if name in ("up", "down", "left", "right", "select", "menu", "home"):
            await getattr(rc, name)(action=action)
        elif name in ("play_pause", "play", "pause", "stop", "next", "previous",
                      "channel_up", "channel_down", "top_menu", "screensaver"):
            await getattr(rc, name)()
        elif name == "control_center":
            await rc.home(action=InputAction.Hold)
        elif name == "skip_forward":
            await rc.skip_forward(kwargs.get("seconds", 10))
        elif name == "skip_backward":
            await rc.skip_backward(kwargs.get("seconds", 10))
        elif name == "seek":
            await rc.set_position(int(kwargs["position"]))
        elif name == "volume_up":
            await self.atv.audio.volume_up()
        elif name == "volume_down":
            await self.atv.audio.volume_down()
        elif name == "mute":
            await self._mrp_hid(*HID_MUTE)
        elif name == "set_volume":
            await self.atv.audio.set_volume(float(kwargs["level"]))
        elif name == "power_on":
            await self.atv.power.turn_on()
        elif name == "power_off":
            await self.atv.power.turn_off()
        elif name == "power_toggle":
            if self.atv.power.power_state == PowerState.On:
                await self.atv.power.turn_off()
            else:
                await self.atv.power.turn_on()
        elif name == "type_text":
            await self.type_text(kwargs["text"])
        elif name == "text_set":
            if kwargs["text"]:
                await self.atv.keyboard.text_set(kwargs["text"])
            else:
                await self.atv.keyboard.text_clear()
        elif name == "launch":
            await self.atv.apps.launch_app(kwargs["app"])
        else:
            raise ValueError(f"Unknown command {name}")


# --- setup helpers (used by the dashboard) ---------------------------------------------

async def scan(address: str | None = None) -> list[dict[str, Any]]:
    loop = asyncio.get_event_loop()
    confs = await pyatv.scan(loop, hosts=[address] if address else None, timeout=5)
    out = []
    for c in confs:
        out.append({
            "name": c.name,
            "address": str(c.address),
            "identifier": c.identifier,
            "model": str(c.device_info.model_str) if c.device_info else None,
            "os": str(c.device_info.version) if c.device_info else None,
            "protocols": [s.protocol.name.lower() for s in c.services],
        })
    return out


class PairingSession:
    """Interactive pairing for one protocol (companion or airplay)."""

    def __init__(self, address: str, protocol: str) -> None:
        self.address = address
        self.protocol = protocol
        self.handler = None
        self.conf = None

    async def begin(self) -> None:
        loop = asyncio.get_event_loop()
        confs = await pyatv.scan(loop, hosts=[self.address], timeout=5)
        if not confs:
            raise ConnectionError(f"Apple TV not found at {self.address}")
        self.conf = confs[0]
        self.handler = await pyatv.pair(self.conf, PROTOCOLS[self.protocol], loop, name="Google TV Bridge")
        await self.handler.begin()

    async def finish(self, pin: str) -> str:
        assert self.handler is not None
        try:
            self.handler.pin(int(pin))
            await self.handler.finish()
            if not self.handler.has_paired:
                raise PermissionError("Pairing failed (wrong PIN?)")
            return self.handler.service.credentials
        finally:
            await self.cancel()

    async def cancel(self) -> None:
        if self.handler is not None:
            handler, self.handler = self.handler, None
            try:
                await handler.close()
            except Exception:  # noqa: BLE001
                pass
