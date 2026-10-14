"""End-to-end tests for the Google TV remote server.

Uses androidtvremote2 (the client library Home Assistant uses to control real
Google TVs) against the bridge, with a fake Apple TV that records commands.

Run from bridge/:  python -m unittest -v
"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any

from androidtvremote2 import AndroidTVRemote, InvalidAuth

from atvbridge import gtv
from atvbridge.store import Store


class FakeAppleTV:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "connected": True,
            "power": "on",
            "volume": 42.0,
            "app": {"name": "YouTube", "id": "com.google.ios.youtube"},
        }
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self._listeners: list = []
        self._keyboard_listeners: list = []
        self.got_command = asyncio.Event()

    def on_change(self, cb) -> None:  # noqa: ANN001
        self._listeners.append(cb)

    def on_keyboard(self, cb) -> None:  # noqa: ANN001
        self._keyboard_listeners.append(cb)

    def set(self, **kwargs: Any) -> None:
        self.state.update(kwargs)
        for cb in self._listeners:
            cb(self.state)

    async def command(self, name: str, **kwargs: Any) -> None:
        self.commands.append((name, kwargs))
        self.got_command.set()

    async def next_command(self) -> tuple[str, dict[str, Any]]:
        await asyncio.wait_for(self.got_command.wait(), 5)
        self.got_command.clear()
        return self.commands[-1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(pred, timeout: float = 5) -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(0.05)


class GoogleTVServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.store = Store(tmp / "data")
        self.store.data["appletv"] = {"name": "Test TV", "mac": "AA:BB:CC:DD:EE:FF"}
        self.atv = FakeAppleTV()
        gtv.REMOTE_PORT, gtv.PAIRING_PORT = free_port(), free_port()
        self.server = gtv.GoogleTVServer(self.store, self.atv)  # type: ignore[arg-type]

        async def no_mdns() -> None:
            pass

        self.server.advertise = no_mdns  # type: ignore[method-assign]
        await self.server.start()
        self.remote = self.client(tmp / "phone")
        await self.remote.async_generate_cert_if_missing()

    async def asyncTearDown(self) -> None:
        self.remote.disconnect()
        await self.server.stop()
        self.tmp.cleanup()

    def client(self, path: Path) -> AndroidTVRemote:
        return AndroidTVRemote(
            "Pixel Test", str(path) + "_cert.pem", str(path) + "_key.pem", "127.0.0.1",
            api_port=gtv.REMOTE_PORT, pair_port=gtv.PAIRING_PORT,
        )

    async def pair(self, code: str | None = None) -> None:
        # Like the phone app: try the remote port first, get refused, then pair.
        with self.assertRaises(InvalidAuth):
            await self.remote.async_connect()
        await self.remote.async_start_pairing()
        await wait_for(lambda: self.server.active_pairing is not None)
        await self.remote.async_finish_pairing(code or self.server.active_pairing["code"])
        await wait_for(lambda: len(self.store.phones) == 1)

    async def test_easy_pairing_code_is_000000(self) -> None:
        await self.pair("000000")
        phone = next(iter(self.store.phones.values()))
        self.assertEqual(phone["name"], "Pixel Test")
        self.assertNotEqual(phone["server_e"], 65537)

    async def test_random_code_when_easy_pairing_disabled(self) -> None:
        self.store.data["easy_pairing"] = False
        await self.pair()
        self.assertEqual(next(iter(self.store.phones.values()))["server_e"], 65537)

    async def test_remote_after_pairing(self) -> None:
        await self.pair("000000")
        await self.remote.async_connect()
        await wait_for(lambda: self.remote.volume_info is not None and self.remote.current_app)
        self.assertTrue(self.remote.is_on)
        self.assertEqual(self.remote.current_app, "com.google.ios.youtube")
        self.assertEqual(self.remote.volume_info["level"], 42)
        self.assertEqual(self.remote.device_info["manufacturer"], "Apple")

        self.remote.send_key_command("DPAD_UP")
        self.assertEqual(await self.atv.next_command(), ("up", {"hold": False}))
        self.remote.send_key_command("BACK")
        self.assertEqual(await self.atv.next_command(), ("menu", {"hold": False}))
        self.remote.send_key_command("MEDIA_PLAY_PAUSE")
        self.assertEqual(await self.atv.next_command(), ("play_pause", {"hold": False}))
        self.remote.send_key_command("DPAD_CENTER", "START_LONG")
        self.assertEqual(await self.atv.next_command(), ("select", {"hold": True}))
        self.remote.send_key_command("VOLUME_DOWN")
        self.assertEqual(await self.atv.next_command(), ("volume_down", {"hold": False}))
        self.remote.send_text("hello")
        self.assertEqual(await self.atv.next_command(), ("text_set", {"text": "hello"}))
        self.remote.send_launch_app_command("https://www.youtube.com")
        self.assertEqual(await self.atv.next_command(), ("launch", {"app": "https://www.youtube.com"}))

    async def test_state_pushed_to_phone(self) -> None:
        await self.pair("000000")
        await self.remote.async_connect()
        await wait_for(lambda: self.remote.is_on is True)
        self.atv.set(power="off")
        await wait_for(lambda: self.remote.is_on is False)
        self.atv.set(volume=10.0)
        await wait_for(lambda: self.remote.volume_info["level"] == 10)

    async def test_paired_phone_reconnects_after_restart(self) -> None:
        await self.pair("000000")
        await self.server.stop()
        self.server = gtv.GoogleTVServer(Store(self.store.data_dir), self.atv)  # type: ignore[arg-type]
        self.server.advertise = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
        await self.server.start()
        await self.remote.async_connect()
        await wait_for(lambda: len(self.server.sessions) == 1)

    async def test_revoked_phone_must_pair_again(self) -> None:
        await self.pair("000000")
        self.store.phones.clear()
        with self.assertRaises(InvalidAuth):
            await self.remote.async_connect()


if __name__ == "__main__":
    unittest.main()
