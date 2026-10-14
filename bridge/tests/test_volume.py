"""Queued relative volume presses (Apple TV driving the TV over HDMI-CEC)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from atvbridge import appletv
from atvbridge.store import Store


class FakeAudio:
    def __init__(self) -> None:
        self.presses: list[str] = []

    async def volume_up(self) -> None:
        self.presses.append("up")
        await asyncio.sleep(0)

    async def volume_down(self) -> None:
        self.presses.append("down")
        await asyncio.sleep(0)


class VolumeQueueTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        appletv.VOLUME_STEP_INTERVAL = 0.01
        self.tmp = tempfile.TemporaryDirectory()
        self.atv = appletv.AppleTV(Store(Path(self.tmp.name)))
        self.audio = FakeAudio()
        self.atv.atv = type("ATV", (), {"audio": self.audio})()  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def drain(self) -> None:
        while self.atv._volume_task and not self.atv._volume_task.done():
            await asyncio.sleep(0.01)

    async def test_exact_number_of_presses(self) -> None:
        self.atv.volume_steps(15)
        await self.drain()
        self.assertEqual(self.audio.presses, ["up"] * 15)

    async def test_dragging_back_cancels_queued_presses(self) -> None:
        # Slider dragged up 30 then straight back down 25: only the net 5 (plus whatever
        # already went out) is sent, never 55 presses.
        self.atv.volume_steps(30)
        self.atv.volume_steps(-25)
        await self.drain()
        self.assertEqual(self.audio.presses.count("up") - self.audio.presses.count("down"), 5)
        self.assertLessEqual(len(self.audio.presses), 7)

    async def test_queue_is_capped(self) -> None:
        self.atv.volume_steps(500)
        await self.drain()
        self.assertEqual(len(self.audio.presses), appletv.MAX_VOLUME_QUEUE)


if __name__ == "__main__":
    unittest.main()
