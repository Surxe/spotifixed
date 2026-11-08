"""Sync step one: a playlist reference becomes a normalized playlist on disk.

The provider's response is kept verbatim as ``tracklist.raw.json`` and is the source of
truth. While it exists nothing is fetched and no token is needed: every sync re-parses
it and brings ``playlist.json`` and the per-track ``track.json`` files in line, so a
parser fix reaches the normalized files without paying for another fetch. Only an
explicit refresh replaces it, and a refresh that fails leaves the old copy untouched.

A refresh that succeeds first archives the response it replaces under ``history/``,
then reports what changed between the two (see :mod:`.changes`) and carries hand-made
settings and cached files across to tracks Spotify gave a new ID.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..cache import CacheStore
from ..errors import SourceError
from ..sources.tracklist import Tracklist, TracklistSource, playlist_url
from .changes import CarriedOver, TracklistChanges, carry_over, compare

SourceFactory = Callable[[str], TracklistSource]


@dataclass(frozen=True, slots=True)
class TracklistResult:
    tracklist: Tracklist
    source: str
    fetched_at: str | None
    # True when this call paid for a fetch rather than reading the saved response.
    fetched_now: bool
    # Set when a refresh replaced a cached tracklist: what changed, and what was carried.
    changes: TracklistChanges | None = None
    carried: tuple[CarriedOver, ...] = ()


def tracklist_is_cached(store: CacheStore, playlist_id: str) -> bool:
    return store.has(store.tracklist_raw(playlist_id))


def load_tracklist(
    store: CacheStore,
    playlist_id: str,
    source_for: SourceFactory,
    default_source: str,
    *,
    refresh: bool = False,
    write: bool = True,
) -> TracklistResult:
    """Return the playlist's tracklist, fetching it only when it is not cached.

    ``source_for`` builds the source registered under a name. ``default_source`` is used
    for a fetch; a cached response is read by the source that produced it. With
    ``write=False`` (a dry run) nothing on disk changes, so the tracklist must already be
    cached.
    """
    raw_path = store.tracklist_raw(playlist_id)
    meta_path = store.tracklist_meta(playlist_id)
    previous: Tracklist | None = None

    if tracklist_is_cached(store, playlist_id) and not refresh:
        meta = store.read_json(meta_path, default={})
        # A response saved before the meta file existed can only be from the default.
        source = source_for(meta.get("actor") or default_source)
        raw = raw_path.read_bytes()
        fetched_now = False
    elif not write:
        raise SourceError(
            f"the tracklist for {playlist_id} is not cached, and fetching it writes to the cache"
        )
    else:
        source = source_for(default_source)
        raw = source.fetch_raw(playlist_id)
        if tracklist_is_cached(store, playlist_id):
            # Only once the fetch succeeded: a failed refresh leaves everything as it was.
            previous = _archive_previous(store, playlist_id, source_for, default_source)
        # Saved before parsing: whatever the parser makes of it, the paid result is kept.
        store.write_bytes(raw_path, raw)
        meta = {
            "actor": source.name,
            "playlist_url": playlist_url(playlist_id),
            "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        store.write_json(meta_path, meta)
        fetched_now = True

    tracklist = source.parse(raw, playlist_id)
    changes = compare(previous, tracklist) if previous is not None else None
    carried: tuple[CarriedOver, ...] = ()
    if write:
        if changes is not None:
            # Before the new track.json files exist, so a carried track is never half-copied.
            carried = tuple(carry_over(store, changes.relinked))
        _write_normalized(store, tracklist)
    return TracklistResult(
        tracklist, source.name, meta.get("fetched_at"), fetched_now, changes, carried
    )


def history_stamp(fetched_at: str | None) -> str:
    """A file-name-safe stamp for an archived tracklist: ``20261012T211809Z``."""
    if fetched_at:
        stamp = "".join(c for c in fetched_at if c.isalnum())
        if stamp:
            return stamp
    return "unknown" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _read_cached(
    store: CacheStore,
    raw_path: Path,
    meta_path: Path,
    playlist_id: str,
    source_for: SourceFactory,
    default_source: str,
) -> tuple[Tracklist, dict[str, Any], bytes]:
    meta = store.read_json(meta_path, default={})
    raw = raw_path.read_bytes()
    source = source_for(meta.get("actor") or default_source)
    return source.parse(raw, playlist_id), meta, raw


def _archive_previous(
    store: CacheStore, playlist_id: str, source_for: SourceFactory, default_source: str
) -> Tracklist:
    """Copy the cached response into ``history/`` and return it parsed."""
    tracklist, meta, raw = _read_cached(
        store,
        store.tracklist_raw(playlist_id),
        store.tracklist_meta(playlist_id),
        playlist_id,
        source_for,
        default_source,
    )
    stamp = history_stamp(meta.get("fetched_at"))
    raw_dest, meta_dest = store.tracklist_history(playlist_id, stamp)
    if not raw_dest.exists():
        store.write_bytes(raw_dest, raw)
        store.write_json(meta_dest, meta)
    return tracklist


def last_changes(
    store: CacheStore, playlist_id: str, source_for: SourceFactory, default_source: str
) -> tuple[str, TracklistChanges] | None:
    """Compare the cached tracklist with the most recently archived one.

    Returns the archive's stamp and the changes, or ``None`` when nothing is archived.
    Reads only; needs no token.
    """
    stamps = store.tracklist_history_stamps(playlist_id)
    if not stamps or not tracklist_is_cached(store, playlist_id):
        return None
    raw_path, meta_path = store.tracklist_history(playlist_id, stamps[-1])
    old, _, _ = _read_cached(store, raw_path, meta_path, playlist_id, source_for, default_source)
    new, _, _ = _read_cached(
        store,
        store.tracklist_raw(playlist_id),
        store.tracklist_meta(playlist_id),
        playlist_id,
        source_for,
        default_source,
    )
    return stamps[-1], compare(old, new)


def _write_normalized(store: CacheStore, tracklist: Tracklist) -> None:
    for track in tracklist.tracks:
        _write_if_changed(store, store.track_json(track.id), track.to_dict())
    playlist = tracklist.playlist
    _write_if_changed(store, store.playlist_json(playlist.id), playlist.to_dict())


def _write_if_changed(store: CacheStore, path: Path, data: Any) -> None:
    # A re-sync of an unchanged playlist then touches no files at all.
    if store.read_json(path, default=None) != data:
        store.write_json(path, data)
