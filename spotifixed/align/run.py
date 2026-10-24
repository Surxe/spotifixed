"""Go through tracks, align the ones that need it, and keep the result in the cache.

A track is aligned when it has synced lyrics and audio. Its ``words.json`` records what
it was made from (the lyric text, the audio file, the engine), and a track whose record
still matches is skipped, so the command can be stopped and run again.

It also records how much of the audio has a voice in it (``voice_share``). Audio with
too little is reported as having no singing; the next ``sync`` replaces it with another
version (see the audio stage).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..cache import CacheStore, assets
from ..errors import PlaylistPlayerError
from ..lyrics import parse_lrc
from ..models import LyricsKind
from .plan import MIN_STRONG_WORDS, WordSpan, settle, spell, strong_words, voice_share, windows

# 2: records voice_share. 3: records strong_words.
VERSION = 3


class Engine(Protocol):
    name: str

    def align(
        self,
        audio: Path,
        spelled: Sequence[Sequence[str]],
        windows_for: Any,
        *,
        transcribe: bool = False,
    ) -> tuple[list[list[WordSpan] | None], list[float], str | None]: ...


@dataclass(frozen=True, slots=True)
class AlignReport:
    track_id: str
    outcome: str  # "aligned", "current", "skipped" or "failed"
    detail: str = ""
    lines: int = 0  # lines with words
    placed: int = 0  # of those, the ones given word times


def _made_from(text: str, audio: Path, offset_ms: int, engine: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "engine": engine,
        "lyrics_sha256": assets.lyrics_digest(text),
        "audio": {"file": audio.name, "bytes": audio.stat().st_size},
        "offset_ms": offset_ms,
    }


def align_track(
    store: CacheStore,
    track_id: str,
    engine: Callable[[], Engine],
    name: str,
    *,
    force: bool,
    listen_for_clean_edits: bool = True,
) -> AlignReport:
    status = assets.track_status(store, track_id)
    stored = assets.read_lyrics_text(store, track_id)
    if status.lyrics_kind is not LyricsKind.SYNCED or stored is None:
        return AlignReport(track_id, "skipped", "no synced lyrics")
    if status.audio is None:
        return AlignReport(track_id, "skipped", "no audio")
    text = stored[1]
    audio = assets.audio_path(store, track_id, status.audio)
    offset_ms = status.state.offset_ms
    made_from = _made_from(text, audio, offset_ms, name)

    previous = store.read_json(store.words_json(track_id), default=None)
    current = isinstance(previous, dict) and all(
        previous.get(key) == value for key, value in made_from.items()
    )
    if current and not force:
        return AlignReport(track_id, "current")

    lines = parse_lrc(text)
    spelled = [[spell(word) for word in line.text.split()] for line in lines]
    in_lyrics = strong_words(" ".join(line.text for line in lines))
    transcribe = listen_for_clean_edits and in_lyrics >= MIN_STRONG_WORDS
    listener = engine()  # outside the try: an engine that cannot be built stops the run
    try:
        found, loudness, said = listener.align(
            audio, spelled, lambda frames: windows(lines, offset_ms, frames), transcribe=transcribe
        )
    except PlaylistPlayerError as exc:
        return AlignReport(track_id, "failed", str(exc))
    words = settle(found, loudness)
    share = voice_share(loudness)
    record: dict[str, Any] = {**made_from, "voice_share": round(share, 3)}
    if said is not None:
        record["strong_words"] = {"in_lyrics": in_lyrics, "heard": strong_words(said)}
    record["lines"] = [None if line is None else [list(w) for w in line] for line in words]
    store.write_json(store.words_json(track_id), record)

    detail = ""
    if not status.audio.pinned:
        if share < assets.NO_VOICE_SHARE:
            detail = "no singing heard; sync will look for another version"
        elif assets.is_clean_edit(store, track_id):
            detail = "sounds like a clean edit; sync will look for the explicit version"
    return AlignReport(
        track_id,
        "aligned",
        detail,
        lines=sum(1 for line in spelled if line),
        placed=sum(1 for line in words if line),
    )


def align_tracks(
    store: CacheStore,
    track_ids: Iterable[str],
    engine: Callable[[], Engine],
    name: str,
    *,
    force: bool = False,
    listen_for_clean_edits: bool = True,
    on_report: Callable[[AlignReport], None] = lambda report: None,
) -> list[AlignReport]:
    """Align each track once (a playlist may repeat one). ``engine`` is built on first use,
    so a run in which everything is current never loads a model."""
    built: list[Engine] = []

    def get() -> Engine:
        if not built:
            built.append(engine())
        return built[0]

    reports = []
    for track_id in dict.fromkeys(track_ids):
        report = align_track(
            store, track_id, get, name, force=force, listen_for_clean_edits=listen_for_clean_edits
        )
        reports.append(report)
        on_report(report)
    return reports


def summary(reports: Sequence[AlignReport]) -> str:
    count = {name: sum(1 for r in reports if r.outcome == name) for name in
             ("aligned", "current", "skipped", "failed")}  # fmt: skip
    lines = sum(r.lines for r in reports)
    placed = sum(r.placed for r in reports)
    share = f", {placed} of {lines} lines placed ({placed / lines:.0%})" if lines else ""
    return (
        f"{count['aligned']} aligned{share}; {count['current']} already current, "
        f"{count['skipped']} skipped, {count['failed']} failed"
    )
