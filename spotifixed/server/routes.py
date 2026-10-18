"""The REST API (v1) and the static page: a request in, a :class:`Response` out.

No sockets here. ``Routes.dispatch`` takes a method, a path and a body and returns what
to send, which is what lets the whole API be tested by calling it. The event stream is
the one endpoint that is not request/response and is handled by the server itself.

Path parameters are IDs. They are matched loosely here (anything without a slash) and
validated by the cache store before they are used, so a malformed ID is a 400, not a
404, and never a path.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import CacheError, InvalidIdError, NotInCacheError, PlaybackError
from ..overlay_settings import OverlaySettingError
from ..playback.base import Player
from ..shortcut_settings import MEDIA_KEYS, Shortcut
from .library import Library
from .overlay_config import MAX_PREVIEW_S, OverlayConfig
from .security import JSON_TYPE
from .session import PlaybackSession, SettingError

API = "/api/v1"
EVENTS_PATH = f"{API}/events"
# Sent by the overlay when it opens the event stream.
OVERLAY_CLIENT_HEADER = "X-Spotifixed-Client"
MAX_BODY_BYTES = 64 * 1024

_STATIC_DIR = Path(__file__).parent / "static"
# An allowlist, not a directory listing: a request path is never joined onto a directory.
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
    "/static/clock.js": ("clock.js", "text/javascript; charset=utf-8"),
    "/static/overlay.js": ("overlay.js", "text/javascript; charset=utf-8"),
    "/static/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
_IMAGE_TYPES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"RIFF", "image/webp"),
)
_TRANSPORT = ("play", "pause", "toggle", "next", "prev", "stop")


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    content_type: str = JSON_TYPE

    @classmethod
    def json(cls, data: Any, status: int = 200) -> Response:
        return cls(status, json.dumps(data, separators=(",", ":")).encode("utf-8"))

    @classmethod
    def error(cls, status: int, message: str) -> Response:
        return cls.json({"error": message}, status)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _number(body: dict[str, Any], key: str) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ApiError(400, f"{key} must be a number")
    return float(value)


Handler = Callable[..., Response]


class Routes:
    def __init__(
        self,
        library: Library,
        player: Player,
        session: PlaybackSession,
        overlay: OverlayConfig | None = None,
        shortcuts: Sequence[Shortcut] = (),
        *,
        media_keys: bool = False,
    ) -> None:
        self._library = library
        self._shortcuts = tuple(shortcuts)
        self._media_keys = MEDIA_KEYS if media_keys else ()
        self._player = player
        self._session = session
        self._overlay = overlay
        # Read once, here: the page must match the code that answers it. Reading on each
        # request would hand out a newer page than this process after an upgrade in place.
        self._static = {
            path: (_STATIC_DIR / name).read_bytes() for path, (name, _type) in _STATIC.items()
        }
        segment = r"([^/]+)"
        self._table: list[tuple[str, re.Pattern[str], Handler]] = [
            ("GET", re.compile(rf"{API}/playlists"), self._playlists),
            ("GET", re.compile(rf"{API}/playlists/{segment}"), self._playlist),
            ("GET", re.compile(rf"{API}/playlists/{segment}/cover"), self._playlist_cover),
            ("GET", re.compile(rf"{API}/tracks/{segment}"), self._track),
            ("GET", re.compile(rf"{API}/tracks/{segment}/lyrics"), self._lyrics),
            ("GET", re.compile(rf"{API}/tracks/{segment}/cover"), self._track_cover),
            ("GET", re.compile(rf"{API}/state"), self._state),
            ("POST", re.compile(rf"{API}/player/load"), self._load),
            ("POST", re.compile(rf"{API}/player/({'|'.join(_TRANSPORT)})"), self._transport),
            ("POST", re.compile(rf"{API}/player/seek"), self._seek),
            ("POST", re.compile(rf"{API}/player/volume"), self._volume),
            ("POST", re.compile(rf"{API}/tracks/{segment}/offset"), self._offset),
            ("GET", re.compile(rf"{API}/session"), self._session_get),
            ("GET", re.compile(rf"{API}/shortcuts"), self._shortcuts_get),
            ("POST", re.compile(rf"{API}/session/settings"), self._session_settings),
            ("POST", re.compile(rf"{API}/playlists/{segment}/shuffle"), self._shuffle),
            ("POST", re.compile(rf"{API}/playlists/{segment}/unshuffle"), self._unshuffle),
        ]
        if overlay is not None:
            self._table += [
                ("GET", re.compile(rf"{API}/overlay"), self._overlay_get),
                ("POST", re.compile(rf"{API}/overlay"), self._overlay_update),
                ("POST", re.compile(rf"{API}/overlay/reset"), self._overlay_reset),
                ("POST", re.compile(rf"{API}/overlay/preview"), self._overlay_preview),
                ("POST", re.compile(rf"{API}/overlay/enabled"), self._overlay_enabled),
                ("POST", re.compile(rf"{API}/overlay/monitors"), self._overlay_monitors),
            ]

    def dispatch(self, method: str, path: str, body: bytes = b"") -> Response:
        """Answer one request. Never raises: every failure becomes an error response."""
        try:
            if method == "GET" and path in _STATIC:
                return Response(200, self._static[path], _STATIC[path][1])
            allowed = set()
            for route_method, pattern, handler in self._table:
                match = pattern.fullmatch(path)
                if not match:
                    continue
                if route_method != method:
                    allowed.add(route_method)
                    continue
                if method == "POST":
                    return handler(*match.groups(), self._json_body(body))
                return handler(*match.groups())
            if allowed or path == EVENTS_PATH:
                return Response.error(405, f"{method} is not allowed here")
            return Response.error(404, "no such endpoint")
        except ApiError as exc:
            return Response.error(exc.status, exc.message)
        except InvalidIdError as exc:
            return Response.error(400, str(exc))
        except NotInCacheError as exc:
            return Response.error(404, str(exc))
        except (OverlaySettingError, SettingError) as exc:
            return Response.error(400, str(exc))
        except (CacheError, OSError) as exc:
            return Response.error(500, f"could not read or write the cache: {exc}")
        except PlaybackError as exc:
            return Response.error(503, f"player unavailable: {exc}")

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        if not body.strip():
            return {}
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "the request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "the request body must be a JSON object")
        return data

    @staticmethod
    def _image(path: Path) -> Response:
        data = path.read_bytes()
        for magic, content_type in _IMAGE_TYPES:
            if data.startswith(magic):
                return Response(200, data, content_type)
        raise NotInCacheError("the cached cover is not an image")

    # --------------------------------- reads -------------------------------- #

    def _playlists(self) -> Response:
        return Response.json({"playlists": self._library.playlists()})

    def _playlist(self, playlist_id: str) -> Response:
        return Response.json(self._session.arranged(self._library.playlist(playlist_id)))

    def _playlist_cover(self, playlist_id: str) -> Response:
        return self._image(self._library.playlist_cover(playlist_id))

    def _track(self, track_id: str) -> Response:
        return Response.json(self._library.track(track_id))

    def _lyrics(self, track_id: str) -> Response:
        return Response.json(self._library.lyrics(track_id))

    def _track_cover(self, track_id: str) -> Response:
        return self._image(self._library.track_cover(track_id))

    def _state(self) -> Response:
        return Response.json(self._player.bus.current.to_dict())

    # -------------------------------- actions ------------------------------- #

    def _ok(self) -> Response:
        # The state that results arrives on the event stream; this only acknowledges.
        return Response.json({"ok": True})

    def _load(self, body: dict[str, Any]) -> Response:
        playlist_id = body.get("playlist_id")
        start = body.get("start_track_id")
        if not isinstance(playlist_id, str):
            raise ApiError(400, "playlist_id is required")
        if start is not None and not isinstance(start, str):
            raise ApiError(400, "start_track_id must be a track ID")
        queue = self._session.queue_for(playlist_id, start_track_id=start)
        self._player.load_queue(queue)
        return Response.json({"ok": True, "queue_length": len(queue)})

    def _transport(self, verb: str, _body: dict[str, Any]) -> Response:
        getattr(self._player, verb)()
        return self._ok()

    def _seek(self, body: dict[str, Any]) -> Response:
        if ("position_s" in body) == ("delta_s" in body):
            raise ApiError(400, "give exactly one of position_s and delta_s")
        if "position_s" in body:
            position = _number(body, "position_s")
            if position < 0:
                raise ApiError(400, "position_s must not be negative")
            self._player.seek(position_s=position)
        else:
            self._player.seek(delta_s=_number(body, "delta_s"))
        return self._ok()

    def _volume(self, body: dict[str, Any]) -> Response:
        if ("volume" in body) == ("delta" in body):
            raise ApiError(400, "give exactly one of volume and delta")
        if "volume" in body:
            volume = _number(body, "volume")
            if not 0 <= volume <= 100:
                raise ApiError(400, "volume must be between 0 and 100")
        else:
            # Relative, for the volume shortcuts; it stops at the ends rather than failing.
            current = self._player.bus.current.volume
            volume = max(0.0, min(100.0, current + _number(body, "delta")))
        self._player.set_volume(volume)
        return self._ok()

    def _offset(self, track_id: str, body: dict[str, Any]) -> Response:
        offset = body.get("offset_ms")
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ApiError(400, "offset_ms must be a whole number of milliseconds")
        stored = self._library.set_offset(track_id, offset)
        self._player.set_offset(track_id, stored)
        return Response.json({"ok": True, "track_id": track_id, "offset_ms": stored})

    # -------------------------------- overlay ------------------------------- #

    def _overlay_get(self) -> Response:
        assert self._overlay is not None
        return Response.json(self._overlay.describe())

    def _overlay_update(self, body: dict[str, Any]) -> Response:
        """Set overrides from ``{setting: value}``; a ``null`` value drops that override."""
        assert self._overlay is not None
        return Response.json(self._overlay.update(body))

    def _overlay_reset(self, _body: dict[str, Any]) -> Response:
        assert self._overlay is not None
        return Response.json(self._overlay.reset())

    def _overlay_preview(self, body: dict[str, Any]) -> Response:
        assert self._overlay is not None
        seconds = _number(body, "seconds")
        if not 0 <= seconds <= MAX_PREVIEW_S:
            raise ApiError(400, f"seconds must be between 0 and {MAX_PREVIEW_S:g}")
        return Response.json(self._overlay.preview(seconds))

    def _overlay_enabled(self, body: dict[str, Any]) -> Response:
        """Switch the overlay on or off: ``{"enabled": true|false}``."""
        assert self._overlay is not None
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(400, "enabled must be true or false")
        return Response.json(self._overlay.set_enabled(enabled))

    def _overlay_monitors(self, body: dict[str, Any]) -> Response:
        """The overlay reports the monitors it can see: ``{"monitors": [...]}``."""
        assert self._overlay is not None
        return Response.json(self._overlay.set_monitors(body.get("monitors")))

    # ------------------------------- session ------------------------------- #

    def _session_get(self) -> Response:
        return Response.json(self._session.document())

    def _shortcuts_get(self) -> Response:
        """The keyboard shortcuts this daemon was configured with, and the media keys if on."""
        return Response.json(
            {
                "shortcuts": [shortcut.to_dict() for shortcut in self._shortcuts],
                "media_keys": [
                    {"keys": keys, "description": what} for keys, what in self._media_keys
                ],
            }
        )

    def _session_settings(self, body: dict[str, Any]) -> Response:
        """Change playback settings from ``{setting: value}``."""
        return Response.json(self._session.set_settings(body))

    def _shuffle(self, playlist_id: str, _body: dict[str, Any]) -> Response:
        """Put a playlist in a new random order, which it keeps."""
        self._session.shuffle(playlist_id)
        return Response.json({"ok": True, "shuffled": True})

    def _unshuffle(self, playlist_id: str, _body: dict[str, Any]) -> Response:
        """Put a playlist back in its own order."""
        self._session.restore(playlist_id)
        return Response.json({"ok": True, "shuffled": False})
