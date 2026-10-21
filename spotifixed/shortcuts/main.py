"""Wiring for the ``shortcuts`` command: settings to bindings to the portal to the daemon.

A press is sent to the daemon on a worker thread, one at a time and in order, so the
GLib main loop never waits on HTTP and a burst of presses is applied as it was made.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

from ..config import Settings
from ..errors import HttpError, PlaylistPlayerError
from ..http import HttpClient
from ..shortcut_settings import Shortcut, from_settings


def _bound_line(shortcuts: list[Shortcut], bound: dict[str, str]) -> str:
    """One line for the terminal: which keys the desktop gave each shortcut."""
    parts = []
    for shortcut in shortcuts:
        keys = bound.get(shortcut.id)
        if keys is None:
            continue
        parts.append(f"{shortcut.description}: {keys or 'no key assigned'}")
    return "Keyboard shortcuts: " + "; ".join(parts) + "." if parts else ""


def run(settings: Settings) -> int:
    shortcuts = from_settings(settings)
    if not shortcuts:
        print("spotifixed: no keyboard shortcuts are set", flush=True)
        return 0
    try:
        from .portal import PortalError
        from .portal import run as hold
    except (ImportError, ValueError) as exc:
        raise PlaylistPlayerError(f"the GObject bindings are not available: {exc}") from exc

    client = HttpClient(timeout=5.0, retries=1)  # one attempt: a late change is a wrong one
    sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shortcut")

    def press(shortcut: Shortcut) -> None:
        sender.submit(send, shortcut)

    def send(shortcut: Shortcut) -> None:
        try:
            client.post_json(settings.base_url + shortcut.path, shortcut.body)
        except HttpError as exc:
            reason = "spotifixed is not running" if exc.status is None else exc.detail or exc
            print(f"spotifixed: the shortcut {shortcut.description!r} did nothing: {reason}",
                  file=sys.stderr, flush=True)  # fmt: skip
        except Exception as exc:  # on a worker thread nothing else would ever show it
            print(f"spotifixed: a shortcut failed: {exc!r}", file=sys.stderr, flush=True)

    def bound(triggers: dict[str, str]) -> None:
        line = _bound_line(shortcuts, triggers)
        if line:
            print(line, flush=True)

    try:
        hold(shortcuts, on_press=press, on_bound=bound)
    except PortalError as exc:
        raise PlaylistPlayerError(f"the keyboard shortcuts are not available: {exc}") from exc
    finally:
        sender.shutdown(wait=True, cancel_futures=True)
    return 0
