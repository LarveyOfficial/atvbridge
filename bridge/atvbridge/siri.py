"""Relay the phone's mic audio to real Siri on the Apple TV.

Uses the appletv-siri-voice bridge (https://github.com/marcusadolfsson/appletv-siri-voice),
which the Apple TV knows as a HomeKit Siri remote. Its control API takes raw PCM16 mono
16 kHz as a chunked POST body; the end of the body ends the utterance. We forward audio as
the phone sends it, so Siri hears it live and shows it on the TV like the Siri Remote.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

LOG = logging.getLogger(__name__)

SIRI_URL = os.environ.get("ATVB_SIRI_URL", "").rstrip("/")
SIRI_RATE = 16000


class SiriStream:
    """One utterance being streamed to Siri."""

    def __init__(self, session: aiohttp.ClientSession, url: str) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task = asyncio.get_running_loop().create_task(self._post(session, url))

    async def _body(self):  # noqa: ANN202
        while (chunk := await self._queue.get()) is not None:
            yield chunk

    async def _post(self, session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
        async with session.post(
            f"{url}/siri/stream", data=self._body(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=aiohttp.ClientTimeout(total=60, sock_connect=3),
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = {}
            if resp.status != 200 or not body.get("ok", False):
                raise SiriUnavailable(body.get("error") or f"HTTP {resp.status}")
            return body

    def feed(self, chunk: bytes) -> None:
        if not self._task.done():
            self._queue.put_nowait(chunk)

    @property
    def failed(self) -> bool:
        return self._task.done() and self._task.exception() is not None

    async def finish(self) -> dict[str, Any]:
        """End the utterance; returns the bridge's reply or raises SiriUnavailable."""
        self._queue.put_nowait(None)
        try:
            return await self._task
        except SiriUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SiriUnavailable(str(exc)) from exc

    def cancel(self) -> None:
        self._task.cancel()


class SiriUnavailable(Exception):
    pass


class SiriRelay:
    def __init__(self, url: str = SIRI_URL) -> None:
        self.url = url
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def start(self) -> SiriStream | None:
        if not self.enabled:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return SiriStream(self._session, self.url)

    async def state(self) -> dict[str, Any] | None:
        """The Siri bridge's /state (pairing / data stream status), or None if unreachable."""
        if not self.enabled:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(f"{self.url}/state", timeout=aiohttp.ClientTimeout(total=3)) as resp:
                return await resp.json(content_type=None)
        except Exception:  # noqa: BLE001
            return None

    async def close(self) -> None:
        if self._session:
            await self._session.close()
