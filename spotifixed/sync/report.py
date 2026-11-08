"""What ``sync`` and ``status`` print. Counts, IDs and titles only; never lyric text."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from ..cache import Miss
from ..cache.assets import TrackStatus
from ..models import LyricsKind, Playlist, Track, TrackState
from .changes import CarriedOver, TracklistChanges
from .pipeline import TrackReport
from .stages import Outcome
from .tracklist import TracklistResult


def tracklist_summary(result: TracklistResult) -> list[str]:
    playlist = result.tracklist.playlist
    origin = "fetched now" if result.fetched_now else "from cache"
    when = f", fetched {result.fetched_at}" if result.fetched_at else ""
    total = "unknown" if playlist.reported_total is None else str(playlist.reported_total)
    lines = [
        f"Playlist {playlist.id}: {playlist.name}",
        f"  tracklist     {origin} ({result.source}{when})",
        f"  tracks        {len(playlist.track_ids)} playable, {playlist.skipped} skipped, "
        f"source total {total}",
    ]
    duplicates = len(playlist.track_ids) - len(result.tracklist.tracks)
    if duplicates:
        lines.append(f"  duplicates    {duplicates} repeated entries share a cached track")
    return lines


def shortfall_warning(result: TracklistResult) -> str | None:
    """A line for stderr when the source returned fewer entries than the playlist holds."""
    playlist = result.tracklist.playlist
    if not playlist.shortfall:
        return None
    return (
        f"WARNING: the tracklist is incomplete: {playlist.shortfall} of "
        f"{playlist.reported_total} entries are missing from the source's response."
    )


def _song(track: Track) -> str:
    return f"{track.id}  {track.primary_artist} - {track.title}"


def _state_note(state: TrackState | None) -> str:
    if state is None:
        return ""
    bits = []
    if state.disabled:
        bits.append("turned off")
    if state.offset_ms:
        bits.append(f"offset {state.offset_ms:+d} ms")
    if state.audio_pin:
        bits.append("audio pinned")
    if state.notes:
        bits.append("notes")
    return f"  [{', '.join(bits)}]" if bits else ""


def changes_lines(
    changes: TracklistChanges,
    carried: Sequence[CarriedOver] = (),
    states: Mapping[str, TrackState] | None = None,
) -> list[str]:
    """What a refresh changed. ``states`` holds hand-made settings, keyed by track ID."""
    states = states or {}
    lines = [
        f"Changes since the previous tracklist: {changes.old_count} -> {changes.new_count} "
        f"tracks; {len(changes.added)} added, {len(changes.removed)} removed, "
        f"{len(changes.relinked)} under a new ID, {len(changes.changed)} with new details, "
        f"{changes.moved} moved"
    ]
    if changes.empty:
        lines[0] = "Changes since the previous tracklist: none"
        return lines
    by_relink = {item.relink.old.id: item for item in carried}
    for title, rows in (
        ("added", [_song(t) for t in changes.added]),
        (
            "removed (settings and files are kept)",
            [_song(t) + _state_note(states.get(t.id)) for t in changes.removed],
        ),
        (
            "under a new ID",
            [
                f"{r.old.id} -> {_song(r.new)}{_state_note(states.get(r.old.id))}"
                + (
                    f"  carried: {'settings, ' if by_relink[r.old.id].state else ''}"
                    f"{by_relink[r.old.id].files} files"
                    if r.old.id in by_relink
                    else ""
                )
                for r in changes.relinked
            ],
        ),
        (
            "new details",
            [
                f"{_song(c.new)}: {', '.join(c.fields)}{_state_note(states.get(c.new.id))}"
                for c in changes.changed
            ],
        ),
    ):
        if rows:
            lines.append(f"  {title}:")
            lines += [f"    {row}" for row in rows]
    return lines


def progress_line(index: int, total: int, report: TrackReport) -> str:
    """One line per finished track: what each stage did, then which track it was."""
    width = len(str(total))
    stages = "  ".join(
        f"{name}: {result.outcome.value}" + (f" ({result.note})" if result.note else "")
        for name, result in report.results.items()
    )
    track = report.track
    return (
        f"[{index:>{width}}/{total}] {track.id}  {stages}  | {track.primary_artist} - {track.title}"
    )


def sync_totals(reports: Sequence[TrackReport], stage_names: Sequence[str]) -> list[str]:
    """Per stage, how many tracks ended in each outcome."""
    lines = [f"Processed {len(reports)} tracks."]
    for name in stage_names:
        counts = Counter(r.results[name].outcome for r in reports if name in r.results)
        parts = [f"{counts[outcome]} {outcome.value}" for outcome in Outcome if counts[outcome]]
        lines.append(f"  {name:<13} {', '.join(parts) or 'nothing to do'}")
    return lines


def _audio_cell(status: TrackStatus, miss: Miss | None) -> str:
    if status.audio:
        return "pinned" if status.audio.pinned else "ok"
    if miss:
        return "review" if miss.reason == "needs_review" else "miss"
    return "-"


def _lyrics_cell(status: TrackStatus) -> str:
    if status.lyrics is None:
        return "-"
    if status.lyrics.kind is LyricsKind.SYNCED and status.lyrics.timing_confidence == "low":
        return "synced?"
    return status.lyrics.kind.value


def coverage(statuses: Sequence[TrackStatus]) -> dict[str, int]:
    """Counts for one playlist: how many tracks have audio, synced lyrics, and so on."""
    kinds = Counter(status.lyrics_kind for status in statuses)
    synced = [s for s in statuses if s.lyrics_kind is LyricsKind.SYNCED]
    return {
        "tracks": len(statuses),
        "audio": sum(1 for s in statuses if s.audio),
        "pinned": sum(1 for s in statuses if s.audio and s.audio.pinned),
        "synced": kinds[LyricsKind.SYNCED],
        "synced_low_confidence": sum(1 for s in synced if s.lyrics.timing_confidence == "low"),
        "plain": kinds[LyricsKind.PLAIN],
        "instrumental": kinds[LyricsKind.INSTRUMENTAL],
        "no_lyrics": kinds[LyricsKind.MISSING],
        "cover": sum(1 for s in statuses if s.has_cover),
        "ready": sum(1 for s in statuses if s.audio and s.lyrics_kind is LyricsKind.SYNCED),
    }


def coverage_line(playlist: Playlist, statuses: Sequence[TrackStatus]) -> str:
    """One line per playlist, for ``status`` with no argument."""
    c = coverage(statuses)
    return (
        f"{playlist.id}  {c['tracks']:>4} tracks  {c['audio']:>4} audio  "
        f"{c['synced']:>4} synced  {c['cover']:>4} covers  {playlist.name}"
    )


def status_table(
    playlist: Playlist, statuses: Sequence[TrackStatus], misses: Sequence[Miss]
) -> list[str]:
    """Per track: audio state, lyrics kind, the audio's length against Spotify's; then totals.

    ``synced?`` marks synced lyrics whose timing confidence is low. Track IDs and titles
    only; no lyric text.
    """
    audio_miss = {miss.track_id: miss for miss in misses if miss.stage == "audio"}
    width = len(str(len(statuses)))
    lines = [
        f"Playlist {playlist.id}: {playlist.name}",
        f"{'#':>{width}}  {'track':<22}  {'audio':<6}  {'lyrics':<12}  {'delta':>6}  "
        f"{'offset':>6}  title",
    ]
    review = 0
    for index, status in enumerate(statuses, start=1):
        track = status.track
        delta = f"{status.audio.duration_s - track.duration_s:+.1f}s" if status.audio else ""
        offset = f"{status.state.offset_ms:+d}" if status.state.offset_ms else ""
        audio = _audio_cell(status, audio_miss.get(track.id))
        # Counted from the cells, not the miss log: the log covers every playlist.
        review += audio == "review"
        lines.append(
            f"{index:>{width}}  {track.id:<22}  {audio:<6}  "
            f"{_lyrics_cell(status):<12}  {delta:>6}  {offset:>6}  "
            f"{track.primary_artist} - {track.title}"
        )

    c = coverage(statuses)
    lines += [
        "",
        f"{c['tracks']} tracks: {c['ready']} ready (audio and synced lyrics)",
        f"  audio    {c['audio']} cached ({c['pinned']} pinned), {review} need review, "
        f"{c['tracks'] - c['audio'] - review} missing",
        f"  lyrics   {c['synced']} synced ({c['synced_low_confidence']} low confidence), "
        f"{c['plain']} plain, {c['instrumental']} instrumental, {c['no_lyrics']} none",
        f"  covers   {c['cover']}",
    ]
    return lines
