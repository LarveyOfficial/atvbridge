"""Volume routing between the LG TV (real level) and the Apple TV (up/down presses)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from atvbridge.appletv import AppleTV
from atvbridge.store import Store


class FakeTV:
    def __init__(self) -> None:
        self.available = True
        self.volume = 14
        self.muted = False
        self.calls: list[tuple] = []

    async def set_volume(self, level: float) -> None:
        self.calls.append(("set", level))

    async def step(self, delta: int) -> None:
        self.calls.append(("step", delta))

    async def toggle_mute(self) -> None:
        self.calls.append(("mute",))


class FakeAudio:
    def __init__(self) -> None:
        self.presses: list[str] = []

    async def volume_up(self) -> None:
        self.presses.append("up")

    async def volume_down(self) -> None:
        self.presses.append("down")


class VolumeRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.atv = AppleTV(Store(Path(self.tmp.name)))
        self.audio = FakeAudio()
        self.atv.atv = type("ATV", (), {"audio": self.audio, "remote_control": None})()  # type: ignore[assignment]
        self.atv._atv_volume_control = "relative"
        self.tv = FakeTV()
        self.atv.tv = self.tv  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_real_level_from_tv(self) -> None:
        self.atv.update_volume_source()
        self.assertEqual(self.atv.state["volume"], 14)
        self.assertEqual(self.atv.state["volume_control"], "absolute")
        await self.atv.command("set_volume", level=11)
        await self.atv.command("volume_up")
        await self.atv.command("mute")
        self.assertEqual(self.tv.calls, [("set", 11.0), ("step", 1), ("mute",)])
        self.assertEqual(self.audio.presses, [])

    async def test_falls_back_to_apple_tv_presses(self) -> None:
        self.tv.available = False
        self.atv.update_volume_source()
        self.assertIsNone(self.atv.state["volume"])
        self.assertEqual(self.atv.state["volume_control"], "relative")
        await self.atv.command("volume_down")
        self.assertEqual(self.audio.presses, ["down"])
        self.assertEqual(self.tv.calls, [])


if __name__ == "__main__":
    unittest.main()
