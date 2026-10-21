"""The ``shortcuts`` command against a stand-in portal on a private session bus.

The portal is ``tests/support/fake_portal.py`` in its own process; the daemon is a
loopback fake that records the requests a press turns into. Nothing here reaches the
real desktop: the private bus has only the two processes on it.
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import time
from importlib.util import find_spec
from pathlib import Path

import pytest

from spotifixed.config import REPO_ROOT

FAKE_PORTAL = Path(__file__).resolve().parent.parent / "support" / "fake_portal.py"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("dbus-daemon") is None, reason="needs dbus-daemon for a private session bus"
    ),
    # Looked up, not imported: gi loaded into the test process would break the overlay's
    # rule that the layer-shell library is loaded before it.
    pytest.mark.skipif(find_spec("gi") is None, reason="needs the system GObject bindings"),
]


@pytest.fixture
def session_bus(monkeypatch):
    bus = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        address = bus.stdout.readline().strip()
        assert address, "the private session bus did not start"
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
        yield address
    finally:
        bus.terminate()
        bus.wait(timeout=5)


@pytest.fixture
def portal(session_bus):
    process = subprocess.Popen(
        [sys.executable, "-I", str(FAKE_PORTAL)], stdout=subprocess.PIPE, text=True
    )
    try:
        for line in process.stdout:
            if line.strip() == "ready":
                break
        else:
            pytest.fail("the stand-in portal did not start")
        yield process
    finally:
        process.terminate()
        process.wait(timeout=5)


def shortcuts_process(port: int, *flags: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "spotifixed", "shortcuts", "--port", str(port), *flags],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


def wait_for(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("timed out")
        time.sleep(0.05)


def test_each_press_becomes_one_request_and_others_are_ignored(portal, fake_server):
    portal_process = portal
    fake_server.route("POST", "/api/v1/player/volume", (200, {"ok": True}))
    fake_server.route("POST", "/api/v1/player/disable", (200, {"ok": True}))
    process = shortcuts_process(port_of(fake_server.url), "--shortcut-volume-step", "7")
    try:
        wait_for(lambda: len([r for r in fake_server.requests if r.method == "POST"]) >= 3)
        time.sleep(0.5)  # the presses it must ignore come after; give them time to arrive
        sent = [(r.path, r.json()) for r in fake_server.requests if r.method == "POST"]
        assert sent == [
            ("/api/v1/player/volume", {"delta": 7}),
            ("/api/v1/player/volume", {"delta": -7}),
            ("/api/v1/player/disable", {}),
        ]
    finally:
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=5)
    assert process.returncode == 0, err
    assert "Keyboard shortcuts: Raise spotifixed's volume: CTRL+XF86AudioRaiseVolume" in out

    portal_process.terminate()
    received = [json.loads(line) for line in portal_process.stdout if line.startswith("{")]
    bound = next(call for call in received if call["method"] == "BindShortcuts")
    assert [(sid, info["preferred_trigger"]) for sid, info in bound["args"][1]] == [
        ("volume-up", "CTRL+XF86AudioRaiseVolume"),
        ("volume-down", "CTRL+XF86AudioLowerVolume"),
        ("skip-disable", "LOGO+XF86AudioNext"),
    ]


@pytest.mark.usefixtures("portal")
def test_an_empty_trigger_leaves_that_shortcut_out(fake_server):
    fake_server.route("POST", "/api/v1/player/volume", (200, {"ok": True}))
    process = shortcuts_process(
        port_of(fake_server.url), "--shortcut-volume-up", "", "--shortcut-skip-disable", ""
    )
    try:
        wait_for(lambda: any(r.method == "POST" for r in fake_server.requests))
        time.sleep(0.5)
        assert [r.json() for r in fake_server.requests] == [{"delta": -5}]
    finally:
        process.send_signal(signal.SIGTERM)
        out, _err = process.communicate(timeout=5)
    assert "Raise" not in out and "Lower spotifixed's volume" in out


def test_without_a_portal_it_says_the_desktop_cannot_hold_shortcuts(session_bus, fake_server):
    process = shortcuts_process(port_of(fake_server.url))
    _out, err = process.communicate(timeout=40)
    assert process.returncode == 1
    assert "this desktop does not offer global shortcuts" in err
    assert fake_server.requests == []


def test_with_no_shortcut_set_it_binds_nothing_and_ends(session_bus, fake_server):
    process = shortcuts_process(
        port_of(fake_server.url),
        "--shortcut-volume-up", "", "--shortcut-volume-down", "", "--shortcut-skip-disable", "",
    )  # fmt: skip
    out, _err = process.communicate(timeout=10)
    assert process.returncode == 0
    assert "no keyboard shortcuts are set" in out


def test_a_second_run_finds_its_shortcuts_bound_and_does_not_bind_again(portal, fake_server):
    """Binding shows the desktop's dialog, so only the first run may bind."""
    fake_server.route("POST", "/api/v1/player/volume", (200, {"ok": True}))
    fake_server.route("POST", "/api/v1/player/disable", (200, {"ok": True}))
    for run in (1, 2):
        process = shortcuts_process(port_of(fake_server.url))
        try:
            wait_for(lambda run=run: len(fake_server.requests) >= 3 * run)
        finally:
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=5)
        assert process.returncode == 0, err
        assert "Raise spotifixed's volume: CTRL+XF86AudioRaiseVolume" in out
    assert [r.json() for r in fake_server.requests] == [{"delta": 5}, {"delta": -5}, {}] * 2

    portal.terminate()
    methods = [json.loads(line)["method"] for line in portal.stdout if line.startswith("{")]
    assert methods == [
        "CreateSession", "ListShortcuts", "BindShortcuts", "CreateSession", "ListShortcuts",
    ]  # fmt: skip
