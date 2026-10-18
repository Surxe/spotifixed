"""The overlay's adjustable settings as one value, with the rules each must obey.

Two programs need the same rules. The overlay takes its starting values from
:class:`~spotifixed.config.Settings`, and the daemon accepts changes to them from
the remote-control page while the overlay is running. Both go through :func:`clean`, so
a value the page may send is exactly a value the overlay could have been started with.

A change made on the page is an *override*: it is layered over the configured value and
kept by the daemon, which saves it as it is made. An override equal to the configured
value is not an override at all (see :func:`prune`): setting a value back by hand is the
same as resetting it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any, get_args

from .config import Settings
from .errors import ConfigError
from .options_schema import OPTIONS_SCHEMA, Anchor

ANCHORS: tuple[str, ...] = get_args(Anchor)

# A colour ends up inside a stylesheet, so only plain forms are accepted.
_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|[a-zA-Z]{3,20}")
# A font name is letters, digits and plain punctuation only. It arrives from a web page
# and is stored and handed to the toolkit; nothing a real font name needs is left out.
_FONT = re.compile(r"[\w][\w ,.\-]{0,79}")
_MONITOR = re.compile(r"[A-Za-z0-9_.:\-]{0,40}")
# A monitor as the overlay reports one: the same characters, but never empty.
MONITOR_CONNECTOR = re.compile(r"[A-Za-z0-9_.:\-]{1,40}")


class OverlaySettingError(ValueError):
    """A value that an overlay setting cannot take. The message says what it must be."""


@dataclass(frozen=True, slots=True)
class _Whole:
    low: int
    high: int

    def clean(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise OverlaySettingError(self.describe())
        if not self.low <= value <= self.high:
            raise OverlaySettingError(self.describe())
        return value

    def describe(self) -> str:
        return f"a whole number from {self.low} to {self.high}"

    def to_json(self) -> dict[str, Any]:
        return {"kind": "int", "min": self.low, "max": self.high}


@dataclass(frozen=True, slots=True)
class _Text:
    pattern: re.Pattern[str]
    what: str
    kind: str = "text"

    def clean(self, value: Any) -> str:
        if not isinstance(value, str) or not self.pattern.fullmatch(value):
            raise OverlaySettingError(self.describe())
        return value

    def describe(self) -> str:
        return self.what

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind}


@dataclass(frozen=True, slots=True)
class _Choice:
    choices: tuple[str, ...]

    def clean(self, value: Any) -> str:
        if not isinstance(value, str) or value not in self.choices:
            raise OverlaySettingError(self.describe())
        return value

    def describe(self) -> str:
        return f"one of {', '.join(self.choices)}"

    def to_json(self) -> dict[str, Any]:
        return {"kind": "choice", "choices": list(self.choices)}


@dataclass(frozen=True, slots=True)
class _Switch:
    def clean(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise OverlaySettingError(self.describe())
        return value

    def describe(self) -> str:
        return "true or false"

    def to_json(self) -> dict[str, Any]:
        return {"kind": "bool"}


_COLOUR_RULE = _Text(_COLOR, "a colour like #RRGGBB", kind="color")

# In the order the page shows them.
RULES: dict[str, _Whole | _Text | _Choice | _Switch] = {
    "anchor": _Choice(ANCHORS),
    "margin_x": _Whole(-5000, 5000),
    "margin_y": _Whole(-5000, 5000),
    "monitor": _Text(_MONITOR, "an output connector such as DP-2, or empty"),
    "font_size": _Whole(4, 300),
    "max_width": _Whole(50, 10000),
    "font": _Text(_FONT, "a font name of letters, digits, spaces and , . - only"),
    "color": _COLOUR_RULE,
    "outline_color": _COLOUR_RULE,
    "lines": _Whole(1, 2),
    "lead_ms": _Whole(-5000, 5000),
    "fade_ms": _Whole(0, 5000),
    "underline_word": _Switch(),
    "volume_indicator": _Switch(),
}


def env_name(key: str) -> str:
    """The environment variable that configures ``key``."""
    return str(OPTIONS_SCHEMA[f"OVERLAY_{key.upper()}"]["env"])


def clean(key: str, value: Any) -> Any:
    """``value`` if ``key`` may take it, else :class:`OverlaySettingError`."""
    rule = RULES.get(key)
    if rule is None:
        raise OverlaySettingError(f"{key!r} is not an overlay setting")
    try:
        return rule.clean(value)
    except OverlaySettingError as exc:
        raise OverlaySettingError(f"{key} must be {exc}; got {value!r}") from None


@dataclass(frozen=True, slots=True)
class OverlaySettings:
    anchor: str
    margin_x: int
    margin_y: int
    monitor: str
    font_size: int
    max_width: int
    font: str
    color: str
    outline_color: str
    lines: int
    lead_ms: int
    fade_ms: int
    underline_word: bool
    volume_indicator: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> OverlaySettings:
        """The configured values. A bad one fails naming the variable that set it."""
        values = {}
        for key, rule in RULES.items():
            value = getattr(settings, f"overlay_{key}")
            try:
                values[key] = rule.clean(value)
            except OverlaySettingError as exc:
                raise ConfigError(f"{env_name(key)} must be {exc}; got {value!r}") from None
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def merged(self, overrides: Mapping[str, Any]) -> OverlaySettings:
        """These values with ``overrides`` on top. Every override is checked."""
        values = self.to_dict()
        for key, value in overrides.items():
            values[key] = clean(key, value)
        return OverlaySettings(**values)


assert tuple(RULES) == tuple(f.name for f in fields(OverlaySettings))


def prune(defaults: OverlaySettings, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """``overrides`` without the entries that only repeat the configured value."""
    base = defaults.to_dict()
    return {key: value for key, value in overrides.items() if base.get(key) != value}


def salvage(overrides: Any) -> tuple[dict[str, Any], list[str]]:
    """The usable part of stored overrides, and a note for each entry that was dropped.

    A file written by an older or newer version, or edited by hand, must not stop the
    daemon from starting; whatever cannot be used is left out and said so.
    """
    if not isinstance(overrides, dict):
        return {}, ["the stored overlay overrides are not an object; ignoring them"]
    kept: dict[str, Any] = {}
    notes: list[str] = []
    for key, value in overrides.items():
        try:
            kept[key] = clean(str(key), value)
        except OverlaySettingError as exc:
            notes.append(f"ignoring a stored overlay override: {exc}")
    return kept, notes
