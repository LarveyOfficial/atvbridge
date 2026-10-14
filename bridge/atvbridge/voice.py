"""Voice input from the phone's Google TV remote (mic button).

The phone streams 16-bit little-endian mono PCM (16 kHz from Google's app). We transcribe
it locally with faster-whisper and act on the text: type it into a focused text field,
run a simple command ("open Netflix", "pause"), or search for it in the Search app.
"""

from __future__ import annotations

import array
import asyncio
import difflib
import logging
import math
import os
import re
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .siri import SIRI_RATE, SiriRelay, SiriStream, SiriUnavailable

if TYPE_CHECKING:
    from .appletv import AppleTV

LOG = logging.getLogger(__name__)

SEARCH_APP = "com.apple.TVSearch"
WHISPER_MODEL = os.environ.get("ATVB_WHISPER_MODEL", "base.en")
WHISPER_RATE = 16000


def stats(pcm: bytes, rate: int) -> tuple[float, float]:
    """(duration in seconds, RMS level 0..1) of 16-bit mono PCM."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0, 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return len(samples) / rate, rms


def trim_padding(pcm: bytes) -> bytes:
    """Drop trailing digital silence (some clients zero-pad chunks to 8 KB)."""
    end = len(pcm) // 2 * 2
    while end >= 2 and pcm[end - 2 : end] == b"\x00\x00":
        end -= 2
    return pcm[:end]


def save_wav(pcm: bytes, rate: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# --- understanding ----------------------------------------------------------------------

# Spoken phrase -> bridge command. Matched against the whole utterance.
PHRASES: dict[str, str] = {
    "play": "play", "resume": "play", "continue": "play", "unpause": "play",
    "pause": "pause", "stop": "pause", "hold on": "pause",
    "next": "next", "skip": "next", "next episode": "next", "next song": "next", "next track": "next",
    "previous": "previous", "previous episode": "previous", "previous song": "previous",
    "back": "menu", "go back": "menu",
    "home": "home", "go home": "home",
    "volume up": "volume_up", "louder": "volume_up", "turn it up": "volume_up",
    "volume down": "volume_down", "quieter": "volume_down", "turn it down": "volume_down",
    "mute": "mute", "unmute": "mute",
    "turn off": "power_off", "turn off the tv": "power_off", "sleep": "power_off", "go to sleep": "power_off",
    "turn on": "power_on", "wake up": "power_on",
    "skip forward": "skip_forward", "fast forward": "skip_forward",
    "skip back": "skip_backward", "rewind": "skip_backward",
}
_FILLER = re.compile(r"^(hey google|ok google|okay google|please|can you|could you)[ ,]+")
_LAUNCH = re.compile(r"^(?:open|launch|start|go to|switch to)\s+(?:the\s+)?(.+?)(?:\s+app)?$")
_SEARCH = re.compile(r"^(?:search(?: for)?|find|look up|show me)\s+(.+)$")


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s'+&-]", "", text)  # drop punctuation whisper adds
    text = re.sub(r"\s+", " ", text).strip()
    return _FILLER.sub("", text)


def parse(text: str) -> tuple[str, str]:
    """Return (kind, arg): ("command", name) | ("launch", app name) | ("search", query)
    | ("query", text) for anything else (an app name on its own, or a search)."""
    t = normalize(text)
    if t in PHRASES:
        return "command", PHRASES[t]
    if m := _LAUNCH.match(t):
        return "launch", m.group(1)
    if m := _SEARCH.match(t):
        return "search", m.group(1)
    return "query", t


def match_app(name: str, apps: dict[str, str], exact: bool = False) -> str | None:
    """Best bundle id for a spoken app name. `apps` maps display name -> bundle id.
    With exact=True only an exact (punctuation-insensitive) name match counts."""
    def key(s: str) -> str:
        # Speech-to-text writes "Disney Plus" for "Disney+".
        return re.sub(r"[^a-z0-9+]", "", re.sub(r"\s*\bplus\b", "+", s.lower()))

    wanted = key(name)
    if not wanted:
        return None
    by_key = {key(n): bid for n, bid in apps.items()}
    if wanted in by_key:
        return by_key[wanted]
    if exact:
        return None
    starts = [k for k in by_key if k.startswith(wanted) or wanted.startswith(k)]
    if starts:
        return by_key[min(starts, key=len)]
    close = difflib.get_close_matches(wanted, list(by_key), n=1, cutoff=0.75)
    return by_key[close[0]] if close else None


# --- assistant --------------------------------------------------------------------------

class VoiceAssistant:
    """Sends speech to real Siri when the Siri bridge is set up (ATVB_SIRI_URL), otherwise
    (or if Siri is unavailable) transcribes it locally and acts on the text."""

    def __init__(self, appletv: AppleTV, data_dir: Path, siri: SiriRelay | None = None) -> None:
        self.appletv = appletv
        self.data_dir = data_dir
        self.siri = siri or SiriRelay()
        self._siri_stream: SiriStream | None = None
        # Last result, shown on the dashboard: {"text", "action", "at"}
        self.last: dict[str, Any] | None = None
        self.on_result: Callable[[], None] = lambda: None
        self._model: Any = None
        self._lock = asyncio.Lock()

    # --- live audio (for Siri) -------------------------------------------------------
    def begin(self, phone: str) -> None:
        if self._siri_stream:
            self._siri_stream.cancel()
        self._siri_stream = self.siri.start()
        if self._siri_stream:
            LOG.info("Streaming voice from %s to Siri", phone)

    def chunk(self, pcm: bytes, rate: int) -> None:
        stream = self._siri_stream
        if stream is None:
            return
        if rate != SIRI_RATE:  # Siri takes 16 kHz; other rates go the local route
            stream.cancel()
            self._siri_stream = None
            return
        stream.feed(pcm)

    # --- finished utterance --------------------------------------------------------
    def handle(self, pcm: bytes, rate: int, phone: str) -> None:
        stream, self._siri_stream = self._siri_stream, None
        asyncio.get_event_loop().create_task(self._handle(pcm, rate, stream))

    async def _handle(self, pcm: bytes, rate: int, stream: SiriStream | None = None) -> None:
        note = ""
        if stream is not None:
            try:
                reply = await stream.finish()
                self._result("", f"sent to Siri ({reply.get('ms', '?')} ms)")
                return
            except SiriUnavailable as exc:
                LOG.warning("Siri unavailable (%s); using local speech recognition", exc)
                note = f" (Siri unavailable: {exc})"
        async with self._lock:  # one utterance at a time
            save_wav(pcm, rate, self.data_dir / "voice" / "last.wav")
            started = time.monotonic()
            try:
                apps = list(await self.appletv.app_names())
            except Exception:  # noqa: BLE001
                apps = []
            try:
                text = await asyncio.get_running_loop().run_in_executor(None, self.transcribe, pcm, rate, apps)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("Transcription failed")
                self._result("", f"speech recognition failed: {exc}{note}")
                return
            LOG.info("Heard %r (%.1f s to transcribe)", text, time.monotonic() - started)
            try:
                action = await self.act(text)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Voice action for %r failed: %s", text, exc)
                action = f"failed: {exc}"
            self._result(text, action + note)

    def _result(self, text: str, action: str) -> None:
        LOG.info("Voice: %r -> %s", text, action)
        self.last = {"text": text, "action": action, "at": time.time()}
        self.on_result()

    def load_model(self) -> None:
        """Load (and on first run download) the speech model; blocking."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        LOG.info("Loading speech model %s (first run downloads ~140 MB)", WHISPER_MODEL)
        self._model = WhisperModel(
            WHISPER_MODEL, device="cpu", compute_type="int8", download_root=str(self.data_dir / "models"),
        )
        LOG.info("Speech model ready")

    def preload(self) -> None:
        """Load the model in the background so the first voice command isn't slow."""
        async def go() -> None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.load_model)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Voice commands unavailable: %s", exc)

        asyncio.get_running_loop().create_task(go())

    def transcribe(self, pcm: bytes, rate: int, vocabulary: list[str] | None = None) -> str:
        import numpy as np

        self.load_model()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if rate != WHISPER_RATE:
            n = int(len(audio) * WHISPER_RATE / rate)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
        # Priming with app names helps with names like "Hulu", "Disney+" or "HIDIVE".
        prompt = "Apple TV voice command, like: open YouTube, pause, search for a show."
        if vocabulary:
            prompt += " Apps: " + ", ".join(vocabulary[:60]) + "."
        segments, _info = self._model.transcribe(
            audio, language="en", beam_size=1, condition_on_previous_text=False, initial_prompt=prompt,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def act(self, text: str) -> str:
        spoken = text.strip().rstrip(".!?").strip()
        if not spoken:
            return "nothing heard"
        atv = self.appletv
        if atv.keyboard_focused:
            await atv.command("text_set", text=spoken)
            return "typed into the text field"
        kind, arg = parse(spoken)
        if kind == "command":
            await atv.command(arg)
            return f"command: {arg}"
        if kind in ("launch", "query"):
            # "open X" matches loosely; a bare utterance only opens an app on an exact name.
            bundle = match_app(arg, await atv.app_names(), exact=kind == "query")
            if bundle:
                await atv.command("launch", app=bundle)
                return f"opened {bundle}"
        return await self.search(arg)

    async def search(self, query: str) -> str:
        atv = self.appletv
        await atv.command("launch", app=SEARCH_APP)
        if await atv.wait_keyboard_focus(timeout=5):
            await atv.command("text_set", text=query)
            return f"searched for {query!r}"
        return f"opened Search, but no text field got focus to type {query!r}"
