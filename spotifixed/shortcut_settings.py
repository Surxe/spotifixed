"""The global keyboard shortcuts: which there are, what they do, how triggers are written.

Two programs need this. The ``shortcuts`` process binds them with the desktop, and the
daemon lists them on the page next to the media keys. Both read them from
:class:`~spotifixed.config.Settings` through :func:`from_settings`, which is also where
a badly written trigger is refused, naming the variable that set it.

A trigger is in the format of the XDG shortcuts specification, which is what the global
shortcuts portal takes: modifiers from a fixed set, each followed by ``+``, then one key
by its xkb name, such as ``CTRL+XF86AudioRaiseVolume`` or ``LOGO+Up``. Only the shape is
checked here. Whether the key name exists is the desktop's to decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .errors import ConfigError

MODIFIERS = ("CTRL", "ALT", "SHIFT", "NUM", "LOGO")

# The daemon's API paths a press is sent to.
VOLUME_PATH = "/api/v1/player/volume"
DISABLE_PATH = "/api/v1/player/disable"

# How a modifier and the commonest keys are written for a person, as KDE writes them.
_MODIFIER_NAMES = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "NUM": "Num", "LOGO": "Meta"}
_KEY_NAMES = {
    "XF86AudioRaiseVolume": "Volume Up",
    "XF86AudioLowerVolume": "Volume Down",
    "XF86AudioMute": "Mute",
    "XF86AudioPlay": "Play",
    "XF86AudioPause": "Pause",
    "XF86AudioNext": "Next",
    "XF86AudioPrev": "Previous",
    "XF86AudioStop": "Stop",
    "Prior": "Page Up",
    "Next": "Page Down",
}

# What the keyboard's media keys do. They are not bound here and cannot be changed here;
# mpv's MPRIS plugin answers them. They are listed so the page can show them beside the
# shortcuts that are.
MEDIA_KEYS = (
    ("Play/Pause", "Play or pause"),
    ("Next", "Next track"),
    ("Previous", "Previous track"),
    ("Stop", "Stop"),
)


@dataclass(frozen=True, slots=True)
class Shortcut:
    """One shortcut: its stable id for the portal, what it is for, and the request it sends."""

    id: str
    description: str
    trigger: str
    path: str
    body: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "description": self.description, "keys": describe(self.trigger)}


def trigger_problem(trigger: str) -> str | None:
    """What is wrong with ``trigger`` as a sentence ending, or None if it is well formed."""
    *modifiers, key = trigger.split("+")
    if not key or any(c.isspace() for c in key):
        return "must end in one key name, with no spaces"
    unknown = [m for m in modifiers if m not in MODIFIERS]
    if unknown:
        return f"has an unknown modifier {unknown[0]!r} (use {', '.join(MODIFIERS)})"
    if len(set(modifiers)) != len(modifiers):
        return "names a modifier twice"
    return None


def describe(trigger: str) -> str:
    """``trigger`` as a person reads it: ``CTRL+XF86AudioRaiseVolume`` is Ctrl+Volume Up."""
    *modifiers, key = trigger.split("+")
    names = [_MODIFIER_NAMES.get(m, m) for m in modifiers]
    names.append(_KEY_NAMES.get(key, key.upper() if len(key) == 1 else key))
    return "+".join(names)


def from_settings(settings: Settings) -> list[Shortcut]:
    """The shortcuts these settings ask for; one whose trigger is empty is left out."""
    step = settings.shortcut_volume_step
    if not 1 <= step <= 100:
        raise ConfigError(f"SPOTIFIXED_SHORTCUT_VOLUME_STEP must be 1-100, got {step}")
    wanted = (
        Shortcut(
            "volume-up", "Raise spotifixed's volume", settings.shortcut_volume_up,
            VOLUME_PATH, {"delta": step},
        ),
        Shortcut(
            "volume-down", "Lower spotifixed's volume", settings.shortcut_volume_down,
            VOLUME_PATH, {"delta": -step},
        ),
        Shortcut(
            "skip-disable", "Skip this song and turn it off", settings.shortcut_skip_disable,
            DISABLE_PATH,
        ),
    )  # fmt: skip
    for shortcut in wanted:
        problem = shortcut.trigger and trigger_problem(shortcut.trigger)
        if problem:
            variable = f"SPOTIFIXED_SHORTCUT_{shortcut.id.upper().replace('-', '_')}"
            raise ConfigError(f"{variable} {problem}, got {shortcut.trigger!r}")
    return [shortcut for shortcut in wanted if shortcut.trigger]
