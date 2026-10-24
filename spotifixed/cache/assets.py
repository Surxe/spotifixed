"""Typed access to what is cached for a track: its audio, its lyrics and its state.

The rule for "is this stage done" lives here and nowhere else: a provenance file
(``audio.json``, ``lyrics.json``) counts only when the file it names is present and
non-empty. A provenance file whose audio was deleted is treated as absent, so the next
sync simply fetches it again. There is no database to fall out of step.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ..errors import CacheError
from ..models import AudioAsset, LyricLine, LyricsInfo, LyricsKind, Playlist, Track, TrackState
from .store import CacheStore

# ------------------------------- tracklist -------------------------------- #


def read_playlist(store: CacheStore, playlist_id: str) -> Playlist:
    return Playlist.from_dict(store.read_json(store.playlist_json(playlist_id)))


def read_track(store: CacheStore, track_id: str) -> Track:
    return Track.from_dict(store.read_json(store.track_json(track_id)))


# ---------------------------------- audio ---------------------------------- #


def read_audio(store: CacheStore, track_id: str) -> AudioAsset | None:
    """The track's audio provenance, or ``None`` unless the audio file is really there."""
    data = store.read_json(store.audio_json(track_id), default=None)
    if not isinstance(data, dict):
        return None
    try:
        asset = AudioAsset.from_dict(data)
        present = store.has(store.track_file(track_id, asset.file))
    except (KeyError, TypeError, ValueError, CacheError):
        return None
    return asset if present else None


def audio_path(store: CacheStore, track_id: str, asset: AudioAsset) -> Path:
    return store.track_file(track_id, asset.file)


def write_audio(store: CacheStore, track_id: str, asset: AudioAsset) -> None:
    """Record provenance for an audio file already in place under its final name."""
    store.write_json(store.audio_json(track_id), asset.to_dict())
    store.remove(store.words_json(track_id))  # word times belong to the audio they were heard in


def clear_audio(store: CacheStore, track_id: str) -> None:
    data = store.read_json(store.audio_json(track_id), default=None)
    if isinstance(data, dict) and isinstance(data.get("file"), str):
        # A file name we would never have written is left alone, not followed.
        with contextlib.suppress(CacheError):
            store.remove(store.track_file(track_id, data["file"]))
    store.remove(store.audio_json(track_id))
    store.remove(store.words_json(track_id))


# ---------------------------------- lyrics --------------------------------- #

_LYRICS_FILES = {LyricsKind.SYNCED: "lyrics.lrc", LyricsKind.PLAIN: "lyrics.txt"}


def read_lyrics_info(store: CacheStore, track_id: str) -> LyricsInfo | None:
    """The track's lyrics provenance, or ``None`` when it is absent or its text is gone."""
    data = store.read_json(store.lyrics_json(track_id), default=None)
    if not isinstance(data, dict):
        return None
    try:
        info = LyricsInfo.from_dict(data)
        present = info.file is None or store.has(store.track_file(track_id, info.file))
    except (KeyError, TypeError, ValueError, CacheError):
        return None
    return info if present else None


def read_lyrics_text(store: CacheStore, track_id: str) -> tuple[LyricsInfo, str] | None:
    """Provenance plus the stored text; ``None`` when there is no text (or no lyrics)."""
    info = read_lyrics_info(store, track_id)
    if info is None or info.file is None:
        return None
    return info, store.track_file(track_id, info.file).read_text(encoding="utf-8")


def write_lyrics(
    store: CacheStore, track_id: str, info: LyricsInfo, text: str | None
) -> LyricsInfo:
    """Store lyrics text (when the kind has any) and its provenance. Returns what was saved."""
    clear_lyrics(store, track_id)
    name = _LYRICS_FILES.get(info.kind) if text else None
    if name:
        store.write_text(
            store.track_file(track_id, name), text if text.endswith("\n") else text + "\n"
        )
    saved = LyricsInfo.from_dict({**info.to_dict(), "file": name})
    store.write_json(store.lyrics_json(track_id), saved.to_dict())
    return saved


def clear_lyrics(store: CacheStore, track_id: str) -> None:
    for path in (
        store.lyrics_lrc(track_id),
        store.lyrics_txt(track_id),
        store.lyrics_json(track_id),
    ):
        store.remove(path)


# ---------------------------------- words ---------------------------------- #


def lyrics_digest(text: str) -> str:
    """What a ``words.json`` records of the lyric text it was made from."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def with_words(
    store: CacheStore, track_id: str, text: str, lines: Sequence[LyricLine]
) -> tuple[LyricLine, ...]:
    """``lines`` (parsed from ``text``) with the word times ``align`` stored for them.

    The stored times are used only if they were made from this very text; anything else
    (no file, an older text, a damaged file) leaves the lines as they are.
    """
    data = store.read_json(store.words_json(track_id), default=None)
    if not isinstance(data, dict) or data.get("lyrics_sha256") != lyrics_digest(text):
        return tuple(lines)
    stored = data.get("lines")
    if not isinstance(stored, list) or len(stored) != len(lines):
        return tuple(lines)
    merged = []
    for line, words in zip(lines, stored, strict=True):
        try:
            times = None if words is None else tuple((int(a), int(b)) for a, b in words)
        except (TypeError, ValueError):
            times = None
        usable = times is not None and len(times) == len(line.text.split())
        merged.append(replace(line, words=times) if usable else line)
    return tuple(merged)


# ---------------------------------- voice ---------------------------------- #

# Audio in which a voice is heard for less than this share of the track has no singing
# worth the name: a karaoke backing or an instrumental. Measured over the cache: such
# uploads come out under 0.1, songs with vocals over 0.4.
NO_VOICE_SHARE = 0.15


def voice_share(store: CacheStore, track_id: str) -> float | None:
    """How much of the track's audio has a voice in it, as ``align`` heard it, or ``None``.

    ``words.json`` is removed whenever the audio changes, so a share read from it is
    always about the file that is there now.
    """
    data = store.read_json(store.words_json(track_id), default=None)
    share = data.get("voice_share") if isinstance(data, dict) else None
    if isinstance(share, bool) or not isinstance(share, int | float):
        return None
    return float(share)


# Audio in which fewer than this share of the lyrics' strong words are heard is a clean
# edit. Measured over a 500-track playlist: clean edits come out under 0.2, nearly always
# at 0; explicit recordings at 0.6 and over. Between the two sit edits that mute some
# words and not others.
CLEAN_EDIT_SHARE = 0.55


def strong_words_heard(store: CacheStore, track_id: str) -> float | None:
    """The share of the lyrics' strong words heard in the audio, or ``None`` if not counted.

    ``align`` counts them only for lyrics with enough of them to judge by. Like
    :func:`voice_share`, this is always about the audio file that is there now.
    """
    data = store.read_json(store.words_json(track_id), default=None)
    counts = data.get("strong_words") if isinstance(data, dict) else None
    if not isinstance(counts, dict):
        return None
    in_lyrics, heard = counts.get("in_lyrics"), counts.get("heard")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (in_lyrics, heard)):
        return None
    return heard / in_lyrics if in_lyrics > 0 else None


def is_clean_edit(store: CacheStore, track_id: str) -> bool:
    """Whether ``align`` heard the track's audio leave out the lyrics' strong words."""
    share = strong_words_heard(store, track_id)
    return share is not None and share < CLEAN_EDIT_SHARE


def rejected_audio(store: CacheStore, track_id: str) -> list[dict]:
    """Audio once downloaded for the track and found unfit, oldest first.

    Each entry says why in ``reason``: ``no singing`` or ``clean edit`` (an entry written
    before there were reasons has none, and was for no singing).
    """
    data = store.read_json(store.rejected_audio_json(track_id), default=None)
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def reject_audio(
    store: CacheStore, track_id: str, asset: AudioAsset, reason: str, measured: dict[str, float]
) -> None:
    """Record that ``asset`` is unfit, so the audio stage looks for another version.

    ``measured`` is what the decision rested on, kept for whoever reads the file.
    """
    entry = {
        "source_id": asset.source_id,
        "url": asset.url,
        "title": asset.title,
        "channel": asset.channel,
        "reason": reason,
        **{key: round(value, 3) for key, value in measured.items()},
    }
    store.write_json(store.rejected_audio_json(track_id), [*rejected_audio(store, track_id), entry])


# ---------------------------------- state ---------------------------------- #


def read_track_state(store: CacheStore, track_id: str) -> TrackState:
    data = store.read_json(store.track_state(track_id), default=None)
    return TrackState.from_dict(data) if isinstance(data, dict) else TrackState()


def write_track_state(store: CacheStore, track_id: str, state: TrackState) -> None:
    store.write_json(store.track_state(track_id), state.to_dict())


# --------------------------------- overview -------------------------------- #


@dataclass(frozen=True, slots=True)
class TrackStatus:
    """Everything cached for one track, read in one go for ``status`` and the API."""

    track: Track
    audio: AudioAsset | None
    lyrics: LyricsInfo | None
    has_cover: bool
    state: TrackState

    @property
    def lyrics_kind(self) -> LyricsKind:
        return self.lyrics.kind if self.lyrics else LyricsKind.MISSING

    @property
    def playable(self) -> bool:
        return self.audio is not None


def track_status(store: CacheStore, track_id: str) -> TrackStatus:
    return TrackStatus(
        track=read_track(store, track_id),
        audio=read_audio(store, track_id),
        lyrics=read_lyrics_info(store, track_id),
        has_cover=store.has(store.track_cover(track_id)),
        state=read_track_state(store, track_id),
    )


def playlist_status(store: CacheStore, playlist: Playlist) -> list[TrackStatus]:
    """Status of each distinct track of ``playlist``, in playlist order."""
    return [track_status(store, track_id) for track_id in dict.fromkeys(playlist.track_ids)]
