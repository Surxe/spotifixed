"""The overlay inside a real compositor: a headless KWin with no display.

What this can show without a graphical session: the surface is a layer-shell surface on
the overlay layer, sits where the settings put it, is mapped while a line is due and
unmapped otherwise, and (asking KWin itself) lands on KWin's overlay layer at the
expected geometry. What it cannot show is how it looks above a real fullscreen game;
that needs a person.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from spotifixed.config import REPO_ROOT, load_settings
from spotifixed.server.app import Daemon
from tests.integration.test_serve_process import serve  # noqa: F401  (a fixture)
from tests.support.library import PLAYLIST_ID, build_library
from tests.support.playback import FakePlayer

KWIN = REPO_ROOT / "scripts" / "headless-kwin.sh"
PROBE = REPO_ROOT / "tests" / "support" / "overlay_probe.py"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
SCREEN = (1920, 1080)
KWIN_OVERLAY_LAYER = 9
KWIN_ACTIVE_LAYER = 5  # where KWin puts the active fullscreen window


def _layer_shell_typelib() -> bool:
    code = (
        "import gi; gi.require_version('Gtk4LayerShell', '1.0'); gi.require_version('Gtk', '4.0')"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.wayland,
    pytest.mark.skipif(
        shutil.which("kwin_wayland") is None, reason="kwin_wayland is not installed"
    ),
    pytest.mark.skipif(not RUNTIME.is_dir(), reason="no XDG runtime directory for this user"),
    pytest.mark.skipif(not _layer_shell_typelib(), reason="GTK4 layer-shell bindings missing"),
]


# A second compositor with two screens side by side, for moving between monitors.
TWO_SCREENS = {
    "SPOTIFIXED_TEST_WAYLAND": "wl-pp-test-two",
    "SPOTIFIXED_TEST_OUTPUTS": "2",
}


def kwin(*args: str, extra_env=None, **kwargs) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "XDG_RUNTIME_DIR": str(RUNTIME), **(extra_env or {})}
    return subprocess.run(
        ["bash", str(KWIN), *args], capture_output=True, text=True, env=env, timeout=60, **kwargs
    )


@pytest.fixture(scope="module")
def compositor():
    already = kwin("status").returncode == 0
    started = kwin("start")
    if started.returncode != 0:
        pytest.skip(f"the headless compositor did not start: {started.stderr.strip()[-200:]}")
    yield
    if not already:
        kwin("stop")


@pytest.fixture(scope="module")
def two_screens():
    already = kwin("status", extra_env=TWO_SCREENS).returncode == 0
    started = kwin("start", extra_env=TWO_SCREENS)
    if started.returncode != 0:
        pytest.skip(f"the two-screen compositor did not start: {started.stderr.strip()[-200:]}")
    yield
    if not already:
        kwin("stop", extra_env=TWO_SCREENS)


def child_env(tmp_path, **settings: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPOTIFIXED_")}
    env.update(XDG_RUNTIME_DIR=str(RUNTIME), PYTHONPATH=str(REPO_ROOT))
    env.pop("APIFY_TOKEN", None)
    env.update({f"SPOTIFIXED_{name}": value for name, value in settings.items()})
    return env


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", header[16:24])


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


# --------------------------------- snapshot --------------------------------- #


def snapshot(tmp_path, name: str, **settings: str) -> tuple[int, int]:
    out = tmp_path / name
    result = subprocess.run(
        ["bash", str(KWIN), "run", sys.executable, "-m", "spotifixed", "overlay",
         "--demo", "--snapshot", str(out)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
        env=child_env(tmp_path, **settings),
    )  # fmt: skip
    assert result.returncode == 0, result.stderr[-500:]
    assert f"wrote {out}" in result.stdout
    return png_size(out)


def test_snapshot_renders_one_row_within_the_width_limit(compositor, tmp_path):
    width, height = snapshot(tmp_path, "one.png")
    assert 600 < width <= 1200 + 40 and 60 < height < 120  # a single row of 32 pt text


def test_a_narrow_limit_wraps_and_two_line_mode_adds_a_row(compositor, tmp_path):
    width, height = snapshot(tmp_path, "narrow.png", OVERLAY_MAX_WIDTH="500")
    assert width <= 500 + 40 and height > 120
    two_width, two_height = snapshot(tmp_path, "two.png", OVERLAY_LINES="2")
    one_width, one_height = snapshot(tmp_path, "one.png")
    assert two_height > one_height + 30


def test_font_size_and_colours_are_applied_without_error(compositor, tmp_path):
    small = snapshot(tmp_path, "small.png", OVERLAY_FONT_SIZE="16", OVERLAY_COLOR="#FFE066",
                     OVERLAY_OUTLINE_COLOR="#102030", OVERLAY_FONT="Serif Italic")  # fmt: skip
    big = snapshot(tmp_path, "big.png", OVERLAY_FONT_SIZE="40")
    assert small[1] < big[1] and small[0] < big[0]


def test_bad_settings_and_no_display_are_one_clear_line(compositor, tmp_path):
    def run(env):
        command = [sys.executable, "-m", "spotifixed", "overlay", "--snapshot"]
        return subprocess.run(
            [*command, str(tmp_path / "x.png")],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmp_path,
            env=env,
        )

    bad_colour = child_env(tmp_path, OVERLAY_COLOR="red; } * { color: blue")
    bad_colour.update(WAYLAND_DISPLAY="wl-pp-test", GDK_BACKEND="wayland")
    result = run(bad_colour)
    assert result.returncode == 1 and "OVERLAY_COLOR must be a colour" in result.stderr
    assert "Traceback" not in result.stderr

    nowhere = child_env(tmp_path)
    nowhere.update(WAYLAND_DISPLAY="wl-there-is-no-such-display", GDK_BACKEND="wayland")
    nowhere.pop("DISPLAY", None)
    result = run(nowhere)
    assert result.returncode == 1 and "no display" in result.stderr
    assert "Traceback" not in result.stderr and not (tmp_path / "x.png").exists()


# ------------------------------ live behaviour ------------------------------ #


def test_the_overlay_follows_playback_on_the_overlay_layer(
    compositor, store, tmp_path, monkeypatch
):
    monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
    monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
    build_library(store)
    player = FakePlayer()
    daemon = Daemon(load_settings(), store, player, port=0, keepalive_s=0.5)
    threading.Thread(target=daemon.serve_forever, daemon=True).start()

    out = tmp_path / "probe.json"
    probe = subprocess.Popen(
        ["bash", str(KWIN), "run", sys.executable, str(PROBE), str(out), "10"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=child_env(
            tmp_path, PORT=str(daemon.port), OVERLAY_ANCHOR="bottom-right",
            OVERLAY_MARGIN_X="40", OVERLAY_MARGIN_Y="90", OVERLAY_MONITOR="Virtual-0",
            OVERLAY_LEAD_MS="150", OVERLAY_FADE_MS="0",
        ),
    )  # fmt: skip
    marks: dict[str, float] = {}
    try:
        wait_until(lambda: daemon.open_streams == 1, timeout=15)
        time.sleep(0.3)  # idle: samples that must show nothing

        # The synthetic lyrics: a line at 1.0 s, a gap at 2.5 s, a line at 4.0 s.
        player.load_queue(daemon.routes._library.queue_for(PLAYLIST_ID))
        base = player.bus.current.captured_at_epoch_ms / 1000  # position 0 at this instant
        marks["base"] = base

        time.sleep(max(0, base + 1.6 - time.time()))
        windows = json.loads(kwin("windows", check=True).stdout)

        time.sleep(max(0, base + 4.5 - time.time()))
        marks["pause"] = time.time()
        player.pause()
        time.sleep(0.5)
        player.position = 4.2
        marks["resume"] = time.time()
        player.play()
        time.sleep(0.5)
        marks["drop"] = time.time()
        daemon.shutdown()
        time.sleep(0.6)
    finally:
        out.with_suffix(".stop").touch()
        try:
            _, stderr = probe.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            probe.kill()
            _, stderr = probe.communicate()
        if not daemon.stopping.is_set():
            daemon.shutdown()
    assert probe.returncode == 0, stderr[-800:]

    recorded = json.loads(out.read_text())
    samples = recorded["samples"]
    assert recorded["monitors"] == ["Virtual-0"]
    base = marks["base"]

    def between(start: float, end: float) -> list[dict]:
        return [s for s in samples if start <= s["t"] <= end]

    def first(predicate, after: float) -> dict:
        return next(s for s in samples if s["t"] >= after and predicate(s))

    # What the surface is, from inside the client.
    shown = first(lambda s: s["mapped"], base)
    assert shown["is_layer_window"] is True
    assert (shown["layer"], shown["namespace"]) == ("overlay", "spotifixed-lyrics")
    assert (shown["keyboard_mode"], shown["exclusive_zone"]) == ("none", -1)
    assert shown["anchors"] == ["bottom", "right"]
    assert shown["margins"] == {"top": 0, "bottom": 90, "left": 0, "right": 40}
    assert shown["monitor"] == "Virtual-0"

    # Hidden while idle and before the first line; the line appears at 1.0 s less the lead.
    assert not any(s["mapped"] for s in samples if s["t"] < base + 0.75)
    assert base + 0.80 <= shown["t"] <= base + 1.10, shown["t"] - base
    assert shown["text"] == "an invented first line"
    assert all(s["mapped"] and s["text"] == "an invented first line"
               for s in between(base + 1.15, base + 2.25))  # fmt: skip
    # The gap line: unmapped, not an empty window.
    assert between(base + 2.50, base + 3.75) and not any(
        s["mapped"] for s in between(base + 2.50, base + 3.75)
    )
    assert all(s["mapped"] and s["text"] == "an invented last line"
               for s in between(base + 4.05, marks["pause"] - 0.02))  # fmt: skip

    # Pause hides it at once; resume brings it back; losing the daemon hides it.
    hidden = first(lambda s: not s["mapped"], marks["pause"])
    assert hidden["t"] - marks["pause"] < 0.15, hidden["t"] - marks["pause"]
    assert not any(s["mapped"] for s in between(hidden["t"], marks["resume"]))
    back = first(lambda s: s["mapped"], marks["resume"])
    assert back["t"] - marks["resume"] < 0.25 and back["text"] == "an invented last line"
    gone = first(lambda s: not s["mapped"], marks["drop"])
    assert gone["t"] - marks["drop"] < 0.8
    assert not any(s["mapped"] for s in samples if s["t"] > gone["t"])

    # And from outside: KWin's own account of its window stack, taken at 1.6 s.
    ours = [w for w in windows if w["layer"] == KWIN_OVERLAY_LAYER]
    assert len(ours) == 1, windows
    assert KWIN_OVERLAY_LAYER > KWIN_ACTIVE_LAYER
    x, y, width, height = ours[0]["geometry"]
    assert (x + width, y + height) == (SCREEN[0] - 40, SCREEN[1] - 90)
    # The size the client had been given by the time KWin was asked (the very first
    # mapped sample is taken before the first allocation).
    settled = first(lambda s: s["mapped"], base + 1.5)
    assert (width, height) == (settled["width"], settled["height"])


def test_settings_changed_through_the_daemon_move_and_restyle_the_running_overlay(
    compositor, store, tmp_path, monkeypatch
):
    """The remote-control page's path: nothing playing, a preview on, settings changing.

    The window must move and resize where it stands. If it were unmapped and mapped
    again for each change it would flicker under the user's hand while they position it.
    """
    monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
    monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
    build_library(store)
    daemon = Daemon(load_settings(), store, FakePlayer(), port=0, keepalive_s=0.5)
    threading.Thread(target=daemon.serve_forever, daemon=True).start()

    out = tmp_path / "probe.json"
    probe = subprocess.Popen(
        ["bash", str(KWIN), "run", sys.executable, str(PROBE), str(out), "10"],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=child_env(
            tmp_path, PORT=str(daemon.port), OVERLAY_ANCHOR="bottom-right",
            OVERLAY_MARGIN_X="40", OVERLAY_MARGIN_Y="90", OVERLAY_MONITOR="Virtual-0",
            OVERLAY_FADE_MS="0",
        ),
    )  # fmt: skip
    marks: dict[str, float] = {}
    stacks: dict[str, list[dict]] = {}

    def step(name: str, change) -> None:
        marks[name] = time.time()
        change()
        time.sleep(0.6)
        stacks[name] = json.loads(kwin("windows", check=True).stdout)

    try:
        wait_until(lambda: daemon.overlay.snapshot()["clients"] == 1, timeout=15)
        time.sleep(0.3)  # idle: samples that must show nothing
        step("preview", lambda: daemon.overlay.preview(30))
        step("corner", lambda: daemon.overlay.update(
            {"anchor": "top-left", "margin_x": 60, "margin_y": 30, "font_size": 16}))  # fmt: skip
        step("centre", lambda: daemon.overlay.update(
            {"anchor": "center", "margin_x": 0, "margin_y": 200, "lines": 2}))  # fmt: skip
        step("reset", daemon.overlay.reset)
        marks["off"] = time.time()
        daemon.overlay.preview(0)
        time.sleep(0.4)
    finally:
        out.with_suffix(".stop").touch()
        try:
            _, stderr = probe.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            probe.kill()
            _, stderr = probe.communicate()
        daemon.shutdown()
    assert probe.returncode == 0, stderr[-800:]

    samples = json.loads(out.read_text())["samples"]

    def at(name: str) -> dict:
        """The last sample before the step after ``name`` began."""
        order = list(marks)
        end = marks[order[order.index(name) + 1]]
        return [s for s in samples if s["t"] < end][-1]

    def geometry(name: str) -> tuple[int, int, int, int]:
        ours = [w for w in stacks[name] if w["layer"] == KWIN_OVERLAY_LAYER]
        assert len(ours) == 1, stacks[name]
        return tuple(ours[0]["geometry"])

    # Nothing before the preview; the sample line from then until it is switched off,
    # without a single unmapped sample in between, through three reconfigurations.
    assert not any(s["mapped"] for s in samples if s["t"] < marks["preview"])
    shown = [s for s in samples if marks["preview"] + 0.25 <= s["t"] <= marks["off"]]
    assert shown and all(s["mapped"] for s in shown)
    assert {s["text"] for s in shown} == {"This is where the current lyric line will appear"}
    hidden = next(s for s in samples if s["t"] >= marks["off"] and not s["mapped"])
    assert hidden["t"] - marks["off"] < 0.15

    # As started: bottom-right, by the configured margins.
    x, y, width, height = geometry("preview")
    assert (x + width, y + height) == (SCREEN[0] - 40, SCREEN[1] - 90)
    assert at("preview")["anchors"] == ["bottom", "right"]

    # Moved to the opposite corner and made smaller, in place.
    cx, cy, cwidth, cheight = geometry("corner")
    assert (cx, cy) == (60, 30)
    assert cwidth < width and cheight < height
    assert at("corner")["anchors"] == ["left", "top"]
    assert at("corner")["margins"] == {"top": 30, "bottom": 0, "left": 60, "right": 0}

    # Centred, pushed 200 px down, with the second line showing.
    mx, my, mwidth, mheight = geometry("centre")
    assert at("centre")["anchors"] == []
    assert abs((mx + mwidth / 2) - SCREEN[0] / 2) <= 1
    assert abs((my + mheight / 2) - SCREEN[1] / 2) <= 1
    assert mheight > cheight + 400  # twice the shift is padded above the text
    assert at("centre")["next_text"] == "and this is the line that follows it"

    # Reset: exactly where and how it started.
    assert geometry("reset") == (x, y, width, height)
    assert at("reset")["next_text"] == ""


class LiveOverlay:
    """A daemon with a fake player and a real overlay following it, sampled every 10 ms."""

    def __init__(self, store, tmp_path, monkeypatch, *, extra_env=None, **settings: str):
        monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
        monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
        build_library(store)
        self.extra_env = extra_env or {}
        self.player = FakePlayer()
        self.daemon = Daemon(load_settings(), store, self.player, port=0, keepalive_s=0.5)
        self.overlay = self.daemon.overlay
        self._tmp_path, self._settings = tmp_path, settings
        self._out = tmp_path / "probe.json"
        self._probe = None
        self.stderr = ""
        self.samples: list[dict] = []
        threading.Thread(target=self.daemon.serve_forever, daemon=True).start()

    def start(self) -> None:
        env = child_env(
            self._tmp_path, PORT=str(self.daemon.port), OVERLAY_FADE_MS="0", **self._settings
        )
        env.update(self.extra_env)
        self._probe = subprocess.Popen(
            ["bash", str(KWIN), "run", sys.executable, str(PROBE), str(self._out), "10"],
            cwd=self._tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env,
        )  # fmt: skip
        wait_until(lambda: self.overlay.snapshot()["clients"] == 1, timeout=15)

    def play(self) -> float:
        """Start the synthetic track. Returns the instant playback position was 0."""
        self.player.load_queue(self.daemon.routes._library.queue_for(PLAYLIST_ID))
        return self.player.bus.current.captured_at_epoch_ms / 1000

    def window(self) -> tuple[int, int, int, int] | None:
        """Where KWin says our surface is, or ``None`` when it has none on screen."""
        stack = json.loads(kwin("windows", check=True, extra_env=self.extra_env).stdout)
        ours = [w for w in stack if w["layer"] == KWIN_OVERLAY_LAYER]
        assert len(ours) <= 1, stack
        return tuple(ours[0]["geometry"]) if ours else None

    def finish(self) -> list[dict]:
        self._out.with_suffix(".stop").touch()
        try:
            _, self.stderr = self._probe.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self._probe.kill()
            _, self.stderr = self._probe.communicate()
        self.daemon.shutdown()
        assert self._probe.returncode == 0, self.stderr[-800:]
        self.samples = json.loads(self._out.read_text())["samples"]
        return self.samples

    def close(self) -> None:
        if self._probe is not None and self._probe.poll() is None:
            self._out.with_suffix(".stop").touch()
            try:
                self._probe.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._probe.kill()
        if not self.daemon.stopping.is_set():
            self.daemon.shutdown()


@pytest.fixture
def live(compositor, store, tmp_path, monkeypatch):
    made: list[LiveOverlay] = []

    def make(**settings: str) -> LiveOverlay:
        made.append(LiveOverlay(store, tmp_path, monkeypatch, **settings))
        return made[-1]

    yield make
    for overlay in made:
        overlay.close()


def sleep_until(instant: float) -> None:
    time.sleep(max(0.0, instant - time.time()))


def test_lines_keep_appearing_after_settings_change_during_a_song(live):
    """Settings changed while a song plays, with its lines coming and going as they do.

    The line that is up when a setting changes must stay up, and every later line must
    still appear: once a change had been applied, the next line after a gap or a pause
    used never to come back.
    """
    overlay = live(OVERLAY_ANCHOR="bottom", OVERLAY_MARGIN_Y="120")
    overlay.start()
    # The synthetic lyrics: a line at 1.0 s, a gap at 2.5 s, a line at 4.0 s.
    base = overlay.play()

    sleep_until(base + 1.5)
    first_before = overlay.window()
    overlay.overlay.update({"font_size": 45})  # while the first line is showing
    sleep_until(base + 2.2)
    first_after = overlay.window()

    sleep_until(base + 3.0)
    assert overlay.window() is None  # the gap
    overlay.overlay.update({"margin_y": 300})  # while nothing is showing

    sleep_until(base + 4.6)
    second = overlay.window()  # the line after both changes

    overlay.player.pause()
    time.sleep(0.4)
    assert overlay.window() is None
    overlay.overlay.update({"anchor": "top-left", "margin_x": 60, "margin_y": 30})
    time.sleep(0.2)
    overlay.player.position = 4.2
    resumed_at = time.time()
    overlay.player.play()
    time.sleep(0.6)
    after_pause = overlay.window()

    overlay.overlay.reset()  # and back, with the line up
    time.sleep(0.6)
    restored = overlay.window()
    samples = overlay.finish()

    assert first_before is not None and first_after is not None
    assert first_after[2] > first_before[2] and first_after[3] > first_before[3]  # bigger text
    assert first_after[1] + first_after[3] == SCREEN[1] - 120
    showing = [s for s in samples if base + 1.15 <= s["t"] <= base + 2.25]
    assert showing and all(s["mapped"] for s in showing)  # no flicker when it changed

    assert second is not None, "the line after a settings change never appeared"
    assert second[1] + second[3] == SCREEN[1] - 300
    assert second[3] == first_after[3]  # still the larger font

    assert after_pause is not None, "the line did not come back after a change made while paused"
    assert after_pause[:2] == (60, 30)
    back = next(s for s in samples if s["t"] >= resumed_at and s["mapped"])
    assert back["t"] - resumed_at < 0.25 and back["text"] == "an invented last line"

    assert restored is not None and restored[3] == first_before[3]  # 32 pt again
    assert restored[1] + restored[3] == SCREEN[1] - 120
    assert "Traceback" not in overlay.stderr and "WARNING" not in overlay.stderr


def test_the_underline_moves_through_the_line_without_resizing_or_fading_it(live):
    """The first line is up from 1.0 s to 2.5 s; its words end 250, 1000, 1250, 1500 ms in."""
    overlay = live(OVERLAY_UNDERLINE_WORD="true", OVERLAY_LEAD_MS="0")
    overlay.start()
    base = overlay.play()
    sleep_until(base + 3.2)
    samples = overlay.finish()

    line = [s for s in samples if s["mapped"] and s["text"] == "an invented first line"]
    order = [s["underlined"] for s in line]
    assert [w for i, w in enumerate(order) if i == 0 or w != order[i - 1]] == [
        "an", "invented", "first", "line",
    ]  # fmt: skip
    for word, middle in (("an", 1.125), ("invented", 1.625), ("first", 2.125), ("line", 2.375)):
        at = next(s for s in line if s["t"] >= base + middle)
        assert at["underlined"] == word, (word, at)
    assert len({(s["width"], s["height"]) for s in line[3:]}) == 1  # one size throughout
    assert "Traceback" not in overlay.stderr


def test_many_changes_shown_and_hidden_never_lose_the_window(live):
    """Every kind of setting, changed both while a line is up and while none is."""
    overlay = live()
    overlay.start()
    changes = [
        {"font_size": 20}, {"max_width": 400}, {"anchor": "top"}, {"margin_y": 250},
        {"lines": 2}, {"font": "Serif Italic"}, {"color": "#ffe066"}, {"anchor": "center"},
        {"margin_x": -200}, {"font_size": 60, "max_width": 1500}, {"monitor": "Virtual-0"},
        {"monitor": ""}, {"anchor": "bottom-left", "margin_x": 10, "margin_y": 10},
    ]  # fmt: skip
    seen = []
    for index, change in enumerate(changes):
        shown_first = index % 2 == 0
        if shown_first:  # change it with the sample line up, then hide and show again
            overlay.overlay.preview(30)
            time.sleep(0.25)
        overlay.overlay.update(change)
        time.sleep(0.2)
        overlay.overlay.preview(0)
        time.sleep(0.2)
        hidden = overlay.window()
        overlay.overlay.preview(30)
        time.sleep(0.35)
        seen.append((change, hidden, overlay.window()))
        overlay.overlay.preview(0)
        time.sleep(0.15)
    overlay.overlay.reset()
    overlay.overlay.preview(30)
    time.sleep(0.4)
    final = overlay.window()
    samples = overlay.finish()

    for change, hidden, shown in seen:
        assert hidden is None, change
        assert shown is not None, f"the overlay did not come back after {change}"
        assert shown[2] > 40 and shown[3] > 20, (change, shown)
    by_change = {json.dumps(change): shown for change, _hidden, shown in seen}
    assert by_change['{"max_width": 400}'][2] <= 440
    assert by_change['{"anchor": "bottom-left", "margin_x": 10, "margin_y": 10}'][0] == 10
    # Back to how it started: centred along the bottom, 120 px up, one row of 32 pt text.
    x, y, width, height = final
    assert abs((x + width / 2) - SCREEN[0] / 2) <= 1 and y + height == SCREEN[1] - 120
    assert 60 < height < 120
    assert samples[-1]["mapped"] and "Traceback" not in overlay.stderr
    assert "WARNING" not in overlay.stderr


def test_an_overlay_started_with_stored_overrides_shows_its_first_line(live, store):
    """The settings arrive before anything has ever been shown; the first line must show."""
    overlay = live(OVERLAY_ANCHOR="bottom", OVERLAY_MARGIN_Y="120")
    overlay.overlay.update({"font_size": 45, "anchor": "top-right", "margin_x": 70, "margin_y": 40})
    overlay.start()
    base = overlay.play()
    sleep_until(base + 1.6)
    first = overlay.window()
    sleep_until(base + 4.6)
    second = overlay.window()
    samples = overlay.finish()
    assert first is not None and second is not None
    assert (first[0] + first[2], first[1]) == (SCREEN[0] - 70, 40)
    assert first[3] > 100  # 45 pt, not the 32 pt it was started with
    assert not any(s["mapped"] for s in samples if base + 2.6 <= s["t"] <= base + 3.7)


def test_the_overlay_moves_between_monitors_and_can_be_handed_back(
    two_screens, store, tmp_path, monkeypatch
):
    overlay = LiveOverlay(store, tmp_path, monkeypatch, extra_env=TWO_SCREENS)
    try:
        overlay.start()
        # The page lists what the overlay can see.
        wait_until(lambda: len(overlay.overlay.snapshot()["monitors"]) == 2)
        reported = overlay.overlay.snapshot()["monitors"]
        overlay.overlay.preview(30)
        time.sleep(0.5)
        chosen = overlay.window()
        overlay.overlay.update({"monitor": "Virtual-1"})
        time.sleep(0.5)
        second = overlay.window()
        overlay.overlay.update({"monitor": "Virtual-0"})
        time.sleep(0.5)
        first = overlay.window()
        overlay.overlay.update({"monitor": "Virtual-1"})
        time.sleep(0.4)
        overlay.overlay.reset()  # no monitor named: the compositor chooses again
        time.sleep(0.5)
        released = overlay.window()
        overlay.overlay.preview(0)  # and it still hides and shows after all that
        time.sleep(0.3)
        hidden = overlay.window()
        overlay.overlay.preview(30)
        time.sleep(0.4)
        again = overlay.window()
        overlay.finish()
    finally:
        overlay.close()
    assert [monitor["connector"] for monitor in reported] == ["Virtual-0", "Virtual-1"]
    assert chosen is not None and 0 <= chosen[0] < SCREEN[0]
    assert second is not None and SCREEN[0] <= second[0] < 2 * SCREEN[0]
    assert (second[0] - SCREEN[0], *second[1:]) == chosen  # the same place, one screen over
    assert first == chosen and released == chosen
    assert hidden is None and again == chosen
    assert "Traceback" not in overlay.stderr and "WARNING" not in overlay.stderr


# ------------------------------- start command ------------------------------- #


@pytest.mark.mpv
@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv is not installed")
@pytest.mark.parametrize("stop", ["closing the terminal", "killing the daemon"])
def test_start_runs_the_overlay_beside_the_daemon_and_takes_it_down_too(
    compositor,
    serve,  # noqa: F811
    stop,
):
    """One command is the whole application, and stopping it leaves nothing behind."""
    run = serve(
        "start", "--no-browser", "--port", str(serve.port),
        # By path: the daemon under test gets a runtime directory of its own for mpv.
        WAYLAND_DISPLAY=str(RUNTIME / "wl-pp-test"), GDK_BACKEND="wayland",
    )  # fmt: skip
    wait_until(lambda: run.request("GET", "/overlay")["clients"] == 1, timeout=15)
    (overlay,) = run.overlay_pids()
    assert len(run.mpv_pids()) == 1

    if stop == "closing the terminal":
        assert run.signal_group(signal.SIGHUP) == 0
    else:
        assert run.signal_daemon(signal.SIGTERM) == 0
    assert run.overlay_pids() == [], f"overlay {overlay} outlived the daemon"
    assert run.mpv_pids() == []
    assert "the overlay stopped" not in run.output and "Traceback" not in run.output
