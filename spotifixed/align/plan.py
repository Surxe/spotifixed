"""The arithmetic of aligning a lyric line to audio; the listening itself is ``engine``.

A lyric file says when each line starts. That is used as an anchor, not thrown away: each
line is looked for only in a window of audio around its own span, so one bad line cannot
drag the rest of the song with it, and an instrumental between lines is simply silence
inside a window.

* :func:`spell` turns a word into the letters the acoustic model knows.
* :func:`windows` says which stretch of audio each line is looked for in.
* :func:`settle` turns what the engine found into word times, or into nothing for a line
  it was not sure of. Such a line keeps the estimate of ``lyrics/words.py``.
* :func:`voice_share` says how much of the track has a voice in it at all, which tells a
  karaoke or instrumental upload from the song.
* :func:`strong_words` counts the words a clean edit mutes, which, against what a speech
  recognizer hears of the voice, tells a clean edit from the explicit recording.

Times are counted in the model's frames (``FRAME_MS`` each) until :func:`settle`, and in
milliseconds of the audio file after it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from ..models import LyricLine

FRAME_MS = 20
# How far outside its span a line is looked for. A line is often stamped a little before
# the singing, so there is more room after than before.
PAD_BEFORE_MS = 400
PAD_AFTER_MS = 600
# The last line has no following timestamp; this much audio is searched for it.
LAST_LINE_MS = 15_000
# A line whose letters the model heard with less than this average probability is not
# trusted. Measured on real tracks: above it, four lines in five agree with a second,
# unrelated aligner to within 200 ms; below it, fewer than half do.
MIN_CONFIDENCE = 0.2
# The last word of a line is held for as long as the voice stays this loud, as a share
# of the track's loud singing (see :func:`settle`).
QUIET_SHARE = 0.1
# A frame in which the separated voice is at least this loud counts as singing. The voice
# is on the scale of the mix, which the engine brings to unit level, so this is a share
# of the whole track's level; what a separator lets through of a karaoke backing stays
# far below it.
AUDIBLE_VOICE = 0.1
# The token that stands for "some sound": a word with no letter the model knows.
ANY = "*"

_UNKNOWN = re.compile(r"[^a-z']")

# Words every clean edit removes, also as a lyric site or a recognizer may write them
# ("f**k", "sh*t", "n***a"). Milder ones ("damn", "ass", "hell") are left out: some clean
# edits keep them, and "ass" hides in too many other words.
_STRONG = re.compile(
    r"\b(?:(?:mother)?f[u*][c*]?[k*]|sh[i*]t|b[i*]tch|n[i*][g*][g*]?a|pussy)[\w*]*"
)
# Lyrics with fewer strong words than this say too little about a clean edit: one word
# missed by the recognizer would decide it.
MIN_STRONG_WORDS = 3


def spell(word: str) -> str:
    """``word`` in the letters the acoustic model knows, or :data:`ANY` when it has none.

    The model was trained on romanized text: lower-case a to z and the apostrophe.
    Accents are dropped ("señor" is "senor"); digits and other scripts become ``ANY``,
    which matches whatever is sung there.
    """
    plain = unicodedata.normalize("NFD", word).lower()
    letters = _UNKNOWN.sub("", plain).strip("'")
    return letters or ANY


def windows(
    lines: Sequence[LyricLine], offset_ms: int, frames: int
) -> list[tuple[int, int] | None]:
    """For each line, the frames ``[start, end)`` to look for it in; None for no words.

    ``offset_ms`` is the track's own correction (positive: the lyrics are late against
    this audio), so a line stamped ``t`` is expected at ``t - offset_ms`` in the file.
    """
    found: list[tuple[int, int] | None] = []
    for index, line in enumerate(lines):
        if not line.text.split():
            found.append(None)
            continue
        start = line.t_ms - offset_ms
        following = lines[index + 1].t_ms - offset_ms if index + 1 < len(lines) else None
        end = start + LAST_LINE_MS if following is None else following
        first = max(0, (start - PAD_BEFORE_MS) // FRAME_MS)
        last = min(frames, -(-(end + PAD_AFTER_MS) // FRAME_MS))
        found.append((first, last) if last > first else None)
    return found


@dataclass(frozen=True, slots=True)
class WordSpan:
    """One word as the engine found it: frames ``[start, end)`` and how sure it was."""

    start: int
    end: int
    score: float


Words = tuple[tuple[int, int], ...]


def settle(
    found: Sequence[Sequence[WordSpan] | None], loudness: Sequence[float]
) -> list[Words | None]:
    """Word times in ms for each line, or None for a line that was not found well enough.

    ``found`` is the engine's answer for each line and ``loudness`` the voice's level in
    every frame. Three things are decided here:

    * a line counts only if its words average :data:`MIN_CONFIDENCE`;
    * each word lasts until the next one starts, since that is how it is shown;
    * the last word of a line is where a held note lives, and the model marks only the
      moment a letter is struck. So its end is moved on for as long as the voice stays
      up, and never past the first word of the next line that was found.
    """
    quiet = QUIET_SHARE * _loud(loudness)
    kept: list[Sequence[WordSpan] | None] = []
    for spans in found:
        sure = spans and sum(span.score for span in spans) / len(spans) >= MIN_CONFIDENCE
        kept.append(spans if sure else None)

    settled: list[Words | None] = []
    for index, spans in enumerate(kept):
        if spans is None:
            settled.append(None)
            continue
        limit = next((later[0].start for later in kept[index + 1 :] if later), len(loudness))
        end = spans[-1].end
        while quiet > 0 and end < limit and loudness[end] >= quiet:
            end += 1
        starts = [span.start for span in spans]
        ends = [*starts[1:], max(end, starts[-1] + 1)]
        settled.append(
            tuple((a * FRAME_MS, max(a, b) * FRAME_MS) for a, b in zip(starts, ends, strict=True))
        )
    return settled


def strong_words(text: str) -> int:
    """How many of the words a clean edit removes ``text`` has."""
    return len(_STRONG.findall(text.lower()))


def voice_share(loudness: Sequence[float]) -> float:
    """The share of the track's frames in which a voice is heard (:data:`AUDIBLE_VOICE`)."""
    if not loudness:
        return 0.0
    return sum(1 for level in loudness if level >= AUDIBLE_VOICE) / len(loudness)


def _loud(loudness: Sequence[float]) -> float:
    """The level of the track's loud singing: the 95th percentile of its frames."""
    if not loudness:
        return 0.0
    ordered = sorted(loudness)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
