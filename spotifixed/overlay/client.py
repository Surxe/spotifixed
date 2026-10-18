"""The overlay's link to the daemon: follow the event stream, fetch lyric lines.

One thread. It connects to the event stream, feeds each playback snapshot to the model,
and when the model says a track's lines are missing it fetches them over the REST API
before reading on. The same stream carries the overlay's own settings as ``overlay``
events, which are handed to ``on_overlay`` as they arrive. Each time it connects, and
whenever :meth:`DaemonClient.report_monitors` is called, it tells the daemon which
monitors the display has, so the page can offer them by name. If the daemon is not
running, or goes away, the model is told it is disconnected (which hides the overlay)
and the thread keeps retrying with a growing pause between attempts.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import sys
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from ..models import LyricLine, PlaybackState
from .presenter import OverlayModel

EVENTS_PATH = "/api/v1/events"
MONITORS_PATH = "/api/v1/overlay/monitors"
# Tells the daemon this stream is an overlay, so the page can say whether one is running.
CLIENT_HEADER = {"X-Spotifixed-Client": "overlay"}
# The daemon sends a keepalive comment every 15 s; silence much longer than that means
# the connection is dead even though nothing closed it.
READ_TIMEOUT_S = 40.0


class DaemonClient:
    def __init__(
        self,
        base_url: str,
        model: OverlayModel,
        *,
        on_change: Callable[[], None],
        on_overlay: Callable[[dict[str, Any]], None] | None = None,
        monitors: Callable[[], list[dict[str, str]]] | None = None,
        reconnect_s: tuple[float, float] = (0.5, 5.0),
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        parts = urlsplit(base_url)
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or 80
        self._model = model
        self._on_change = on_change
        self._on_overlay = on_overlay
        self._monitors = monitors
        self._reconnect_first_s, self._reconnect_max_s = reconnect_s
        self._read_timeout_s = read_timeout_s
        self._stopping = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="overlay-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        current = self._socket
        if current is not None:
            with contextlib.suppress(OSError):
                current.shutdown(socket.SHUT_RDWR)  # wakes the blocked read
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        delay = self._reconnect_first_s
        while not self._stopping.is_set():
            try:
                self._follow()
                delay = self._reconnect_first_s  # it was a real connection; start over
            except (OSError, http.client.HTTPException, ValueError):
                pass
            self._model.set_connected(False)
            self._on_change()
            if self._stopping.wait(delay):
                return
            delay = min(delay * 2, self._reconnect_max_s)

    def _follow(self) -> None:
        """Read the event stream until it ends. Raises when it cannot be read at all."""
        connection = http.client.HTTPConnection(
            self._host, self._port, timeout=self._read_timeout_s
        )
        try:
            connection.request(
                "GET", EVENTS_PATH, headers={"Accept": "text/event-stream", **CLIENT_HEADER}
            )
            self._socket = connection.sock
            response = connection.getresponse()
            if response.status != 200:
                raise OSError(f"event stream answered HTTP {response.status}")
            self._model.set_connected(True)
            self._on_change()
            # The daemon counts this overlay once the stream answers, and forgets what
            # an overlay reported when the last one leaves, so report after that.
            self.send_monitors()

            versions = {"state": 0, "overlay": 0}
            event = "message"
            data: list[str] = []
            for raw in response:
                if self._stopping.is_set():
                    return
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line:
                    name, payload = event, "\n".join(data)
                    event, data = "message", []
                    if not payload or name not in versions:
                        continue  # a keepalive, or a kind of event this version ignores
                    document = json.loads(payload)
                    version = document.get("version") if isinstance(document, dict) else None
                    if not isinstance(version, int) or version <= versions[name]:
                        continue  # malformed, stale or repeated
                    versions[name] = version
                    if name == "state":
                        self._apply(PlaybackState.from_dict(document))
                    elif self._on_overlay is not None:
                        try:
                            self._on_overlay(document)
                        except Exception as exc:
                            # Settings that cannot be applied must not end this thread:
                            # it is also what keeps the lyrics following playback.
                            print(f"spotifixed: overlay settings: {exc}", file=sys.stderr)
                        self._on_change()
        finally:
            self._socket = None
            connection.close()

    def _apply(self, state: PlaybackState) -> None:
        self._model.set_state(state)
        wanted = self._model.lyrics_wanted()
        if wanted is not None:
            self._model.set_lyrics(wanted, self.fetch_lines(wanted))
        self._on_change()

    def report_monitors(self) -> None:
        """The display's monitors changed: tell the daemon, without blocking the caller."""
        threading.Thread(target=self.send_monitors, name="overlay-monitors", daemon=True).start()

    def send_monitors(self) -> None:
        """Tell the daemon which monitors there are. A failure is left to the next connect."""
        if self._monitors is None:
            return
        body = json.dumps({"monitors": self._monitors()}).encode()
        connection = http.client.HTTPConnection(self._host, self._port, timeout=5.0)
        try:
            headers = {"Content-Type": "application/json"}
            connection.request("POST", MONITORS_PATH, body=body, headers=headers)
            response = connection.getresponse()
            response.read()
            if response.status != 200:
                print(f"spotifixed: monitors not reported: HTTP {response.status}", file=sys.stderr)
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()

    def fetch_lines(self, track_id: str) -> tuple[LyricLine, ...]:
        """The track's timed lines from the API; empty when there are none or it fails."""
        connection = http.client.HTTPConnection(self._host, self._port, timeout=5.0)
        try:
            connection.request("GET", f"/api/v1/tracks/{track_id}/lyrics")
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                return ()
            lines = json.loads(body).get("lines") or []
            return tuple(LyricLine.from_dict(line) for line in lines)
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            return ()
        finally:
            connection.close()
