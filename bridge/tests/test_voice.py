"""Voice command understanding and actions (no audio / speech model needed)."""

from __future__ import annotations

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
