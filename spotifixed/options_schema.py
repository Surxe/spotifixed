"""The settings schema: every user-tunable setting, in one place.

``config.py`` resolves each option from (in priority order) a CLI argument, an
environment variable, a ``.env`` entry in the working directory, then the ``default``
below. The format is OptionsConfig's, so the same schema generates ``.env.example`` and
the Configuration section of ``docs/setup.md`` (run ``python build.py`` after editing
this file).

Two things are deliberately NOT here:

* Per-track values (timing offsets, audio pins). Those are variable-length, hand-authored
  data and live as JSON under the state directory.
* The tracklist Actor's snapshot option. Snapshots stay off, always; it is not a setting.
"""

from __future__ import annotations

from typing import Literal

_STORAGE = "Storage"
_TRACKLIST = "Tracklist (Apify)"
_SYNC = "Sync"
_DAEMON = "Daemon"
_OVERLAY = "Overlay"
_SHORTCUTS = "Shortcuts"

Anchor = Literal[
    "top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right",
    "center",
]  # fmt: skip

OPTIONS_SCHEMA = {
    # ------------------------------ Storage ------------------------------ #
    "CACHE_DIR": {
        "env": "SPOTIFIXED_CACHE_DIR",
        "arg": "--cache-dir",
        "type": str,
        "default": "cache",
        "section": _STORAGE,
        "help": (
            "Directory for everything fetched: tracklists, audio, lyrics, cover art. "
            "A relative path resolves against the repo root, not the working directory."
        ),
    },
    "STATE_DIR": {
        "env": "SPOTIFIXED_STATE_DIR",
        "arg": "--state-dir",
        "type": str,
        "default": "state",
        "section": _STORAGE,
        "help": (
            "Directory for hand-authored per-track values (timing offsets, audio pins). "
            "Kept apart from the cache so wiping the cache never loses a correction."
        ),
    },
    # ----------------------------- Tracklist ----------------------------- #
    "APIFY_TOKEN": {
        "env": "APIFY_TOKEN",
        # Present because the schema requires an arg name, but config.py never registers
        # it on the parser: a token on a command line is visible in the process list.
        "arg": "--apify-token",
        "type": str,
        "default": None,
        "section": _TRACKLIST,
        "sensitive": True,
        "help": (
            "Apify API token. Environment only, never a CLI argument and never a "
            "committed file. Needed solely to fetch a playlist's track list that is not "
            "cached yet; see secrets.env.example for where the real value lives."
        ),
    },
    "TRACKLIST_ACTOR": {
        "env": "SPOTIFIXED_TRACKLIST_ACTOR",
        "arg": "--tracklist-actor",
        "type": str,
        "default": "s-r~free-spotify-playlist-intelligence",
        "section": _TRACKLIST,
        "help": (
            "Apify Actor that returns a playlist's tracks, as user~name. The response "
            "shape is Actor-specific, so a new Actor also needs a parser."
        ),
    },
    "APIFY_MAX_CHARGE_USD": {
        "env": "SPOTIFIXED_APIFY_MAX_CHARGE_USD",
        "arg": "--apify-max-charge-usd",
        "type": float,
        "default": 0.05,
        "section": _TRACKLIST,
        "help": "Charge cap sent with each Actor run, in US dollars.",
    },
    "APIFY_BASE_URL": {
        "env": "SPOTIFIXED_APIFY_BASE_URL",
        "arg": "--apify-base-url",
        "type": str,
        "default": "https://api.apify.com",
        "section": _TRACKLIST,
        "help": "Apify API origin. Tests point this at a local stub.",
    },
    # -------------------------------- Sync ------------------------------- #
    "LRCLIB_BASE_URL": {
        "env": "SPOTIFIXED_LRCLIB_BASE_URL",
        "arg": "--lrclib-base-url",
        "type": str,
        "default": "https://lrclib.net",
        "section": _SYNC,
        "help": "LRCLIB origin, the lyrics source.",
    },
    "AUDIO_DURATION_TOLERANCE_S": {
        "env": "SPOTIFIXED_AUDIO_DURATION_TOLERANCE_S",
        "arg": "--audio-duration-tolerance-s",
        "type": float,
        "default": 3.0,
        "section": _SYNC,
        "help": (
            "An audio candidate within this many seconds of Spotify's duration gets full "
            "marks for duration."
        ),
    },
    "AUDIO_DURATION_REJECT_S": {
        "env": "SPOTIFIXED_AUDIO_DURATION_REJECT_S",
        "arg": "--audio-duration-reject-s",
        "type": float,
        "default": 7.0,
        "section": _SYNC,
        "help": "An audio candidate further than this from Spotify's duration is rejected.",
    },
    "AUDIO_PREFER_EXPLICIT": {
        "env": "SPOTIFIXED_AUDIO_PREFER_EXPLICIT",
        "arg": "--audio-prefer-explicit",
        "type": bool,
        "default": True,
        "section": _SYNC,
        "help": (
            "Want the explicit recording of a song, not its clean edit. align listens for "
            "the lyrics' strong words (with Whisper, from requirements-align.txt), and sync "
            "replaces audio in which they were not heard. Off: clean edits are kept, and "
            "align does not load Whisper."
        ),
    },
    "LYRICS_DURATION_TOLERANCE_S": {
        "env": "SPOTIFIXED_LYRICS_DURATION_TOLERANCE_S",
        "arg": "--lyrics-duration-tolerance-s",
        "type": float,
        "default": 2.0,
        "section": _SYNC,
        "help": (
            "A lyrics record further than this from the audio's real duration is still "
            "kept, but flagged as low timing confidence."
        ),
    },
    "SYNC_WORKERS": {
        "env": "SPOTIFIXED_SYNC_WORKERS",
        "arg": "--sync-workers",
        "type": int,
        "default": 2,
        "section": _SYNC,
        "help": "Tracks processed in parallel. Keep low to stay polite to the sources.",
    },
    "MISS_RETRY_DAYS": {
        "env": "SPOTIFIXED_MISS_RETRY_DAYS",
        "arg": "--miss-retry-days",
        "type": int,
        "default": 14,
        "section": _SYNC,
        "help": "A recorded miss (no lyrics, no audio match) is retried after this many days.",
    },
    # ------------------------------- Daemon ------------------------------ #
    "HOST": {
        "env": "SPOTIFIXED_HOST",
        "arg": "--host",
        "type": str,
        "default": "127.0.0.1",
        "section": _DAEMON,
        "help": "Address the daemon binds. Anything other than loopback is refused.",
    },
    "PORT": {
        "env": "SPOTIFIXED_PORT",
        "arg": "--port",
        "type": int,
        "default": 8765,
        "section": _DAEMON,
        "help": "Port for the daemon's HTTP API and remote-control page.",
    },
    "MPV_BIN": {
        "env": "SPOTIFIXED_MPV_BIN",
        "arg": "--mpv-bin",
        "type": str,
        "default": "mpv",
        "section": _DAEMON,
        "help": "mpv executable.",
    },
    "MPV_AUDIO_OUTPUT": {
        "env": "SPOTIFIXED_MPV_AUDIO_OUTPUT",
        "arg": "--mpv-audio-output",
        "type": str,
        "default": "",
        "section": _DAEMON,
        "help": "mpv --ao value. Empty lets mpv choose; tests use `null` for silence.",
    },
    "MPV_MPRIS_PLUGIN": {
        "env": "SPOTIFIXED_MPV_MPRIS_PLUGIN",
        "arg": "--mpv-mpris-plugin",
        "type": str,
        "default": "",
        "section": _DAEMON,
        "help": (
            "Path to mpv's MPRIS plugin (Debian: /usr/lib/mpv-mpris/mpris.so) so desktop "
            "media keys control playback. Empty disables it."
        ),
    },
    # ------------------------------- Overlay ----------------------------- #
    "OVERLAY_MONITOR": {
        "env": "SPOTIFIXED_OVERLAY_MONITOR",
        "arg": "--overlay-monitor",
        "type": str,
        "default": "",
        "section": _OVERLAY,
        "help": "Output connector to show the overlay on, e.g. DP-2. Empty: compositor's choice.",
    },
    "OVERLAY_ANCHOR": {
        "env": "SPOTIFIXED_OVERLAY_ANCHOR",
        "arg": "--overlay-anchor",
        "type": Anchor,
        "default": "bottom",
        "section": _OVERLAY,
        "help": "Screen edge or corner the overlay is attached to.",
    },
    "OVERLAY_MARGIN_X": {
        "env": "SPOTIFIXED_OVERLAY_MARGIN_X",
        "arg": "--overlay-margin-x",
        "type": int,
        "default": 0,
        "section": _OVERLAY,
        "help": "Horizontal distance in pixels from the anchored edge.",
    },
    "OVERLAY_MARGIN_Y": {
        "env": "SPOTIFIXED_OVERLAY_MARGIN_Y",
        "arg": "--overlay-margin-y",
        "type": int,
        "default": 120,
        "section": _OVERLAY,
        "help": "Vertical distance in pixels from the anchored edge.",
    },
    "OVERLAY_MAX_WIDTH": {
        "env": "SPOTIFIXED_OVERLAY_MAX_WIDTH",
        "arg": "--overlay-max-width",
        "type": int,
        "default": 1200,
        "section": _OVERLAY,
        "help": "Maximum line width in pixels; longer lines wrap.",
    },
    "OVERLAY_FONT": {
        "env": "SPOTIFIXED_OVERLAY_FONT",
        "arg": "--overlay-font",
        "type": str,
        "default": "Sans Bold",
        "section": _OVERLAY,
        "help": "Font family and style.",
    },
    "OVERLAY_FONT_SIZE": {
        "env": "SPOTIFIXED_OVERLAY_FONT_SIZE",
        "arg": "--overlay-font-size",
        "type": int,
        "default": 32,
        "section": _OVERLAY,
        "help": "Font size in points.",
    },
    "OVERLAY_COLOR": {
        "env": "SPOTIFIXED_OVERLAY_COLOR",
        "arg": "--overlay-color",
        "type": str,
        "default": "#FFFFFF",
        "section": _OVERLAY,
        "help": "Text colour.",
    },
    "OVERLAY_OUTLINE_COLOR": {
        "env": "SPOTIFIXED_OVERLAY_OUTLINE_COLOR",
        "arg": "--overlay-outline-color",
        "type": str,
        "default": "#000000",
        "section": _OVERLAY,
        "help": "Outline colour, for contrast against any background.",
    },
    "OVERLAY_LINES": {
        "env": "SPOTIFIXED_OVERLAY_LINES",
        "arg": "--overlay-lines",
        "type": int,
        "default": 1,
        "section": _OVERLAY,
        "help": "1 shows the current line; 2 adds the next line, dimmed.",
    },
    "OVERLAY_LEAD_MS": {
        "env": "SPOTIFIXED_OVERLAY_LEAD_MS",
        "arg": "--overlay-lead-ms",
        "type": int,
        "default": 150,
        "section": _OVERLAY,
        "help": "Show each line this many milliseconds before its timestamp.",
    },
    "OVERLAY_FADE_MS": {
        "env": "SPOTIFIXED_OVERLAY_FADE_MS",
        "arg": "--overlay-fade-ms",
        "type": int,
        "default": 120,
        "section": _OVERLAY,
        "help": "Fade duration when a line appears or disappears.",
    },
    "OVERLAY_UNDERLINE_WORD": {
        "env": "SPOTIFIXED_OVERLAY_UNDERLINE_WORD",
        "arg": "--overlay-underline-word",
        "type": bool,
        "default": False,
        "section": _OVERLAY,
        "help": (
            "Underline the word being sung, in the overlay and on the page. The word is "
            "estimated from the line timing, so it needs the track's offset to be right."
        ),
    },
    "OVERLAY_VOLUME_INDICATOR": {
        "env": "SPOTIFIXED_OVERLAY_VOLUME_INDICATOR",
        "arg": "--overlay-volume-indicator",
        "type": bool,
        "default": True,
        "section": _OVERLAY,
        "help": (
            "Show spotifixed's volume in the overlay for a moment whenever it changes, "
            "above the lyric line, even while paused, and the level rising during the "
            "fade-in on resume."
        ),
    },
    # ------------------------------ Shortcuts ---------------------------- #
    "SHORTCUT_VOLUME_UP": {
        "env": "SPOTIFIXED_SHORTCUT_VOLUME_UP",
        "arg": "--shortcut-volume-up",
        "type": str,
        "default": "CTRL+XF86AudioRaiseVolume",
        "section": _SHORTCUTS,
        "help": (
            "Global shortcut that raises spotifixed's own volume, in the XDG shortcuts "
            "format (modifiers CTRL, ALT, SHIFT, NUM, LOGO joined by + to an xkb key name, "
            "e.g. CTRL+XF86AudioRaiseVolume or LOGO+Up). It is the trigger suggested to the "
            "desktop's global shortcuts portal, which may let the user change it. Empty "
            "disables it. Play, pause, next and previous are the keyboard's media keys "
            "(SPOTIFIXED_MPV_MPRIS_PLUGIN), not settings here."
        ),
    },
    "SHORTCUT_VOLUME_DOWN": {
        "env": "SPOTIFIXED_SHORTCUT_VOLUME_DOWN",
        "arg": "--shortcut-volume-down",
        "type": str,
        "default": "CTRL+XF86AudioLowerVolume",
        "section": _SHORTCUTS,
        "help": (
            "Global shortcut that lowers spotifixed's own volume, in the same format as "
            "SPOTIFIXED_SHORTCUT_VOLUME_UP. Empty disables it."
        ),
    },
    "SHORTCUT_SKIP_DISABLE": {
        "env": "SPOTIFIXED_SHORTCUT_SKIP_DISABLE",
        "arg": "--shortcut-skip-disable",
        "type": str,
        "default": "LOGO+XF86AudioNext",
        "section": _SHORTCUTS,
        "help": (
            "Global shortcut that skips the song that is on and turns it off, so it is not "
            "played again until it is turned back on in the page's track list. In the same "
            "format as SPOTIFIXED_SHORTCUT_VOLUME_UP. Empty disables it."
        ),
    },
    "SHORTCUT_VOLUME_STEP": {
        "env": "SPOTIFIXED_SHORTCUT_VOLUME_STEP",
        "arg": "--shortcut-volume-step",
        "type": int,
        "default": 5,
        "section": _SHORTCUTS,
        "help": "How much one press of a volume shortcut changes the volume, out of 100.",
    },
}
