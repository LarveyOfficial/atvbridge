"""Real volume from an LG webOS TV.

The Apple TV changes volume with HDMI-CEC/IR presses and can't read a level. An LG TV (also
when it drives a soundbar over HDMI ARC) reports the actual level over its local API, pushes
changes live (including from the LG remote) and can set it directly. When connected, volume
commands go to the TV and the level is real; otherwise the Apple TV's presses are used.

Configured by data/lg.json {"host", "client_key"} (or ATVB_LG_HOST); the first connection
shows an "allow" prompt on the TV.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)


class LGTV:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "lg.json"
        cfg: dict[str, Any] = {}
        if self._path.exists():
            cfg = json.loads(self._path.read_text())
        self.host: str | None = os.environ.get("ATVB_LG_HOST") or cfg.get("host")
        self._key: str | None = cfg.get("client_key")
        self.volume: int | None = None
        self.muted = False
        self.connected = False
        self.on_change: Callable[[], None] = lambda: None
        self._client: Any = None
        self._task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        return self.connected and self.volume is not None

    def start(self) -> None:
        if self.host and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        from aiowebostv import WebOsClient

        delay = 5
        while True:
            # First connection waits for the "allow" prompt on the TV to be accepted.
            client = WebOsClient(self.host, client_key=self._key, connect_timeout=10 if self._key else 60)
            try:
                await client.connect()
                if client.client_key != self._key:
                    self._key = client.client_key
                    self._path.write_text(json.dumps({"host": self.host, "client_key": self._key}))
                self._client = client
                await client.register_state_update_callback(self._state_update)
                LOG.info("Connected to LG TV at %s (volume %s, output %s)",
                         self.host, client.tv_state.volume, client.tv_state.sound_output)
                delay = 5
                while client.is_connected():
                    await asyncio.sleep(2)
                LOG.info("LG TV at %s disconnected", self.host)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.debug("LG TV at %s not reachable: %s", self.host, exc)
            finally:
                self._client = None
                if self.connected:
                    self.connected = False
                    self.on_change()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _state_update(self, tv_state: Any) -> None:
        volume = tv_state.volume
        muted = bool(tv_state.muted)
        connected = bool(tv_state.is_on) and volume is not None
        if (volume, muted, connected) != (self.volume, self.muted, self.connected):
            LOG.info("LG TV reports volume %s%s (output %s)", volume, " muted" if muted else "",
                     tv_state.sound_output)
            self.volume, self.muted, self.connected = volume, muted, connected
            self.on_change()

    # --- commands --------------------------------------------------------------------
    async def set_volume(self, level: float) -> None:
        await self._client.set_volume(max(0, min(100, int(round(level)))))

    async def step(self, delta: int) -> None:
        if self.volume is not None:
            await self.set_volume(self.volume + delta)

    async def toggle_mute(self) -> None:
        await self._client.set_mute(not self.muted)
