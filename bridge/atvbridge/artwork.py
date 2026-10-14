"""Artwork for apps that don't publish any to the Apple TV's now-playing info.

YouTube sends title and channel but no artwork or video id, so we look the video up on
YouTube and use its thumbnail when the title and channel match. Otherwise (or for other apps
without artwork) we use the app's icon from the App Store. Disable with ATVB_ONLINE_ARTWORK=0.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from collections import OrderedDict

import aiohttp

LOG = logging.getLogger(__name__)

ENABLED = os.environ.get("ATVB_ONLINE_ARTWORK", "1") not in ("0", "false", "no")
YOUTUBE_APPS = {"com.google.ios.youtube", "com.google.ios.youtubeunplugged"}
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)", "Accept-Language": "en-US,en"}
_INITIAL_DATA = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.S)


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def pick_video(results: list[dict], title: str, channel: str | None) -> str | None:
    """Video id of the first result whose title (and channel, if known) matches."""
    want = _norm(title)
    for r in results:
        if difflib.SequenceMatcher(None, want, _norm(r["title"])).ratio() < 0.85:
            continue
        if channel and _norm(channel) != _norm(r["channel"]):
            continue
        return r["id"]
    return None


def parse_search(html: str) -> list[dict]:
    m = _INITIAL_DATA.search(html)
    if not m:
        return []
    out: list[dict] = []

    def walk(o: object) -> None:
        if isinstance(o, dict):
            v = o.get("videoRenderer")
            if isinstance(v, dict) and "videoId" in v:
                out.append({
                    "id": v["videoId"],
                    "title": "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])),
                    "channel": "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])),
                })
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(json.loads(m.group(1)))
    return out


class ArtworkFinder:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[str, tuple[bytes, str] | None] = OrderedDict()

    async def find(self, app_id: str, title: str | None, artist: str | None) -> tuple[bytes, str] | None:
        """(image bytes, mime type) or None. Results (including misses) are cached."""
        if not ENABLED:
            return None
        key = f"{app_id}\n{title}\n{artist}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        result = None
        try:
            if app_id in YOUTUBE_APPS and title:
                result = await self._youtube(title, artist)
            if result is None and app_id:
                result = await self._app_icon(app_id)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Artwork lookup failed: %s", exc)
        self._cache[key] = result
        while len(self._cache) > 50:
            self._cache.popitem(last=False)
        return result

    async def _get(self, url: str, **params: str) -> aiohttp.ClientResponse:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=10))
        return await self._session.get(url, params=params or None)

    async def _image(self, url: str) -> tuple[bytes, str] | None:
        async with await self._get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read(), resp.content_type or "image/jpeg"

    async def _youtube(self, title: str, channel: str | None) -> tuple[bytes, str] | None:
        query = f"{title} {channel}" if channel else title
        async with await self._get("https://www.youtube.com/results", search_query=query) as resp:
            html = await resp.text()
        video = pick_video(parse_search(html)[:5], title, channel)
        if not video:
            LOG.debug("No YouTube match for %r", title)
            return None
        for size in ("maxresdefault", "hq720", "mqdefault"):  # 16:9, best first
            image = await self._image(f"https://i.ytimg.com/vi/{video}/{size}.jpg")
            if image:
                LOG.info("Artwork: YouTube thumbnail for %r (%s)", title, video)
                return image
        return None

    async def _app_icon(self, bundle_id: str) -> tuple[bytes, str] | None:
        async with await self._get("https://itunes.apple.com/lookup", bundleId=bundle_id) as resp:
            data = await resp.json(content_type=None)
        results = data.get("results") or []
        url = results[0].get("artworkUrl512") if results else None
        return await self._image(url) if url else None

    async def close(self) -> None:
        if self._session:
            await self._session.close()
