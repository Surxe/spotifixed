"""Command-line entry point: ``python -m spotifixed <command>``.

This module only parses arguments and wires objects together. Each command's work lives
in the package it belongs to, imported inside the handler so that, for example, ``sync``
never imports GTK and ``overlay`` never imports yt-dlp.

Commands:

* ``sync``     fetch a playlist's tracklist, audio, lyrics and cover art into the cache
* ``status``   show what is cached for a playlist
* ``pin``      choose the audio for a track by hand
* ``serve``    run the daemon: mpv, the HTTP API and the remote-control page
* ``ctl``      send a transport command to a running daemon
* ``overlay``  show the current lyric line above other windows
* ``shortcuts`` hold the global volume shortcuts and send their presses to the daemon
* ``start``    the whole application at once: ``serve``, ``overlay``, ``shortcuts`` and the page
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from . import __version__
from .config import Settings, add_settings_arguments, load_settings, settings_arguments
from .errors import PlaylistPlayerError

Handler = Callable[[argparse.Namespace, Settings], int]

# Exit status when a command did its work but the tracklist it worked from is incomplete.
_INCOMPLETE = 2


def _cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    from .cache import CacheStore, MissLog
    from .http import HttpClient
    from .sources.audio import YtDlpSource
    from .sources.lyrics import LrclibSource
    from .sources.tracklist import build_tracklist_source, playlist_id_from
    from .sync import report
    from .sync.audio import AudioStage
    from .sync.cover import CoverStage, fetch_playlist_cover
    from .sync.lyrics import LyricsStage
    from .sync.pipeline import Pipeline
    from .sync.tracklist import load_tracklist, tracklist_is_cached

    store = CacheStore(settings.cache_dir, settings.state_dir)
    http = HttpClient()
    playlist_id = playlist_id_from(args.playlist)

    def source_for(name: str):
        return build_tracklist_source(
            name,
            http=http,
            apify_base_url=settings.apify_base_url,
            apify_token=settings.require_apify_token,
            apify_max_charge_usd=settings.apify_max_charge_usd,
        )

    will_fetch = args.refresh_tracklist or not tracklist_is_cached(store, playlist_id)
    if will_fetch and args.dry_run:
        print(f"Playlist {playlist_id}: would fetch the tracklist (a paid Actor run).")
        print("  Nothing further can be planned until the tracklist is cached.")
        return 0
    if will_fetch:
        print(
            f"Fetching the tracklist for {playlist_id}: a paid Actor run "
            f"({settings.tracklist_actor}, capped at ${settings.apify_max_charge_usd:g}) ...",
            flush=True,
        )

    result = load_tracklist(
        store,
        playlist_id,
        source_for,
        settings.tracklist_actor,
        refresh=args.refresh_tracklist,
        write=not args.dry_run,
    )
    print("\n".join(report.tracklist_summary(result)))
    warning = report.shortfall_warning(result)
    if warning:
        print(f"spotifixed: {warning}", file=sys.stderr)
    status = _INCOMPLETE if warning else 0
    if args.only == "tracklist":
        return status

    # Stages in pipeline order: lyrics are chosen against the real audio's length.
    stages = {
        "audio": lambda: AudioStage(
            store,
            YtDlpSource(),
            tolerance_s=settings.audio_duration_tolerance_s,
            reject_s=settings.audio_duration_reject_s,
            prefer_explicit=settings.audio_prefer_explicit,
        ),
        "lyrics": lambda: LyricsStage(
            store,
            LrclibSource(http, base_url=settings.lrclib_base_url),
            tolerance_s=settings.lyrics_duration_tolerance_s,
        ),
        "cover": lambda: CoverStage(store, http),
    }
    wanted = [args.only] if args.only else list(stages)
    if "cover" in wanted and not args.dry_run:
        fetch_playlist_cover(store, http, result.tracklist.playlist)

    tracks = list(result.tracklist.tracks)
    if args.track:
        tracks = [track for track in tracks if track.id == args.track]
        if not tracks:
            raise PlaylistPlayerError(f"track {args.track} is not in playlist {playlist_id}")
    if args.limit is not None:
        tracks = tracks[: max(0, args.limit)]

    total = len(tracks)
    done = 0

    def on_track(track_report) -> None:
        nonlocal done
        done += 1
        print(report.progress_line(done, total, track_report), flush=True)

    pipeline = Pipeline(
        [stages[name]() for name in wanted],
        MissLog(store),
        miss_retry_days=settings.miss_retry_days,
        workers=settings.sync_workers,
        retry_misses=args.retry_misses,
        dry_run=args.dry_run,
        on_track=on_track,
    )
    reports = pipeline.run(tracks)
    print("\n".join(report.sync_totals(reports, wanted)))
    return status


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    from .cache import CacheStore, MissLog, assets
    from .sources.tracklist import playlist_id_from
    from .sync import report

    store = CacheStore(settings.cache_dir, settings.state_dir)
    if args.playlist is None:
        playlist_ids = store.playlist_ids()
        if not playlist_ids:
            print("No playlists are cached yet. Run: sync <playlist-url>")
            return 0
        for playlist_id in playlist_ids:
            playlist = assets.read_playlist(store, playlist_id)
            print(report.coverage_line(playlist, assets.playlist_status(store, playlist)))
        return 0

    playlist_id = playlist_id_from(args.playlist)
    if not store.has(store.playlist_json(playlist_id)):
        raise PlaylistPlayerError(f"playlist {playlist_id} is not cached; run sync on it first")
    playlist = assets.read_playlist(store, playlist_id)
    statuses = assets.playlist_status(store, playlist)
    print("\n".join(report.status_table(playlist, statuses, MissLog(store).all())))
    return 0


def _cmd_align(args: argparse.Namespace, settings: Settings) -> int:
    from .align import align_tracks, summary
    from .align.engine import NAME, TorchEngine
    from .cache import CacheStore, assets
    from .sources.tracklist import playlist_id_from

    store = CacheStore(settings.cache_dir, settings.state_dir)
    if args.track:
        track_ids = [args.track]
    else:
        playlist_ids = [playlist_id_from(args.playlist)] if args.playlist else store.playlist_ids()
        track_ids = []
        for playlist_id in playlist_ids:
            if not store.has(store.playlist_json(playlist_id)):
                raise PlaylistPlayerError(f"playlist {playlist_id} is not cached; sync it first")
            track_ids += assets.read_playlist(store, playlist_id).track_ids
    if args.limit is not None:
        track_ids = list(dict.fromkeys(track_ids))[: args.limit]

    def said(report) -> None:
        if report.outcome == "aligned":
            note = f"; {report.detail}" if report.detail else ""
            print(
                f"  {report.track_id}  {report.placed}/{report.lines} lines placed{note}",
                flush=True,
            )
        elif report.outcome == "failed":
            print(f"  {report.track_id}  FAILED: {report.detail}", file=sys.stderr, flush=True)

    reports = align_tracks(
        store,
        track_ids,
        TorchEngine,
        NAME,
        force=args.force,
        listen_for_clean_edits=settings.audio_prefer_explicit,
        on_report=said,
    )
    print(summary(reports))
    return 1 if any(report.outcome == "failed" for report in reports) else 0


def _cmd_pin(args: argparse.Namespace, settings: Settings) -> int:
    from .cache import CacheStore, MissLog
    from .sync.pin import pin_audio

    store = CacheStore(settings.cache_dir, settings.state_dir)
    url = None if args.url == "clear" else args.url
    track = pin_audio(store, MissLog(store), args.track, url)
    action = "cleared the audio pin for" if url is None else "pinned audio for"
    print(f"{action} {track.id} ({track.primary_artist} - {track.title})")
    print(f"  its cached audio and lyrics were removed; run: sync <playlist> --track {track.id}")
    return 0


def _cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    from .server.app import run

    return run(settings)


def _cmd_ctl(args: argparse.Namespace, settings: Settings) -> int:
    from .server.ctl import send

    send(settings.base_url, args.verb, args.value)
    return 0


def _cmd_overlay(args: argparse.Namespace, settings: Settings) -> int:
    from .overlay.main import run

    return run(settings, demo=args.demo, snapshot=args.snapshot)


def _cmd_shortcuts(args: argparse.Namespace, settings: Settings) -> int:
    from .shortcuts.main import run

    return run(settings)


def _cmd_start(args: argparse.Namespace, settings: Settings) -> int:
    from .launch import run

    return run(
        settings,
        settings_argv=settings_arguments(args),
        overlay=not args.no_overlay,
        shortcuts=not args.no_shortcuts,
        open_page=not args.no_browser,
    )


def build_parser() -> argparse.ArgumentParser:
    # Settings flags are shared by every command, so they hang off a parent parser.
    common = argparse.ArgumentParser(add_help=False)
    add_settings_arguments(common.add_argument_group("settings"))

    parser = argparse.ArgumentParser(
        prog="spotifixed",
        description=(
            "Cache a Spotify playlist's audio and synced lyrics, play it, and overlay the lyrics."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name: str, summary: str, handler: Handler) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=summary, description=summary, parents=[common])
        sub.set_defaults(handler=handler)
        return sub

    sync = add("sync", "Fetch tracklist, audio, lyrics and cover art into the cache.", _cmd_sync)
    sync.add_argument("playlist", help="playlist URL, spotify:playlist:ID URI, or bare ID")
    sync.add_argument("--limit", type=int, metavar="N", help="process only the first N tracks")
    sync.add_argument(
        "--only",
        choices=("tracklist", "audio", "lyrics", "cover"),
        help="run a single stage (the tracklist is always loaded first)",
    )
    sync.add_argument("--track", metavar="ID", help="process a single track")
    sync.add_argument("--retry-misses", action="store_true", help="retry stages recorded as misses")
    sync.add_argument(
        "--refresh-tracklist", action="store_true", help="re-fetch the tracklist (a paid Actor run)"
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="report what would be done; write nothing"
    )

    align = add(
        "align",
        "Time each word of the synced lyrics against the audio (needs requirements-align.txt).",
        _cmd_align,
    )
    align.add_argument("playlist", nargs="?", help="playlist to align (default: every cached one)")
    align.add_argument("--track", metavar="ID", help="align a single track")
    align.add_argument("--limit", type=int, metavar="N", help="look at only the first N tracks")
    align.add_argument("--force", action="store_true", help="align again what is already aligned")

    status = add("status", "Show what is cached for a playlist.", _cmd_status)
    status.add_argument(
        "playlist", nargs="?", help="playlist URL, URI or ID; omit to list cached playlists"
    )

    pin = add("pin", "Choose the audio for a track by hand.", _cmd_pin)
    pin.add_argument("track", help="track ID")
    pin.add_argument("url", help="URL of the audio to use, or `clear` to remove the pin")

    add("serve", "Run the daemon: mpv, the HTTP API and the remote-control page.", _cmd_serve)

    ctl = add("ctl", "Send a transport command to a running daemon.", _cmd_ctl)
    ctl.add_argument(
        "verb", choices=("toggle", "play", "pause", "next", "prev", "stop", "seek", "volume")
    )
    ctl.add_argument(
        "value", nargs="?", help="seek: seconds (+10, -5, 90); volume: 0-100, or +5/-5 to change it"
    )

    overlay = add("overlay", "Show the current lyric line above other windows.", _cmd_overlay)
    overlay.add_argument(
        "--demo", action="store_true", help="show a placeholder line, for positioning"
    )
    overlay.add_argument("--snapshot", metavar="FILE", help="render one frame to a PNG and exit")

    add(
        "shortcuts",
        "Hold the global volume shortcuts for a running daemon (through the desktop portal).",
        _cmd_shortcuts,
    )

    summary = "Run the daemon, the overlay and the shortcuts together, and open the page."
    start = add("start", summary, _cmd_start)
    start.add_argument("--no-overlay", action="store_true", help="do not start the overlay")
    start.add_argument(
        "--no-shortcuts", action="store_true", help="do not register the volume shortcuts"
    )
    start.add_argument("--no-browser", action="store_true", help="do not open the page")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args)
        return args.handler(args, settings)
    except PlaylistPlayerError as exc:
        print(f"spotifixed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
