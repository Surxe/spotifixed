"""Plain data types shared by every layer.

This module imports nothing from the rest of the package, and nothing here performs
I/O. The ``to_dict`` / ``from_dict`` pairs define the JSON written to the cache and sent
over the HTTP API, so changing a field name here is a change to both contracts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class LyricsKind(StrEnum):
    """What we hold for a track's lyrics. Only ``SYNCED`` can drive the overlay."""

    SYNCED = "synced"
    PLAIN = "plain"
    INSTRUMENTAL = "instrumental"
    MISSING = "missing"


class PlayerStatus(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Track:
    """One playlist entry, normalized from whatever the tracklist source returned."""

    id: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int
    album: str | None = None
    cover_url: str | None = None
    explicit: bool = False

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["artists"] = list(self.artists)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        return cls(
            id=data["id"],
            title=data["title"],
            artists=tuple(data.get("artists") or ()),
            duration_ms=int(data["duration_ms"]),
            album=data.get("album"),
            cover_url=data.get("cover_url"),
            explicit=bool(data.get("explicit", False)),
        )


@dataclass(frozen=True, slots=True)
class Playlist:
    """Playlist metadata plus its track IDs in playlist order.

    Tracks themselves are stored once each under ``cache/tracks/<id>/`` so a song shared
    between playlists is fetched once; the playlist only holds the order.
    """

    id: str
    name: str
    track_ids: tuple[str, ...]
    owner: str | None = None
    url: str | None = None
    image_url: str | None = None
    # What the source says the playlist holds. Compared against len(track_ids) to detect
    # a truncated fetch; see the tracklist source for why this must be Spotify's own total.
    reported_total: int | None = None
    # Entries the source returned that were dropped (unplayable, or not a music track).
    skipped: int = 0

    @property
    def shortfall(self) -> int:
        """Entries the source says exist but did not return; 0 when complete or unknown."""
        if self.reported_total is None:
            return 0
        return max(0, self.reported_total - len(self.track_ids) - self.skipped)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["track_ids"] = list(self.track_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Playlist:
        return cls(
            id=data["id"],
            name=data["name"],
            track_ids=tuple(data.get("track_ids") or ()),
            owner=data.get("owner"),
            url=data.get("url"),
            image_url=data.get("image_url"),
            reported_total=data.get("reported_total"),
            skipped=int(data.get("skipped", 0)),
        )


@dataclass(frozen=True, slots=True)
class LyricLine:
    """One timed line. Empty ``text`` is a deliberate gap: show nothing from ``t_ms``.

    ``words``, when the track has been aligned and this line was found, is when each of
    its words is sung: ``(start_ms, end_ms)`` in the audio file's own time, which the
    track's offset does not shift. ``t_ms`` is the lyric file's time, which it does.
    """

    t_ms: int
    text: str
    words: tuple[tuple[int, int], ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"t_ms": self.t_ms, "text": self.text}
        if self.words is not None:
            data["words"] = [list(word) for word in self.words]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LyricLine:
        words = data.get("words")
        return cls(
            int(data["t_ms"]),
            str(data["text"]),
            None if words is None else tuple((int(a), int(b)) for a, b in words),
        )


@dataclass(frozen=True, slots=True)
class Lyrics:
    kind: LyricsKind
    lines: tuple[LyricLine, ...] = ()
    plain: str | None = None


@dataclass(frozen=True, slots=True)
class AudioAsset:
    """Provenance of a track's cached audio file (``audio.json``)."""

    file: str  # file name inside the track's cache directory
    duration_s: float  # measured from the file, not taken from the source's metadata
    source: str
    source_id: str | None = None
    url: str | None = None
    title: str | None = None
    channel: str | None = None
    codec: str | None = None
    score: float | None = None
    pinned: bool = False
    fetched_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AudioAsset:
        known = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        known["duration_s"] = float(known["duration_s"])
        return cls(**known)


@dataclass(frozen=True, slots=True)
class LyricsInfo:
    """Provenance of a track's cached lyrics (``lyrics.json``).

    ``reference`` says which duration the record was chosen against: ``audio`` (the real
    file, which is what keeps the overlay in time) or ``spotify`` (no audio yet).
    ``timing_confidence`` is ``low`` when even the best synced record is further from
    that duration than the tolerance; it is ``None`` for lyrics that carry no timing.
    """

    kind: LyricsKind
    source: str
    file: str | None = None
    record_id: str | None = None
    record_duration_s: float | None = None
    reference: str = "spotify"
    reference_duration_s: float | None = None
    duration_delta_s: float | None = None
    timing_confidence: str | None = None
    chosen_by: str | None = None
    fetched_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LyricsInfo:
        known = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        known["kind"] = LyricsKind(known["kind"])
        return cls(**known)


@dataclass(frozen=True, slots=True)
class TrackState:
    """Hand-authored per-track values (``state/tracks/<id>.json``); survive a cache wipe."""

    offset_ms: int = 0
    audio_pin: str | None = None
    notes: str = ""
    # Turned off from the page: left out of every playlist's queue until turned back on.
    disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackState:
        return cls(
            offset_ms=int(data.get("offset_ms") or 0),
            audio_pin=data.get("audio_pin") or None,
            notes=str(data.get("notes") or ""),
            disabled=data.get("disabled") is True,
        )


@dataclass(frozen=True, slots=True)
class PlaybackState:
    """The wire contract between the daemon and its clients.

    Clients do not poll for position. They take ``position_s`` as true at
    ``captured_at_epoch_ms`` and extrapolate with ``speed`` while ``paused`` is false;
    both ends run on one machine and so share a wall clock.
    """

    version: int = 0
    status: PlayerStatus = PlayerStatus.IDLE
    paused: bool = True
    playlist_id: str | None = None
    track_id: str | None = None
    queue_index: int | None = None
    queue_length: int = 0
    position_s: float = 0.0
    duration_s: float | None = None
    speed: float = 1.0
    volume: float = 100.0
    captured_at_epoch_ms: int = 0
    lyrics_kind: LyricsKind = LyricsKind.MISSING
    offset_ms: int = 0
    error: str | None = field(default=None)
    # A fade-in under way: it began when this track was at ``fade_from_s`` and rises
    # over ``fade_in_s`` seconds of playback. Set on the snapshots published while it
    # runs, so a client can draw the rising level itself; 0 means no fade.
    fade_from_s: float = 0.0
    fade_in_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["lyrics_kind"] = self.lyrics_kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaybackState:
        known = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        if "status" in known:
            known["status"] = PlayerStatus(known["status"])
        if "lyrics_kind" in known:
            known["lyrics_kind"] = LyricsKind(known["lyrics_kind"])
        return cls(**known)

    def position_at(self, now_epoch_ms: int) -> float:
        """Extrapolated playback position at ``now_epoch_ms``."""
        if self.paused or self.status is not PlayerStatus.PLAYING:
            return self.position_s
        elapsed_s = max(0, now_epoch_ms - self.captured_at_epoch_ms) / 1000
        position = self.position_s + elapsed_s * self.speed
        return min(position, self.duration_s) if self.duration_s else position
