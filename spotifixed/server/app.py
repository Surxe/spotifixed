"""Wiring for the daemon: settings to store to player to bus to HTTP server.

:class:`Daemon` is the HTTP side and takes any :class:`Player`, which is how the tests
run it with a fake. :func:`run` is the real thing: it starts mpv, serves until asked to
stop, and takes mpv down with it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import tempfile
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..cache import CacheStore
from ..config import Settings
from ..overlay_settings import OverlaySettings
from ..playback.base import Player
from ..playback.mpv import MpvPlayer
from ..playback.state import StateBus
from ..shortcut_settings import from_settings as shortcuts_from
from . import sse
from .library import Library
from .overlay_config import OverlayConfig
from .routes import EVENTS_PATH, MAX_BODY_BYTES, OVERLAY_CLIENT_HEADER, Response, Routes
from .security import allowed_hosts, check_request
from .session import PlaybackSession

# Sent with every response. No CORS header appears anywhere, on purpose.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True  # an open event stream must not keep the process alive
    allow_reuse_address = True


class Daemon:
    def __init__(
        self,
        settings: Settings,
        store: CacheStore,
        player: Player,
        *,
        port: int | None = None,
        keepalive_s: float = sse.KEEPALIVE_S,
        log: bool = False,
    ) -> None:
        self.player = player
        self.overlay = OverlayConfig(OverlaySettings.from_settings(settings), store)
        library = Library(store)
        self.session = PlaybackSession(store, library, player)
        self.routes = Routes(
            library,
            player,
            self.session,
            self.overlay,
            shortcuts=shortcuts_from(settings),
            media_keys=bool(settings.mpv_mpris_plugin),
        )
        self.keepalive_s = keepalive_s
        self.stopping = threading.Event()
        self._streams = 0
        self._streams_lock = threading.Lock()
        self._log = log
        self._httpd = _Server(
            (settings.host, settings.port if port is None else port), self._handler_class()
        )
        self.host = settings.host
        self.port = self._httpd.server_address[1]
        self._hosts = allowed_hosts(self.host, self.port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def open_streams(self) -> int:
        with self._streams_lock:
            return self._streams

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        """Stop accepting requests and let open event streams end."""
        self.stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self.session.flush()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "spotifixed"
            sys_version = ""

            def _send(self, response: Response) -> None:
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                for name, value in _SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(response.body)

            def _handle(self) -> None:
                refusal = check_request(self.command, self.headers, daemon._hosts)
                if refusal is not None:
                    self._send(Response.error(*refusal))
                    return
                path = urlsplit(self.path).path
                if self.command == "GET" and path == EVENTS_PATH:
                    self._stream()
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY_BYTES:
                    self._send(Response.error(413, "request body too large"))
                    return
                body = self.rfile.read(length) if length else b""
                self._send(daemon.routes.dispatch(self.command, path, body))

            def _stream(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                for name, value in _SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                # An overlay says who it is, so the page can tell whether one is running.
                is_overlay = self.headers.get(OVERLAY_CLIENT_HEADER) == "overlay"
                with daemon._streams_lock:
                    daemon._streams += 1
                if is_overlay:
                    daemon.overlay.attach()
                try:
                    sse.stream_states(
                        self.wfile,
                        daemon.player.bus,
                        daemon.stopping,
                        keepalive_s=daemon.keepalive_s,
                        overlay=daemon.overlay,
                        session=daemon.session,
                    )
                finally:
                    if is_overlay:
                        daemon.overlay.detach()
                    with daemon._streams_lock:
                        daemon._streams -= 1

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle  # noqa: N815

            def log_request(self, code: object = "-", size: object = "-") -> None:
                # Actions and refusals only; an access log of every read is noise.
                status = (
                    int(code) if str(int(code) if isinstance(code, int) else code).isdigit() else 0
                )
                if daemon._log and (self.command != "GET" or status >= 400):
                    sys.stderr.write(f"{self.command} {urlsplit(self.path).path} {status}\n")

            def log_message(self, format: str, *args: object) -> None:
                pass  # the standard library's own chatter (malformed requests and the like)

        return Handler


def runtime_dir() -> Path:
    """Where the mpv socket lives: private to the user and gone at logout."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and Path(base).is_dir():
        return Path(base) / "spotifixed"
    return Path(tempfile.gettempdir()) / f"spotifixed-{os.getuid()}"


def run(settings: Settings, *, on_ready: Callable[[Daemon], None] | None = None) -> int:
    """Run the daemon in the foreground until SIGINT, SIGTERM or SIGHUP.

    ``on_ready`` is called once the daemon is listening, just before it starts serving.
    """
    store = CacheStore(settings.cache_dir, settings.state_dir)
    # A bad overlay or shortcut setting fails before mpv starts.
    OverlaySettings.from_settings(settings)
    shortcuts_from(settings)
    player = MpvPlayer(
        StateBus(),
        socket_path=runtime_dir() / f"mpv-{settings.port}.sock",
        mpv_bin=settings.mpv_bin,
        audio_output=settings.mpv_audio_output,
        mpris_plugin=settings.mpv_mpris_plugin,
    )
    player.start()
    try:
        daemon = Daemon(settings, store, player, log=True)
    except OSError as exc:
        player.close()
        print(f"spotifixed: cannot listen on {settings.base_url}: {exc}", file=sys.stderr)
        return 1
    except BaseException:
        player.close()  # never leave mpv running behind a daemon that did not start
        raise
    daemon.session.resume()

    def stop(_signum: int, _frame: object) -> None:
        # shutdown() blocks until serve_forever returns, so it cannot run on this thread.
        threading.Thread(target=daemon.shutdown, daemon=True).start()

    # SIGHUP is what closing the terminal sends. Left alone it would end this process
    # on the spot, with nothing saved and mpv still running with nobody to control it.
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, stop)
    print(f"spotifixed daemon on {daemon.url} (cache: {settings.cache_dir})", flush=True)
    try:
        if on_ready is not None:
            on_ready(daemon)
        daemon.serve_forever()
    finally:
        with contextlib.suppress(Exception):
            player.close()
    return 0
