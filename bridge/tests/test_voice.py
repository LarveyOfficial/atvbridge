"""Voice command understanding and actions (no audio / speech model needed)."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from atvbridge import voice

APPS = {
    "YouTube": "com.google.ios.youtube", "YouTube TV": "com.google.ios.youtubeunplugged",
    "Netflix": "com.netflix.Netflix", "Prime Video": "com.amazon.aiv.AIVApp",
    "Disney+": "com.disney.disneyplus", "Plex": "com.plexapp.plex", "Spotify": "com.spotify.client",
    "Peacock TV: Stream TV & Movies": "com.peacocktv.peacock", "Settings": "com.apple.TVSettings",
}


class ParseTest(unittest.TestCase):
    def test_commands(self) -> None:
        self.assertEqual(voice.parse("Pause."), ("command", "pause"))
        self.assertEqual(voice.parse("Hey Google, go home!"), ("command", "home"))
        self.assertEqual(voice.parse("Volume up"), ("command", "volume_up"))
        self.assertEqual(voice.parse("next episode"), ("command", "next"))

    def test_launch(self) -> None:
        self.assertEqual(voice.parse("Open Netflix."), ("launch", "netflix"))
        self.assertEqual(voice.parse("launch the YouTube app"), ("launch", "youtube"))

    def test_search(self) -> None:
        self.assertEqual(voice.parse("Search for Stranger Things"), ("search", "stranger things"))
        self.assertEqual(voice.parse("The Office"), ("query", "the office"))


class MatchAppTest(unittest.TestCase):
    def test_exact_and_prefix(self) -> None:
        self.assertEqual(voice.match_app("youtube", APPS), "com.google.ios.youtube")
        self.assertEqual(voice.match_app("youtube tv", APPS), "com.google.ios.youtubeunplugged")
        self.assertEqual(voice.match_app("prime", APPS), "com.amazon.aiv.AIVApp")
        self.assertEqual(voice.match_app("peacock", APPS), "com.peacocktv.peacock")
        self.assertEqual(voice.match_app("disney plus", APPS), "com.disney.disneyplus")
        self.assertEqual(voice.match_app("disney+", APPS), "com.disney.disneyplus")

    def test_fuzzy_and_miss(self) -> None:
        self.assertEqual(voice.match_app("net flicks", APPS), "com.netflix.Netflix")
        self.assertIsNone(voice.match_app("hbo", APPS))


class FakeATV:
    def __init__(self, focused: bool = False) -> None:
        self.keyboard_focused = focused
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def command(self, name: str, **kw: Any) -> None:
        self.commands.append((name, kw))
        if name == "launch" and kw.get("app") == voice.SEARCH_APP:
            self.keyboard_focused = True

    async def app_names(self) -> dict[str, str]:
        return APPS

    async def wait_keyboard_focus(self, timeout: float) -> bool:
        return self.keyboard_focused


class ActTest(unittest.IsolatedAsyncioTestCase):
    async def run_act(self, text: str, focused: bool = False) -> FakeATV:
        atv = FakeATV(focused)
        await voice.VoiceAssistant(atv, None).act(text)  # type: ignore[arg-type]
        return atv

    async def test_types_into_focused_field(self) -> None:
        atv = await self.run_act("Stranger Things.", focused=True)
        self.assertEqual(atv.commands, [("text_set", {"text": "Stranger Things"})])

    async def test_launch_app(self) -> None:
        atv = await self.run_act("Open Netflix")
        self.assertEqual(atv.commands, [("launch", {"app": "com.netflix.Netflix"})])

    async def test_unknown_app_searches(self) -> None:
        atv = await self.run_act("open HBO")
        self.assertEqual(atv.commands, [("launch", {"app": voice.SEARCH_APP}), ("text_set", {"text": "hbo"})])

    async def test_bare_app_name_opens_it(self) -> None:
        atv = await self.run_act("Disney+.")
        self.assertEqual(atv.commands, [("launch", {"app": "com.disney.disneyplus"})])

    async def test_bare_phrase_only_searches(self) -> None:
        # "you" is a prefix of "youtube"; a bare phrase must not open apps loosely.
        atv = await self.run_act("You")
        self.assertEqual(atv.commands[0], ("launch", {"app": voice.SEARCH_APP}))

    async def test_command(self) -> None:
        self.assertEqual((await self.run_act("Pause")).commands, [("pause", {})])


if __name__ == "__main__":
    unittest.main()


class FakeSiriBridge:
    """Stands in for appletv-siri-voice's control API."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.received = bytearray()
        self.chunks_before_end = 0
        self.first_chunk = asyncio.Event()

    async def start(self) -> str:
        from aiohttp import web

        async def stream(request: web.Request) -> web.Response:
            async for chunk in request.content.iter_any():
                self.received += chunk
                self.first_chunk.set()
            if self.status != 200:
                return web.json_response({"error": "target is not connected via HDS",
                                          "retryAfterSeconds": 60}, status=self.status)
            return web.json_response({"ok": True, "bytes": len(self.received), "ms": 42, "target": "1"})

        app = web.Application()
        app.router.add_post("/siri/stream", stream)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"http://127.0.0.1:{port}"


class SiriRouteTest(unittest.IsolatedAsyncioTestCase):
    async def make(self, status: int = 200) -> tuple[voice.VoiceAssistant, FakeSiriBridge, list]:
        import tempfile
        from pathlib import Path

        from atvbridge.siri import SiriRelay

        bridge = FakeSiriBridge(status)
        url = await bridge.start()
        self.addAsyncCleanup(bridge.runner.cleanup)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        assistant = voice.VoiceAssistant(FakeATV(), Path(tmp.name), SiriRelay(url))  # type: ignore[arg-type]
        self.addAsyncCleanup(assistant.siri.close)
        results: list = []
        assistant.on_result = lambda: results.append(assistant.last)
        return assistant, bridge, results

    async def test_audio_streams_live_to_siri(self) -> None:
        assistant, bridge, results = await self.make()
        assistant.begin("phone")
        audio = bytes(range(256)) * 10
        assistant.chunk(audio[:1280], 16000)
        # Siri must hear audio while the user is still talking, before the utterance ends.
        await asyncio.wait_for(bridge.first_chunk.wait(), 2)
        assistant.chunk(audio[1280:], 16000)
        assistant.handle(audio, 16000, "phone")
        while not results:
            await asyncio.sleep(0.02)
        self.assertEqual(bytes(bridge.received), audio)
        self.assertIn("sent to Siri", results[0]["action"])

    async def test_falls_back_to_local_when_siri_unavailable(self) -> None:
        assistant, bridge, results = await self.make(status=503)
        assistant.transcribe = lambda pcm, rate, vocab=None: "pause"  # type: ignore[method-assign]
        assistant.begin("phone")
        assistant.chunk(b"\x01\x00" * 800, 16000)
        assistant.handle(b"\x01\x00" * 800, 16000, "phone")
        while not results:
            await asyncio.sleep(0.02)
        self.assertEqual(assistant.appletv.commands, [("pause", {})])
        self.assertIn("Siri unavailable", results[0]["action"])

    async def test_8khz_audio_skips_siri(self) -> None:
        assistant, bridge, results = await self.make()
        assistant.transcribe = lambda pcm, rate, vocab=None: "pause"  # type: ignore[method-assign]
        assistant.begin("phone")
        assistant.chunk(b"\x01\x00" * 400, 8000)
        assistant.handle(b"\x01\x00" * 400, 8000, "phone")
        while not results:
            await asyncio.sleep(0.02)
        self.assertEqual(bridge.received, b"")
        self.assertEqual(results[0]["action"], "command: pause")
