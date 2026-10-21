"""What the daemon remembers between runs: track orders, the song, its position, the volume.

One small file in the state directory (``session.json``), rewritten whenever what it
holds changes: on every playback snapshot, and once more as the daemon stops.

Order. Shuffling a playlist draws a new random order of its playable tracks and keeps it
as that playlist's order: the track list shows it, the queue plays it, and it is the same
after a restart, until the playlist is shuffled again or put back in its own order. If
the playlist is the one playing, the queue is rearranged where it stands: the song that
is on carries on, and is placed first so that everything else is still to come. Tracks
that a later sync adds go to the end of a kept order; tracks it removes drop out.

Resume. On start the saved volume is put back, then the saved playlist is loaded in its
kept order at the saved track and position. It is loaded paused: audio does not begin on
its own at boot. A song whose saved position is at its very end starts again from the
beginning.

Settings. The few settings of playback itself that are set from the page are kept here
too. One is how long the sound takes to come up from silence each time playback is
resumed after a pause, the paused song a start puts back included (``fade_in_s``; 0, the
default, is no fade); it is handed to the player on start and again whenever it changes.
``mono`` mixes the sound so that both ears hear the same, and ``loop`` starts the
playlist again from its first track once the last one ends; both are off by default, and
are handed to the player the same way. The next two say which tracks a playlist is made of:
``include_without_lyrics`` and ``include_without_audio``, both on by default. With the
first off, tracks that have no synced lyrics are neither listed nor queued; with the
second off, tracks that have no audio (which are never queued) are not listed either. A
change applies at once, to the queue that is playing too. Neither touches a kept order:
a track that is left out keeps its place for when it is let back in.

Turned off. A track can also be turned off by hand, from its row in the track list or
with the skip-and-turn-off shortcut. That is kept with the track itself (its ``disabled``
value in the state directory), not here, so it holds in every playlist the track is in.
A track that is off is listed as usual, so it can be turned back on, but is not queued;
like the two filters it keeps its place in a kept order. Turning off the song that is on
skips it: playback goes on from the next song that is still on, playing or paused as it
was. Clicking a track that is off plays it anyway, as a one-off.

The last two say what the remote-control page draws: ``show_lyrics`` (the lyric pane, with
its progress bar and underline) and ``show_position`` (the elapsed time and seek slider),
both on by default. They change nothing in playback; with both off the page does no work
between state events.

Nothing is written before the daemon has tried to resume. Until then the player is only
reporting what it started with, and that must not overwrite what was saved.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from ..cache import CacheStore
from ..errors import CacheError, NotInCacheError, PlaybackError
from ..models import PlaybackState
from ..playback.base import Player
from ..playback.queue import PlayQueue
from .library import Library

# A saved song this close to its end starts again instead, rather than ending at once.
_END_MARGIN_S = 1.0
# A shuffle that happens to repeat the order in force is drawn again, this many times.
_REDRAWS = 8
MAX_FADE_IN_S = 30.0
# Which tracks a playlist is made of. Both default to on: nothing is left out.
TRACK_FILTERS = ("include_without_lyrics", "include_without_audio")
# What the remote-control page draws. Both default to on; neither changes playback.
PAGE_SWITCHES = ("show_lyrics", "show_position")
SWITCHES = TRACK_FILTERS + PAGE_SWITCHES
# Switches of the player itself. These default to off.
PLAYER_SWITCHES = ("mono", "loop")


class SettingError(ValueError):
    """A value a playback setting cannot take. The message says what it must be."""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class PlaybackSession:
    def __init__(
        self,
        store: CacheStore,
        library: Library,
        player: Player,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._library = library
        self._player = player
        self._rng = rng or random.Random()
        self._clock = clock
        self._lock = threading.Lock()  # guards _data and _saving
        self._ordering = threading.Lock()  # one change of order at a time, player included
        self._data: dict[str, Any] = self._read()
        self._saving = False
        # Counts the changes to which tracks are turned off, for clients that list them.
        self._tracks_version = 0
        self._tracks_listeners: list[Callable[[], None]] = []
        player.bus.subscribe(self._on_state)

    # ------------------------------- storage ------------------------------- #

    def _read(self) -> dict[str, Any]:
        stored: Any = {}
        try:
            stored = self._store.read_json(self._store.session_json, {})
        except (CacheError, OSError) as exc:
            print(f"spotifixed: {exc}; starting without the saved session", file=sys.stderr)
        if not isinstance(stored, dict):
            print(
                "spotifixed: the saved session is not an object; ignoring it",
                file=sys.stderr,
            )
            stored = {}
        orders = stored.get("orders")
        stored["orders"] = orders if isinstance(orders, dict) else {}
        for retired in ("shuffle", "order", "order_playlist_id"):
            stored.pop(retired, None)  # written by a version that had shuffle as a switch
        settings = stored.get("settings")
        stored["settings"] = settings if isinstance(settings, dict) else {}
        return stored

    def _save(self) -> None:
        """Write what is held. A failure is reported, never raised: playback goes on."""
        try:
            self._store.write_json(self._store.session_json, self._data)
        except (CacheError, OSError) as exc:
            print(f"spotifixed: could not save the session: {exc}", file=sys.stderr)

    def document(self) -> dict[str, Any]:
        """What the page reads of the session."""
        with self._lock:
            return {
                "playlist_id": self._data.get("playlist_id"),
                "track_id": self._data.get("track_id"),
                "position_s": _number(self._data.get("position_s")) or 0.0,
                "volume": _number(self._data.get("volume")),
                "settings": self._settings(),
            }

    # ------------------------------- settings ------------------------------- #

    def _fade_in_s(self) -> float:
        """The fade-in length in force. A stored value that is not usable counts as off."""
        value = _number(self._data["settings"].get("fade_in_s"))
        return value if value is not None and 0 <= value <= MAX_FADE_IN_S else 0.0

    def _switch_on(self, name: str) -> bool:
        """Whether a switch is on. Anything but a stored ``False`` is on."""
        return self._data["settings"].get(name) is not False

    def _player_switch_on(self, name: str) -> bool:
        """Whether a player switch is on. Only a stored ``True`` is on."""
        return self._data["settings"].get(name) is True

    def _settings(self) -> dict[str, Any]:
        """Every setting as it is in force. Lock held."""
        return {
            "fade_in_s": self._fade_in_s(),
            **{name: self._switch_on(name) for name in SWITCHES},
            **{name: self._player_switch_on(name) for name in PLAYER_SWITCHES},
        }

    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Change playback settings. All of ``changes`` applies, or none of it does."""
        accepted: dict[str, Any] = {}
        for key, value in changes.items():
            if key in SWITCHES or key in PLAYER_SWITCHES:
                if not isinstance(value, bool):
                    raise SettingError(f"{key} must be true or false; got {value!r}")
                accepted[key] = value
            elif key == "fade_in_s":
                seconds = _number(value)
                if seconds is None or not 0 <= seconds <= MAX_FADE_IN_S:
                    raise SettingError(
                        f"fade_in_s must be a number of seconds from 0 to {MAX_FADE_IN_S:g}; "
                        f"got {value!r}"
                    )
                accepted[key] = round(seconds, 2)
            else:
                raise SettingError(f"{key!r} is not a playback setting")
        with self._ordering:
            with self._lock:
                before = self._settings()
                self._data["settings"].update(accepted)
                self._save()
                refiltered = any(before[name] != self._switch_on(name) for name in TRACK_FILTERS)
                fade_in_s = self._fade_in_s()
                switches = {name: self._player_switch_on(name) for name in PLAYER_SWITCHES}
            if "fade_in_s" in accepted:
                self._set_fade_in(fade_in_s)
            for name, on in switches.items():
                if name in accepted:
                    self._set_player_switch(name, on)
            playing = self._player.bus.current.playlist_id
            if refiltered and playing is not None:
                try:
                    self._apply(playing)
                except (NotInCacheError, CacheError, PlaybackError) as exc:
                    print(f"spotifixed: the queue is as it was: {exc}", file=sys.stderr)
        return self.document()

    # --------------------------------- order -------------------------------- #

    def _kept_order(self, playlist_id: str, playable: list[str]) -> list[str] | None:
        """The playlist's kept order, brought up to date with ``playable``. Lock held.

        ``None`` when the playlist has never been shuffled. A kept order that no longer
        matches the tracks (a sync added or removed some) is repaired, not thrown away:
        what is still there keeps its place and anything new goes to the end.
        """
        kept = self._data["orders"].get(playlist_id)
        if not isinstance(kept, list):
            return None
        remaining = Counter(playable)
        order = []
        for track_id in kept:
            if remaining.get(track_id, 0) > 0:
                order.append(track_id)
                remaining[track_id] -= 1
        for track_id in playable:
            if remaining[track_id] > 0:
                order.append(track_id)
                remaining[track_id] -= 1
        if order != kept:
            self._data["orders"][playlist_id] = order
        return order

    def _queue_order(self, playlist_id: str, *, keep: str | None = None) -> list[str]:
        """What a playlist queues: its order, less the tracks the settings leave out. Lock held.

        ``keep`` is a track that stays whatever the settings say: the one that is
        playing, or the one that was asked for.
        """
        playable = self._library.playable_ids(playlist_id)
        order = self._kept_order(playlist_id, playable) or playable
        if not self._switch_on("include_without_lyrics"):
            synced = set(self._library.playable_ids(playlist_id, synced_only=True))
            order = [track_id for track_id in order if track_id in synced or track_id == keep]
            if not order:
                raise NotInCacheError(
                    f"playlist {playlist_id} has no tracks with synced lyrics to play; "
                    "tracks without lyrics are left out by a setting"
                )
        enabled = set(self._library.playable_ids(playlist_id, enabled_only=True))
        order = [track_id for track_id in order if track_id in enabled or track_id == keep]
        if not order:
            raise NotInCacheError(
                "every track in this playlist that could play is turned off; "
                "turn one back on in the track list"
            )
        return order

    def _apply(self, playlist_id: str) -> None:
        """Bring the player's queue up to date if it holds this playlist. Ordering lock held."""
        state = self._player.bus.current
        if state.playlist_id != playlist_id or not state.queue_length:
            return
        with self._lock:
            order = self._queue_order(playlist_id, keep=state.track_id)
        queue = self._library.queue_for(playlist_id, start_track_id=state.track_id, order=order)
        self._player.reorder(queue)

    def shuffle(self, playlist_id: str) -> list[str]:
        """Draw a new random order for a playlist, keep it, and play by it from now on."""
        with self._ordering:
            with self._lock:
                playable = self._library.playable_ids(playlist_id)
                before = self._kept_order(playlist_id, playable) or playable
                state = self._player.bus.current
                loaded_here = state.playlist_id == playlist_id and state.queue_length
                current = state.track_id if loaded_here else None
                order = list(playable)
                for _ in range(_REDRAWS):
                    self._rng.shuffle(order)
                    if current in order:
                        # What is playing goes first: the rest is all still to come.
                        order.remove(current)
                        order.insert(0, current)
                    if order != before:
                        break
                self._data["orders"][playlist_id] = order
                self._save()
            self._apply(playlist_id)
        return order

    def restore(self, playlist_id: str) -> None:
        """Forget a playlist's kept order: back to the order the playlist itself has."""
        with self._ordering:
            with self._lock:
                self._library.playable_ids(playlist_id)  # refuses a playlist that is not cached
                self._data["orders"].pop(playlist_id, None)
                self._save()
            self._apply(playlist_id)

    def arranged(self, playlist: dict[str, Any]) -> dict[str, Any]:
        """A playlist as the API describes it, with its tracks in the kept order.

        Tracks that cannot be played have no place in an order; they follow the rest.
        Tracks the settings leave out are not listed, only counted (``hidden``). Tracks
        turned off are listed, and counted too (``disabled``).
        """
        tracks = playlist["tracks"]
        playable = [track["id"] for track in tracks if track["has_audio"]]
        with self._lock:
            order = self._kept_order(playlist["id"], playable)
            lyricless = self._switch_on("include_without_lyrics")
            audioless = self._switch_on("include_without_audio")
        if order is not None:
            by_id = {track["id"]: track for track in tracks}
            unplayable = [track for track in tracks if not track["has_audio"]]
            tracks = [by_id[track_id] for track_id in order] + unplayable
        listed = [
            track
            for track in tracks
            if (audioless or track["has_audio"]) and (lyricless or track["lyrics_kind"] == "synced")
        ]
        return {
            **playlist,
            "shuffled": order is not None,
            "tracks": listed,
            "hidden": len(tracks) - len(listed),
            "disabled": sum(1 for track in listed if track["disabled"]),
        }

    # ------------------------------ turning off ----------------------------- #

    @property
    def tracks_version(self) -> int:
        return self._tracks_version

    def subscribe_tracks(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` after a track is turned off or on. Returns an unsubscribe."""
        with self._lock:
            self._tracks_listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._tracks_listeners:
                    self._tracks_listeners.remove(listener)

        return unsubscribe

    def set_disabled(self, track_id: str, disabled: bool) -> bool:
        """Turn a track off, or back on, in every playlist. The playing queue follows."""
        with self._ordering:
            return self._set_disabled(track_id, disabled)

    def disable_current(self) -> str | None:
        """Turn off the song that is on, which skips it. ``None`` when nothing is on."""
        with self._ordering:
            track_id = self._player.bus.current.track_id
            if track_id is not None:
                self._set_disabled(track_id, True)
            return track_id

    def _set_disabled(self, track_id: str, disabled: bool) -> bool:
        """Ordering lock held. Refused, and nothing kept, if it would leave nothing to play."""
        state = self._player.bus.current
        playing = state.playlist_id if state.queue_length else None
        skip = disabled and playing is not None and state.track_id == track_id
        if skip:
            with self._lock:
                before = self._queue_order(playing, keep=track_id)
        was = self._library.track(track_id)["disabled"]
        self._library.set_disabled(track_id, disabled)
        try:
            if skip:
                self._skip(playing, track_id, before, paused=state.paused)
            elif playing is not None:
                self._apply(playing)
        except NotInCacheError:
            self._library.set_disabled(track_id, was)
            raise
        with self._lock:
            self._tracks_version += 1
            listeners = list(self._tracks_listeners)
        for listener in listeners:
            listener()
        return disabled

    def _skip(self, playlist_id: str, track_id: str, before: list[str], *, paused: bool) -> None:
        """Play on from the song after ``track_id`` in ``before`` that is still queued.

        Past the end the queue starts again from its first song: playing on if the
        playlist repeats, and paused, as at the end of a playlist, if it does not.
        """
        with self._lock:
            order = self._queue_order(playlist_id)
            repeats = self._player_switch_on("loop")
        queued = set(order)
        after = before[before.index(track_id) + 1 :]
        start = next((candidate for candidate in after if candidate in queued), None)
        if start is None:
            start, paused = order[0], paused or not repeats
        queue = self._library.queue_for(playlist_id, start_track_id=start, order=order)
        self._player.load_queue(queue, paused=paused)

    # -------------------------------- loading ------------------------------ #

    def queue_for(self, playlist_id: str, *, start_track_id: str | None = None) -> PlayQueue:
        """The queue for a request to play a playlist, in its kept order if it has one."""
        with self._lock:
            order = self._queue_order(playlist_id, keep=start_track_id)
            queue = self._library.queue_for(playlist_id, start_track_id=start_track_id, order=order)
            self._data["playlist_id"] = playlist_id
            self._save()
        return queue

    # ------------------------------- recording ----------------------------- #

    def _on_state(self, state: PlaybackState) -> None:
        with self._lock:
            if self._saving:
                self._record(state)

    def _record(self, state: PlaybackState) -> None:
        """Keep what ``state`` says about the song, its position and the volume. Lock held."""
        if state.playlist_id is not None:
            self._data["playlist_id"] = state.playlist_id
        if state.track_id is not None:
            now_ms = int(self._clock() * 1000)
            self._data["track_id"] = state.track_id
            self._data["position_s"] = round(state.position_at(now_ms), 2)
        self._data["volume"] = round(float(state.volume), 2)
        self._save()

    def flush(self) -> None:
        """Record where playback is now, as the daemon stops."""
        with self._lock:
            if self._saving:
                self._record(self._player.bus.current)

    # -------------------------------- resuming ----------------------------- #

    def resume(self) -> None:
        """Put back the saved volume, playlist, song and position, paused. Then save again."""
        with self._lock:
            saved = dict(self._data)
        volume = _number(saved.get("volume"))
        if volume is not None and 0 <= volume <= 100:
            self._player.set_volume(volume)
        playlist_id, track_id = saved.get("playlist_id"), saved.get("track_id")
        if isinstance(playlist_id, str) and isinstance(track_id, str):
            try:
                queue = self.queue_for(playlist_id, start_track_id=track_id)
                position = self._resume_position(saved, track_id)
                self._player.load_queue(queue, paused=True, position_s=position)
            except (NotInCacheError, CacheError, PlaybackError) as exc:
                print(f"spotifixed: not resuming the last song: {exc}", file=sys.stderr)
        with self._lock:
            fade_in_s = self._fade_in_s()
            switches = {name: self._player_switch_on(name) for name in PLAYER_SWITCHES}
            self._saving = True
        if fade_in_s > 0:
            self._set_fade_in(fade_in_s)
        for name, on in switches.items():
            if on:
                self._set_player_switch(name, True)

    def _set_fade_in(self, seconds: float) -> None:
        try:
            self._player.set_fade_in(seconds)
        except PlaybackError as exc:
            print(f"spotifixed: no fade-in this time: {exc}", file=sys.stderr)

    def _set_player_switch(self, name: str, on: bool) -> None:
        setter = {"mono": self._player.set_mono, "loop": self._player.set_loop}[name]
        try:
            setter(on)
        except PlaybackError as exc:
            print(f"spotifixed: the {name} setting is not in force: {exc}", file=sys.stderr)

    def _resume_position(self, saved: dict[str, Any], track_id: str) -> float | None:
        position = _number(saved.get("position_s"))
        if position is None or position <= 0:
            return None
        duration_ms = _number(self._library.track(track_id).get("duration_ms"))
        if duration_ms is not None and position >= duration_ms / 1000 - _END_MARGIN_S:
            return None
        return position
