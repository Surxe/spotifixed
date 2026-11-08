"""Paths and atomic writes for everything kept on disk.

Layout::

    <cache_dir>/
      playlists/<playlist_id>/
        tracklist.raw.json    the tracklist source's response, verbatim
        tracklist.meta.json   which Actor produced it, and when
        playlist.json         normalized Playlist (metadata + ordered track ids)
        cover.jpg
        history/<fetched_at>.raw.json, .meta.json
                              a tracklist replaced by a refresh, kept for comparison
      tracks/<track_id>/
        track.json            normalized Track
        audio.<ext>           real file name is recorded in audio.json
        audio.json            where the audio came from and how it scored
        lyrics.lrc | lyrics.txt
        lyrics.json           which record was chosen and how confident the timing is
        words.json            when each word is sung, from `align`; absent until it is run
        cover.jpg
      misses.json             negative cache, see misses.py
    <state_dir>/
      tracks/<track_id>.json  hand-authored: timing offset, audio pin

No other module builds one of these paths by hand; they all come from
:class:`CacheStore`, so the layout can change in one place.

Files and directories get ordinary permissions: whatever the process umask allows.
Sharing a cache between accounts is therefore a matter of how it is run (a group-owned
setgid directory and a umask of 002), not something this code imposes.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..errors import CacheError, InvalidIdError

# Spotify IDs are 22 base-62 characters. Accept a range so tests can use short ones, but
# never anything that could be a path component with meaning ("..", "a/b").
_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


# A file name recorded in a provenance JSON. It is read back from disk, so it is checked
# before being joined onto a directory: no separators, no leading dot.
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


def validate_id(value: str) -> str:
    """Return ``value`` if it is safe to use as a directory name, else raise."""
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise InvalidIdError(f"not a valid playlist or track ID: {value!r}")
    return value


def ensure_dir(path: Path) -> Path:
    """Create ``path`` and any missing parents."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so a reader never sees a partial file.

    The temp file lives in the destination directory so ``os.replace`` is a rename on one
    filesystem. An interrupted write leaves the previous file, or nothing, never a stub.
    It is created with mode 0666 less the umask, as any ordinary file would be
    (``tempfile`` would force 0600 and hide the file from a group sharing the cache).
    """
    ensure_dir(path.parent)
    tmp_name = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


class CacheStore:
    """Every path under the cache and state roots, plus JSON read/write helpers."""

    def __init__(self, cache_dir: Path, state_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.state_dir = Path(state_dir)

    # ------------------------------ playlists ------------------------------ #
    def playlist_dir(self, playlist_id: str) -> Path:
        return self.cache_dir / "playlists" / validate_id(playlist_id)

    def tracklist_raw(self, playlist_id: str) -> Path:
        return self.playlist_dir(playlist_id) / "tracklist.raw.json"

    def tracklist_meta(self, playlist_id: str) -> Path:
        return self.playlist_dir(playlist_id) / "tracklist.meta.json"

    def playlist_json(self, playlist_id: str) -> Path:
        return self.playlist_dir(playlist_id) / "playlist.json"

    def playlist_cover(self, playlist_id: str) -> Path:
        return self.playlist_dir(playlist_id) / "cover.jpg"

    def tracklist_history_dir(self, playlist_id: str) -> Path:
        return self.playlist_dir(playlist_id) / "history"

    def tracklist_history(self, playlist_id: str, stamp: str) -> tuple[Path, Path]:
        """The archived raw response and meta file for one replaced tracklist."""
        if not _FILE_NAME_RE.fullmatch(stamp):
            raise CacheError(f"not a plain history stamp: {stamp!r}")
        root = self.tracklist_history_dir(playlist_id)
        return root / f"{stamp}.raw.json", root / f"{stamp}.meta.json"

    def tracklist_history_stamps(self, playlist_id: str) -> list[str]:
        """Stamps of the archived tracklists, oldest first."""
        root = self.tracklist_history_dir(playlist_id)
        if not root.is_dir():
            return []
        return sorted(p.name.removesuffix(".raw.json") for p in root.glob("*.raw.json"))

    def playlist_ids(self) -> list[str]:
        """IDs of playlists that have been normalized, sorted."""
        root = self.cache_dir / "playlists"
        if not root.is_dir():
            return []
        return sorted(
            d.name
            for d in root.iterdir()
            if _ID_RE.fullmatch(d.name) and (d / "playlist.json").is_file()
        )

    # -------------------------------- tracks -------------------------------- #
    def track_dir(self, track_id: str) -> Path:
        return self.cache_dir / "tracks" / validate_id(track_id)

    def track_json(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "track.json"

    def track_file(self, track_id: str, name: str) -> Path:
        """A file inside the track's directory by bare name, e.g. the audio file."""
        if not _FILE_NAME_RE.fullmatch(name):
            raise CacheError(f"not a plain file name: {name!r}")
        return self.track_dir(track_id) / name

    def audio_json(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "audio.json"

    def lyrics_json(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "lyrics.json"

    def lyrics_lrc(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "lyrics.lrc"

    def lyrics_txt(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "lyrics.txt"

    def words_json(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "words.json"

    def rejected_audio_json(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "rejected_audio.json"

    def track_cover(self, track_id: str) -> Path:
        return self.track_dir(track_id) / "cover.jpg"

    # --------------------------------- misc --------------------------------- #
    @property
    def misses_json(self) -> Path:
        return self.cache_dir / "misses.json"

    def track_state(self, track_id: str) -> Path:
        return self.state_dir / "tracks" / f"{validate_id(track_id)}.json"

    @property
    def session_json(self) -> Path:
        """What the daemon resumes after a restart: shuffle, song, position and volume."""
        return self.state_dir / "session.json"

    @property
    def overlay_json(self) -> Path:
        """Overlay settings changed from the remote-control page."""
        return self.state_dir / "overlay.json"

    # ---------------------------------- I/O ---------------------------------- #
    def write_bytes(self, path: Path, data: bytes) -> None:
        self._require_inside(path)
        atomic_write_bytes(path, data)

    def write_text(self, path: Path, text: str) -> None:
        self.write_bytes(path, text.encode("utf-8"))

    def write_json(self, path: Path, data: Any) -> None:
        self.write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def read_json(self, path: Path, default: Any = ...) -> Any:
        """Parse ``path``. A missing file returns ``default`` when one is given."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if default is ...:
                raise CacheError(f"missing cache file: {path}") from None
            return default
        except json.JSONDecodeError as exc:
            raise CacheError(f"corrupt cache file {path}: {exc}") from exc

    @contextlib.contextmanager
    def work_dir(self, parent: Path) -> Iterator[Path]:
        """A hidden scratch directory inside ``parent``, removed on exit.

        Downloads land here and are moved out with :meth:`adopt` once complete, so an
        interrupted one never has a final name. Leftovers from a killed run are swept
        the next time the same parent is used.
        """
        self._require_inside(parent)
        ensure_dir(parent)
        for stale in parent.glob(".work.*"):
            shutil.rmtree(stale, ignore_errors=True)
        work = Path(tempfile.mkdtemp(dir=parent, prefix=".work."))
        try:
            yield work
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def adopt(self, source: Path, dest: Path) -> None:
        """Move a finished file on the same filesystem into the cache under its final name."""
        self._require_inside(dest)
        ensure_dir(dest.parent)
        os.replace(source, dest)

    def remove(self, path: Path) -> bool:
        """Delete ``path`` if it exists. Returns whether anything was removed."""
        self._require_inside(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def has(self, path: Path) -> bool:
        """True when ``path`` is a non-empty file. A zero-byte file counts as absent."""
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def _require_inside(self, path: Path) -> None:
        resolved = path.resolve()
        for root in (self.cache_dir.resolve(), self.state_dir.resolve()):
            if resolved == root or root in resolved.parents:
                return
        raise CacheError(f"refusing to write outside the cache and state directories: {path}")
