"""The shortcut settings: their defaults, how a trigger is checked, and how it reads."""

from __future__ import annotations

import pytest

from spotifixed.config import load_settings
from spotifixed.errors import ConfigError
from spotifixed.shortcut_settings import (
    DISABLE_PATH,
    VOLUME_PATH,
    describe,
    from_settings,
    trigger_problem,
)


def test_the_defaults_are_ctrl_volume_five_at_a_time_and_meta_next_to_skip():
    up, down, skip = from_settings(load_settings())
    assert (up.id, up.trigger, up.path, up.body) == (
        "volume-up", "CTRL+XF86AudioRaiseVolume", VOLUME_PATH, {"delta": 5},
    )  # fmt: skip
    assert (down.id, down.trigger, down.body) == (
        "volume-down", "CTRL+XF86AudioLowerVolume", {"delta": -5},
    )  # fmt: skip
    assert (skip.id, skip.trigger, skip.path, skip.body) == (
        "skip-disable", "LOGO+XF86AudioNext", DISABLE_PATH, {},
    )  # fmt: skip
    assert describe(skip.trigger) == "Meta+Next"


def test_an_empty_trigger_leaves_that_shortcut_out(monkeypatch):
    monkeypatch.setenv("SPOTIFIXED_SHORTCUT_VOLUME_UP", "")
    monkeypatch.setenv("SPOTIFIXED_SHORTCUT_SKIP_DISABLE", "")
    monkeypatch.setenv("SPOTIFIXED_SHORTCUT_VOLUME_STEP", "10")
    (down,) = from_settings(load_settings())
    assert (down.id, down.body) == ("volume-down", {"delta": -10})


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("SPOTIFIXED_SHORTCUT_VOLUME_UP", "ctrl+Up", "VOLUME_UP has an unknown modifier 'ctrl'"),
        ("SPOTIFIXED_SHORTCUT_VOLUME_DOWN", "CTRL+", "VOLUME_DOWN must end in one key name"),
        ("SPOTIFIXED_SHORTCUT_VOLUME_DOWN", "CTRL+Page Down", "one key name"),
        ("SPOTIFIXED_SHORTCUT_VOLUME_UP", "SHIFT+SHIFT+a", "twice"),
        ("SPOTIFIXED_SHORTCUT_VOLUME_STEP", "0", "VOLUME_STEP"),
        ("SPOTIFIXED_SHORTCUT_VOLUME_STEP", "101", "VOLUME_STEP"),
    ],
)
def test_a_bad_setting_is_refused_naming_its_variable(monkeypatch, name, value, match):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=match):
        from_settings(load_settings())


@pytest.mark.parametrize("trigger", ["CTRL+a", "LOGO+Up", "CTRL+ALT+SHIFT+F12", "XF86AudioMute"])
def test_well_formed_triggers_pass(trigger):
    assert trigger_problem(trigger) is None


@pytest.mark.parametrize(
    ("trigger", "shown"),
    [
        ("CTRL+XF86AudioRaiseVolume", "Ctrl+Volume Up"),
        ("CTRL+XF86AudioLowerVolume", "Ctrl+Volume Down"),
        ("LOGO+SHIFT+a", "Meta+Shift+A"),
        ("ALT+F5", "Alt+F5"),
    ],
)
def test_a_trigger_is_described_the_way_a_person_reads_it(trigger, shown):
    assert describe(trigger) == shown
