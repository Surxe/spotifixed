"""The daemon over a real socket: request checks, the event stream, ``ctl``, and one
end-to-end run with a real mpv (silent) playing generated tones.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from spotifixed import cli
from spotifixed.cache import assets
from spotifixed.config import load_settings
from spotifixed.playback.mpv import MpvPlayer
from spotifixed.playback.state import StateBus
from spotifixed.server.app import Daemon
from tests.support.audio import tone
from tests.support.library import PLAYLIST_ID, build_library
from tests.support.playback import FakePlayer, StateLog


class Client:
    """A deliberately low-level HTTP client, so tests control every header."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon

    def request(self, method, path, body=None, headers=None, *, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.daemon.port, timeout=5)
        send = {"Host": host or f"127.0.0.1:{self.daemon.port}"}
        raw = None
        if body is not None:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            send["Content-Type"] = "application/json"
        send.update(headers or {})
        connection.putrequest(method, path, skip_host=True)
        for name, value in send.items():
            if value is not None:
                connection.putheader(name, value)
        if raw is not None:
            connection.putheader("Content-Length", str(len(raw)))
        connection.endheaders(raw)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def json(self, method, path, body=None, **kwargs):
        response, data = self.request(method, path, body, **kwargs)
        return response.status, json.loads(data)


class EventStream:
    """Reads an event stream on a thread and collects the events and comments it sees.

    ``events`` are the playback states; ``overlay`` are the overlay settings documents;
    ``tracks`` are the notes that a track was turned off or on.
    """

    def __init__(self, daemon: Daemon, headers: dict[str, str] | None = None) -> None:
        self.events: list[dict] = []
        self.overlay: list[dict] = []
        self.tracks: list[dict] = []
        self.comments: list[str] = []
        self.headers = None
        self._seen = threading.Condition()
        self._connection = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=10)
        self._connection.request("GET", "/api/v1/events", headers=headers or {})
        # Kept for close(): http.client hands the socket over to the response and
        # forgets it once a response has no Content-Length.
        self._socket = self._connection.sock
        self._response = self._connection.getresponse()
        self.status = self._response.status
        self.headers = self._response.headers
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        block: list[str] = []
        try:
            for raw in self._response:
                line = raw.decode().rstrip("\n")
                if line:
                    block.append(line)
                    continue
                with self._seen:
                    kind = self.events
                    for entry in block:
                        if entry == "event: overlay":
                            kind = self.overlay
                        elif entry == "event: tracks":
                            kind = self.tracks
                        elif entry.startswith("data: "):
                            kind.append(json.loads(entry[6:]))
                        elif entry.startswith(":"):
                            self.comments.append(entry)
                    self._seen.notify_all()
                block = []
        except (OSError, ValueError, AttributeError):
            pass

    def wait(self, predicate, timeout=5.0):
        with self._seen:
            assert self._seen.wait_for(predicate, timeout), (self.events[-3:], self.comments)

    def close(self) -> None:
        # The client just goes away. Shut the socket down first: that wakes the reader
        # thread, which otherwise holds the response's lock until its read times out.
        with contextlib.suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._response.close()
        self._connection.close()


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


@pytest.fixture
def settings(store, monkeypatch):
    monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
    monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
    return load_settings()


@pytest.fixture
def start(settings, store):
    running = []

    def start(player, **kwargs) -> Daemon:
        daemon = Daemon(settings, store, player, port=0, **kwargs)
        threading.Thread(target=daemon.serve_forever, daemon=True).start()
        running.append(daemon)
        return daemon

    yield start
    for daemon in running:
        daemon.shutdown()


@pytest.fixture
def fake(start, store):
    build_library(store)
    player = FakePlayer()
    daemon = start(player, keepalive_s=0.3)
    return daemon, player, Client(daemon)


# ------------------------------ request checks ------------------------------ #


def test_reads_and_actions_work_for_the_daemons_own_address(fake):
    daemon, player, client = fake
    status, body = client.json("GET", "/api/v1/playlists")
    assert status == 200 and body["playlists"][0]["id"] == PLAYLIST_ID
    status, body = client.json("POST", "/api/v1/player/load", {"playlist_id": PLAYLIST_ID})
    assert (status, body["queue_length"]) == (200, 4)
    status, _ = client.json("GET", "/api/v1/state", host=f"localhost:{daemon.port}")
    assert status == 200
    own = f"http://127.0.0.1:{daemon.port}"
    status, _ = client.json("POST", "/api/v1/player/pause", {}, headers={"Origin": own})
    assert status == 200 and player.calls[-1] == ("pause",)


def test_responses_carry_security_headers_and_no_cors(fake):
    _, _, client = fake
    for path in ("/", "/api/v1/state", "/static/app.js"):
        response, _ = client.request("GET", path)
        assert response.status == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert not [h for h in response.headers if h.lower().startswith("access-control-")]
        assert "Python" not in response.headers.get("Server", "")


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1", "10.0.0.5:8765"])
def test_a_wrong_host_header_is_refused(fake, host):
    _, player, client = fake
    status, body = client.json("GET", "/api/v1/state", host=host)
    assert status == 403 and "loopback" in body["error"]
    status, _ = client.json("POST", "/api/v1/player/play", {}, host=host)
    assert status == 403 and player.calls == []


def test_a_foreign_origin_is_refused(fake):
    _, player, client = fake
    for origin in ("https://evil.example", "http://127.0.0.1:1", "null"):
        status, body = client.json("POST", "/api/v1/player/play", {}, headers={"Origin": origin})
        assert status == 403 and "cross-origin" in body["error"]
        status, _ = client.json("GET", "/api/v1/playlists", headers={"Origin": origin})
        assert status == 403
    assert player.calls == []


def test_a_post_without_the_json_content_type_is_refused(fake):
    _, player, client = fake
    for content_type in (None, "text/plain", "application/x-www-form-urlencoded"):
        response, _ = client.request(
            "POST", "/api/v1/player/play", b"{}", headers={"Content-Type": content_type}
        )
        assert response.status == 415
    assert player.calls == []


def test_oversized_and_malformed_requests(fake):
    daemon, player, client = fake
    status, _ = client.json("POST", "/api/v1/player/seek", b"x" * 70_000)
    assert status == 413
    status, _ = client.json("POST", "/api/v1/player/seek", b"{broken")
    assert status == 400
    status, _ = client.json("GET", "/api/v1/tracks/..%2F..%2Fetc%2Fpasswd")
    assert status == 400
    status, _ = client.json("PUT", "/api/v1/player/play", {})
    assert status == 405
    assert player.calls == []


def test_query_strings_are_ignored_not_misrouted(fake):
    _, _, client = fake
    status, body = client.json("GET", "/api/v1/state?cache=bust")
    assert status == 200 and body["status"] == "idle"


# ------------------------------- event stream ------------------------------- #


def test_event_stream_sends_the_current_state_then_changes_then_keepalives(fake):
    daemon, player, client = fake
    stream = EventStream(daemon)
    try:
        assert stream.status == 200
        assert stream.headers["Content-Type"].startswith("text/event-stream")
        stream.wait(lambda: len(stream.events) >= 1)
        assert (stream.events[0]["status"], stream.events[0]["version"]) == ("idle", 1)
        assert daemon.open_streams == 1

        client.json("POST", "/api/v1/player/load", {"playlist_id": PLAYLIST_ID})
        stream.wait(lambda: any(e["track_id"] == "TRK0000" for e in stream.events))
        client.json("POST", "/api/v1/player/next", {})
        stream.wait(lambda: stream.events[-1]["track_id"] == "TRK0001")
        versions = [e["version"] for e in stream.events]
        assert versions == sorted(versions) and len(set(versions)) == len(versions)

        stream.wait(lambda: len(stream.comments) >= 1, timeout=3)
        assert stream.comments[0] == ": keepalive"
    finally:
        stream.close()
    # The next write (a keepalive at the latest) notices the client is gone.
    wait_until(lambda: daemon.open_streams == 0)


def test_event_stream_carries_the_overlay_settings_alongside_playback(fake):
    daemon, player, client = fake
    daemon.overlay.update({"margin_y": 300})
    browser = EventStream(daemon)
    try:
        browser.wait(lambda: browser.overlay and browser.events)
        assert browser.overlay[0]["overrides"] == {"margin_y": 300}
        assert browser.overlay[0]["clients"] == 0  # a browser is not an overlay

        status, body = client.json("POST", "/api/v1/overlay", {"anchor": "top"})
        assert status == 200 and body["settings"]["anchor"] == "top"
        browser.wait(lambda: browser.overlay[-1]["settings"]["anchor"] == "top")

        overlay = EventStream(daemon, {"X-Spotifixed-Client": "overlay"})
        try:
            browser.wait(lambda: browser.overlay[-1]["clients"] == 1)
            # Playback events still arrive, and are not mistaken for settings.
            client.json("POST", "/api/v1/player/volume", {"volume": 35})
            overlay.wait(lambda: overlay.events and overlay.events[-1]["volume"] == 35)
            assert all("volume" not in document for document in overlay.overlay)
        finally:
            overlay.close()
        browser.wait(lambda: browser.overlay[-1]["clients"] == 0, timeout=5)
    finally:
        browser.close()


def test_event_stream_says_when_a_track_is_turned_off_or_on(fake):
    daemon, player, client = fake
    browser = EventStream(daemon)
    try:
        browser.wait(lambda: browser.events)
        assert browser.tracks == []  # nothing on connecting
        status, _ = client.json("POST", "/api/v1/tracks/TRK0001/disabled", {"disabled": True})
        assert status == 200
        browser.wait(lambda: browser.tracks == [{"version": 1}])
        client.json("POST", "/api/v1/tracks/TRK0001/disabled", {"disabled": False})
        browser.wait(lambda: browser.tracks[-1] == {"version": 2})
    finally:
        browser.close()


def test_overlay_changes_are_refused_from_another_origin(fake):
    daemon, player, client = fake
    status, body = client.json(
        "POST", "/api/v1/overlay", {"margin_y": 300}, headers={"Origin": "http://evil.example"}
    )
    assert status == 403
    response, _ = client.request(
        "POST", "/api/v1/overlay", b"margin_y=300", headers={"Content-Type": "text/plain"}
    )
    assert response.status == 415
    assert daemon.overlay.snapshot()["overrides"] == {}


def test_several_streams_each_get_every_event(fake):
    daemon, player, client = fake
    streams = [EventStream(daemon) for _ in range(3)]
    try:
        wait_until(lambda: daemon.open_streams == 3)
        client.json("POST", "/api/v1/player/volume", {"volume": 40})
        for stream in streams:
            stream.wait(lambda s=stream: s.events and s.events[-1]["volume"] == 40)
    finally:
        for stream in streams:
            stream.close()
    wait_until(lambda: daemon.open_streams == 0)


def test_shutdown_ends_open_streams(start, store):
    build_library(store)
    daemon = start(FakePlayer(), keepalive_s=30)
    stream = EventStream(daemon)
    stream.wait(lambda: len(stream.events) >= 1)
    daemon.stopping.set()
    wait_until(lambda: daemon.open_streams == 0, timeout=3)
    stream.close()


def test_the_event_stream_refuses_a_foreign_origin(fake):
    daemon, _, client = fake
    status, _ = client.json("GET", "/api/v1/events", headers={"Origin": "https://evil.example"})
    assert status == 403 and daemon.open_streams == 0


# ----------------------------------- ctl ----------------------------------- #


@pytest.fixture
def ctl(fake, monkeypatch):
    daemon, player, _ = fake
    monkeypatch.setenv("SPOTIFIXED_PORT", str(daemon.port))
    return player


@pytest.mark.parametrize(
    ("argv", "call"),
    [
        (["toggle"], ("toggle",)),
        (["play"], ("play",)),
        (["pause"], ("pause",)),
        (["next"], ("next",)),
        (["prev"], ("prev",)),
        (["stop"], ("stop",)),
        (["seek", "+10"], ("seek", None, 10.0)),
        (["seek", "-5.5"], ("seek", None, -5.5)),
        (["seek", "90"], ("seek", 90.0, None)),
        (["volume", "70"], ("set_volume", 70.0)),
    ],
)
def test_ctl_sends_each_verb(ctl, argv, call):
    assert cli.main(["ctl", *argv]) == 0
    assert ctl.calls == [call]


def test_ctl_reports_bad_values_and_refusals_in_one_line(ctl, capsys):
    assert cli.main(["ctl", "seek"]) == 1
    assert "ctl seek needs a number" in capsys.readouterr().err
    assert cli.main(["ctl", "volume", "400"]) == 1
    assert "volume must be between 0 and 100" in capsys.readouterr().err
    assert ctl.calls == []


def test_ctl_with_no_daemon_says_so(monkeypatch, capsys):
    monkeypatch.setenv("SPOTIFIXED_PORT", "1")  # nothing listens there
    assert cli.main(["ctl", "toggle"]) == 1
    assert "no daemon is answering at http://127.0.0.1:1" in capsys.readouterr().err


# -------------------------- end to end with real mpv -------------------------- #


@pytest.mark.mpv
@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv is not installed")
def test_a_playlist_plays_end_to_end_driven_by_ctl(start, store, tmp_path_factory, monkeypatch):
    seconds = 1.5
    build_library(store, audio_file=tone(tmp_path_factory.mktemp("tones"), seconds, "opus"),
                  seconds=seconds)  # fmt: skip
    runtime = Path(tempfile.mkdtemp(prefix="pp-"))
    bus = StateBus()
    log = StateLog(bus)
    player = MpvPlayer(bus, socket_path=runtime / "mpv.sock", audio_output="null")
    player.start()
    try:
        daemon = start(player)
        client = Client(daemon)
        monkeypatch.setenv("SPOTIFIXED_PORT", str(daemon.port))
        stream = EventStream(daemon)

        status, body = client.json("POST", "/api/v1/player/load", {"playlist_id": PLAYLIST_ID})
        assert (status, body["queue_length"]) == (200, 4)
        first = log.wait_for(lambda s: s.status.value == "playing" and s.track_id == "TRK0000")
        assert first.lyrics_kind.value == "synced"

        assert cli.main(["ctl", "pause"]) == 0
        paused = log.wait_for(lambda s: s.status.value == "paused", after=first.version)
        status, body = client.json("POST", "/api/v1/tracks/TRK0000/offset", {"offset_ms": 200})
        assert status == 200
        nudged = log.wait_for(lambda s: s.offset_ms == 200, after=paused.version)
        assert assets.read_track_state(store, "TRK0000").offset_ms == 200

        assert cli.main(["ctl", "next"]) == 0
        log.wait_for(lambda s: s.track_id == "TRK0001", after=nudged.version)
        assert cli.main(["ctl", "play"]) == 0

        # Left alone, it plays the rest of the queue (the plain-lyrics track included).
        for expected in ("TRK0001", "TRK0002", "TRK0003"):
            log.wait_for(
                lambda s, t=expected: s.status.value == "playing" and s.track_id == t,
                after=nudged.version,
            )
        ended = log.wait_for(lambda s: s.status.value == "ended", after=nudged.version)
        assert ended.queue_length == 4

        # The event stream told the same story the bus did, in order.
        stream.wait(lambda: stream.events and stream.events[-1]["status"] == "ended")
        seen = [e["track_id"] for e in stream.events if e["status"] == "playing"]
        assert seen[0] == "TRK0000" and seen[-1] == "TRK0003"
        assert [e["version"] for e in stream.events] == sorted(e["version"] for e in stream.events)
        status, state = client.json("GET", "/api/v1/state")
        assert state["status"] == "ended" and state["version"] == ended.version
        stream.close()
    finally:
        player.close()
        shutil.rmtree(runtime, ignore_errors=True)


def test_serve_refuses_a_bad_overlay_setting_before_starting_mpv(monkeypatch, capsys):
    """The daemon holds the overlay's settings now, so it is the one to say they are wrong."""
    started = []
    monkeypatch.setattr(MpvPlayer, "start", lambda self: started.append(self))
    monkeypatch.setenv("SPOTIFIXED_OVERLAY_COLOR", "red; }")
    assert cli.main(["serve"]) == 1
    error = capsys.readouterr().err
    assert "SPOTIFIXED_OVERLAY_COLOR must be a colour" in error and "Traceback" not in error
    assert started == []
