"""The ``start`` command's wiring: what it launches, and that it takes it down again."""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from spotifixed import cli, launch
from spotifixed.config import load_settings, settings_arguments
from spotifixed.overlay_settings import OverlaySettings
from spotifixed.server.overlay_config import OverlayConfig

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_settings_flags_given_to_start_are_handed_on_as_given():
    args = cli.build_parser().parse_args(
        ["start", "--no-browser", "--port", "9100", "--overlay-anchor", "top-left"]
    )
    assert settings_arguments(args) == ["--port", "9100", "--overlay-anchor", "top-left"]
    command = launch.overlay_command(settings_arguments(args))
    assert command[:4] == [sys.executable, "-m", "spotifixed", "overlay"]
    # What is handed on parses as the overlay's own flags, to the same settings.
    assert load_settings(cli.build_parser().parse_args(command[3:])) == load_settings(args)


def config(*, enabled: bool = True) -> OverlayConfig:
    made = OverlayConfig(OverlaySettings.from_settings(load_settings()))
    made._enabled = enabled
    return made


def wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_a_running_overlay_is_stopped_and_that_is_not_reported(capfd):
    overlay = launch.Overlay(SLEEP, config(), threading.Event())
    overlay.follow()
    process = overlay._process
    assert process.poll() is None
    overlay.stop()
    assert process.poll() is not None
    time.sleep(0.1)
    assert capfd.readouterr().err == ""


def test_the_overlay_follows_the_switch_and_is_not_reported_when_switched_off(capfd):
    switch = config()
    overlay = launch.Overlay(SLEEP, switch, threading.Event())
    overlay.follow()
    assert switch.snapshot()["managed"] is True
    first = overlay._process
    switch.set_enabled(False)
    assert first.poll() is not None and overlay._process is None
    switch.set_enabled(True)
    second = overlay._process
    assert second is not first and second.poll() is None
    time.sleep(1.2)  # past the moment a stop by itself would be reported
    assert capfd.readouterr().err == ""
    overlay.stop()
    assert second.poll() is not None
    switch.set_enabled(False)  # no longer followed: nothing to do, nothing raised
    switch.set_enabled(True)
    assert overlay._process is None


def test_switched_off_it_is_not_started_at_all(capfd):
    overlay = launch.Overlay(SLEEP, config(enabled=False), threading.Event())
    overlay.follow()
    assert overlay._process is None
    assert "switched off" in capfd.readouterr().out
    overlay.stop()


def test_an_overlay_that_ends_by_itself_is_reported_and_restarted_only_when_switched(
    capfd, monkeypatch
):
    stopping = threading.Event()
    monkeypatch.setattr(stopping, "wait", lambda _timeout: False)  # nothing else is stopping
    switch = config()
    overlay = launch.Overlay([sys.executable, "-c", "raise SystemExit(3)"], switch, stopping)
    overlay.follow()
    wait_for(lambda: switch.snapshot()["problem"])
    assert "the overlay stopped (exit status 3)" in capfd.readouterr().err
    assert overlay._process is None  # and not started again on its own
    switch.set_enabled(False)
    assert switch.snapshot()["problem"] == ""
    switch.set_enabled(True)
    assert overlay._process is not None  # switched off and on: tried again
    wait_for(lambda: switch.snapshot()["problem"])
    overlay.stop()  # nothing left to stop; must not raise


@pytest.fixture
def fake_serve(monkeypatch):
    """Stands in for the daemon: reports ready, then returns as if it had been stopped."""
    daemon = SimpleNamespace(
        stopping=threading.Event(), url="http://127.0.0.1:9100", overlay=config()
    )
    seen = SimpleNamespace(daemon=daemon, calls=0, overlays=[])

    def serve(settings, *, on_ready):
        seen.calls += 1
        on_ready(daemon)
        seen.overlays = [o._process for o in started]
        assert all(p.poll() is None for p in seen.overlays)
        return 0

    started: list[launch.Overlay] = []
    real_follow = launch.Overlay.follow

    def follow(self):
        started.append(self)
        real_follow(self)

    monkeypatch.setattr(launch, "serve", serve)
    monkeypatch.setattr(launch.Overlay, "follow", follow)
    monkeypatch.setattr(launch, "overlay_command", lambda argv: [*SLEEP, *argv])
    monkeypatch.setattr(launch, "is_listening", lambda _settings: False)

    real_start = launch.Shortcuts.start

    def start(self):
        real_start(self)
        seen.shortcuts.append(self._process)

    seen.shortcuts = []
    monkeypatch.setattr(launch, "child_command", lambda command, argv: [*SLEEP, command, *argv])
    monkeypatch.setattr(launch.Shortcuts, "start", start)
    return seen


def test_start_launches_the_overlay_and_the_page_and_stops_the_overlay_with_the_daemon(
    fake_serve,
):
    opened: list[str] = []
    status = launch.run(load_settings(), settings_argv=["--port", "9100"], open_url=opened.append)
    assert status == 0 and opened == ["http://127.0.0.1:9100/"]
    (process,) = fake_serve.overlays
    assert process.args[-2:] == ["--port", "9100"]
    assert process.poll() is not None, "the overlay was left running"
    (keys,) = fake_serve.shortcuts
    assert keys.args[-3:] == ["shortcuts", "--port", "9100"]
    assert keys.poll() is not None, "the shortcuts were left running"


def test_start_leaves_out_the_shortcuts_when_asked_or_when_none_are_set(fake_serve, monkeypatch):
    launch.run(load_settings(), overlay=False, shortcuts=False, open_page=False, open_url=print)
    monkeypatch.setenv("SPOTIFIXED_SHORTCUT_VOLUME_UP", "")
    monkeypatch.setenv("SPOTIFIXED_SHORTCUT_VOLUME_DOWN", "")
    monkeypatch.setenv("SPOTIFIXED_SHORTCUT_SKIP_DISABLE", "")
    launch.run(load_settings(), overlay=False, open_page=False, open_url=print)
    assert fake_serve.calls == 2 and fake_serve.shortcuts == []


def test_start_can_leave_out_the_overlay_and_the_page(fake_serve):
    opened: list[str] = []
    assert launch.run(load_settings(), overlay=False, open_page=False, open_url=opened.append) == 0
    assert fake_serve.calls == 1 and fake_serve.overlays == [] and opened == []
    assert fake_serve.daemon.overlay.snapshot()["managed"] is False  # not switchable either


def test_started_a_second_time_it_only_opens_the_page(fake_serve, monkeypatch):
    monkeypatch.setattr(launch, "is_listening", lambda _settings: True)
    settings = load_settings()
    opened: list[str] = []
    assert launch.run(settings, open_url=opened.append) == 0
    assert fake_serve.calls == 0 and opened == [f"{settings.base_url}/"]
