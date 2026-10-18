"""The overlay settings panel in a real browser, against a daemon with invented data.

Headless Chrome loads the page and is driven over the DevTools protocol (see
``tests/js/drive_page.mjs``): it clicks, drags and types as a person would, and the
assertions are on what the daemon then holds, since that is what a running overlay
follows. Nothing here needs a display.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from spotifixed.cache import assets
from spotifixed.config import REPO_ROOT, load_settings
from spotifixed.server.app import Daemon
from tests.support.library import PLAYLIST_ID, SYNCED, build_library
from tests.support.playback import FakePlayer

DRIVER = REPO_ROOT / "tests" / "js" / "drive_page.mjs"
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
NODE = shutil.which("node")


def _node_has_websocket() -> bool:
    if NODE is None:
        return False
    check = subprocess.run(
        [NODE, "-e", "process.exit(typeof WebSocket === 'function' ? 0 : 1)"],
        capture_output=True,
    )
    return check.returncode == 0


pytestmark = [
    pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium to load the page in"),
    pytest.mark.skipif(not _node_has_websocket(), reason="node 22 or newer is needed"),
]


@pytest.fixture
def daemon(store, monkeypatch):
    monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
    monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
    monkeypatch.setenv("SPOTIFIXED_OVERLAY_MARGIN_Y", "120")
    build_library(store)
    daemon = Daemon(load_settings(), store, FakePlayer(), port=0, keepalive_s=0.5)
    threading.Thread(target=daemon.serve_forever, daemon=True).start()
    yield daemon
    daemon.shutdown()


def drive(daemon: Daemon, tmp_path: Path, steps: list[dict]) -> list:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPOTIFIXED_")}
    env.update(CHROME=CHROME, HOME=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
    result = subprocess.run(
        [NODE, str(DRIVER), f"{daemon.url}/", str(tmp_path / "chrome-profile")],
        input=json.dumps(steps),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    return json.loads(result.stdout)


# Helpers installed in the page: act on a control the way a browser reports a person
# doing it, and read back what the panel shows.
HELPERS = """
window.t = {
  el: (id) => document.getElementById(id),
  field: (key) => document.getElementById('overlay-field-' + key),
  row: (key) => document.getElementById('overlay-field-' + key).closest('.row'),
  beside: (label) => document.querySelector(`#overlay-form input[aria-label="${label}"]`),
  text: (id) => document.getElementById(id).textContent,
  values: (...keys) => keys.map((key) => document.getElementById('overlay-field-' + key).value),
  overridden: (...keys) => keys.map((key) => t.row(key).classList.contains('overridden')),
  type: (element, value) => {
    element.focus();
    element.value = value;
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.blur();
  },
  drag: (element, value) => {
    element.value = value;
    element.dispatchEvent(new Event('input', { bubbles: true }));
  },
  pause: (ms) => new Promise((done) => setTimeout(done, ms)),
};
"""
OPEN = [
    {"wait": "document.getElementById('playlists').options.length"},
    {"eval": HELPERS},
    {"eval": "t.el('settings-open').click()"},
    {"wait": "document.querySelectorAll('#overlay-form .row').length === 14"},
]


def test_the_panel_shows_the_settings_and_changes_reach_the_daemon(daemon, tmp_path, store):
    shot = tmp_path / "panel.png"
    steps = [
        {"eval": "document.getElementById('settings-pane').hidden"},
        *OPEN,
        {"eval": "[t.values('margin_y')[0], t.text('overlay-status')]"},
        {"eval": "t.el('overlay-reset').disabled"},
        # A number box, a select, a text box, then a slider and a colour picker.
        {"eval": "t.type(t.field('margin_y'), '260')"},
        {"eval": "t.type(t.field('anchor'), 'top-right')"},
        {"eval": "t.type(t.field('font'), 'Serif Italic')"},
        {"eval": "t.drag(t.beside('Font size'), '48')"},
        {"eval": "t.drag(t.beside('Text colour'), '#ffe066')"},
        {"wait": "t.overridden('color')[0]"},  # the last change has come back from the daemon
        {"eval": "document.getElementById('settings-pane').textContent"},
        {"eval": "t.values('font_size', 'color')"},  # the slider's and the picker's partners
        {"eval": "t.overridden('margin_y', 'margin_x', 'font')"},
        {"eval": "t.el('overlay-reset').disabled"},
        {"screenshot": str(shot)},
    ]  # fmt: skip
    results = drive(daemon, tmp_path, steps)
    assert results[0] is True
    assert results[len(OPEN) + 1 : len(OPEN) + 3] == [["120", "overlay not running"], True]

    panel_text, partners, overridden, reset_disabled, _shot = results[-5:]
    # The panel says changes need no saving, and offers nothing that suggests they do.
    said = " ".join(panel_text.split())
    assert "Changes are saved automatically." in said and "take effect immediately" in said
    assert "Copy" not in panel_text and "SPOTIFIXED_" not in panel_text
    assert partners == ["48", "#ffe066"]
    assert overridden == [True, False, True] and reset_disabled is False
    assert shot.stat().st_size > 10_000

    held = daemon.overlay.snapshot()
    assert held["overrides"] == {
        "anchor": "top-right", "margin_y": 260, "font": "Serif Italic", "font_size": 48,
        "color": "#ffe066",
    }  # fmt: skip
    assert json.loads(store.overlay_json.read_text())["overrides"] == held["overrides"]


def test_dropping_one_override_resetting_all_and_a_refused_value(daemon, tmp_path):
    daemon.overlay.update({"margin_x": 75, "lines": 2, "fade_ms": 0})
    steps = [
        *OPEN,
        {"eval": "t.values('margin_x', 'lines', 'fade_ms')"},
        # The x on a row puts that one setting back.
        {"eval": "t.row('lines').querySelector('.drop').click()"},
        {"wait": "!t.overridden('lines')[0]"},
        {"eval": "t.values('lines')"},
        # A font name the daemon refuses: the reason is shown and the box goes back.
        {"eval": "t.type(t.field('font'), 'Sans; rm -rf')"},
        {"wait": "t.text('overlay-note').includes('font must be')"},
        {"eval": "t.values('font')"},
        {"eval": "t.el('overlay-reset').click()"},
        {"wait": "t.el('overlay-reset').disabled"},
        {"eval": "t.values('margin_x', 'fade_ms')"},
        {"eval": "t.overridden('margin_x', 'lines', 'fade_ms', 'font')"},
    ]
    results = drive(daemon, tmp_path, steps)[len(OPEN) :]
    assert results[0] == ["75", "2", "0"]
    assert results[3] == ["1"]
    assert results[6] == ["Sans Bold"]
    assert results[-2] == ["0", "120"]
    assert results[-1] == [False, False, False, False]
    assert daemon.overlay.snapshot()["overrides"] == {}


def test_a_change_made_elsewhere_and_the_overlay_count_appear_without_reloading(daemon, tmp_path):
    def later():
        time.sleep(1.5)
        daemon.overlay.attach()
        daemon.overlay.update({"margin_y": 333})

    threading.Thread(target=later, daemon=True).start()
    steps = [
        *OPEN,
        {"wait": "t.values('margin_y')[0] === '333'"},
        {"eval": "t.text('overlay-status')"},
    ]
    assert drive(daemon, tmp_path, steps)[-1] == "overlay running"


def test_the_monitor_is_picked_from_the_monitors_the_overlay_can_see(daemon, tmp_path):
    daemon.overlay.attach()
    daemon.overlay.set_monitors([
        {"connector": "DP-1", "description": "Dell Inc. DELL U2720Q"},
        {"connector": "HDMI-A-1", "description": ""},
    ])  # fmt: skip
    options = "[...t.field('monitor').options].map((o) => o.textContent)"
    steps = [
        *OPEN,
        {"eval": options},
        {"eval": "t.type(t.field('monitor'), 'DP-1')"},
        {"wait": "t.overridden('monitor')[0]"},
        # The overlay goes away: the choice stays, and the list says why it is short.
        {"eval": "fetch('/api/v1/overlay/monitors', {method: 'POST', "
                 "headers: {'Content-Type': 'application/json'}, body: '{\"monitors\": []}'})"},
        {"wait": f"{options}.length === 3"},
        {"eval": f"[t.values('monitor')[0], {options}]"},
    ]  # fmt: skip
    results = drive(daemon, tmp_path, steps)[len(OPEN) :]
    assert results[0] == [
        "Automatic (the screen in use)", "Dell Inc. DELL U2720Q (DP-1)", "HDMI-A-1",
    ]  # fmt: skip
    assert daemon.overlay.snapshot()["overrides"] == {"monitor": "DP-1"}
    assert results[-1] == [
        "DP-1",
        ["Automatic (the screen in use)", "DP-1",
         "Your monitors are listed while the overlay is running"],
    ]  # fmt: skip


def test_the_sample_line_is_asked_for_kept_alive_and_withdrawn(daemon, tmp_path):
    seen: list[int] = []
    daemon.overlay.subscribe(lambda: seen.append(daemon.overlay.snapshot()["preview_until_ms"]))
    started_ms = time.time() * 1000
    space = "new KeyboardEvent('keydown', { key: ' ', bubbles: true })"
    steps = [
        *OPEN,
        {"eval": "t.el('overlay-preview').click()"},
        {"eval": "t.pause(3600)"},  # long enough for one renewal
        # Space in a text box types a space; it must not reach the transport keys.
        {"eval": f"(t.field('font').focus(), t.field('font').dispatchEvent({space}))"},
        {"eval": "t.el('settings-close').click()"},
        {"wait": "t.el('settings-pane').hidden"},
        {"eval": "t.pause(300)"},
    ]
    drive(daemon, tmp_path, steps)
    asked = [value for value in seen if value]
    assert len(asked) >= 2 and asked == sorted(asked)  # asked, then renewed
    assert 6000 < asked[0] - started_ms < 14000  # a few seconds ahead, never open-ended
    assert seen[-1] == 0  # closing the panel takes the sample line down
    assert daemon.player.calls == []  # the space did not toggle playback


def test_the_overlay_switch_turns_it_off_and_on_where_spotifixed_runs_it(daemon, tmp_path):
    unmanaged = drive(daemon, tmp_path, [*OPEN, {"eval": "t.el('overlay-enabled').disabled"}])
    assert unmanaged[-1] is True  # under serve the overlay is run by hand: no switch

    daemon.overlay.set_managed()
    steps = [
        *OPEN,
        {"wait": "!t.el('overlay-enabled').disabled && t.el('overlay-enabled').checked"},
        {"eval": "t.el('overlay-enabled').click()"},
        {"wait": "t.text('overlay-status') === 'overlay off'"},
        {"eval": "t.el('overlay-preview').disabled"},
    ]
    assert drive(daemon, tmp_path, steps)[-1] is True
    assert daemon.overlay.enabled is False

    daemon.overlay.report_problem("It stopped.")
    steps = [
        *OPEN,
        {"wait": "!t.el('overlay-enabled').checked"},
        {"eval": "t.el('overlay-enabled').click()"},
        {"wait": "t.text('overlay-status') === 'overlay not running'"},
        {"eval": "t.text('overlay-problem')"},
    ]
    assert drive(daemon, tmp_path, steps)[-1] == ""  # switching clears the old problem
    assert daemon.overlay.enabled is True


LISTED = "[...document.querySelectorAll('#tracks li')].map((row) => row.dataset.id)"


def as_js_string(value) -> str:
    """``value`` as the browser's JSON.stringify would write it, quoted as a JS string."""
    return json.dumps(json.dumps(value, separators=(",", ":")))


READY = {"wait": "document.querySelectorAll('#tracks li').length === 5"}


def test_shuffle_reorders_the_list_at_once_and_the_order_is_kept(daemon, tmp_path):
    """Shuffle is an action: a new order, shown at once, and still there after a reload."""
    own_order = [f"TRK000{i}" for i in range(5)]
    first = drive(daemon, tmp_path, [
        READY,
        {"eval": LISTED},
        {"eval": "document.getElementById('unshuffle').hidden"},
        {"eval": "document.getElementById('shuffle').click()"},
        {"wait": "!document.getElementById('unshuffle').hidden"},
        {"eval": LISTED},
    ])  # fmt: skip
    assert first[1] == own_order and first[2] is True
    shuffled = first[-1]
    assert shuffled != own_order and sorted(shuffled) == own_order
    assert shuffled[-1] == "TRK0004"  # no audio: it has no place in the order
    kept = json.loads(daemon.session._store.session_json.read_text())["orders"]
    assert list(kept.values()) == [shuffled[:4]]

    # A reload, as after a restart: the same order, and the way back is offered.
    second = drive(daemon, tmp_path, [
        READY,
        {"eval": LISTED},
        {"eval": "document.getElementById('unshuffle').hidden"},
        {"eval": "document.getElementById('shuffle').click()"},
        {"wait": f"JSON.stringify({LISTED}) !== {as_js_string(shuffled)}"},
        {"eval": LISTED},
        {"eval": "document.getElementById('unshuffle').click()"},
        {"wait": "document.getElementById('unshuffle').hidden"},
        {"eval": LISTED},
    ])  # fmt: skip
    assert second[1] == shuffled and second[2] is False
    assert second[5] != shuffled and sorted(second[5]) == own_order  # shuffled again: new order
    assert second[-1] == own_order
    assert json.loads(daemon.session._store.session_json.read_text())["orders"] == {}


def test_clicking_a_track_in_a_shuffled_list_plays_on_down_the_list(daemon, tmp_path):
    daemon.session.shuffle(next(iter(daemon.routes._library.playlists()))["id"])
    results = drive(daemon, tmp_path, [
        READY,
        {"eval": LISTED},
        {"eval": "document.querySelectorAll('#tracks li')[1].click()"},
        {"wait": "document.querySelector('#tracks li.current')"},
        {"eval": "document.querySelector('#tracks li.current').dataset.id"},
    ])  # fmt: skip
    listed = results[1]
    assert results[-1] == listed[1]
    assert daemon.player.calls[-1] == ("load_queue", listed[:4], 1)


OFFSET_TEXT_OF_TRK0002 = (
    "document.querySelector('#tracks li[data-id=\"TRK0002\"] .offset').textContent === '+50 ms'"
)


def test_the_track_list_shows_each_tracks_offset_and_follows_changes(daemon, tmp_path):
    """Every row has its offset; a changed one shows at once, playing or not."""
    daemon.routes.dispatch("POST", "/api/v1/tracks/TRK0001/offset", b'{"offset_ms": 250}')
    listed = (
        "[...document.querySelectorAll('#tracks li')].map((row) => "
        "[row.dataset.id, row.querySelector('.offset').textContent, "
        "row.querySelector('.offset').classList.contains('set')])"
    )
    results = drive(daemon, tmp_path, [
        READY,
        {"eval": listed},
        {"eval": "document.querySelectorAll('#tracks li')[2].click()"},  # TRK0002 plays
        {"wait": "document.querySelector('#tracks li.current')"},
        {"eval": "document.querySelector('#tracks li[data-id=\"TRK0002\"] .offset').click()"},
        {"eval": "document.querySelector('#offset-tools [data-nudge=\"50\"]').click()"},
        {"wait": OFFSET_TEXT_OF_TRK0002},
        {"eval": listed},
    ])  # fmt: skip
    before, after = results[1], results[-1]
    assert before[0] == ["TRK0000", "0 ms", False]
    assert before[1] == ["TRK0001", "+250 ms", True]
    assert after[0] == ["TRK0000", "0 ms", False]  # the others are untouched
    assert after[1] == ["TRK0001", "+250 ms", True]
    assert after[2] == ["TRK0002", "+50 ms", True]


# What the bar under the current lyric line looks like right now: the line's text, how
# far the page says playback is through it, and the drawn width of the fill and its rail.
BAR = """(() => {
  const row = document.querySelector('#lyrics li.current');
  if (!row) return null;
  const fill = getComputedStyle(row, '::after');
  const rail = getComputedStyle(row, '::before');
  return {
    text: row.textContent,
    progress: row.dataset.progress ?? null,
    drawn: fill.content !== 'none',
    fill: parseFloat(fill.width),
    rail: parseFloat(rail.width),
  };
})()"""


def bar_at(daemon, tmp_path, position_s: float) -> dict:
    """The bar as the page draws it with the synthetic track paused at ``position_s``."""
    daemon.player.position = position_s
    daemon.player._publish()
    results = drive(daemon, tmp_path, [
        {"wait": "document.querySelector('#lyrics li.current')"},
        {"eval": "new Promise((done) => setTimeout(done, 200))"},
        {"eval": BAR},
    ])  # fmt: skip
    return results[-1]


def test_the_current_lyric_line_shows_how_far_through_it_playback_is(daemon, tmp_path):
    """Lines at 1.0 s and 4.0 s with a pause marked at 2.5 s; the fake track is 5 s long.

    The page shows each line 150 ms before its timestamp, and the bar is on that clock.
    """
    queue = daemon.routes._library.queue_for(PLAYLIST_ID)
    daemon.player.load_queue(queue, paused=True)

    first = bar_at(daemon, tmp_path, 1.75)  # (1750 + 150 - 1000) / (2500 - 1000)
    assert first["text"] == "an invented first line" and first["drawn"] is True
    assert float(first["progress"]) == pytest.approx(0.6, abs=0.001)
    assert first["rail"] > 100
    assert first["fill"] == pytest.approx(0.6 * first["rail"], abs=1.0)

    # A later offset shows the lyrics sooner, so the same instant is further through the line.
    daemon.routes.dispatch("POST", "/api/v1/tracks/TRK0000/offset", b'{"offset_ms": 300}')
    shifted = bar_at(daemon, tmp_path, 1.75)  # (1750 + 150 + 300 - 1000) / 1500
    assert float(shifted["progress"]) == pytest.approx(0.8, abs=0.001)
    assert shifted["fill"] == pytest.approx(0.8 * shifted["rail"], abs=1.0)
    daemon.routes.dispatch("POST", "/api/v1/tracks/TRK0000/offset", b'{"offset_ms": 0}')

    # The pause between the lines is a line of its own with no words: nothing is drawn.
    pause = bar_at(daemon, tmp_path, 3.0)
    assert pause["text"] == "" and pause["drawn"] is False

    # The last line runs to the end of the track: (4200 + 150 - 4000) / (5000 + 150 - 4000).
    last = bar_at(daemon, tmp_path, 4.2)
    assert last["text"] == "an invented last line" and last["drawn"] is True
    assert float(last["progress"]) == pytest.approx(350 / 1150, abs=0.001)
    assert last["fill"] == pytest.approx(350 / 1150 * last["rail"], abs=1.0)


def test_the_bar_moves_while_the_song_plays_and_stops_when_it_is_paused(daemon, tmp_path):
    queue = daemon.routes._library.queue_for(PLAYLIST_ID)
    daemon.player.load_queue(queue, paused=True)
    daemon.player.position = 1.0  # the line began 150 ms ago (the lead) and lasts 1.5 s
    daemon.player._publish()
    read = "Number(document.querySelector('#lyrics li.current')?.dataset.progress ?? -1)"
    # Playback is started from the page, once it is up: the line is only 1.5 s long, and
    # a browser that was slow to start must not find it already over.
    play = (
        "fetch('/api/v1/player/play', {method: 'POST', body: '{}',"
        " headers: {'Content-Type': 'application/json'}}).then((r) => r.ok)"
    )
    results = drive(daemon, tmp_path, [
        {"wait": f"{read} > 0"},
        {"eval": play},
        {"wait": "document.getElementById('toggle').textContent === 'Pause'"},
        {"eval": f"[{read}, performance.now()]"},
        {"eval": "new Promise((done) => setTimeout(done, 300))"},
        {"eval": f"[{read}, performance.now()]"},
    ])  # fmt: skip
    (earlier, earlier_ms), (later, later_ms) = results[3], results[5]
    assert results[1] is True and later > earlier > 0
    # The bar advanced by the time that passed, as a share of the line's 1.5 s.
    assert later - earlier == pytest.approx((later_ms - earlier_ms) / 1500, abs=0.03)

    daemon.player.position = 1.6
    daemon.player.pause()
    paused = drive(daemon, tmp_path, [
        {"wait": f"{read} > 0"},
        {"eval": read},
        {"eval": "new Promise((done) => setTimeout(done, 300))"},
        {"eval": read},
    ])  # fmt: skip
    assert paused[1] == paused[3] == pytest.approx(0.5, abs=0.001)


def test_the_fade_in_setting_is_shown_and_saved_from_the_settings_pane(daemon, tmp_path):
    daemon.session.set_settings({"fade_in_s": 2.5})
    typed = (
        "(() => { const box = t.el('fade-in'); box.focus(); box.value = '4';"
        " box.dispatchEvent(new Event('input', { bubbles: true })); box.blur(); })()"
    )
    dragged = (
        "(() => { const s = t.el('fade-in-slider'); s.value = '6.5';"
        " s.dispatchEvent(new Event('input', { bubbles: true })); })()"
    )
    results = drive(daemon, tmp_path, [
        *OPEN,
        {"wait": "t.el('fade-in').value === '2.5'"},
        {"eval": "[t.el('fade-in').value, t.el('fade-in-slider').value]"},
        {"eval": typed},
        {"eval": "t.el('fade-in-slider').value"},  # the slider follows the box at once
        {"eval": "t.pause(500)"},
        {"eval": dragged},
        {"eval": "t.el('fade-in').value"},
        {"eval": "t.pause(500)"},
        {"eval": "document.getElementById('playback-settings').textContent"},
    ])[len(OPEN):]  # fmt: skip
    assert results[1] == ["2.5", "2.5"]
    assert results[3] == "4" and results[6] == "6.5"
    said = " ".join(results[-1].split())
    assert "the music fades in over this many seconds" in said
    assert "When you press play after pausing" in said and "daemon" not in said
    assert daemon.session.document()["settings"]["fade_in_s"] == 6.5


def test_the_mono_switch_is_off_to_begin_with_and_reaches_the_player(daemon, tmp_path):
    results = drive(daemon, tmp_path, [
        *OPEN,
        {"wait": "t.el('playback-settings').textContent.includes('both ears')"},
        {"eval": "t.el('mono').checked"},
        {"eval": "t.el('mono').click()"},
        {"wait": "t.el('mono').checked"},
        {"eval": "t.pause(300)"},
        {"eval": "document.getElementById('playback-settings').textContent"},
    ])[len(OPEN):]  # fmt: skip
    assert results[1] is False
    said = " ".join(results[-1].split())
    assert "Same sound in both ears" in said and "mono" in said
    assert daemon.session.document()["settings"]["mono"] is True
    assert ("set_mono", True) in daemon.player.calls


def test_the_repeat_switch_is_off_to_begin_with_and_reaches_the_player(daemon, tmp_path):
    results = drive(daemon, tmp_path, [
        *OPEN,
        {"wait": "t.el('playback-settings').textContent.includes('Repeat playlist')"},
        {"eval": "t.el('loop').checked"},
        {"eval": "t.el('loop').click()"},
        {"wait": "t.el('loop').checked"},
        {"eval": "t.pause(300)"},
    ])[len(OPEN):]  # fmt: skip
    assert results[1] is False
    assert daemon.session.document()["settings"]["loop"] is True
    assert ("set_loop", True) in daemon.player.calls


def test_the_track_switches_take_tracks_out_of_the_list_and_put_them_back(daemon, tmp_path):
    rows = "[...document.querySelectorAll('#tracks li')].map((li) => li.dataset.id)"
    shot = tmp_path / "tracks.png"
    results = drive(daemon, tmp_path, [
        *OPEN,
        {"wait": "t.el('include-without-lyrics').checked"},
        {"eval": "t.el('include-without-audio').checked"},
        {"eval": "t.el('include-without-lyrics').click()"},
        {"wait": "document.querySelectorAll('#tracks li').length === 4"},
        {"eval": rows},
        {"eval": "t.text('coverage')"},
        {"eval": "t.el('include-without-audio').click()"},
        {"wait": "document.querySelectorAll('#tracks li').length === 3"},
        {"screenshot": str(shot)},
        {"eval": "t.el('include-without-lyrics').click()"},
        {"wait": "document.querySelectorAll('#tracks li').length === 4"},
        {"eval": rows},
    ])[len(OPEN):]  # fmt: skip
    assert results[1] is True
    assert results[4] == ["TRK0000", "TRK0001", "TRK0002", "TRK0004"]
    assert "1 left out by settings" in results[5]
    assert results[-1] == ["TRK0000", "TRK0001", "TRK0002", "TRK0003"]
    settings = daemon.session.document()["settings"]
    assert settings["include_without_lyrics"] is True
    assert settings["include_without_audio"] is False
    assert shot.stat().st_size > 0


def test_the_page_switches_hide_the_lyrics_and_the_position_and_are_kept(daemon, tmp_path):
    queue = daemon.routes._library.queue_for(PLAYLIST_ID)
    daemon.player.load_queue(queue, paused=True)
    daemon.player.position = 1.0
    daemon.player._publish()
    shown = "(id) => getComputedStyle(t.el(id)).display !== 'none'"
    results = drive(daemon, tmp_path, [
        *OPEN,
        {"wait": "document.querySelectorAll('#lyrics li').length > 0"},
        {"eval": f"window.shown = {shown}"},
        {"wait": "t.el('show-lyrics').checked && t.el('show-position').checked"},
        {"eval": "t.el('show-lyrics').click()"},
        {"wait": "!shown('lyrics-pane')"},
        {"eval": "[t.el('lyrics').children.length, shown('seek-row')]"},
        {"eval": "t.el('show-position').click()"},
        {"wait": "!shown('seek-row')"},
        {"eval": "t.el('show-lyrics').click()"},
        {"wait": "shown('lyrics-pane') && document.querySelectorAll('#lyrics li').length > 0"},
        {"eval": "document.querySelector('#lyrics li.current')?.textContent ?? null"},
    ])[len(OPEN):]  # fmt: skip
    assert results[5] == [0, True]  # the lines are dropped, not just hidden
    assert results[-1]  # back on, the current track's lines are fetched and followed
    settings = daemon.session.document()["settings"]
    assert settings["show_lyrics"] is True and settings["show_position"] is False


# Counts what the page does from here on: animation frames asked for, changes to the
# time and the lyric pane, and writes to the slider (a property, so no mutation shows it).
WATCH = """(() => {
  window.seen = { frames: 0, elapsed: 0, lyrics: 0, seek: 0 };
  const ask = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (step) => { seen.frames += 1; return ask(step); };
  const watch = (id, key) => new MutationObserver((records) => { seen[key] += records.length; })
    .observe(document.getElementById(id),
      { childList: true, subtree: true, characterData: true, attributes: true });
  watch('elapsed', 'elapsed');
  watch('lyrics', 'lyrics');
  const seek = document.getElementById('seek');
  const value = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  Object.defineProperty(seek, 'value', {
    get() { return value.get.call(this); },
    set(v) { seen.seek += 1; value.set.call(this, v); },
  });
})()"""
PLAY = (
    "fetch('/api/v1/player/play', {method: 'POST', body: '{}',"
    " headers: {'Content-Type': 'application/json'}}).then((r) => r.ok)"
)


def watched(daemon, tmp_path, ms: int, *, play: bool) -> dict:
    """What the page did over ``ms`` once settled, paused or (with ``play``) playing."""
    queue = daemon.routes._library.queue_for(PLAYLIST_ID)
    daemon.player.load_queue(queue, paused=True)
    daemon.player.position = 1.0
    daemon.player._publish()
    start = [{"eval": PLAY}, {"wait": "t.el('toggle').textContent === 'Pause'"}] if play else []
    return drive(daemon, tmp_path, [
        {"wait": "document.getElementById('playlists').options.length"},
        {"eval": HELPERS},
        {"wait": "t.text('title') !== 'Nothing playing'"},
        *start,
        {"eval": "t.pause(300)"},
        {"eval": WATCH},
        {"eval": f"t.pause({ms})"},
        {"eval": "seen"},
    ])[-1]  # fmt: skip


def test_a_paused_page_draws_nothing(daemon, tmp_path):
    assert watched(daemon, tmp_path, 600, play=False) == {
        "frames": 0, "elapsed": 0, "lyrics": 0, "seek": 0,
    }  # fmt: skip


def test_with_lyrics_and_position_off_a_playing_page_draws_nothing(daemon, tmp_path):
    daemon.session.set_settings({"show_lyrics": False, "show_position": False})
    assert watched(daemon, tmp_path, 1500, play=True) == {
        "frames": 0, "elapsed": 0, "lyrics": 0, "seek": 0,
    }  # fmt: skip


def test_with_only_the_position_shown_the_page_draws_once_a_second(daemon, tmp_path):
    daemon.session.set_settings({"show_lyrics": False})
    seen = watched(daemon, tmp_path, 2200, play=True)
    assert seen["frames"] == 0 and seen["lyrics"] == 0
    # Each new second of the song is written once: two or three of them in 2.2 s.
    assert 2 <= seen["elapsed"] <= 3 and 2 <= seen["seek"] <= 3


# The word the page underlines in the current lyric line, and how it is drawn.
SUNG = """(() => {
  const words = document.querySelectorAll('#lyrics li.current span.sung');
  if (words.length !== 1) return words.length ? 'several' : null;
  return [words[0].textContent, getComputedStyle(words[0]).textDecorationLine];
})()"""


def sung_at(daemon, tmp_path, position_s: float, before: list[dict] = ()) -> list | None:
    """The underlined word with the synthetic track paused at ``position_s``."""
    daemon.player.position = position_s
    daemon.player._publish()
    results = drive(daemon, tmp_path, [
        *before,
        {"wait": "document.querySelector('#lyrics li.current')"},
        {"eval": "new Promise((done) => setTimeout(done, 200))"},
        {"eval": SUNG},
    ])  # fmt: skip
    return results[-1]


def test_the_word_being_sung_is_underlined_once_the_setting_is_on(daemon, tmp_path):
    """The first line starts at 1.0 s and the pause after it at 2.5 s.

    Its six syllables share that 1.5 s, 250 ms each: "an" to 250, "invented" to 1000,
    "first" to 1250, "line" to 1500, on the page's clock (150 ms ahead of the song).
    """
    queue = daemon.routes._library.queue_for(PLAYLIST_ID)
    daemon.player.load_queue(queue, paused=True)

    assert sung_at(daemon, tmp_path, 1.75) is None  # off until asked for

    switched = [*OPEN, {"eval": "t.field('underline_word').click()"}]
    assert sung_at(daemon, tmp_path, 1.75, switched) == ["invented", "underline"]
    assert daemon.overlay.snapshot()["overrides"] == {"underline_word": True}

    assert sung_at(daemon, tmp_path, 0.9) == ["an", "underline"]
    assert sung_at(daemon, tmp_path, 2.3) == ["line", "underline"]
    assert sung_at(daemon, tmp_path, 3.0) is None  # the pause: a line with no words
    # A later offset brings the words sooner, as it does the line.
    daemon.routes.dispatch("POST", "/api/v1/tracks/TRK0000/offset", b'{"offset_ms": 300}')
    assert sung_at(daemon, tmp_path, 1.75) == ["first", "underline"]


def test_a_line_aligned_to_the_audio_is_underlined_by_its_own_word_times(daemon, tmp_path, store):
    """With word times from ``align``, the page follows them and not the estimate."""
    store.write_json(
        store.words_json("TRK0000"),
        {
            "lyrics_sha256": assets.lyrics_digest(SYNCED),
            # The singing starts late and the last word is held: nothing like 250 ms each.
            "lines": [[[1500, 1600], [1600, 1700], [1700, 1800], [1800, 2400]], None, None],
        },
    )
    daemon.overlay.update({"underline_word": True})
    queue = daemon.routes._library.queue_for(PLAYLIST_ID)
    daemon.player.load_queue(queue, paused=True)

    assert sung_at(daemon, tmp_path, 1.2) is None  # the line is up; nobody is singing yet
    assert sung_at(daemon, tmp_path, 1.5) == ["an", "underline"]
    assert sung_at(daemon, tmp_path, 2.0) == ["line", "underline"]  # the estimate says "first"
    # The offset moves the line, not the words: they were heard in this audio.
    daemon.routes.dispatch("POST", "/api/v1/tracks/TRK0000/offset", b'{"offset_ms": 300}')
    assert sung_at(daemon, tmp_path, 2.0) == ["line", "underline"]
    # The last line was not aligned, so it is estimated as before.
    assert sung_at(daemon, tmp_path, 4.2) == ["invented", "underline"]


def test_the_keyboard_section_lists_the_volume_keys_and_the_media_keys(
    store, tmp_path, monkeypatch
):
    monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
    monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
    monkeypatch.setenv("SPOTIFIXED_MPV_MPRIS_PLUGIN", "/nonexistent/mpris.so")
    build_library(store)
    daemon = Daemon(load_settings(), store, FakePlayer(), port=0, keepalive_s=0.5)
    threading.Thread(target=daemon.serve_forever, daemon=True).start()
    try:
        results = drive(daemon, tmp_path, [
            *OPEN,
            {"wait": "t.el('shortcut-list').children.length"},
            {"eval": "[...t.el('shortcut-list').children].map((n) => n.textContent)"},
            {"eval": "t.text('keyboard-settings')"},
        ])[len(OPEN):]  # fmt: skip
    finally:
        daemon.shutdown()
    rows = results[1]
    assert rows[:4] == [
        "Ctrl+Volume Up", "Raise spotifixed's volume",
        "Ctrl+Volume Down", "Lower spotifixed's volume",
    ]  # fmt: skip
    assert "Play/Pause" in rows and "Next track" in rows
    said = " ".join(results[-1].split())
    assert "even while a game or another window is in front" in said
    assert "daemon" not in said and "portal" not in said
