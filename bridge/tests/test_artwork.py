"""Matching YouTube search results to what the Apple TV reports."""

from __future__ import annotations

import json
import unittest

from atvbridge import artwork

TITLE = "🕵️ did dream cheat... again...? THE YARD GAMING NIGHT (loser gets kicked off the podcast)"


def page(*videos: tuple[str, str, str]) -> str:
    items = [{"videoRenderer": {"videoId": v, "title": {"runs": [{"text": t}]},
                                "ownerText": {"runs": [{"text": c}]}}} for v, t, c in videos]
    return f"<script>var ytInitialData = {json.dumps({'contents': items})};</script>"


class YouTubeMatchTest(unittest.TestCase):
    def test_picks_matching_title_and_channel(self) -> None:
        results = artwork.parse_search(page(
            ("cJ3uhNhxwKA", "Ludwig Cheated. Again. | The Yard", "The Yard"),
            ("_Jlr2DcL29w", TITLE, "Ludwig"),
        ))
        self.assertEqual(artwork.pick_video(results, TITLE, "Ludwig"), "_Jlr2DcL29w")

    def test_rejects_wrong_channel_or_different_title(self) -> None:
        results = artwork.parse_search(page(("_Jlr2DcL29w", TITLE, "Ludwig Clips")))
        self.assertIsNone(artwork.pick_video(results, TITLE, "Ludwig"))
        results = artwork.parse_search(page(("x", "Huge Dream Cheating Scandal!", "Ludwig")))
        self.assertIsNone(artwork.pick_video(results, TITLE, "Ludwig"))

    def test_no_channel_matches_on_title(self) -> None:
        results = artwork.parse_search(page(("abc", TITLE.upper(), "Whoever")))
        self.assertEqual(artwork.pick_video(results, TITLE, None), "abc")


if __name__ == "__main__":
    unittest.main()
