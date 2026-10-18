"""Helpers for tests that watch playback state."""

from __future__ import annotations

import time
from collections.abc import Callable

from spotifixed.models import PlaybackState
from spotifixed.playback.state import StateBus


class StateLog:
    """Records every snapshot published on a bus and waits for one that matches."""

    def __init__(self, bus: StateBus) -> None:
        self.bus = bus
        self.states: list[PlaybackState] = []
        bus.subscribe(self.states.append)

    def wait_for(
        self, predicate: Callable[[PlaybackState], bool], timeout: float = 10.0, *, after: int = 0
    ) -> PlaybackState:
        """The first snapshot newer than version ``after`` that satisfies ``predicate``."""
        deadline = time.monotonic() + timeout
        seen = after
        while True:
            for state in list(self.states):
                if state.version > after and predicate(state):
                    return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                trail = [(s.version, s.status.value, s.track_id) for s in self.states[-8:]]
                raise AssertionError(f"no matching snapshot within {timeout} s; last: {trail}")
            newer = self.bus.wait_newer(seen, min(remaining, 0.5))
            if newer is not None:
                seen = newer.version

    def statuses(self, *, after: int = 0) -> list[str]:
        """Status values in publication order, consecutive repeats collapsed."""
        out: list[str] = []
        for state in self.states:
            if state.version > after and (not out or out[-1] != state.status.value):
                out.append(state.status.value)
        return out


class FakePlayer:
    """Implements the Player protocol in memory: records calls, publishes plausible state."""

    def __init__(self) -> None:
        from spotifixed.playback.queue import EMPTY_QUEUE

        self.bus = StateBus()
        self.calls: list[tuple] = []
        self.queue = EMPTY_QUEUE
        self.index: int | None = None
        self.paused = True
        self.position = 0.0
        self.volume = 100.0
        self.fail_with: Exception | None = None
        self._publish()

    def _publish(self) -> None:
        from spotifixed.models import LyricsKind, PlayerStatus

        entry = self.queue.entry_at(self.index)
        if entry is None:
            status = PlayerStatus.IDLE
        else:
            status = PlayerStatus.PAUSED if self.paused else PlayerStatus.PLAYING
        self.bus.publish(
            PlaybackState(
                status=status,
                paused=self.paused or entry is None,
                playlist_id=self.queue.playlist_id,
                track_id=entry.track_id if entry else None,
                queue_index=self.index if entry else None,
                queue_length=len(self.queue),
                position_s=self.position,
                duration_s=5.0 if entry else None,
                volume=self.volume,
                captured_at_epoch_ms=int(time.time() * 1000),
                lyrics_kind=entry.lyrics_kind if entry else LyricsKind.MISSING,
                offset_ms=entry.offset_ms if entry else 0,
            )
        )

    def _record(self, *call) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(call)

    def load_queue(self, queue, *, paused: bool = False, position_s=None) -> None:
        self._record("load_queue", [e.track_id for e in queue.entries], queue.start_index)
        self.queue, self.index, self.paused = queue, queue.start_index, paused
        self.position = position_s or 0.0
        self.loaded_at = self.position
        self._publish()

    def reorder(self, queue) -> None:
        self._record("reorder", [e.track_id for e in queue.entries])
        current = self.queue.entry_at(self.index)
        self.queue = queue
        if current is not None:
            self.index = queue.index_of(current.track_id)
        self._publish()

    def play(self) -> None:
        self._record("play")
        self.paused = False
        if self.index is None and len(self.queue):
            self.index = 0
        self._publish()

    def pause(self) -> None:
        self._record("pause")
        self.paused = True
        self._publish()

    def toggle(self) -> None:
        self._record("toggle")
        self.paused = not self.paused
        self._publish()

    def next(self) -> None:
        self._record("next")
        if self.index is not None and self.index + 1 < len(self.queue):
            self.index += 1
        self._publish()

    def prev(self) -> None:
        self._record("prev")
        if self.index:
            self.index -= 1
        self._publish()

    def seek(self, *, position_s=None, delta_s=None) -> None:
        self._record("seek", position_s, delta_s)
        self.position = position_s if position_s is not None else self.position + delta_s
        self._publish()

    def set_volume(self, volume: float) -> None:
        self._record("set_volume", volume)
        self.volume = volume
        self._publish()

    def set_fade_in(self, seconds: float) -> None:
        self._record("set_fade_in", seconds)

    def set_mono(self, on: bool) -> None:
        self._record("set_mono", on)

    def set_loop(self, on: bool) -> None:
        self._record("set_loop", on)

    def set_offset(self, track_id: str, offset_ms: int) -> None:
        self._record("set_offset", track_id, offset_ms)
        if self.queue.set_offset(track_id, offset_ms):
            self._publish()

    def stop(self) -> None:
        self._record("stop")
        self.index = None
        self._publish()

    def close(self) -> None:
        self._record("close")
