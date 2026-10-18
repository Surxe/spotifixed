"""The playback engine against a real mpv with audio output disabled.

Tones are generated; nothing is audible and nothing real is played.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import pytest

from spotifixed.errors import PlaybackError
from spotifixed.models import LyricsKind, PlayerStatus
from spotifixed.playback.mpv import MpvPlayer
from spotifixed.playback.mpv_ipc import MpvCommandError
from spotifixed.playback.queue import PlayQueue, QueueEntry
from spotifixed.playback.state import StateBus
from tests.support.audio import tone
from tests.support.playback import StateLog

pytestmark = [
    pytest.mark.mpv,
    pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv is not installed"),
]

MPRIS_PLUGIN = Path("/usr/lib/mpv-mpris/mpris.so")


@pytest.fixture(scope="session")
def tones(tmp_path_factory):
    return tmp_path_factory.mktemp("tones")


@pytest.fixture
def runtime():
    # Unix socket paths are limited to about 100 bytes; pytest's tmp_path is too deep.
    path = Path(tempfile.mkdtemp(prefix="pp-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def make_player(runtime):
    players = []

    def make(**kwargs) -> tuple[MpvPlayer, StateLog]:
        bus = StateBus()
        log = StateLog(bus)
        kwargs.setdefault("heartbeat_s", 5.0)
        player = MpvPlayer(bus, socket_path=runtime / "mpv.sock", audio_output="null", **kwargs)
        players.append(player)
        player.start()
        return player, log

    yield make
    for player in players:
        player.close()


def queue_of(tones, lengths, **kwargs) -> PlayQueue:
    entries = [
        QueueEntry(f"T{i}", str(tone(tones, seconds, "opus")), LyricsKind.SYNCED, offset_ms=i * 10)
        for i, seconds in enumerate(lengths)
    ]
    # Identical files would share a path; give each entry its own copy.
    unique = []
    for entry in entries:
        copy = Path(entry.path).with_name(f"{entry.track_id}-{Path(entry.path).name}")
        if not copy.exists():
            shutil.copyfile(entry.path, copy)
        unique.append(QueueEntry(entry.track_id, str(copy), entry.lyrics_kind, entry.offset_ms))
    return PlayQueue(unique, playlist_id="PL1", **kwargs)


def playing(track_id=None):
    return lambda s: s.status is PlayerStatus.PLAYING and track_id in (None, s.track_id)


def test_starts_idle_with_a_private_socket(make_player, runtime):
    player, log = make_player()
    state = log.wait_for(lambda s: True)
    assert (state.status, state.track_id, state.queue_length) == (PlayerStatus.IDLE, None, 0)
    assert state.paused and state.version == 1
    assert oct(os.stat(runtime).st_mode & 0o777) == "0o700"
    assert oct(os.stat(runtime / "mpv.sock").st_mode & 0o777) == "0o600"
    command = " ".join(player.command_line())
    for flag in ("--no-config", "--load-scripts=no", "--ytdl=no", "--no-video", "--ao=null",
                 "--no-terminal", "--no-input-default-bindings"):  # fmt: skip
        assert flag in command
    assert "--script" not in command


def test_plays_a_queue_through_and_reports_each_track_then_ended(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [1.5, 1.5, 1.5]))

    for index in range(3):
        state = log.wait_for(playing(f"T{index}"))
        assert (state.queue_index, state.queue_length, state.playlist_id) == (index, 3, "PL1")
        assert state.lyrics_kind is LyricsKind.SYNCED and state.offset_ms == index * 10
        assert state.duration_s == pytest.approx(1.5, abs=0.1)
        assert not state.paused
    ended = log.wait_for(lambda s: s.status is PlayerStatus.ENDED)
    assert ended.track_id is None and ended.paused and ended.position_s == 0

    statuses = log.statuses()
    assert statuses[0] == "idle" and statuses[-1] == "ended"
    assert statuses.index("loading") < statuses.index("playing")
    versions = [s.version for s in log.states]
    assert versions == list(range(1, len(versions) + 1))
    # Position ticks alone never publish: a 4.5 s queue is a handful of snapshots.
    assert len(log.states) < 25


def test_play_after_the_queue_ended_starts_it_again(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [1.5]))
    ended = log.wait_for(lambda s: s.status is PlayerStatus.ENDED)
    player.play()
    again = log.wait_for(playing("T0"), after=ended.version)
    assert again.queue_index == 0


def test_a_looping_queue_starts_over_after_its_last_track(make_player, tones):
    player, log = make_player()
    player.set_loop(True)
    assert player._command("get_property", "loop-playlist") == "inf"
    player.load_queue(queue_of(tones, [1.5, 1.5]))
    second = log.wait_for(playing("T1"))
    again = log.wait_for(playing("T0"), after=second.version)
    assert again.queue_index == 0
    assert "ended" not in log.statuses()

    player.set_loop(False)
    assert player._command("get_property", "loop-playlist") is False
    log.wait_for(lambda s: s.status is PlayerStatus.ENDED, after=again.version)


def test_pause_freezes_the_position_and_resume_continues(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [5]))
    started = log.wait_for(playing("T0"))
    time.sleep(1.0)

    player.pause()
    paused = log.wait_for(lambda s: s.status is PlayerStatus.PAUSED, after=started.version)
    assert paused.paused and paused.position_s == pytest.approx(1.0, abs=0.25)
    time.sleep(0.8)
    # Nothing advances while paused: neither the published state nor its extrapolation.
    now_ms = int(time.time() * 1000)
    assert log.bus.current.position_s == paused.position_s
    assert log.bus.current.position_at(now_ms) == paused.position_s

    player.play()
    resumed = log.wait_for(playing("T0"), after=paused.version)
    assert resumed.position_s == pytest.approx(paused.position_s, abs=0.15)

    player.toggle()
    log.wait_for(lambda s: s.status is PlayerStatus.PAUSED, after=resumed.version)


def test_seek_publishes_one_snapshot_at_the_new_position(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [5]))
    started = log.wait_for(playing("T0"))

    player.seek(position_s=3.0)
    sought = log.wait_for(
        lambda s: s.status is PlayerStatus.PLAYING and s.position_s > 2.5, after=started.version
    )
    assert sought.position_s == pytest.approx(3.0, abs=0.2)
    time.sleep(0.3)
    after_seek = [s for s in log.states if s.version > started.version]
    # Exactly one playing snapshot carries the new position; nothing follows it.
    assert [s.status for s in after_seek].count(PlayerStatus.PLAYING) == 1
    assert after_seek[-1] is sought and len(after_seek) <= 2

    player.seek(delta_s=-2.0)
    back = log.wait_for(
        lambda s: s.status is PlayerStatus.PLAYING and s.position_s < 2.0, after=sought.version
    )
    assert back.position_s == pytest.approx(1.3, abs=0.4)


def test_extrapolated_position_tracks_mpv_within_60_ms(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [5]))
    snapshot = log.wait_for(playing("T0"))

    worst = 0.0
    for _ in range(10):
        time.sleep(0.25)
        before = time.time()
        actual = player._command("get_property", "audio-pts")
        after = time.time()
        expected = snapshot.position_at(int((before + after) / 2 * 1000))
        worst = max(worst, abs(actual - expected))
    # 2.5 s after a single snapshot, with no update in between.
    assert log.bus.current.version == snapshot.version
    assert worst < 0.060, f"worst error {worst * 1000:.1f} ms"


def test_heartbeat_republishes_while_playing(make_player, tones):
    player, log = make_player(heartbeat_s=0.5)
    player.load_queue(queue_of(tones, [5]))
    first = log.wait_for(playing("T0"))
    second = log.wait_for(playing("T0"), after=first.version, timeout=3)
    assert second.position_s > first.position_s
    elapsed = (second.captured_at_epoch_ms - first.captured_at_epoch_ms) / 1000
    assert second.position_s - first.position_s == pytest.approx(elapsed, abs=0.06)


def test_next_prev_volume_offset_and_stop(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [5, 5, 5], start_track_id="T1"))
    state = log.wait_for(playing("T1"))
    assert state.queue_index == 1

    player.next()
    state = log.wait_for(playing("T2"), after=state.version)
    player.next()  # already last: stays put rather than stopping
    time.sleep(0.3)
    assert log.bus.current.track_id == "T2" and log.bus.current.status is PlayerStatus.PLAYING

    player.prev()
    state = log.wait_for(playing("T1"), after=state.version)
    time.sleep(0.2)
    player.seek(position_s=4.0)
    state = log.wait_for(lambda s: s.position_s > 3.5, after=state.version)
    player.prev()  # well into the track: back to its start, same track
    state = log.wait_for(lambda s: s.position_s < 1.0, after=state.version)
    assert state.track_id == "T1"

    player.set_volume(35)
    state = log.wait_for(lambda s: s.volume == 35, after=state.version)
    player.set_volume(250)
    state = log.wait_for(lambda s: s.volume == 100, after=state.version)

    player.set_offset("T1", -240)
    state = log.wait_for(lambda s: s.offset_ms == -240, after=state.version)
    assert state.track_id == "T1"

    player.stop()
    state = log.wait_for(lambda s: s.status is PlayerStatus.IDLE, after=state.version)
    assert state.track_id is None and state.queue_length == 3
    player.play()  # the queue was kept
    log.wait_for(playing("T0"), after=state.version)


def test_loading_a_new_queue_replaces_the_old_one(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [5, 5]))
    first = log.wait_for(playing("T0"))
    replacement = PlayQueue(
        [QueueEntry("NEW", str(tone(tones, 5, "opus")), LyricsKind.PLAIN)], playlist_id="PL2"
    )
    player.load_queue(replacement, paused=True)
    state = log.wait_for(lambda s: s.track_id == "NEW", after=first.version)
    state = log.wait_for(
        lambda s: s.track_id == "NEW" and s.status is PlayerStatus.PAUSED, after=first.version
    )
    assert (state.playlist_id, state.queue_length, state.lyrics_kind) == (
        "PL2",
        1,
        LyricsKind.PLAIN,
    )


def test_a_file_that_cannot_be_played_is_reported_and_skipped(make_player, tones, tmp_path):
    player, log = make_player()
    broken = tmp_path / "broken.opus"
    broken.write_bytes(b"not audio at all")
    good = queue_of(tones, [1.5]).entries[0]
    player.load_queue(PlayQueue([QueueEntry("BAD", str(broken)), good]))
    state = log.wait_for(playing("T0"))
    assert any(s.error and "could not play" in s.error for s in log.states)
    assert state.queue_index == 1


def test_mpv_dying_is_reported_and_it_is_restarted_once(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [5]))
    started = log.wait_for(playing("T0"))
    first_pid = player._process.pid

    os.kill(first_pid, signal.SIGKILL)
    error = log.wait_for(lambda s: s.status is PlayerStatus.ERROR, after=started.version)
    assert "mpv exited unexpectedly" in error.error
    recovered = log.wait_for(lambda s: s.status is PlayerStatus.IDLE, after=error.version)
    assert recovered.error is None and recovered.queue_length == 0
    assert player._process.pid != first_pid

    player.load_queue(queue_of(tones, [5]))  # the new mpv works
    log.wait_for(playing("T0"), after=recovered.version)


def test_close_stops_mpv_and_removes_the_socket(make_player, tones, runtime):
    player, log = make_player()
    player.load_queue(queue_of(tones, [5]))
    log.wait_for(playing("T0"))
    process = player._process
    player.close()
    assert process.poll() is not None
    assert not (runtime / "mpv.sock").exists()
    assert not any(s.status is PlayerStatus.ERROR for s in log.states)  # a clean stop is no error


@pytest.mark.skipif(
    not MPRIS_PLUGIN.exists()
    or shutil.which("dbus-daemon") is None
    or shutil.which("busctl") is None,
    reason="needs the mpv MPRIS plugin, dbus-daemon and busctl",
)
def test_mpris_plugin_loads_and_its_playpause_reaches_our_state(make_player, tones, monkeypatch):
    bus_process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        address = bus_process.stdout.readline().strip()
        assert address, "the private session bus did not start"
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)

        player, log = make_player(mpris_plugin=str(MPRIS_PLUGIN))
        assert f"--script={MPRIS_PLUGIN}" in player.command_line()
        player.load_queue(queue_of(tones, [5]))
        started = log.wait_for(playing("T0"))

        def bus_names() -> list[str]:
            out = subprocess.run(
                ["busctl", "--user", "list", "--no-legend"], capture_output=True, text=True
            ).stdout
            return [line.split()[0] for line in out.splitlines() if line.strip()]

        deadline = time.monotonic() + 5
        names: list[str] = []
        while time.monotonic() < deadline and not names:
            names = [n for n in bus_names() if n.startswith("org.mpris.MediaPlayer2.mpv")]
            time.sleep(0.1)
        assert names, "mpv did not register on the session bus"

        def play_pause() -> None:
            subprocess.run(
                ["busctl", "--user", "call", names[0], "/org/mpris/MediaPlayer2",
                 "org.mpris.MediaPlayer2.Player", "PlayPause"],
                check=True, capture_output=True,
            )  # fmt: skip

        play_pause()  # someone else paused; we only observe
        paused = log.wait_for(lambda s: s.status is PlayerStatus.PAUSED, after=started.version)
        play_pause()
        log.wait_for(playing("T0"), after=paused.version)
    finally:
        bus_process.terminate()
        bus_process.wait(timeout=5)


def test_a_resumed_song_starts_paused_at_its_position_and_at_the_saved_volume(make_player, tones):
    """What a restart does: volume first, then the song loaded paused and seeked to where it was."""
    player, log = make_player()
    player.set_volume(40)
    player.load_queue(queue_of(tones, [30, 30, 30]), paused=True, position_s=12.5)
    state = log.wait_for(paused_at("T0", 12.5), timeout=8)
    time.sleep(0.4)
    state = log.bus.current
    assert state.status is PlayerStatus.PAUSED and state.track_id == "T0"
    assert state.position_s == pytest.approx(12.5, abs=0.1)
    assert state.volume == pytest.approx(40, abs=0.5)


def test_a_resume_position_is_not_applied_to_a_later_track(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [30, 30]), paused=False, position_s=20)
    log.wait_for(playing("T0"), timeout=8)
    player.next()
    state = log.wait_for(playing("T1"), after=0, timeout=8)
    time.sleep(0.5)
    assert state.position_s < 3  # started from the beginning, not from 20 s


def paused_at(track_id: str, position: float):
    def check(state):
        return (
            state.track_id == track_id
            and state.status is PlayerStatus.PAUSED
            and state.position_s >= position - 0.2
        )

    return check


def test_a_socket_path_too_long_for_the_system_is_one_clear_error(tmp_path):
    deep = tmp_path / ("d" * 110) / "mpv.sock"
    player = MpvPlayer(StateBus(), socket_path=deep, audio_output="null")
    with pytest.raises(PlaybackError, match="socket path is too long"):
        player.start()
    assert not deep.parent.exists()  # refused before anything was created or started


def reordered(queue: PlayQueue, ids: list[str]) -> PlayQueue:
    by_id = {entry.track_id: entry for entry in queue.entries}
    return PlayQueue([by_id[track_id] for track_id in ids], playlist_id=queue.playlist_id)


def test_reordering_the_queue_does_not_interrupt_the_song_that_is_playing(make_player, tones):
    player, log = make_player(heartbeat_s=0.2)
    queue = queue_of(tones, [30, 30, 30, 30, 30])
    player.load_queue(queue, position_s=10)
    log.wait_for(lambda s: s.track_id == "T0" and s.position_s >= 9.5 and playing()(s), timeout=8)
    time.sleep(0.6)
    before = log.bus.current

    player.reorder(reordered(queue, ["T3", "T0", "T4", "T2", "T1"]))
    time.sleep(0.8)
    after = log.bus.current
    # The same song, still playing, and the clock ran straight through the change.
    assert (after.track_id, after.status) == ("T0", PlayerStatus.PLAYING)
    assert after.queue_index == 1 and after.queue_length == 5
    ran = (after.captured_at_epoch_ms - before.captured_at_epoch_ms) / 1000
    assert after.position_s - before.position_s == pytest.approx(ran, abs=0.15)
    between = [s for s in log.states if before.version < s.version <= after.version]
    assert all(s.track_id == "T0" for s in between)
    assert not any(s.status is PlayerStatus.LOADING for s in between)

    # What follows is the new order, both ways.
    player.next()
    state = log.wait_for(playing("T4"), after=after.version, timeout=8)
    player.next()
    state = log.wait_for(playing("T2"), after=state.version, timeout=8)
    player.prev()
    state = log.wait_for(playing("T4"), after=state.version, timeout=8)
    player.prev()
    state = log.wait_for(playing("T0"), after=state.version, timeout=8)
    player.prev()
    log.wait_for(playing("T3"), after=state.version, timeout=8)


def test_reordering_while_paused_or_stopped_keeps_the_place(make_player, tones):
    player, log = make_player()
    queue = queue_of(tones, [30, 30, 30])
    player.load_queue(queue, paused=True, position_s=7)
    log.wait_for(paused_at("T0", 7), timeout=8)
    player.reorder(reordered(queue, ["T2", "T1", "T0"]))
    time.sleep(0.6)
    state = log.bus.current
    assert (state.track_id, state.status, state.queue_index) == ("T0", PlayerStatus.PAUSED, 2)
    assert state.position_s == pytest.approx(7, abs=0.1)

    player.stop()
    log.wait_for(lambda s: s.status is PlayerStatus.IDLE, after=state.version, timeout=8)
    player.reorder(reordered(queue, ["T1", "T2", "T0"]))
    player.play()  # a stopped queue starts again from its first track
    log.wait_for(playing("T1"), after=state.version, timeout=8)


def test_reordering_to_tracks_the_player_does_not_hold_loads_them_instead(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [30, 30]))
    state = log.wait_for(playing("T0"), timeout=8)
    other = queue_of(tones, [30, 30, 30])
    player.reorder(reordered(other, ["T2", "T0", "T1"]))
    state = log.wait_for(lambda s: s.queue_length == 3 and s.track_id == "T0", timeout=8)
    assert state.queue_index == 1


def test_a_restarted_mpv_keeps_the_volume_and_never_reports_the_default(make_player, tones):
    player, log = make_player()
    player.set_volume(40)
    player.load_queue(queue_of(tones, [5]))
    started = log.wait_for(lambda s: playing("T0")(s) and s.volume == 40)
    first_pid = player._process.pid

    os.kill(first_pid, signal.SIGKILL)
    error = log.wait_for(lambda s: s.status is PlayerStatus.ERROR, after=started.version)
    recovered = log.wait_for(lambda s: s.status is PlayerStatus.IDLE, after=error.version)
    assert player._process.pid != first_pid
    assert "--volume=40" in player.command_line()
    later = [s.volume for s in log.states if s.version >= error.version]
    assert set(later) == {40.0}, later

    player.load_queue(queue_of(tones, [5]))
    assert log.wait_for(playing("T0"), after=recovered.version).volume == 40


def test_a_volume_changed_from_outside_is_the_one_a_restart_keeps(make_player, tones):
    """The media keys change the volume inside mpv; that is still the listener's setting."""
    player, log = make_player()
    player.load_queue(queue_of(tones, [5]))
    started = log.wait_for(playing("T0"))
    player._command("set_property", "volume", 55)  # not through set_volume
    log.wait_for(lambda s: s.volume == 55, after=started.version)
    os.kill(player._process.pid, signal.SIGKILL)
    log.wait_for(lambda s: s.status is PlayerStatus.IDLE and s.volume == 55, after=started.version)
    assert "--volume=55" in player.command_line()


def test_mpv_runs_in_its_own_process_group(make_player):
    """A Ctrl-C in the terminal goes to our group; mpv must not be in it."""
    player, _ = make_player()
    assert os.getpgid(player._process.pid) == player._process.pid
    assert os.getpgid(player._process.pid) != os.getpgid(0)
    assert not any(arg.startswith("--volume") for arg in player.command_line())  # none set yet


def gain(player) -> float:
    return float(player._command("get_property", "volume-gain"))


def test_a_fade_in_is_silent_while_paused_then_rises_and_leaves_the_volume_alone(
    make_player, tones
):
    player, log = make_player()
    player.set_volume(40)
    player.load_queue(queue_of(tones, [30]), paused=True)
    log.wait_for(lambda s: s.status is PlayerStatus.PAUSED and s.track_id == "T0")
    assert gain(player) == 0.0  # nothing asked for yet

    player.set_fade_in(1.0)
    time.sleep(0.3)
    assert gain(player) == pytest.approx(-60.0)  # silent, and staying so while paused

    player.play()
    started = time.monotonic()
    log.wait_for(playing("T0"))
    readings = []
    while time.monotonic() - started < 1.8:
        readings.append((time.monotonic() - started, gain(player)))
        time.sleep(0.04)
    levels = [level for _, level in readings]
    assert levels == sorted(levels), "the level went down during a fade-in"
    assert levels[0] < -12 and levels[-1] == 0.0
    # Half-way through the fade the sound is at half amplitude, about -6 dB.
    midway = min(readings, key=lambda reading: abs(reading[0] - 0.5))[1]
    assert -12 < midway < -3, midway
    reached = next(t for t, level in readings if level == 0.0)
    assert 0.8 < reached < 1.5, reached
    # The listener's volume was never touched, so nothing has to put it back.
    assert {s.volume for s in log.states if s.volume is not None} == {100.0, 40.0}
    assert log.bus.current.volume == 40
    # The snapshot that started playback said so, for the overlay to draw the rise.
    first_playing = next(s for s in log.states if s.status is PlayerStatus.PLAYING)
    assert first_playing.fade_in_s == 1.0
    assert first_playing.fade_from_s == pytest.approx(first_playing.position_s)
    assert all(s.fade_in_s == 0 for s in log.states if s.status is PlayerStatus.PAUSED)


def test_every_resume_fades_in_from_silence_and_a_pause_mid_fade_starts_over(make_player, tones):
    player, log = make_player()
    player.set_fade_in(1.0)
    player.load_queue(queue_of(tones, [30]))
    log.wait_for(playing("T0"))
    time.sleep(0.2)
    assert gain(player) == 0.0  # a song played straight away is not a resume

    player.pause()
    time.sleep(0.3)
    assert gain(player) == pytest.approx(-60.0)
    player.play()
    time.sleep(0.4)
    part_way = gain(player)
    assert -60 < part_way < 0
    player.pause()
    time.sleep(0.3)
    assert gain(player) == pytest.approx(-60.0)  # back to silence, not held part-way
    player.play()
    time.sleep(1.5)
    assert gain(player) == 0.0

    player.next()  # skipping while playing does not fade
    time.sleep(0.3)
    assert gain(player) == 0.0


def test_turning_the_fade_off_while_paused_restores_the_level(make_player, tones):
    player, log = make_player()
    player.set_fade_in(2.0)
    player.load_queue(queue_of(tones, [30]), paused=True)
    log.wait_for(lambda s: s.status is PlayerStatus.PAUSED and s.track_id == "T0")
    time.sleep(0.3)
    assert gain(player) == pytest.approx(-60.0)
    player.set_fade_in(0)
    assert gain(player) == 0.0 and player._fade_s is None
    player.play()
    log.wait_for(playing("T0"))
    time.sleep(0.2)
    assert gain(player) == 0.0


def test_a_fade_of_no_length_and_an_mpv_without_the_control_do_nothing(
    make_player, tones, monkeypatch, capsys
):
    player, log = make_player()
    player.set_fade_in(0)
    assert gain(player) == 0.0 and player._fade_s is None

    real = player._command

    def old_mpv(*args):
        if "volume-gain" in args:
            raise MpvCommandError("mpv refused ['set_property', 'volume-gain']: property not found")
        return real(*args)

    monkeypatch.setattr(player, "_command", old_mpv)
    player.set_fade_in(3.0)  # must not raise
    player.load_queue(queue_of(tones, [5]), paused=True)
    log.wait_for(lambda s: s.status is PlayerStatus.PAUSED and s.track_id == "T0")
    time.sleep(0.2)
    assert player._fade_s is None and player._fade_len == 0
    assert "no fade-in with this mpv" in capsys.readouterr().err
    player.play()
    log.wait_for(playing("T0"))  # and playback is unaffected


def filters(player) -> list:
    return player._command("get_property", "af")


def test_mono_mixes_while_playing_turns_off_and_outlives_a_restart(make_player, tones):
    player, log = make_player()
    player.load_queue(queue_of(tones, [30]))
    started = log.wait_for(playing("T0"))
    assert filters(player) == []

    player.set_mono(True)
    (mono,) = filters(player)
    assert mono["label"] == "mono" and "pan=stereo" in mono["params"]["graph"]
    time.sleep(0.3)
    assert log.bus.current.status is PlayerStatus.PLAYING  # the song plays on, mixed
    player.set_mono(True)  # setting it twice is setting it once
    assert len(filters(player)) == 1

    os.kill(player._process.pid, signal.SIGKILL)
    error = log.wait_for(lambda s: s.status is PlayerStatus.ERROR, after=started.version)
    log.wait_for(lambda s: s.status is PlayerStatus.IDLE, after=error.version)
    assert [f["label"] for f in filters(player)] == ["mono"]  # the new mpv starts mixed

    player.set_mono(False)
    assert filters(player) == []
    player.load_queue(queue_of(tones, [5]))
    log.wait_for(playing("T0"), after=error.version)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_mono_puts_one_sided_sound_in_both_ears(runtime):
    """What mpv writes out with mono on is the same on both sides."""
    left_only, written = runtime / "left.wav", runtime / "out.wav"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=1",
         "-af", "pan=stereo|c0=c0|c1=0*c0", str(left_only)],
        check=True,
    )  # fmt: skip
    player = MpvPlayer(StateBus(), socket_path=runtime / "mpv.sock")
    player._mono = True  # as a restart after set_mono(True) would start it
    args = [a for a in player.command_line() if not a.startswith(("--idle", "--input-ipc"))]
    subprocess.run(
        [*args, "--ao=pcm", f"--ao-pcm-file={written}", str(left_only)], check=True, timeout=20
    )
    with wave.open(str(written)) as out:
        assert out.getnchannels() == 2
        samples = memoryview(out.readframes(out.getnframes())).cast("h")
    left, right = samples[0::2], samples[1::2]
    assert max(abs(sample) for sample in left) > 1000  # there is sound, not silence
    assert left.tolist() == right.tolist()


def test_a_resume_part_way_through_marks_its_fade_where_it_resumed(make_player, tones):
    """What the overlay draws the rising level from, on a resume in the middle of a song."""
    from spotifixed.overlay.presenter import fade_level

    player, log = make_player()
    player.set_volume(64)
    player.set_fade_in(2.0)
    player.load_queue(queue_of(tones, [30]))
    log.wait_for(playing("T0"))
    time.sleep(1.5)
    player.pause()
    paused = log.wait_for(lambda s: s.status is PlayerStatus.PAUSED)
    time.sleep(0.3)
    player.play()
    resumed = log.wait_for(playing("T0"), after=paused.version)
    assert resumed.fade_in_s == 2.0
    assert resumed.fade_from_s == pytest.approx(paused.position_s, abs=0.3)
    level = fade_level(resumed, resumed.captured_at_epoch_ms + 500)
    assert level is not None and 0 < level < 64, level
