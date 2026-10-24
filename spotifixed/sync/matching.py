"""Choosing between candidates. Pure scoring: no I/O, tested on canned lists.

Audio: a search returns the album recording, music videos with intros and outros, live
takes, covers and remixes, all under similar titles. Lyric timestamps are made against
the album recording, so that is the one to find: right length, uploaded as album audio,
and not labelled as something else.

Lyrics: a provider returns several records for one song, typically the same words timed
against different releases whose lengths differ by a few seconds. The record whose
duration is closest to the audio actually being played is the one whose timestamps will
line up, so duration is the tie-breaker after "has timed lyrics at all".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..lyrics import parse_lrc
from ..models import LyricsKind, Track
from ..sources.audio import AudioCandidate
from ..sources.lyrics import LyricsRecord

# ------------------------------ name matching ------------------------------ #

_BRACKETED = re.compile(r"\s*[(\[][^)\]]*[)\]]")
# " - Remastered 2011", " - Single Version", " - Radio Edit", " - Live at ..."
_DASH_SUFFIX = re.compile(
    r"\s+[-–—]\s+(?=[^-–—]*\b("
    r"remaster(?:ed)?|version|edit|mix|remix|mono|stereo|single|radio|bonus|deluxe|live|"
    r"acoustic|demo|feat|featuring|from|with|soundtrack|anniversary"
    r")\b).*$",
    re.IGNORECASE,
)
_FEATURING = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)


def _fold(text: str) -> str:
    """Case-fold, drop accents and punctuation, collapse spaces."""
    decomposed = unicodedata.normalize("NFKD", text.replace("&", " and "))
    kept = (c if c.isalnum() else " " for c in decomposed if not unicodedata.combining(c))
    return " ".join("".join(kept).casefold().split())


def normalize_title(title: str) -> str:
    """A title reduced to what identifies the song, for comparing across catalogues.

    Drops bracketed suffixes ("(Remastered 2011)", "[feat. X]"), release qualifiers
    after a dash ("- Single Version"), a trailing "feat. X", accents, punctuation and
    case. A title that is nothing but such decoration is kept as it is.
    """
    stripped = _FEATURING.sub("", _DASH_SUFFIX.sub("", _BRACKETED.sub("", title)))
    return _fold(stripped) or _fold(title)


def normalize_artist(name: str) -> str:
    return _fold(name)


def is_same_song(track: Track, record: LyricsRecord) -> bool:
    """Whether a provider's record names the same song and artist as ``track``."""
    if normalize_title(track.title) != normalize_title(record.title):
        return False
    theirs = normalize_artist(record.artist)
    ours = [normalize_artist(name) for name in track.artists]
    if not theirs or not ours:
        return False
    # Providers credit collaborations in one string ("A feat. B", "A, B", "A & B"), so
    # the primary artist appearing in it as whole words is a match, as is the record
    # crediting any one of the track's artists alone.
    return theirs in ours or f" {ours[0]} " in f" {theirs} "


def matching_records(track: Track, records: Iterable[LyricsRecord]) -> list[LyricsRecord]:
    return [record for record in records if is_same_song(track, record)]


# ------------------------------ audio scoring ------------------------------ #

# Points. A candidate at or above the threshold is downloaded without asking; below it
# the track is flagged for review instead, with the best candidates listed for pinning.
_DURATION_POINTS = 40.0  # full inside the tolerance, sliding to zero at the reject bound
_TITLE_POINTS = 30.0  # the song's title appears in the candidate's title ...
_TITLE_ABSENT = -30.0  # ... and if it does not, this is probably another song
_ARTIST_POINTS = 15.0  # an artist's name appears in the title or the channel
_ALBUM_AUDIO_POINTS = 25.0  # uploaded as the album recording
_AUDIO_LABEL_POINTS = 5.0  # titled "(Audio)": the recording without a video's extras
_OTHER_VERSION = -60.0  # labelled live, remix, cover, ... when the track is not
_EXPLICIT_LABEL_POINTS = 10.0  # titled "(Explicit)" for an explicit track
AUDIO_REVIEW_THRESHOLD = 60.0

# Labels that mean "not the album recording". Each is ignored when the track's own
# title carries it. "lyrics" is deliberately absent: a lyric video is usually the album
# audio, so it is neutral.
_OTHER_VERSION_WORDS = (
    "live", "remix", "cover", "karaoke", "instrumental", "sped up", "slowed", "nightcore",
    "8d", "reverb", "acoustic", "reaction", "tv mix", "backing track", "off vocal",
)  # fmt: skip
# How a karaoke or tribute recording credits the artist whose song it is. The artist's
# name follows the phrase, so it says nothing about who is performing.
_CREDIT_PHRASES = (
    "originally performed by", "made famous by", "made popular by", "in the style of",
    "as performed by", "originally by",
)  # fmt: skip
# Words that mark a whole channel as another version's. Narrower than the title's list:
# a channel called "Live Nation" or "Acoustic Records" publishes the real thing too.
_OTHER_VERSION_CHANNEL_WORDS = ("karaoke", "instrumental", "instrumentals", "backing tracks")
# For an explicit track, labels that mean the words were cleaned up, and labels that
# promise they were not. Ignored when an artist's name carries them ("Clean Bandit").
_CLEAN_WORDS = ("clean", "edited", "censored", "radio edit")
_EXPLICIT_WORDS = ("explicit", "dirty", "uncensored")


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: AudioCandidate
    score: float
    duration_delta_s: float  # candidate minus track
    album_audio: bool
    reasons: tuple[str, ...]  # what moved the score, for the review listing

    @property
    def acceptable(self) -> bool:
        return self.score >= AUDIO_REVIEW_THRESHOLD

    def summary(self) -> dict[str, Any]:
        """What a person needs to judge this candidate by hand (stored with a miss)."""
        c = self.candidate
        return {
            "id": c.id,
            "url": c.url,
            "title": c.title,
            "channel": c.channel,
            "duration_s": c.duration_s,
            "score": round(self.score, 1),
            "reasons": list(self.reasons),
        }


def _has_phrase(haystack: str, phrase: str) -> bool:
    """Whole-word containment over already folded text."""
    return bool(phrase) and f" {phrase} " in f" {haystack} "


def _before_credit(text: str) -> str:
    """Folded text up to the first credit phrase: the part that names the performer."""
    cut = min(
        (
            f" {text} ".find(f" {phrase} ")
            for phrase in _CREDIT_PHRASES
            if _has_phrase(text, phrase)
        ),
        default=len(text),
    )
    return text[:cut].strip()


def _other_version_labels(
    title: str, channel: str, track_title: str, artists: str, explicit: bool
) -> list[str]:
    """What marks a candidate as not the album recording, ignoring what the track carries."""
    labels = [w for w in _OTHER_VERSION_WORDS + _CREDIT_PHRASES if _has_phrase(title, w)]
    labels += [w for w in _CREDIT_PHRASES if _has_phrase(channel, w)]
    labels += [w for w in _OTHER_VERSION_CHANNEL_WORDS if _has_phrase(channel, w)]
    if explicit:
        labels += [w for w in _CLEAN_WORDS if _has_phrase(title, w) and not _has_phrase(artists, w)]
    return [w for w in dict.fromkeys(labels) if not _has_phrase(track_title, w)]


def is_album_audio(candidate: AudioCandidate) -> bool:
    """Auto-generated album uploads: a "- Topic" channel, or the label's standard credit."""
    return candidate.channel.rstrip().endswith(" - Topic") or (
        candidate.description.lstrip().startswith("Provided to YouTube by")
    )


def score_audio(
    track: Track,
    candidate: AudioCandidate,
    *,
    tolerance_s: float,
    reject_s: float,
    explicit: bool | None = None,
) -> ScoredCandidate | None:
    """Score one candidate, or ``None`` when it is rejected outright.

    Rejected: no duration, or further from the track's length than ``reject_s``. A
    recording that far off is a different edit, and its lyrics would never line up.

    ``explicit`` (by default the track's own flag) says the explicit recording is wanted:
    a clean edit is then another version, and a title promising the explicit one scores.
    """
    explicit = track.explicit if explicit is None else explicit
    if candidate.duration_s is None:
        return None
    delta = candidate.duration_s - track.duration_s
    distance = abs(delta)
    if distance > reject_s:
        return None

    reasons: list[str] = []
    if distance <= tolerance_s:
        score = _DURATION_POINTS
    else:
        score = _DURATION_POINTS * (reject_s - distance) / (reject_s - tolerance_s)
    reasons.append(f"duration {delta:+.0f}s")

    title = _fold(candidate.title)
    channel = _fold(candidate.channel)
    track_title = _fold(track.title)
    # Either form of the track's title will do: with its decoration stripped ("Song" for
    # "Song (feat. X)"), or whole, for a title whose brackets are part of its name.
    if _has_phrase(title, normalize_title(track.title)) or _has_phrase(title, track_title):
        score += _TITLE_POINTS
    else:
        score += _TITLE_ABSENT
        reasons.append("title differs")

    # A name after "made famous by" credits the song's artist, not the one performing it.
    performer_title, performer_channel = _before_credit(title), _before_credit(channel)
    if any(
        _has_phrase(performer_title, a) or _has_phrase(performer_channel, a)
        for a in map(_fold, track.artists)
    ):
        score += _ARTIST_POINTS
    else:
        reasons.append("no artist name")

    album_audio = is_album_audio(candidate)
    if album_audio:
        score += _ALBUM_AUDIO_POINTS
        reasons.append("album audio")

    elif _has_phrase(title, "audio"):
        score += _AUDIO_LABEL_POINTS
        reasons.append("audio upload")

    artists = " ".join(map(_fold, track.artists))
    labels = _other_version_labels(title, channel, track_title, artists, explicit)
    if explicit and any(_has_phrase(title, w) for w in _EXPLICIT_WORDS):
        score += _EXPLICIT_LABEL_POINTS
        reasons.append("labelled explicit")
    if labels:
        score += _OTHER_VERSION
        reasons.append("labelled " + ", ".join(labels))

    return ScoredCandidate(candidate, score, round(delta, 3), album_audio, tuple(reasons))


def rank_audio(
    track: Track,
    candidates: Iterable[AudioCandidate],
    *,
    tolerance_s: float,
    reject_s: float,
    explicit: bool | None = None,
) -> list[ScoredCandidate]:
    """Every candidate that survives rejection, best first (``explicit``: see score_audio).

    Equal scores go to the one closer to the track's length, then to search order.
    """
    scored = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.id in seen:
            continue
        seen.add(candidate.id)
        result = score_audio(
            track, candidate, tolerance_s=tolerance_s, reject_s=reject_s, explicit=explicit
        )
        if result is not None:
            scored.append(result)
    scored.sort(key=lambda item: (-item.score, abs(item.duration_delta_s)))  # stable
    return scored


# ----------------------------- lyric selection ----------------------------- #

_KIND_RANK = {LyricsKind.SYNCED: 0, LyricsKind.PLAIN: 1, LyricsKind.INSTRUMENTAL: 2}


@dataclass(frozen=True, slots=True)
class LyricsChoice:
    record: LyricsRecord
    kind: LyricsKind
    duration_delta_s: float | None  # record minus reference; None when the record has none
    timing_confidence: str | None  # "high" | "low" for synced lyrics, else None


def record_kind(record: LyricsRecord) -> LyricsKind:
    """What a record can actually provide. Timed text that parses to nothing is not synced."""
    if record.synced and parse_lrc(record.synced):
        return LyricsKind.SYNCED
    if record.plain and record.plain.strip():
        return LyricsKind.PLAIN
    if record.instrumental:
        return LyricsKind.INSTRUMENTAL
    return LyricsKind.MISSING


def choose_lyrics(
    records: Iterable[LyricsRecord], reference_duration_s: float, tolerance_s: float
) -> LyricsChoice | None:
    """Pick the best of ``records`` (already known to be the right song), or ``None``.

    Synced beats plain beats instrumental; within a kind the smallest distance from
    ``reference_duration_s`` wins, and a record with no duration loses to one that has
    it. A synced record outside ``tolerance_s`` is still chosen, flagged low confidence:
    slightly late lyrics that can be nudged are better than none.
    """
    best: tuple[tuple[int, float], LyricsChoice] | None = None
    seen: set[str] = set()
    for record in records:
        kind = record_kind(record)
        if kind is LyricsKind.MISSING or record.id in seen:
            continue
        seen.add(record.id)
        delta = None if record.duration_s is None else record.duration_s - reference_duration_s
        distance = float("inf") if delta is None else abs(delta)
        confidence = None
        if kind is LyricsKind.SYNCED:
            confidence = "high" if distance <= tolerance_s else "low"
        key = (_KIND_RANK[kind], distance)
        if best is None or key < best[0]:  # strict: an earlier record wins a tie
            rounded = None if delta is None else round(delta, 3)
            best = (key, LyricsChoice(record, kind, rounded, confidence))
    return best[1] if best else None
