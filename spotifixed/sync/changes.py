"""What a tracklist refresh changed, and carrying hand-made settings to relinked tracks.

Spotify sometimes replaces a track's ID with a new one for the same recording (a
re-release, a relinked catalogue entry). Everything spotifixed keeps about a track is
filed under its ID, so a relinked track would otherwise lose its offset, pin and on/off
setting, and be downloaded again. :func:`compare` pairs such removals and additions;
:func:`carry_over` copies the old ID's state and cached files to the new one.

:func:`compare` is pure and works on normalized tracklists, so it is independent of the
source that produced them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..cache import CacheStore
from ..models import Track
from ..sources.tracklist import Tracklist
from .matching import normalize_artist, normalize_title

# A relinked track is the same recording, so its length barely moves. A remaster or a
# different edit under the same title usually differs by more than this.
RELINK_DURATION_TOLERANCE_MS = 3000

# Fields whose change is reported for a track kept under the same ID. Cover URLs are left
# out: Spotify rotates them, and the cached cover stays valid.
_COMPARED_FIELDS = ("title", "artists", "duration_ms", "album", "explicit")

# Per-track cache files carried to a relinked ID. track.json is not: the new ID's own is
# written from the new tracklist.
_CARRIED_FILE_EXCLUDES = frozenset({"track.json"})


@dataclass(frozen=True, slots=True)
class Relink:
    old: Track
    new: Track


@dataclass(frozen=True, slots=True)
class FieldChange:
    old: Track
    new: Track
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TracklistChanges:
    """The difference between two tracklists of one playlist, old to new."""

    old_count: int
    new_count: int
    added: tuple[Track, ...]
    removed: tuple[Track, ...]
    relinked: tuple[Relink, ...]
    changed: tuple[FieldChange, ...]
    # Tracks present in both whose order relative to each other changed.
    moved: int

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.relinked or self.changed or self.moved)


def _song_key(track: Track) -> tuple[str, str]:
    return normalize_title(track.title), normalize_artist(track.primary_artist)


def compare(old: Tracklist, new: Tracklist) -> TracklistChanges:
    old_tracks = {track.id: track for track in old.tracks}
    new_tracks = {track.id: track for track in new.tracks}
    removed = [t for t in old.tracks if t.id not in new_tracks]
    added = [t for t in new.tracks if t.id not in old_tracks]

    # Pair each removed track with the first unclaimed added track of the same song.
    relinked: list[Relink] = []
    unclaimed = list(added)
    for gone in list(removed):
        for arrived in unclaimed:
            if (
                _song_key(arrived) == _song_key(gone)
                and abs(arrived.duration_ms - gone.duration_ms) <= RELINK_DURATION_TOLERANCE_MS
            ):
                relinked.append(Relink(gone, arrived))
                unclaimed.remove(arrived)
                removed.remove(gone)
                break

    changed = []
    for track in old.tracks:
        if track.id in new_tracks:
            after = new_tracks[track.id]
            fields = tuple(f for f in _COMPARED_FIELDS if getattr(track, f) != getattr(after, f))
            if fields:
                changed.append(FieldChange(track, after, fields))

    kept_old = [i for i in dict.fromkeys(old.playlist.track_ids) if i in new_tracks]
    kept_new = [i for i in dict.fromkeys(new.playlist.track_ids) if i in old_tracks]
    moved = sum(1 for a, b in zip(kept_old, kept_new, strict=True) if a != b)

    return TracklistChanges(
        old_count=len(old.playlist.track_ids),
        new_count=len(new.playlist.track_ids),
        added=tuple(unclaimed),
        removed=tuple(removed),
        relinked=tuple(relinked),
        changed=tuple(changed),
        moved=moved,
    )


@dataclass(frozen=True, slots=True)
class CarriedOver:
    relink: Relink
    state: bool
    files: int


def carry_over(store: CacheStore, relinks: tuple[Relink, ...]) -> list[CarriedOver]:
    """Copy each old ID's state and cached files to its new ID.

    Nothing the new ID already has is overwritten: its state file if it has one, and its
    cached files if it has audio. The old ID's files are left in place.
    """
    results = []
    for relink in relinks:
        old_id, new_id = relink.old.id, relink.new.id
        state = False
        old_state = store.track_state(old_id)
        if store.has(old_state) and not store.track_state(new_id).exists():
            store.write_bytes(store.track_state(new_id), old_state.read_bytes())
            state = True

        files = 0
        old_dir = store.track_dir(old_id)
        if old_dir.is_dir() and not store.audio_json(new_id).exists():
            for path in sorted(old_dir.iterdir()):
                carried = path.name not in _CARRIED_FILE_EXCLUDES and not path.name.startswith(".")
                if carried and path.is_file():
                    store.write_bytes(store.track_file(new_id, path.name), path.read_bytes())
                    files += 1
        results.append(CarriedOver(relink, state, files))
    return results
