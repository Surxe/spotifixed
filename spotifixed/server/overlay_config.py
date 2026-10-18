"""The daemon's copy of the overlay's settings: configured values plus page overrides.

The overlay is a separate program. It learns its settings from the daemon's event
stream, so a change made on the remote-control page reaches a running overlay at once.
This object is what that stream reads: a versioned document that wakes its listeners
whenever it changes, in the same way the state bus does for playback.

The document also carries the overlay's on/off switch, ``enabled``. Under ``start`` the
overlay is a child process that follows it: switched off, the overlay is not running at
all, and switched on it is started again (see :mod:`spotifixed.launch`). The daemon
only holds the switch; ``managed`` says whether anything is following it, which is not
the case under ``serve`` or ``start --no-overlay``, where the overlay is run by hand if
at all. ``problem`` is a sentence for the page when the overlay stopped by itself.

``monitors`` is what the running overlay reports it can see: each monitor's connector
(the value the ``monitor`` setting takes) and a description such as its make and model,
so the page can offer a list to pick from instead of asking for a connector by name. It
is forgotten when the last overlay goes away.

Only the overrides and the switch are stored (``overlay.json`` in the state directory).
The configured values are whatever this daemon was started with. The rest of the
document is never stored: ``preview_until_ms``, which asks the overlay to show a sample
line until that instant so it can be positioned with nothing playing, ``clients``, the
number of overlays listening right now, ``managed``, ``problem`` and ``monitors``.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..cache import CacheStore
from ..errors import CacheError
from ..overlay_settings import (
    MONITOR_CONNECTOR,
    RULES,
    OverlaySettingError,
    OverlaySettings,
    clean,
    prune,
    salvage,
)

MAX_PREVIEW_S = 60.0
MAX_MONITORS = 16
MAX_MONITOR_DESCRIPTION = 120

Listener = Callable[[], None]


class OverlayConfig:
    def __init__(
        self,
        defaults: OverlaySettings,
        store: CacheStore | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._defaults = defaults
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._version = 1
        self._preview_until_ms = 0
        self._clients = 0
        self._managed = False
        self._problem = ""
        self._enabled = True
        self._monitors: list[dict[str, str]] = []
        self._overrides = self._load()

    # ------------------------------- storage ------------------------------- #

    def _load(self) -> dict[str, Any]:
        if self._store is None:
            return {}
        try:
            stored = self._store.read_json(self._store.overlay_json, {})
        except (CacheError, OSError) as exc:
            print(f"spotifixed: {exc}; starting without overlay overrides", file=sys.stderr)
            return {}
        if not isinstance(stored, dict):
            stored = {}
        self._enabled = stored.get("enabled") is not False  # anything but a stored False is on
        kept, notes = salvage(stored.get("overrides", {}))
        for note in notes:
            print(f"spotifixed: {note}", file=sys.stderr)
        return prune(self._defaults, kept)

    def _save(self, overrides: Mapping[str, Any], enabled: bool) -> None:
        if self._store is not None:
            document: dict[str, Any] = {"overrides": dict(overrides)}
            if not enabled:
                document["enabled"] = False
            self._store.write_json(self._store.overlay_json, document)

    # -------------------------------- reading ------------------------------- #

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def snapshot(self) -> dict[str, Any]:
        """The document as the event stream sends it."""
        with self._lock:
            return {
                "version": self._version,
                "defaults": self._defaults.to_dict(),
                "overrides": dict(self._overrides),
                "settings": self._defaults.merged(self._overrides).to_dict(),
                "preview_until_ms": self._preview_until_ms,
                "clients": self._clients,
                "enabled": self._enabled,
                "managed": self._managed,
                "problem": self._problem,
                "monitors": [dict(monitor) for monitor in self._monitors],
            }

    def describe(self) -> dict[str, Any]:
        """The snapshot plus what each setting accepts, for a client building a form."""
        return {**self.snapshot(), "rules": {key: rule.to_json() for key, rule in RULES.items()}}

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener`` after every change. Returns a function that unsubscribes."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    # -------------------------------- changing ------------------------------ #

    def _changed(self) -> list[Listener]:
        """Bump the version. Call with the lock held; run the result after releasing it."""
        self._version += 1
        return list(self._listeners)

    @staticmethod
    def _notify(listeners: list[Listener]) -> None:
        for listener in listeners:
            with contextlib.suppress(Exception):
                listener()

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Set overrides; a ``None`` value drops one. All of ``changes`` applies or none.

        Raises :class:`~spotifixed.overlay_settings.OverlaySettingError` for a value
        a setting cannot take, and :class:`OSError` or ``CacheError`` if the result
        cannot be saved (in which case nothing changes).
        """
        with self._lock:
            overrides = dict(self._overrides)
            for key, value in changes.items():
                if key not in RULES:
                    raise OverlaySettingError(f"{key!r} is not an overlay setting")
                if value is None:
                    overrides.pop(key, None)
                else:
                    overrides[key] = clean(key, value)
            overrides = prune(self._defaults, overrides)
            if overrides == self._overrides:
                listeners = []
            else:
                self._save(overrides, self._enabled)
                self._overrides = overrides
                listeners = self._changed()
        self._notify(listeners)
        return self.snapshot()

    def reset(self) -> dict[str, Any]:
        """Drop every override."""
        with self._lock:
            if self._overrides:
                self._save({}, self._enabled)
                self._overrides = {}
                listeners = self._changed()
            else:
                listeners = []
        self._notify(listeners)
        return self.snapshot()

    def preview(self, seconds: float) -> dict[str, Any]:
        """Ask overlays to show a sample line for ``seconds`` from now; 0 ends it.

        It expires by itself, so a page that is closed mid-preview does not leave the
        sample on screen: the page keeps a preview alive by asking again.
        """
        seconds = max(0.0, min(MAX_PREVIEW_S, seconds))
        with self._lock:
            self._preview_until_ms = int((self._clock() + seconds) * 1000) if seconds else 0
            listeners = self._changed()
        self._notify(listeners)
        return self.snapshot()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        """Switch the overlay on or off, for whatever follows the switch to act on.

        Raises :class:`~spotifixed.overlay_settings.OverlaySettingError` when nothing
        follows it, since the switch would then do nothing, and :class:`OSError` or
        ``CacheError`` if it cannot be saved (in which case nothing changes).
        """
        with self._lock:
            if not self._managed:
                raise OverlaySettingError(
                    "the overlay is not started by spotifixed here, so it cannot be "
                    "switched on or off from the page; start spotifixed with `start`"
                )
            if enabled == self._enabled:
                listeners = []
            else:
                self._save(self._overrides, enabled)
                self._enabled = enabled
                self._problem = ""
                listeners = self._changed()
        self._notify(listeners)
        return self.snapshot()

    def set_managed(self, managed: bool = True) -> None:
        """Say whether something now follows the switch (``start`` does)."""
        self._set(_managed=managed)

    def report_problem(self, problem: str) -> None:
        """Tell the page why the overlay is not running though it is switched on."""
        self._set(_problem=problem)

    def _set(self, **fields: Any) -> None:
        with self._lock:
            if all(getattr(self, name) == value for name, value in fields.items()):
                return
            for name, value in fields.items():
                setattr(self, name, value)
            listeners = self._changed()
        self._notify(listeners)

    def set_monitors(self, monitors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Record the monitors an overlay can see, as ``{connector, description}`` items.

        Raises :class:`~spotifixed.overlay_settings.OverlaySettingError` for a list that
        is not one, so a confused client cannot put arbitrary text on the page.
        """
        self._set(_monitors=clean_monitors(monitors))
        return self.snapshot()

    def attach(self) -> None:
        """An overlay started listening."""
        self._count(1)

    def detach(self) -> None:
        self._count(-1)

    def _count(self, step: int) -> None:
        with self._lock:
            self._clients = max(0, self._clients + step)
            if not self._clients:
                self._monitors = []  # nothing is left to say what it can see
            listeners = self._changed()
        self._notify(listeners)


def clean_monitors(monitors: Any) -> list[dict[str, str]]:
    """``monitors`` as stored, or :class:`OverlaySettingError` if it is not a monitor list."""
    if not isinstance(monitors, list) or len(monitors) > MAX_MONITORS:
        raise OverlaySettingError(f"monitors must be a list of at most {MAX_MONITORS}")
    cleaned = []
    for item in monitors:
        connector = item.get("connector") if isinstance(item, dict) else None
        description = item.get("description", "") if isinstance(item, dict) else None
        if not isinstance(connector, str) or not MONITOR_CONNECTOR.fullmatch(connector):
            raise OverlaySettingError(f"each monitor needs an output connector; got {item!r}")
        if (
            not isinstance(description, str)
            or len(description) > MAX_MONITOR_DESCRIPTION
            or not description.isprintable()
        ):
            raise OverlaySettingError(f"a monitor's description must be short text; got {item!r}")
        cleaned.append({"connector": connector, "description": description})
    return cleaned
