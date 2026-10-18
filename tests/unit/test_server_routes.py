"""The REST API, called directly through the router with a fake player."""

from __future__ import annotations

import json
import random

import pytest

from spotifixed import shortcut_settings
from spotifixed.cache import assets
from spotifixed.config import load_settings
from spotifixed.errors import PlaybackError
from spotifixed.overlay_settings import OverlaySettings
from spotifixed.server.library import Library
from spotifixed.server.overlay_config import OverlayConfig
from spotifixed.server.routes import API, Routes
from spotifixed.server.security import allowed_hosts, check_request
from spotifixed.server.session import PlaybackSession
from tests.support.library import JPEG, PLAYLIST_ID, build_library
from tests.support.playback import FakePlayer


@pytest.fixture
def player():
    return FakePlayer()


@pytest.fixture
def routes(store, player):
    build_library(store)
    library = Library(store)
    session = PlaybackSession(store, library, player, rng=random.Random(4))
    overlay = OverlayConfig(OverlaySettings.from_settings(load_settings()), store)
    return Routes(library, player, session, overlay)


def get(routes, path):
    response = routes.dispatch("GET", API + path)
    body = json.loads(response.body) if response.content_type == "application/json" else None
    return response, body


def post(routes, path, body=None):
    raw = b"" if body is None else json.dumps(body).encode()
    response = routes.dispatch("POST", API + path, raw)
    return response, json.loads(response.body)


# ---------------------------------- reads ---------------------------------- #


def test_playlists_lists_coverage(routes):
    response, body = get(routes, "/playlists")
    assert response.status == 200
    assert body == {
        "playlists": [
            {
                "id": PLAYLIST_ID,
                "name": "A Playlist That Does Not Exist",
                "owner": "Some Curator",
                "has_cover": True,
                "coverage": {"tracks": 5, "audio": 4, "synced": 4, "ready": 3},
            }
        ]
    }


def test_playlist_detail_has_tracks_in_order_with_status(routes):
    response, body = get(routes, f"/playlists/{PLAYLIST_ID}")
    assert response.status == 200 and body["coverage"]["tracks"] == 5
    tracks = body["tracks"]
    assert [t["id"] for t in tracks] == [f"TRK000{i}" for i in range(5)]
    assert tracks[0] == {
        "id": "TRK0000",
        "title": "Invented Song 0",
        "artists": ["Made Up Artist 0", "A Guest"],
        "album": "Imaginary Album 0",
        "duration_ms": 5000,
        "explicit": False,
        "has_audio": True,
        "audio_duration_s": 5,
        "audio_pinned": False,
        "lyrics_kind": "synced",
        "timing_confidence": "high",
        "has_cover": True,
        "offset_ms": 0,
    }
    assert tracks[2]["timing_confidence"] == "low"
    assert (tracks[3]["lyrics_kind"], tracks[3]["has_cover"]) == ("plain", False)
    assert (tracks[4]["has_audio"], tracks[4]["audio_duration_s"]) == (False, None)
    # Nothing that looks like a filesystem path reaches a client.
    assert "/" not in json.dumps([{k: v for k, v in t.items() if k != "title"} for t in tracks])


def test_track_and_lyrics(routes):
    response, body = get(routes, "/tracks/TRK0001")
    assert response.status == 200 and body["explicit"] is True

    response, body = get(routes, "/tracks/TRK0000/lyrics")
    assert response.status == 200
    assert body == {
        "track_id": "TRK0000",
        "kind": "synced",
        "timing_confidence": "high",
        "offset_ms": 0,
        "lines": [
            {"t_ms": 1000, "text": "an invented first line"},
            {"t_ms": 2500, "text": ""},
            {"t_ms": 4000, "text": "an invented last line"},
        ],
    }
    _, plain = get(routes, "/tracks/TRK0003/lyrics")
    assert (plain["kind"], plain["lines"], plain["plain"]) == (
        "plain",
        [],
        "invented plain words\n",
    )


def test_lyrics_for_a_track_without_any(routes, store):
    assets.clear_lyrics(store, "TRK0004")
    response, body = get(routes, "/tracks/TRK0004/lyrics")
    assert response.status == 200 and (body["kind"], body["lines"]) == ("missing", [])


def test_covers_are_served_as_images(routes):
    for path in ("/tracks/TRK0000/cover", f"/playlists/{PLAYLIST_ID}/cover"):
        response = routes.dispatch("GET", API + path)
        assert (response.status, response.content_type, response.body) == (200, "image/jpeg", JPEG)
    assert routes.dispatch("GET", f"{API}/tracks/TRK0003/cover").status == 404


def test_a_cached_cover_that_is_not_an_image_is_not_served(routes, store):
    store.write_bytes(store.track_cover("TRK0000"), b"<script>alert(1)</script>")
    assert routes.dispatch("GET", f"{API}/tracks/TRK0000/cover").status == 404


def test_state_is_the_current_snapshot(routes, player):
    response, body = get(routes, "/state")
    assert response.status == 200
    assert (body["status"], body["version"], body["track_id"]) == ("idle", 1, None)
    assert set(body) >= {"position_s", "captured_at_epoch_ms", "lyrics_kind", "offset_ms", "speed"}


# --------------------------------- actions --------------------------------- #


def test_load_queues_only_playable_tracks(routes, player):
    response, body = post(routes, "/player/load", {"playlist_id": PLAYLIST_ID})
    assert (response.status, body) == (200, {"ok": True, "queue_length": 4})
    assert player.calls == [("load_queue", ["TRK0000", "TRK0001", "TRK0002", "TRK0003"], 0)]
    # mpv is handed absolute paths that the daemon resolved; the client never supplied one.
    assert all(entry.path.startswith("/") for entry in player.queue.entries)
    assert player.queue.entry_at(3).lyrics_kind.value == "plain"


def test_load_can_start_at_a_track(routes, player):
    post(routes, "/player/load", {"playlist_id": PLAYLIST_ID, "start_track_id": "TRK0002"})
    assert player.calls[-1] == ("load_queue", ["TRK0000", "TRK0001", "TRK0002", "TRK0003"], 2)


def test_shuffle_gives_the_playlist_a_new_order_that_it_keeps(routes, player, store):
    response, body = post(routes, f"/playlists/{PLAYLIST_ID}/shuffle")
    assert (response.status, body) == (200, {"ok": True, "shuffled": True})
    _, described = get(routes, f"/playlists/{PLAYLIST_ID}")
    order = [track["id"] for track in described["tracks"]]
    assert described["shuffled"] is True
    assert order[-1] == "TRK0004" and sorted(order[:4]) == [f"TRK000{i}" for i in range(4)]
    assert order[:4] != [f"TRK000{i}" for i in range(4)]
    assert json.loads(store.session_json.read_text())["orders"][PLAYLIST_ID] == order[:4]

    # Loading plays that order, starting where the chosen track falls in it.
    post(routes, "/player/load", {"playlist_id": PLAYLIST_ID, "start_track_id": "TRK0002"})
    assert player.calls[-1] == ("load_queue", order[:4], order.index("TRK0002"))

    # Shuffling again while it plays rearranges the queue around the current song.
    post(routes, f"/playlists/{PLAYLIST_ID}/shuffle")
    assert player.calls[-1][0] == "reorder" and player.calls[-1][1][0] == "TRK0002"
    assert player.calls[-1][1] != order[:4]


def test_unshuffle_puts_the_playlist_back_in_its_own_order(routes, player):
    post(routes, f"/playlists/{PLAYLIST_ID}/shuffle")
    response, body = post(routes, f"/playlists/{PLAYLIST_ID}/unshuffle")
    assert (response.status, body) == (200, {"ok": True, "shuffled": False})
    _, described = get(routes, f"/playlists/{PLAYLIST_ID}")
    assert described["shuffled"] is False
    assert [track["id"] for track in described["tracks"]] == [f"TRK000{i}" for i in range(5)]


def test_shuffle_refuses_what_it_cannot_shuffle(routes, store):
    assert post(routes, "/playlists/NotCached/shuffle")[0].status == 404
    assert post(routes, "/playlists/..%2Fetc/shuffle")[0].status == 400
    assert post(routes, "/playlists/NotCached/unshuffle")[0].status == 404
    assert routes.dispatch("GET", API + f"/playlists/{PLAYLIST_ID}/shuffle").status == 405
    assert not store.session_json.exists()


def test_the_session_document_says_what_would_be_resumed(routes, player):
    assert get(routes, "/session")[1] == {
        "playlist_id": None, "track_id": None, "position_s": 0.0, "volume": None,
        "settings": {
            "fade_in_s": 0.0,
            "include_without_lyrics": True,
            "include_without_audio": True,
            "show_lyrics": True,
            "show_position": True,
            "mono": False,
        },
    }  # fmt: skip
    post(routes, "/player/load", {"playlist_id": PLAYLIST_ID})
    assert get(routes, "/session")[1]["playlist_id"] == PLAYLIST_ID


def test_the_page_is_the_one_this_process_started_with(routes, monkeypatch, tmp_path):
    """An upgrade in place must not hand a running daemon's clients a newer page.

    The newer page would call endpoints this process does not have.
    """
    from spotifixed.server import routes as routes_module

    before = routes.dispatch("GET", "/static/app.js").body
    replaced = tmp_path / "static"
    replaced.mkdir()
    names = ("index.html", "app.js", "clock.js", "icon.svg", "overlay.js", "settings.js")
    for name in (*names, "style.css"):
        (replaced / name).write_text("// a later version")
    monkeypatch.setattr(routes_module, "_STATIC_DIR", replaced)
    assert routes.dispatch("GET", "/static/app.js").body == before


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({}, 400),
        ({"playlist_id": 7}, 400),
        ({"playlist_id": PLAYLIST_ID, "start_track_id": 3}, 400),
        ({"playlist_id": "NotCached"}, 404),
        ({"playlist_id": "../../etc"}, 400),
        ({"playlist_id": PLAYLIST_ID, "start_track_id": "TRK0004"}, 404),  # no audio
        ({"playlist_id": PLAYLIST_ID, "start_track_id": "Unknown"}, 404),
    ],
)
def test_load_rejects_bad_requests_without_touching_the_player(routes, player, body, status):
    response, payload = post(routes, "/player/load", body)
    assert response.status == status and "error" in payload
    assert player.calls == []


def test_a_playlist_with_no_audio_cannot_be_loaded(routes, player, store):
    for i in range(4):
        assets.clear_audio(store, f"TRK000{i}")
    response, body = post(routes, "/player/load", {"playlist_id": PLAYLIST_ID})
    assert response.status == 404 and "no cached audio" in body["error"]


@pytest.mark.parametrize("verb", ["play", "pause", "toggle", "next", "prev", "stop"])
def test_transport_verbs(routes, player, verb):
    response, body = post(routes, f"/player/{verb}")
    assert (response.status, body) == (200, {"ok": True})
    assert player.calls == [(verb,)]


def test_seek_absolute_and_relative(routes, player):
    assert post(routes, "/player/seek", {"position_s": 42.5})[0].status == 200
    assert post(routes, "/player/seek", {"delta_s": -10})[0].status == 200
    assert player.calls == [("seek", 42.5, None), ("seek", None, -10.0)]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"position_s": 1, "delta_s": 1},
        {"position_s": "1"},
        {"position_s": -1},
        {"delta_s": True},
        {"delta_s": None},
        {"position_s": float("inf")},
    ],  # fmt: skip
)
def test_seek_rejects_bad_bodies(routes, player, body):
    raw = json.dumps(body).encode()
    assert routes.dispatch("POST", f"{API}/player/seek", raw).status == 400
    assert player.calls == []


def test_volume(routes, player):
    assert post(routes, "/player/volume", {"volume": 70})[0].status == 200
    assert player.calls == [("set_volume", 70.0)]
    for bad in ({"volume": 101}, {"volume": -1}, {"volume": "loud"}, {}):
        assert post(routes, "/player/volume", bad)[0].status == 400
    assert len(player.calls) == 1


def test_a_volume_delta_is_relative_and_stops_at_the_ends(routes, player):
    post(routes, "/player/volume", {"volume": 50})
    for delta in (5, -12, 80, -200):
        assert post(routes, "/player/volume", {"delta": delta})[0].status == 200
    assert [value for name, value in player.calls if name == "set_volume"] == [
        50.0, 55.0, 43.0, 100.0, 0.0,
    ]  # fmt: skip
    for bad in ({"delta": "up"}, {"delta": 5, "volume": 50}):
        assert post(routes, "/player/volume", bad)[0].status == 400


def test_shortcuts_lists_the_configured_ones_and_the_media_keys(store, player):
    build_library(store)
    library = Library(store)
    session = PlaybackSession(store, library, player)
    shortcuts = shortcut_settings.from_settings(load_settings())
    _response, body = get(
        Routes(library, player, session, shortcuts=shortcuts, media_keys=True), "/shortcuts"
    )
    assert body["shortcuts"] == [
        {"id": "volume-up", "description": "Raise spotifixed's volume", "keys": "Ctrl+Volume Up"},
        {
            "id": "volume-down",
            "description": "Lower spotifixed's volume",
            "keys": "Ctrl+Volume Down",
        },
    ]
    assert {"keys": "Play/Pause", "description": "Play or pause"} in body["media_keys"]


def test_shortcuts_is_empty_when_none_are_set_and_mpv_answers_no_media_keys(routes):
    assert get(routes, "/shortcuts")[1] == {"shortcuts": [], "media_keys": []}


def test_offset_is_persisted_to_state_and_pushed_to_the_player(routes, player, store):
    post(routes, "/player/load", {"playlist_id": PLAYLIST_ID})
    response, body = post(routes, "/tracks/TRK0000/offset", {"offset_ms": -150})
    assert (response.status, body) == (200, {"ok": True, "track_id": "TRK0000", "offset_ms": -150})
    assert assets.read_track_state(store, "TRK0000").offset_ms == -150
    assert store.track_state("TRK0000").is_file()  # under state/, not cache/
    assert player.calls[-1] == ("set_offset", "TRK0000", -150)
    assert player.bus.current.offset_ms == -150
    assert get(routes, "/tracks/TRK0000")[1]["offset_ms"] == -150
    assert get(routes, "/tracks/TRK0000/lyrics")[1]["offset_ms"] == -150


def test_offset_keeps_a_pin_is_clamped_and_validated(routes, store):
    assets.write_track_state(store, "TRK0001", assets.TrackState(audio_pin="https://v.invalid/x"))
    _, body = post(routes, "/tracks/TRK0001/offset", {"offset_ms": 999_999})
    assert body["offset_ms"] == 30_000
    assert assets.read_track_state(store, "TRK0001").audio_pin == "https://v.invalid/x"
    for bad in ({"offset_ms": 1.5}, {"offset_ms": "50"}, {"offset_ms": True}, {}):
        assert post(routes, "/tracks/TRK0001/offset", bad)[0].status == 400
    assert post(routes, "/tracks/Unknown/offset", {"offset_ms": 1})[0].status == 404


# ---------------------------------- errors --------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/tracks/..%2f..%2fetc",
        "/tracks/has space",
        "/tracks/a.b",
        "/playlists/x!y",
        "/tracks/" + "a" * 65,
        "/tracks/%00",
    ],  # fmt: skip
)
def test_malformed_ids_are_400_never_a_path(routes, path):
    response, body = get(routes, path)
    assert response.status == 400 and "not a valid" in body["error"]


def test_unknown_ids_and_routes_are_404(routes):
    assert get(routes, "/tracks/NeverSynced")[0].status == 404
    assert get(routes, "/playlists/NeverSynced")[0].status == 404
    assert get(routes, "/nothing/here")[0].status == 404
    assert routes.dispatch("GET", "/etc/passwd").status == 404
    assert routes.dispatch("GET", "/static/../routes.py").status == 404
    assert routes.dispatch("GET", "/static/secrets.env").status == 404


def test_wrong_method_is_405(routes, player):
    assert routes.dispatch("POST", f"{API}/playlists", b"{}").status == 405
    assert routes.dispatch("GET", f"{API}/player/play").status == 405
    assert routes.dispatch("POST", f"{API}/events", b"{}").status == 405
    assert routes.dispatch("DELETE", f"{API}/tracks/TRK0000").status == 405
    assert player.calls == []


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"text"', b"\xff\xfe"])
def test_bad_json_is_400(routes, player, raw):
    response = routes.dispatch("POST", f"{API}/player/seek", raw)
    assert response.status == 400 and "JSON" in json.loads(response.body)["error"]
    assert player.calls == []


def test_a_player_failure_is_503_not_a_crash(routes, player):
    player.fail_with = PlaybackError("mpv IPC is not connected")
    response, body = post(routes, "/player/play")
    assert response.status == 503 and "mpv IPC is not connected" in body["error"]


def test_there_is_no_audio_or_sync_endpoint(routes):
    for path in ("/tracks/TRK0000/audio", "/sync", "/player/sync", "/tracks/TRK0000/pin"):
        assert routes.dispatch("GET", API + path).status == 404
        assert routes.dispatch("POST", API + path, b"{}").status == 404


def test_static_files_come_from_an_allowlist(routes):
    page = routes.dispatch("GET", "/")
    assert page.status == 200 and page.content_type.startswith("text/html")
    assert b'src="/static/app.js"' in page.body
    for name, kind in [("app.js", "text/javascript"), ("clock.js", "text/javascript"),
                       ("overlay.js", "text/javascript"), ("settings.js", "text/javascript"),
                       ("style.css", "text/css"), ("icon.svg", "image/svg+xml")]:  # fmt: skip
        response = routes.dispatch("GET", f"/static/{name}")
        assert response.status == 200 and response.content_type.startswith(kind)
    assert routes.dispatch("GET", "/static/index.html").status == 404
    # The page is served with a strict policy, so it must carry no inline script or style.
    assert b"<script>" not in page.body and b"style=" not in page.body


# --------------------------------- security -------------------------------- #

HOSTS = allowed_hosts("127.0.0.1", 8765)


def test_allowed_hosts():
    assert allowed_hosts("127.0.0.1", 8765) == {"127.0.0.1:8765", "localhost:8765"}
    assert "[::1]:9000" in allowed_hosts("::1", 9000)


@pytest.mark.parametrize(
    ("method", "headers", "status"),
    [
        ("GET", {"Host": "127.0.0.1:8765"}, None),
        ("GET", {"Host": "LOCALHOST:8765"}, None),
        ("GET", {"Host": "127.0.0.1:8765", "Origin": "http://localhost:8765"}, None),
        ("POST", {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}, None),
        (
            "POST",
            {"Host": "127.0.0.1:8765", "Content-Type": "application/json; charset=utf-8"},
            None,
        ),
        ("GET", {}, 403),
        ("GET", {"Host": "evil.example:8765"}, 403),  # DNS rebinding
        ("GET", {"Host": "127.0.0.1:9999"}, 403),
        ("GET", {"Host": "127.0.0.1"}, 403),
        ("GET", {"Host": "127.0.0.1:8765", "Origin": "https://evil.example"}, 403),
        ("GET", {"Host": "127.0.0.1:8765", "Origin": "null"}, 403),
        (
            "POST",
            {
                "Host": "127.0.0.1:8765",
                "Origin": "http://evil.example",
                "Content-Type": "application/json",
            },
            403,
        ),
        ("POST", {"Host": "127.0.0.1:8765"}, 415),
        ("POST", {"Host": "127.0.0.1:8765", "Content-Type": "text/plain"}, 415),
        (
            "POST",
            {"Host": "127.0.0.1:8765", "Content-Type": "application/x-www-form-urlencoded"},
            415,
        ),
    ],  # fmt: skip
)
def test_check_request(method, headers, status):
    refusal = check_request(method, headers, HOSTS)
    assert (refusal[0] if refusal else None) == status


# ----------------------------- overlay settings ----------------------------- #


def test_overlay_settings_are_described_with_their_rules(routes):
    response, body = get(routes, "/overlay")
    assert response.status == 200
    assert body["settings"]["margin_y"] == 120 and body["overrides"] == {}
    assert body["rules"]["anchor"]["choices"][0] == "top"
    assert body["rules"]["font_size"] == {"kind": "int", "min": 4, "max": 300}
    assert body["rules"]["color"] == {"kind": "color"}


def test_overlay_settings_can_be_changed_dropped_and_reset(routes, store):
    response, body = post(routes, "/overlay", {"margin_y": 300, "anchor": "top-left"})
    assert response.status == 200
    assert body["overrides"] == {"margin_y": 300, "anchor": "top-left"}
    assert json.loads(store.overlay_json.read_text())["overrides"] == body["overrides"]

    _, body = post(routes, "/overlay", {"anchor": None})
    assert body["overrides"] == {"margin_y": 300}
    _, body = post(routes, "/overlay/reset")
    assert body["overrides"] == {} and body["settings"]["margin_y"] == 120


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"margin_y": "300"}, "margin_y must be a whole number"),
        ({"margin_y": 300.5}, "margin_y must be a whole number"),
        ({"font": "Sans; rm -rf ~"}, "font must be a font name"),
        ({"color": "red; } body { display: none"}, "color must be a colour"),
        ({"monitor": "../../etc"}, "monitor must be an output connector"),
        ({"cache_dir": "/tmp"}, "'cache_dir' is not an overlay setting"),
        ({"margin_y": 300, "lines": 9}, "lines must be"),
    ],
)
def test_overlay_values_are_checked_and_a_refused_change_changes_nothing(routes, store, body, says):
    response, answer = post(routes, "/overlay", body)
    assert response.status == 400 and says in answer["error"]
    assert get(routes, "/overlay")[1]["overrides"] == {}
    assert not store.overlay_json.exists()


def test_overlay_preview_takes_a_bounded_number_of_seconds(routes):
    response, body = post(routes, "/overlay/preview", {"seconds": 8})
    assert response.status == 200 and body["preview_until_ms"] > 0
    assert post(routes, "/overlay/preview", {"seconds": 0})[1]["preview_until_ms"] == 0
    for bad in ({"seconds": -1}, {"seconds": 61}, {"seconds": "8"}, {"seconds": True}, {}):
        assert post(routes, "/overlay/preview", bad)[0].status == 400


def test_the_overlay_is_switched_on_and_off_only_where_something_follows_the_switch(routes):
    response, answer = post(routes, "/overlay/enabled", {"enabled": False})
    assert response.status == 400 and "cannot be switched on or off" in answer["error"]
    routes._overlay.set_managed()
    response, body = post(routes, "/overlay/enabled", {"enabled": False})
    assert response.status == 200 and body["enabled"] is False
    assert get(routes, "/overlay")[1]["enabled"] is False
    for bad in ({"enabled": "no"}, {"enabled": 0}, {}):
        assert post(routes, "/overlay/enabled", bad)[0].status == 400


def test_the_overlay_reports_its_monitors_for_the_page(routes):
    seen = [{"connector": "DP-1", "description": "Dell Inc. DELL U2720Q"}]
    response, body = post(routes, "/overlay/monitors", {"monitors": seen})
    assert response.status == 200 and body["monitors"] == seen
    assert get(routes, "/overlay")[1]["monitors"] == seen
    response, answer = post(routes, "/overlay/monitors", {"monitors": [{"connector": "../x"}]})
    assert response.status == 400 and "output connector" in answer["error"]
    assert post(routes, "/overlay/monitors", {})[0].status == 400


def test_overlay_endpoints_refuse_other_methods_and_report_a_failed_save(
    routes, store, monkeypatch
):
    assert routes.dispatch("GET", API + "/overlay/reset").status == 405
    assert routes.dispatch("DELETE", API + "/overlay").status == 405

    def refuse(path, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "write_json", refuse)
    response, answer = post(routes, "/overlay", {"margin_y": 300})
    assert response.status == 500 and "read-only file system" in answer["error"]
    assert get(routes, "/overlay")[1]["overrides"] == {}


def test_without_an_overlay_store_the_endpoints_do_not_exist(store, player):
    library = Library(store)
    bare = Routes(library, player, PlaybackSession(store, library, player))
    assert bare.dispatch("GET", API + "/overlay").status == 404


def test_an_offset_is_kept_per_track_and_comes_back_after_a_restart(store, player):
    build_library(store)
    library = Library(store)
    session = PlaybackSession(store, library, player)
    first = Routes(library, player, session)
    response, body = post(first, "/tracks/TRK0001/offset", {"offset_ms": 250})
    assert (response.status, body["offset_ms"]) == (200, 250)
    post(first, "/tracks/TRK0002/offset", {"offset_ms": -4000})  # clamped at the limit
    _, described = get(first, f"/playlists/{PLAYLIST_ID}")
    offsets = {t["id"]: t["offset_ms"] for t in described["tracks"]}
    assert offsets["TRK0001"] == 250 and offsets["TRK0002"] == -4000
    assert offsets["TRK0000"] == 0

    # A new process reads the same files: the offsets are the ones that were saved.
    second = Routes(Library(store), FakePlayer(), PlaybackSession(store, Library(store), player))
    _, again = get(second, f"/playlists/{PLAYLIST_ID}")
    assert {t["id"]: t["offset_ms"] for t in again["tracks"]} == offsets
    _, track = get(second, "/tracks/TRK0001")
    assert track["offset_ms"] == 250
    assert second.dispatch("GET", API + "/tracks/TRK0001/lyrics").status == 200


def test_playback_settings_are_changed_through_the_api_and_checked(routes, store):
    response, body = post(routes, "/session/settings", {"fade_in_s": 3})
    assert response.status == 200 and body["settings"]["fade_in_s"] == 3.0
    assert get(routes, "/session")[1]["settings"]["fade_in_s"] == 3.0
    assert json.loads(store.session_json.read_text())["settings"] == {"fade_in_s": 3.0}
    for bad in ({"fade_in_s": 31}, {"fade_in_s": "3"}, {"cache_dir": "/tmp"}):
        response, answer = post(routes, "/session/settings", bad)
        assert response.status == 400 and "error" in answer
    assert get(routes, "/session")[1]["settings"]["fade_in_s"] == 3.0
    assert routes.dispatch("GET", API + "/session/settings").status == 405


def test_track_filters_change_what_a_playlist_lists_and_loads(routes, player):
    response, body = post(routes, "/session/settings", {"include_without_lyrics": False})
    assert response.status == 200 and body["settings"]["include_without_lyrics"] is False
    _, playlist = get(routes, f"/playlists/{PLAYLIST_ID}")
    assert [t["id"] for t in playlist["tracks"]] == ["TRK0000", "TRK0001", "TRK0002", "TRK0004"]
    assert playlist["hidden"] == 1 and playlist["coverage"]["tracks"] == 5
    _, loaded = post(routes, "/player/load", {"playlist_id": PLAYLIST_ID})
    assert loaded["queue_length"] == 3
    assert post(routes, "/session/settings", {"include_without_audio": "no"})[0].status == 400
