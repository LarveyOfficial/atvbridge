"""Voice input from the phone's Google TV remote (mic button).

Audio arrives as 16-bit little-endian PCM, mono, 8 kHz (see gtv.RemoteSession).
"""

from __future__ import annotations

import array
import logging
import math
import wave
from pathlib import Path

LOG = logging.getLogger(__name__)

SAMPLE_RATE = 8000


def stats(pcm: bytes) -> tuple[float, float]:
    """(duration in seconds, RMS level 0..1) of 16-bit mono PCM."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0, 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return len(samples) / SAMPLE_RATE, rms


def trim_padding(pcm: bytes) -> bytes:
    """Drop trailing silence: the phone zero-pads every chunk to 8 KB."""
    end = len(pcm) // 2 * 2
    while end >= 2 and pcm[end - 2 : end] == b"\x00\x00":
        end -= 2
    return pcm[:end]


def save_wav(pcm: bytes, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
