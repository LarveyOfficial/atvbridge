"""Asleep takes precedence over the Apple TV's lingering now-playing info."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pyatv.const import DeviceState, MediaType, PowerState

from atvbridge.appletv import AppleTV
from atvbridge.store import Store


def item(title: str, state: DeviceState) -> SimpleNamespace:
    return SimpleNamespace(title=title, artist="Ludwig", album=None, series_name=None, device_state=state,
                           position=10, total_time=100, media_type=MediaType.Video)


class FakeATV:
    def __init__(self) -> None:
        self.playing = item("Video A", DeviceState.Playing)
        self.power = SimpleNamespace(power_state=PowerState.On)
        self.calls: list[str] = []
        self.remote_control = None

        async def playing() -> SimpleNamespace:
            return self.playing

        async def turn_off() -> None:
            self.calls.append("off")  # the real Apple TV reports the change later (or not)

        self.metadata = SimpleNamespace(playing=playing, app=None, artwork_id=None)
        self.power.turn_off = turn_off


class SleepTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.atv = AppleTV(Store(Path(self.tmp.name)))
        self.fake = FakeATV()
        self.atv.atv = self.fake  # type: ignore[assignment]

        async def no_artwork(*args: object) -> None:
            return None

        self.atv._finder.find = no_artwork  # type: ignore[method-assign]
        await self.atv._refresh_now_playing()
        self.assertEqual(self.atv.state["now_playing"]["title"], "Video A")

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def refresh(self) -> dict | None:
        await self.atv._refresh_now_playing()
        return self.atv.state["now_playing"]

    async def test_sleep_clears_now_playing_despite_lingering_item(self) -> None:
        self.fake.power.power_state = PowerState.Off
        self.atv.powerstate_update(PowerState.On, PowerState.Off)
        self.assertEqual(self.atv.state["power"], "off")
        self.assertIsNone(self.atv.state["now_playing"])
        # The Apple TV keeps reporting the old item while asleep.
        self.assertIsNone(await self.refresh())

    async def test_stale_item_stays_hidden_after_waking_until_it_plays(self) -> None:
        self.fake.power.power_state = PowerState.Off
        self.atv.powerstate_update(PowerState.On, PowerState.Off)
        self.fake.power.power_state = PowerState.On
        self.atv.powerstate_update(PowerState.Off, PowerState.On)
        self.fake.playing = item("Video A", DeviceState.Paused)
        self.assertIsNone(await self.refresh())
        self.fake.playing = item("Video A", DeviceState.Playing)  # resumed: show it again
        self.assertEqual((await self.refresh())["title"], "Video A")

    async def test_new_item_after_waking_shows(self) -> None:
        self.fake.power.power_state = PowerState.Off
        self.atv.powerstate_update(PowerState.On, PowerState.Off)
        self.fake.power.power_state = PowerState.On
        self.atv.powerstate_update(PowerState.Off, PowerState.On)
        self.fake.playing = item("Video B", DeviceState.Paused)
        self.assertEqual((await self.refresh())["title"], "Video B")

    async def test_power_off_command_applies_immediately(self) -> None:
        await self.atv.command("power_off")
        self.assertEqual(self.fake.calls, ["off"])
        self.assertEqual(self.atv.state["power"], "off")
        self.assertIsNone(self.atv.state["now_playing"])
        # The Apple TV still says On (lagging): a now-playing update must not bring it back.
        self.assertIsNone(await self.refresh())


if __name__ == "__main__":
    unittest.main()
