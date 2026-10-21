"""Settings: one typed, immutable object resolved from the schema in ``options_schema``.

The rest of the package never reads ``os.environ`` or a ``.env`` file itself. It asks
for a :class:`Settings` (or is handed one), which keeps every module testable with a
plain constructor call and keeps the resolution rules in one file.

Each setting is taken from the first of these that has it:

1. a command-line flag;
2. an environment variable;
3. a ``.env`` file in the working directory;
4. the default in the schema.

Resolution uses only the standard library, so every command runs on a bare Python. The
schema is in OptionsConfig's format, and OptionsConfig is what generates ``.env.example``
and the Configuration section of ``docs/setup.md`` from it (``build.py``); it is a development
dependency and is not imported here.

Options marked ``sensitive`` are never registered as command-line flags. A secret given
on a command line is visible to every local user in the process list.
"""

from __future__ import annotations

import ipaddress
import os
from argparse import ArgumentParser, Namespace
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, get_args

from .errors import ConfigError
from .options_schema import OPTIONS_SCHEMA

# Relative storage paths resolve against this, not the working directory, so the same
# cache is used no matter where a command is started from.
REPO_ROOT = Path(__file__).resolve().parent.parent

_CLI_SCHEMA = {name: opt for name, opt in OPTIONS_SCHEMA.items() if not opt.get("sensitive")}


@dataclass(frozen=True, slots=True)
class Settings:
    cache_dir: Path
    state_dir: Path
    apify_token: str | None
    tracklist_actor: str
    apify_max_charge_usd: float
    apify_base_url: str
    lrclib_base_url: str
    audio_duration_tolerance_s: float
    audio_duration_reject_s: float
    lyrics_duration_tolerance_s: float
    sync_workers: int
    miss_retry_days: int
    host: str
    port: int
    mpv_bin: str
    mpv_audio_output: str
    mpv_mpris_plugin: str
    overlay_monitor: str
    overlay_anchor: str
    overlay_margin_x: int
    overlay_margin_y: int
    overlay_max_width: int
    overlay_font: str
    overlay_font_size: int
    overlay_color: str
    overlay_outline_color: str
    overlay_lines: int
    overlay_lead_ms: int
    overlay_fade_ms: int
    overlay_underline_word: bool
    overlay_volume_indicator: bool
    shortcut_volume_up: str
    shortcut_volume_down: str
    shortcut_skip_disable: str
    shortcut_volume_step: int

    def __repr__(self) -> str:
        # A Settings object ends up in logs and tracebacks; never let the token ride along.
        def show(name: str) -> str:
            if name == "apify_token":
                return "<set>" if self.apify_token else "None"
            return repr(getattr(self, name))

        shown = ", ".join(f"{f.name}={show(f.name)}" for f in fields(self))
        return f"Settings({shown})"

    def require_apify_token(self) -> str:
        """Return the token, or fail naming the variable. Call only when about to use it."""
        if not self.apify_token:
            raise ConfigError(
                "APIFY_TOKEN is not set. It is needed only to fetch a playlist's track list "
                "that is not cached yet; see secrets.env.example for where it lives."
            )
        return self.apify_token

    @property
    def base_url(self) -> str:
        """The daemon's own origin, as clients should address it."""
        return f"http://{self.host}:{self.port}"


def _choices(option: Mapping[str, Any]) -> tuple[str, ...]:
    """The allowed values of an option whose type is a ``Literal``; empty otherwise."""
    return tuple(get_args(option["type"])) if option["type"] not in (str, int, float, bool) else ()


def add_settings_arguments(parser: ArgumentParser) -> None:
    """Register every non-secret setting as a ``--flag`` on ``parser``.

    Flags default to ``None`` and are kept as text; :func:`load_settings` falls through to
    the environment for the ones not given and converts every source the same way.
    """
    for name, option in OPTIONS_SCHEMA.items():
        if option.get("sensitive"):
            continue
        parser.add_argument(
            option["arg"],
            dest=name.lower(),
            default=None,
            metavar=name,
            choices=_choices(option) or None,
            help=f"{option['help']} (default: {option['default']!r})",
        )


def settings_arguments(args: Namespace) -> list[str]:
    """The settings flags that were given in ``args``, as an argument list again.

    For handing the same settings to a child process running another command.
    """
    argv: list[str] = []
    for name, option in _CLI_SCHEMA.items():
        value = getattr(args, name.lower(), None)
        if value is not None:
            argv += [option["arg"], value]
    return argv


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines from ``path``; an absent file is simply empty.

    Understands what ``.env.example`` contains and little more: blank lines, ``#``
    comments, an optional ``export`` prefix, and values in single or double quotes.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()  # a trailing comment
        values[key.strip()] = value
    return values


_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def _convert(name: str, option: Mapping[str, Any], raw: Any) -> Any:
    """``raw`` (text from a flag, the environment or a file) as the option's type."""
    kind = option["type"]
    label = option["env"]
    if kind in (int, float):
        try:
            return kind(raw)
        except (TypeError, ValueError):
            word = "a whole number" if kind is int else "a number"
            raise ConfigError(f"{label} must be {word}, got {raw!r}") from None
    if kind is bool:
        word = str(raw).strip().lower()
        if word not in _TRUE and word not in _FALSE:
            raise ConfigError(f"{label} must be true or false, got {raw!r}")
        return word in _TRUE
    choices = _choices(option)
    if choices and raw not in choices:
        raise ConfigError(f"{label} must be one of {', '.join(choices)}; got {raw!r}")
    return str(raw)


def _resolve_dir(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path)


def load_settings(
    args: Namespace | None = None,
    *,
    root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> Settings:
    """Resolve every setting (flags, environment, ``.env``, defaults) and validate."""
    environment = os.environ if environ is None else environ
    from_file = read_env_file(Path(".env") if env_file is None else env_file)

    values: dict[str, Any] = {}
    for name, option in OPTIONS_SCHEMA.items():
        field_name = name.lower()
        numeric = option["type"] in (int, float, bool)
        candidates = (
            getattr(args, field_name, None) if args is not None else None,
            environment.get(option["env"]),
            from_file.get(option["env"]),
        )
        # An empty value cannot be a number or a switch, so there it means "not set".
        raw = next((c for c in candidates if c is not None and not (numeric and c == "")), None)
        if raw is None:
            values[field_name] = option["default"]
        else:
            values[field_name] = _convert(name, option, raw)

    values["cache_dir"] = _resolve_dir(values["cache_dir"], root)
    values["state_dir"] = _resolve_dir(values["state_dir"], root)
    values["apify_token"] = values["apify_token"] or None
    for name in ("apify_base_url", "lrclib_base_url"):
        values[name] = values[name].rstrip("/")
    settings = Settings(**values)
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    try:
        loopback = s.host == "localhost" or ipaddress.ip_address(s.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError(
            f"SPOTIFIXED_HOST must be a loopback address, got {s.host!r}. The daemon "
            "has no authentication and is not meant to be reachable from the network."
        )
    if not 1 <= s.port <= 65535:
        raise ConfigError(f"SPOTIFIXED_PORT must be 1-65535, got {s.port}")
    if s.sync_workers < 1:
        raise ConfigError(f"SPOTIFIXED_SYNC_WORKERS must be at least 1, got {s.sync_workers}")
    if s.audio_duration_reject_s < s.audio_duration_tolerance_s:
        raise ConfigError(
            "SPOTIFIXED_AUDIO_DURATION_REJECT_S must not be smaller than "
            "SPOTIFIXED_AUDIO_DURATION_TOLERANCE_S"
        )
    if s.overlay_lines not in (1, 2):
        raise ConfigError(f"SPOTIFIXED_OVERLAY_LINES must be 1 or 2, got {s.overlay_lines}")
