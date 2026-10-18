"""The overlay's daemon client against a real daemon (with a fake player).

No display is involved: the "renderer" here is an event that the client sets whenever
it would wake one.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from spotifixed.config import load_settings
from spotifixed.overlay.client import DaemonClient
from spotifixed.overlay.clock import now_epoch_ms
from spotifixed.overlay.presenter import OverlayModel
from spotifixed.server.app import Daemon
from tests.support.library import PLAYLIST_ID, build_library
from tests.support.playback import FakePlayer


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(predicate, timeout=5.0, message="condition not met in time"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)


class Harness:
    def __init__(self, store, monkeypatch):
        monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
        monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
        build_library(store)
        self.store = store
        self.settings = load_settings()
        self.port = free_port()
        self.daemon: Daemon | None = None
        self.player: FakePlayer | None = None
        self.model = OverlayModel(lead_ms=0)
        self.woken = 0
        self.fetched: list[str] = []
        self.documents: list[dict] = []
        self.client = DaemonClient(
            f"http://127.0.0.1:{self.port}",
            self.model,
            on_change=self._wake,
            on_overlay=self.documents.append,
            reconnect_s=(0.05, 0.2),
        )
        original = self.client.fetch_lines

        def counting(track_id):
            self.fetched.append(track_id)
            return original(track_id)

        self.client.fetch_lines = counting

    def _wake(self) -> None:
        self.woken += 1

    def start_daemon(self) -> None:
        self.player = FakePlayer()
        self.daemon = Daemon(
            self.settings, self.store, self.player, port=self.port, keepalive_s=0.2
        )
        threading.Thread(target=self.daemon.serve_forever, daemon=True).start()

    def stop_daemon(self) -> None:
        if self.daemon is not None:
            self.daemon.shutdown()
            self.daemon = None

    def play(self, **kwargs) -> None:
        queue = self.daemon.routes._library.queue_for(PLAYLIST_ID, **kwargs)
        self.player.load_queue(queue)

    def text_in(self, seconds: float) -> str:
        """What the overlay would show ``seconds`` from now, if playback just continues."""
        shown = self.model.view(now_epoch_ms() + int(seconds * 1000))
        return shown.text if shown.visible else ""

    def close(self) -> None:
        self.client.stop()
        self.stop_daemon()


@pytest.fixture
def harness(store, monkeypatch):
    h = Harness(store, monkeypatch)
    yield h
    h.close()


def test_follows_state_and_fetches_each_tracks_lines_once(harness):
    harness.start_daemon()
    harness.client.start()
    wait_until(lambda: harness.daemon.open_streams == 1)
    assert harness.text_in(1.2) == ""  # idle: nothing to show

    harness.play()  # TRK0000 from position 0, playing
    wait_until(lambda: harness.text_in(1.2) == "an invented first line")
    assert harness.text_in(0.5) == ""  # before the first line
    assert harness.text_in(3.0) == ""  # the gap
    assert harness.text_in(4.5) == "an invented last line"
    assert harness.fetched == ["TRK0000"]

    harness.player.set_volume(55)  # more snapshots for the same track: no new fetch
    harness.player.seek(position_s=0.0)
    time.sleep(0.2)
    assert harness.fetched == ["TRK0000"]

    harness.player.next()
    wait_until(lambda: harness.fetched == ["TRK0000", "TRK0001"])
    wait_until(lambda: harness.text_in(1.2) == "an invented first line")

    harness.player.pause()
    wait_until(lambda: harness.text_in(1.2) == "")
    harness.player.play()
    wait_until(lambda: harness.text_in(1.2) == "an invented first line")


def test_a_track_without_synced_lyrics_is_hidden_and_not_fetched(harness):
    harness.start_daemon()
    harness.client.start()
    harness.play(start_track_id="TRK0003")  # plain lyrics only
    wait_until(lambda: harness.daemon.open_streams == 1 and harness.woken >= 2)
    time.sleep(0.2)
    assert harness.text_in(1.2) == "" and harness.fetched == []


def test_a_dropped_connection_hides_the_overlay_and_reconnecting_restores_it(harness):
    harness.start_daemon()
    harness.client.start()
    harness.play()
    wait_until(lambda: harness.text_in(1.2) == "an invented first line")

    harness.stop_daemon()
    wait_until(lambda: harness.text_in(1.2) == "", message="still showing after the daemon left")
    woken_while_down = harness.woken

    # A restarted daemon numbers its snapshots from 1 again; they must not look stale.
    harness.start_daemon()
    wait_until(lambda: harness.daemon.open_streams == 1, message="the client did not reconnect")
    assert harness.woken > woken_while_down
    assert harness.text_in(1.2) == ""  # connected, but the new daemon is idle
    harness.play(start_track_id="TRK0001")
    wait_until(lambda: harness.text_in(1.2) == "an invented first line")


def test_with_no_daemon_it_keeps_trying_quietly_and_stops_promptly(harness):
    harness.client.start()
    wait_until(lambda: harness.woken >= 3)  # several failed attempts, each reported
    assert harness.text_in(1.2) == ""
    started = time.monotonic()
    harness.client.stop()
    assert time.monotonic() - started < 1.0
    assert not harness.client._thread.is_alive()


def test_stop_interrupts_a_client_that_is_waiting_on_the_stream(harness):
    harness.start_daemon()
    harness.client.start()
    wait_until(lambda: harness.daemon.open_streams == 1)
    started = time.monotonic()
    harness.client.stop()
    assert time.monotonic() - started < 1.0
    assert not harness.client._thread.is_alive()
    wait_until(lambda: harness.daemon.open_streams == 0)


def test_a_failed_lyrics_fetch_is_an_empty_answer_not_an_error(harness):
    harness.start_daemon()
    assert harness.client.fetch_lines("TRK0000")[0].text == "an invented first line"
    assert harness.client.fetch_lines("NeverSynced") == ()
    assert harness.client.fetch_lines("../bad") == ()
    harness.stop_daemon()
    assert harness.client.fetch_lines("TRK0000") == ()


def test_the_overlay_settings_arrive_on_connecting_and_on_every_change(harness):
    harness.start_daemon()
    harness.daemon.overlay.update({"margin_y": 300})
    harness.client.start()
    wait_until(lambda: harness.documents)
    assert harness.documents[0]["overrides"] == {"margin_y": 300}
    # The daemon can tell an overlay from a browser, and says so in the document.
    wait_until(lambda: harness.documents[-1]["clients"] == 1)

    woken = harness.woken
    harness.daemon.overlay.update({"anchor": "top"})
    wait_until(lambda: harness.documents[-1]["overrides"] == {"margin_y": 300, "anchor": "top"})
    assert harness.woken > woken  # the renderer is told to look again

    harness.play()  # playback events and settings events share the stream
    wait_until(lambda: harness.text_in(1.2) == "an invented first line")
    harness.daemon.overlay.preview(5)
    wait_until(lambda: harness.documents[-1]["preview_until_ms"] > 0)
    versions = [document["version"] for document in harness.documents]
    assert versions == sorted(set(versions))


def test_a_restarted_daemon_sends_its_settings_again_and_forgets_the_overlay_it_lost(harness):
    harness.start_daemon()
    harness.client.start()
    wait_until(lambda: harness.documents and harness.documents[-1]["clients"] == 1)
    daemon = harness.daemon
    harness.client.stop()
    wait_until(lambda: daemon.overlay.snapshot()["clients"] == 0)

    harness.stop_daemon()
    harness.documents.clear()
    harness.client = DaemonClient(
        f"http://127.0.0.1:{harness.port}", harness.model, on_change=harness._wake,
        on_overlay=harness.documents.append, reconnect_s=(0.05, 0.2),
    )  # fmt: skip
    harness.start_daemon()  # versions start again from 1; they must not look stale
    harness.client.start()
    wait_until(lambda: harness.documents and harness.documents[-1]["clients"] == 1)


def test_the_monitors_are_reported_on_connecting_and_when_they_change(harness):
    seen = [{"connector": "DP-1", "description": "Dell Inc. DELL U2720Q"}]
    harness.client = DaemonClient(
        f"http://127.0.0.1:{harness.port}", harness.model, on_change=harness._wake,
        on_overlay=harness.documents.append, monitors=lambda: list(seen),
        reconnect_s=(0.05, 0.2),
    )  # fmt: skip
    harness.start_daemon()
    harness.client.start()
    wait_until(lambda: harness.documents and harness.documents[-1]["monitors"] == seen)
    assert harness.documents[-1]["clients"] == 1

    seen.append({"connector": "HDMI-A-1", "description": ""})
    harness.client.report_monitors()
    wait_until(lambda: len(harness.documents[-1]["monitors"]) == 2)

    # Lost when the overlay goes, and reported again when it reconnects.
    harness.stop_daemon()
    harness.start_daemon()
    wait_until(lambda: harness.daemon.overlay.snapshot()["monitors"] == seen)


def test_a_client_without_a_settings_handler_still_follows_playback(harness):
    client = DaemonClient(
        f"http://127.0.0.1:{harness.port}", harness.model, on_change=harness._wake,
        reconnect_s=(0.05, 0.2),
    )  # fmt: skip
    harness.start_daemon()
    client.start()
    try:
        wait_until(lambda: harness.daemon.open_streams == 1)
        harness.daemon.overlay.update({"lines": 2})
        harness.play()
        wait_until(lambda: harness.text_in(1.2) == "an invented first line")
    finally:
        client.stop()


def test_settings_that_cannot_be_applied_do_not_stop_the_lyrics(harness, capsys):
    def explode(document):
        raise RuntimeError("could not apply")

    client = DaemonClient(
        f"http://127.0.0.1:{harness.port}", harness.model, on_change=harness._wake,
        on_overlay=explode, reconnect_s=(0.05, 0.2),
    )  # fmt: skip
    harness.start_daemon()
    client.start()
    try:
        wait_until(lambda: harness.daemon.open_streams == 1)
        harness.daemon.overlay.update({"lines": 2})
        harness.play()
        wait_until(lambda: harness.text_in(1.2) == "an invented first line")
        assert client._thread.is_alive()
        assert "overlay settings: could not apply" in capsys.readouterr().err
    finally:
        client.stop()
