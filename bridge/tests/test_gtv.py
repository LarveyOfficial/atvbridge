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

from atvbridge import gtv, ime
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
        self.keyboard_focused = False
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

    def keyboard(self, focused: bool, text: str | None = None) -> None:
        for cb in self._keyboard_listeners:
            cb(focused, text)

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
        # Google's app only sends volume keys to a TV that identifies like its remote service.
        self.assertEqual(self.remote.device_info["sw_version"], "6.1.683155326")

        self.remote.send_key_command("DPAD_UP")
        self.assertEqual(await self.atv.next_command(), ("up", {}))
        self.remote.send_key_command("BACK")
        self.assertEqual(await self.atv.next_command(), ("menu", {}))
        self.remote.send_key_command("MEDIA_PLAY_PAUSE")
        self.assertEqual(await self.atv.next_command(), ("play_pause", {}))
        self.remote.send_key_command("DPAD_CENTER", "START_LONG")
        self.assertEqual(await self.atv.next_command(), ("select", {"hold": True}))
        self.remote.send_key_command("VOLUME_DOWN")
        self.assertEqual(await self.atv.next_command(), ("volume_down", {}))
        self.remote.send_key_command("MUTE")
        self.assertEqual(await self.atv.next_command(), ("mute", {}))
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

    async def test_easy_pairing_when_phone_skips_remote_port(self) -> None:
        # Google's app goes straight to the pairing port. The first connection is dropped
        # so that the retry is served a key that makes 000000 valid.
        from androidtvremote2 import ConnectionClosed

        with self.assertRaises((ConnectionClosed, InvalidAuth, OSError)):
            await self.remote.async_start_pairing()
            await self.remote.async_finish_pairing("000000")
        self.remote.disconnect()
        await self.remote.async_start_pairing()
        await self.remote.async_finish_pairing("000000")
        await wait_for(lambda: len(self.store.phones) == 1)

    async def connected_remote(self) -> None:
        await self.pair("000000")
        await self.remote.async_connect()
        await wait_for(lambda: len(self.server.sessions) == 1)

    async def test_press_and_release_is_one_tap(self) -> None:
        await self.connected_remote()
        self.remote.send_key_command("DPAD_RIGHT", "START_LONG")
        await asyncio.sleep(0.1)
        self.remote.send_key_command("DPAD_RIGHT", "END_LONG")
        self.assertEqual(await self.atv.next_command(), ("right", {}))
        await asyncio.sleep(gtv.HOLD_DELAY + 0.2)
        self.assertEqual(self.atv.commands, [("right", {})])

    async def test_held_arrow_repeats_until_release(self) -> None:
        await self.connected_remote()
        self.remote.send_key_command("DPAD_DOWN", "START_LONG")
        await asyncio.sleep(gtv.HOLD_DELAY + 4 * gtv.REPEAT_INTERVAL)
        self.remote.send_key_command("DPAD_DOWN", "END_LONG")
        await asyncio.sleep(0.1)
        count = len(self.atv.commands)
        self.assertGreaterEqual(count, 3)
        self.assertTrue(all(c == ("down", {}) for c in self.atv.commands))
        await asyncio.sleep(3 * gtv.REPEAT_INTERVAL)
        self.assertEqual(len(self.atv.commands), count, "kept repeating after release")

    async def test_held_select_is_long_press(self) -> None:
        await self.connected_remote()
        self.remote.send_key_command("DPAD_CENTER", "START_LONG")
        self.assertEqual(await self.atv.next_command(), ("select", {"hold": True}))
        self.remote.send_key_command("DPAD_CENTER", "END_LONG")
        await asyncio.sleep(0.1)
        self.assertEqual(self.atv.commands, [("select", {"hold": True})])

    async def test_swipe_does_not_click(self) -> None:
        # Touchpad swipe as the app sends it: select down, direction, select up.
        await self.connected_remote()
        self.remote.send_key_command("DPAD_CENTER", "START_LONG")
        await asyncio.sleep(0.03)
        self.remote.send_key_command("DPAD_LEFT", "SHORT")
        await asyncio.sleep(0.05)
        self.remote.send_key_command("DPAD_CENTER", "END_LONG")
        self.assertEqual(await self.atv.next_command(), ("left", {}))
        await asyncio.sleep(gtv.HOLD_DELAY + 0.2)
        self.assertEqual(self.atv.commands, [("left", {})])

    async def test_relative_volume(self) -> None:
        # Apple TV driving the TV over HDMI-CEC: only up/down, no level.
        self.atv.state.update(volume=None, volume_control="relative")
        await self.connected_remote()
        await wait_for(lambda: self.remote.volume_info is not None)
        self.assertEqual(self.remote.volume_info["level"], gtv.RELATIVE_VOLUME_PLACEHOLDER)

        self.remote.send_key_command("VOLUME_UP")
        self.assertEqual(await self.atv.next_command(), ("volume_up", {}))

        # A slider drag (set level) becomes steps relative to the placeholder.
        session = next(iter(self.server.sessions))
        self.atv.commands.clear()
        session._set_volume(gtv.RELATIVE_VOLUME_PLACEHOLDER - 2)
        await wait_for(lambda: len(self.atv.commands) == 2)
        self.assertEqual(self.atv.commands, [("volume_down", {})] * 2)

    async def test_voice_session_captures_audio(self) -> None:
        import array
        import math

        heard: list[tuple[bytes, int]] = []
        self.server.on_voice = lambda pcm, rate, phone: heard.append((pcm, rate))  # type: ignore[assignment]
        self.remote = AndroidTVRemote(
            "Pixel Test", self.remote._certfile, self.remote._keyfile, "127.0.0.1",
            api_port=gtv.REMOTE_PORT, pair_port=gtv.PAIRING_PORT, enable_voice=True,
        )
        await self.connected_remote()
        # 0.5 s of a 440 Hz tone, 16-bit mono 8 kHz like the phone's mic stream.
        tone = array.array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(4000)))
        async with await self.remote.start_voice() as stream:
            stream.send_chunk(tone.tobytes())
        await wait_for(lambda: heard)
        # Zero padding from the client's 8 KB chunks is trimmed off.
        self.assertEqual(heard[0], (tone.tobytes().rstrip(b"\x00"), 8000))

    async def test_voice_raw_pcm_frames_from_google_app(self) -> None:
        # Google's app sends field 31 as raw 16 kHz PCM bytes, not a RemoteVoicePayload.
        from atvbridge.tls import ProtobufFraming

        heard: list[tuple[bytes, int]] = []
        self.server.on_voice = lambda pcm, rate, phone: heard.append((pcm, rate))  # type: ignore[assignment]
        await self.connected_remote()
        session = next(iter(self.server.sessions))
        self.remote.send_key_command("SEARCH")
        await wait_for(lambda: session._voice_id is not None)
        audio = bytes(range(1, 201)) * 3
        for i in range(0, len(audio), 200):
            chunk = audio[i:i + 200]
            session.on_data(ProtobufFraming.encode(gtv.VOICE_PAYLOAD_TAG + bytes([0xC8, 0x01]) + chunk))
        end = gtv.RemoteMessage()
        end.remote_voice_end.session_id = session._voice_id
        session.on_data(ProtobufFraming.encode(end.SerializeToString()))
        await wait_for(lambda: heard)
        self.assertEqual(heard[0], (audio, 16000))

    def edit_frame(self, field_id: int, counter: int, *edits: bytes) -> bytes:
        """A BatchEdit as Google's app sends it (see atvbridge/ime.py)."""
        body = ime._int(1, field_id) + ime._int(2, counter) + b"".join(ime._bytes(3, e) for e in edits)
        return ime._bytes(ime.BATCH_EDIT, body)

    async def test_keyboard_focus_announces_text_field(self) -> None:
        await self.connected_remote()
        self.atv.keyboard(True, "stra")
        # Our StartInput decodes with androidtvremote2's proto too: its "app_info" is EditorInfo.
        start, show = self.server.ime_start_frames()
        msg = gtv.RemoteMessage.FromString(start)
        self.assertEqual(msg.remote_ime_key_inject.app_info.int2, ime.TYPE_CLASS_TEXT)  # inputType
        self.assertEqual(msg.remote_ime_key_inject.app_info.app_package, "com.google.ios.youtube")
        self.assertEqual(msg.remote_ime_key_inject.text_field_status.value, "stra")
        self.assertEqual(ime.read_fields(show), [(ime.SHOW_HIDE, ime._int(1, 1))])
        self.atv.keyboard(False)
        self.assertEqual(ime.read_fields(self.server.ime_start_frames()[1]), [(ime.SHOW_HIDE, ime._int(1, 0))])

    async def test_phone_typing_edits_apple_tv_text(self) -> None:
        from atvbridge.tls import ProtobufFraming

        await self.connected_remote()
        session = next(iter(self.server.sessions))
        self.atv.keyboard(True, "stra")
        fid, counter = self.server.ime_field_id, self.server.ime_text_counter
        replace = lambda a, b, t: ime._bytes(2, ime._int(1, a) + ime._int(2, b) + ime._str(3, t))  # noqa: E731
        select = lambda a, b: ime._bytes(1, ime._int(2, a) + ime._int(3, b))  # noqa: E731
        session.on_data(ProtobufFraming.encode(self.edit_frame(fid, counter, replace(4, 4, "nger things"))))
        self.assertEqual(await self.atv.next_command(), ("type_text", {"text": "stranger things"}))
        # Backspace twice, as a selection + replace with "".
        session.on_data(ProtobufFraming.encode(self.edit_frame(
            fid, self.server.ime_text_counter, select(13, 15), replace(13, 15, ""))))
        self.assertEqual(await self.atv.next_command(), ("type_text", {"text": "stranger thin"}))
        # Edits for a field that is no longer focused are ignored.
        session.on_data(ProtobufFraming.encode(self.edit_frame(fid + 7, 0, replace(0, 0, "x"))))
        await asyncio.sleep(0.1)
        self.assertEqual(self.server.ime_text, "stranger thin")

    async def test_options_offer_output_encoding(self) -> None:
        # Google's remote app (Polo) needs our *output* encodings to match its input
        # encodings, or it aborts with STATUS_BAD_CONFIGURATION (401).
        # androidtvremote2 doesn't check this, so inspect the reply directly.
        from androidtvremote2.polo_pb2 import Options, OuterMessage

        session = gtv.PairingSession(self.server)
        sent: list[OuterMessage] = []
        session.send = sent.append  # type: ignore[method-assign]
        msg = OuterMessage(protocol_version=2, status=OuterMessage.Status.STATUS_OK)
        enc = msg.options.input_encodings.add()
        enc.type = Options.Encoding.ENCODING_TYPE_HEXADECIMAL
        enc.symbol_length = 6
        msg.options.preferred_role = Options.RoleType.ROLE_TYPE_INPUT
        session.handle(msg)
        reply = sent[-1]
        self.assertEqual(reply.status, OuterMessage.Status.STATUS_OK)
        self.assertIn((Options.Encoding.ENCODING_TYPE_HEXADECIMAL, 6),
                      [(e.type, e.symbol_length) for e in reply.options.output_encodings])

    async def test_revoked_phone_must_pair_again(self) -> None:
        await self.pair("000000")
        self.store.phones.clear()
        with self.assertRaises(InvalidAuth):
            await self.remote.async_connect()


if __name__ == "__main__":
    unittest.main()
