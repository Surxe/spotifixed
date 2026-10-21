"""The daemon's session: a playlist's kept order, what is saved, and what a restart resumes."""

from __future__ import annotations

import json
import random
import threading

import pytest

from spotifixed.cache import assets
from spotifixed.config import load_settings
from spotifixed.errors import NotInCacheError, PlaybackError
from spotifixed.models import PlaybackState, PlayerStatus
from spotifixed.server.app import Daemon
from spotifixed.server.library import Library
from spotifixed.server.session import PlaybackSession, SettingError
from tests.support.library import PLAYLIST_ID, build_library
from tests.support.playback import FakePlayer

PLAYABLE = ["TRK0000", "TRK0001", "TRK0002", "TRK0003"]  # TRK0004 has no audio


@pytest.fixture
def library(store):
    build_library(store)
    return Library(store)


def session_on(store, library, *, seed=1, clock=None) -> tuple[PlaybackSession, FakePlayer]:
    player = FakePlayer()
    kwargs = {"rng": random.Random(seed)}
    if clock is not None:
        kwargs["clock"] = clock
    return PlaybackSession(store, library, player, **kwargs), player


def saved(store) -> dict:
    return json.loads(store.session_json.read_text())


def ids(queue) -> list[str]:
    return [entry.track_id for entry in queue.entries]


# --------------------------------- order --------------------------------- #


def test_an_unshuffled_playlist_plays_in_its_own_order(store, library):
    session, _ = session_on(store, library)
    assert ids(session.queue_for(PLAYLIST_ID)) == PLAYABLE
    assert saved(store)["orders"] == {}


def test_shuffling_draws_a_new_order_of_the_playable_tracks_and_keeps_it(store, library):
    session, _ = session_on(store, library)
    order = session.shuffle(PLAYLIST_ID)
    assert sorted(order) == PLAYABLE and order != PLAYABLE
    assert saved(store)["orders"] == {PLAYLIST_ID: order}
    # It is the playlist's order from now on: every load plays it.
    assert ids(session.queue_for(PLAYLIST_ID)) == order
    assert ids(session.queue_for(PLAYLIST_ID, start_track_id="TRK0002")) == order


def test_each_shuffle_gives_a_different_order(store, library):
    session, _ = session_on(store, library)
    seen = [session.shuffle(PLAYLIST_ID) for _ in range(12)]
    assert all(later != earlier for earlier, later in zip(seen, seen[1:], strict=False))
    assert all(sorted(order) == PLAYABLE for order in seen)


def test_the_order_survives_a_restart(store, library):
    order = session_on(store, library)[0].shuffle(PLAYLIST_ID)
    later, _ = session_on(store, library, seed=99)  # a different random source, same file
    assert ids(later.queue_for(PLAYLIST_ID)) == order


def test_starting_a_track_plays_on_from_its_place_in_the_order(store, library):
    session, _ = session_on(store, library)
    order = session.shuffle(PLAYLIST_ID)
    assert session.queue_for(PLAYLIST_ID, start_track_id="TRK0002").start_index == order.index(
        "TRK0002"
    )


def test_restoring_puts_the_playlist_back_in_its_own_order(store, library):
    session, _ = session_on(store, library)
    session.shuffle(PLAYLIST_ID)
    session.restore(PLAYLIST_ID)
    assert ids(session.queue_for(PLAYLIST_ID)) == PLAYABLE
    assert saved(store)["orders"] == {}
    session.restore(PLAYLIST_ID)  # nothing kept: nothing to do, and not an error


def test_shuffling_the_playlist_that_is_playing_rearranges_the_queue_around_the_song(
    store, library
):
    session, player = session_on(store, library)
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0002"))
    order = session.shuffle(PLAYLIST_ID)
    # The song that is on comes first, so everything else is still to come.
    assert order[0] == "TRK0002" and sorted(order) == PLAYABLE
    assert player.calls[-1] == ("reorder", order)
    assert player.bus.current.track_id == "TRK0002" and player.bus.current.queue_index == 0

    session.restore(PLAYLIST_ID)
    assert player.calls[-1] == ("reorder", PLAYABLE)
    assert player.bus.current.track_id == "TRK0002" and player.bus.current.queue_index == 2


def test_shuffling_a_playlist_that_is_not_loaded_leaves_the_player_alone(store, library):
    session, player = session_on(store, library)
    session.shuffle(PLAYLIST_ID)
    session.restore(PLAYLIST_ID)
    assert player.calls == []


def test_a_kept_order_follows_the_playlist_when_tracks_come_and_go(store, library):
    store.write_json(store.session_json, {
        "orders": {PLAYLIST_ID: ["TRK0003", "TRK0009", "TRK0001"]},
    })  # fmt: skip
    session, _ = session_on(store, library)
    # TRK0009 is gone; TRK0000 and TRK0002 are new to the order and go to the end.
    assert ids(session.queue_for(PLAYLIST_ID)) == ["TRK0003", "TRK0001", "TRK0000", "TRK0002"]
    assert saved(store)["orders"][PLAYLIST_ID] == ["TRK0003", "TRK0001", "TRK0000", "TRK0002"]


def test_a_track_that_gains_audio_joins_the_end_of_the_kept_order(store, library):
    session, _ = session_on(store, library)
    order = session.shuffle(PLAYLIST_ID)
    donor = assets.track_status(store, "TRK0000").audio
    store.write_bytes(store.track_file("TRK0004", donor.file), b"placeholder, not audio")
    assets.write_audio(store, "TRK0004", donor)
    assert ids(session.queue_for(PLAYLIST_ID)) == [*order, "TRK0004"]


def test_the_track_list_is_described_in_the_kept_order(store, library):
    session, _ = session_on(store, library)
    plain = session.arranged(library.playlist(PLAYLIST_ID))
    assert plain["shuffled"] is False
    assert [t["id"] for t in plain["tracks"]] == [*PLAYABLE, "TRK0004"]

    order = session.shuffle(PLAYLIST_ID)
    arranged = session.arranged(library.playlist(PLAYLIST_ID))
    assert arranged["shuffled"] is True
    # The unplayable track has no place in an order; it follows the rest.
    assert [t["id"] for t in arranged["tracks"]] == [*order, "TRK0004"]
    assert arranged["coverage"] == plain["coverage"]


def test_a_playlist_that_is_not_cached_is_refused_and_nothing_is_kept(store, library):
    session, _ = session_on(store, library)
    for action in (session.shuffle, session.restore, session.queue_for):
        with pytest.raises(NotInCacheError):
            action("NotCached")
    assert not store.session_json.exists() or saved(store)["orders"] == {}


def test_a_file_from_the_version_with_a_shuffle_switch_is_read_without_its_old_keys(store, library):
    store.write_json(store.session_json, {
        "shuffle": True, "order": PLAYABLE[::-1], "order_playlist_id": PLAYLIST_ID,
        "playlist_id": PLAYLIST_ID, "track_id": "TRK0001", "position_s": 2.0, "volume": 50.0,
    })  # fmt: skip
    session, _ = session_on(store, library)
    assert ids(session.queue_for(PLAYLIST_ID)) == PLAYABLE
    assert set(saved(store)) == {
        "playlist_id", "track_id", "position_s", "volume", "orders", "settings",
    }  # fmt: skip


# ------------------------------- recording ------------------------------ #


def test_nothing_is_saved_before_the_daemon_has_tried_to_resume(store, library):
    _, player = session_on(store, library)
    player.load_queue(library.queue_for(PLAYLIST_ID))  # a snapshot of playing, say
    player.set_volume(40)
    assert not store.session_json.exists()


def test_what_plays_is_saved_as_it_changes(store, library):
    session, player = session_on(store, library)
    session.resume()  # nothing saved yet; from here on it is
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0001"))
    player.set_volume(35)
    document = saved(store)
    assert (document["playlist_id"], document["track_id"]) == (PLAYLIST_ID, "TRK0001")
    assert document["volume"] == 35.0


def test_the_position_is_where_playback_is_now_not_where_the_snapshot_was(store, library):
    now = [1000.0]
    session, player = session_on(store, library, clock=lambda: now[0])
    session.resume()
    player.bus.publish(PlaybackState(
        status=PlayerStatus.PLAYING, paused=False, playlist_id=PLAYLIST_ID, track_id="TRK0000",
        position_s=10.0, captured_at_epoch_ms=1_000_000, volume=100.0, duration_s=60.0,
    ))  # fmt: skip
    now[0] = 1003.0  # three seconds later, nothing new published
    session.flush()
    assert saved(store)["position_s"] == pytest.approx(13.0)


def test_a_stopped_or_empty_player_does_not_forget_the_song(store, library):
    session, player = session_on(store, library)
    session.resume()
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0002"))
    player.bus.publish(PlaybackState(status=PlayerStatus.IDLE, paused=True, volume=60.0))
    document = saved(store)
    assert document["track_id"] == "TRK0002" and document["volume"] == 60.0


def test_a_failed_save_is_reported_and_playback_goes_on(store, library, monkeypatch, capsys):
    session, player = session_on(store, library)
    session.resume()

    def refuse(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_json", refuse)
    player.set_volume(20)  # must not raise
    assert "could not save the session: disk full" in capsys.readouterr().err


# -------------------------------- resuming ------------------------------ #


def test_a_restart_puts_back_the_volume_and_loads_the_song_paused_at_its_position(store, library):
    store.write_json(store.session_json, {
        "playlist_id": PLAYLIST_ID, "track_id": "TRK0001", "position_s": 2.5, "volume": 45.0,
    })  # fmt: skip
    session, player = session_on(store, library)
    session.resume()
    assert ("set_volume", 45.0) in player.calls
    assert player.index == 1 and player.paused is True
    assert player.loaded_at == 2.5
    assert player.bus.current.status is PlayerStatus.PAUSED


def test_a_shuffled_playlist_comes_back_in_its_kept_order(store, library):
    order = ["TRK0003", "TRK0000", "TRK0002", "TRK0001"]
    store.write_json(store.session_json, {
        "playlist_id": PLAYLIST_ID, "track_id": "TRK0002", "position_s": 0,
        "orders": {PLAYLIST_ID: order},
    })  # fmt: skip
    session, player = session_on(store, library, seed=77)
    session.resume()
    assert ids(player.queue) == order and player.index == 2


def test_a_song_saved_at_its_very_end_starts_again(store, library):
    store.write_json(store.session_json, {
        "playlist_id": PLAYLIST_ID, "track_id": "TRK0000", "position_s": 4.9,
    })  # fmt: skip
    session, player = session_on(store, library)
    session.resume()
    assert player.index == 0 and player.loaded_at == 0.0


def test_a_saved_song_that_is_no_longer_cached_is_skipped_with_a_note(store, library, capsys):
    store.write_json(store.session_json, {
        "playlist_id": PLAYLIST_ID, "track_id": "TRK0004", "position_s": 3, "volume": 30.0,
    })  # fmt: skip
    session, player = session_on(store, library)
    session.resume()
    assert ("set_volume", 30.0) in player.calls  # the volume is still put back
    assert not any(call[0] == "load_queue" for call in player.calls)
    assert "not resuming the last song" in capsys.readouterr().err


def test_a_damaged_session_file_starts_with_nothing_remembered(store, library, capsys):
    store.session_json.parent.mkdir(parents=True, exist_ok=True)
    store.session_json.write_text("{ not json")
    session, player = session_on(store, library)
    session.resume()
    assert player.calls == []
    assert "starting without the saved session" in capsys.readouterr().err
    assert session.document()["playlist_id"] is None
    session.shuffle(PLAYLIST_ID)  # and it works normally from there
    assert sorted(saved(store)["orders"][PLAYLIST_ID]) == PLAYABLE


def test_a_daemon_that_stops_and_starts_again_plays_where_it_was(store, monkeypatch):
    monkeypatch.setenv("SPOTIFIXED_CACHE_DIR", str(store.cache_dir))
    monkeypatch.setenv("SPOTIFIXED_STATE_DIR", str(store.state_dir))
    build_library(store)
    settings = load_settings()

    first_player = FakePlayer()
    first = Daemon(settings, store, first_player, port=0, keepalive_s=0.5)
    threading.Thread(target=first.serve_forever, daemon=True).start()  # shutdown() waits for it
    first.session.resume()
    load = json.dumps({"playlist_id": PLAYLIST_ID, "start_track_id": "TRK0001"}).encode()
    first.routes.dispatch("POST", "/api/v1/player/load", load)
    first.routes.dispatch("POST", f"/api/v1/playlists/{PLAYLIST_ID}/shuffle", b"{}")
    shuffled = ids(first_player.queue)
    first_player.set_volume(25)
    first_player.pause()  # paused, so the saved position is exact rather than extrapolated
    first_player.next()
    first_player.position = 1.75
    first_player._publish()
    first.shutdown()

    second_player = FakePlayer()
    second = Daemon(settings, store, second_player, port=0, keepalive_s=0.5)
    threading.Thread(target=second.serve_forever, daemon=True).start()
    second.session.resume()
    second.shutdown()
    assert shuffled[0] == "TRK0001" and sorted(shuffled) == PLAYABLE
    assert ids(second_player.queue) == shuffled
    assert second_player.index == 1 and second_player.loaded_at == pytest.approx(1.75)
    assert ("set_volume", 25.0) in second_player.calls and second_player.paused is True


def test_saving_and_shuffling_from_several_threads_leave_a_whole_file(store, library):
    session, player = session_on(store, library)
    session.resume()
    player.load_queue(session.queue_for(PLAYLIST_ID))

    def volumes(n):
        for i in range(30):
            player.set_volume(float((n * 30 + i) % 100))

    def shuffles():
        for _ in range(15):
            session.shuffle(PLAYLIST_ID)

    threads = [threading.Thread(target=volumes, args=(n,)) for n in range(3)]
    threads += [threading.Thread(target=shuffles) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    document = saved(store)
    assert 0 <= document["volume"] <= 100
    assert document["orders"][PLAYLIST_ID] == ids(player.queue)  # file and queue agree


# -------------------------------- settings ------------------------------ #


def test_the_fade_in_is_off_until_it_is_set_and_then_it_is_kept(store, library):
    session, _ = session_on(store, library)
    assert session.document()["settings"]["fade_in_s"] == 0.0
    assert session.set_settings({"fade_in_s": 3.5})["settings"]["fade_in_s"] == 3.5
    assert saved(store)["settings"] == {"fade_in_s": 3.5}
    assert session_on(store, library)[0].document()["settings"]["fade_in_s"] == 3.5
    assert session.set_settings({"fade_in_s": 0})["settings"]["fade_in_s"] == 0.0


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"fade_in_s": -1}, "from 0 to 30"),
        ({"fade_in_s": 30.5}, "from 0 to 30"),
        ({"fade_in_s": "3"}, "must be a number"),
        ({"fade_in_s": True}, "must be a number"),
        ({"fade_in_s": None}, "must be a number"),
        ({"volume": 5}, "'volume' is not a playback setting"),
        ({"fade_in_s": 2, "nonsense": 1}, "not a playback setting"),
    ],
)
def test_a_setting_that_cannot_be_taken_changes_nothing(store, library, changes, says):
    session, _ = session_on(store, library)
    session.set_settings({"fade_in_s": 4})
    with pytest.raises(SettingError, match=says):
        session.set_settings(changes)
    assert session.document()["settings"]["fade_in_s"] == 4.0


def test_a_start_sets_the_fade_in_after_the_song_is_loaded_and_only_when_it_is_on(store, library):
    store.write_json(store.session_json, {
        "playlist_id": PLAYLIST_ID, "track_id": "TRK0001", "position_s": 2.5, "volume": 45.0,
        "settings": {"fade_in_s": 4},
    })  # fmt: skip
    session, player = session_on(store, library)
    session.resume()
    kinds = [call[0] for call in player.calls]
    assert kinds == ["set_volume", "load_queue", "set_fade_in"]  # the volume is not lowered
    assert player.calls[-1] == ("set_fade_in", 4.0)
    assert saved(store)["volume"] == 45.0

    off, quiet = session_on(store, library)
    off.set_settings({"fade_in_s": 0})
    quiet.calls.clear()
    off.resume()
    assert not any(call[0] == "set_fade_in" for call in quiet.calls)


def test_the_fade_in_is_set_even_with_no_song_to_resume(store, library):
    store.write_json(store.session_json, {"settings": {"fade_in_s": 2}})
    session, player = session_on(store, library)
    session.resume()
    assert player.calls == [("set_fade_in", 2.0)]


def test_a_change_to_the_fade_in_reaches_the_player_at_once(store, library):
    session, player = session_on(store, library)
    session.set_settings({"fade_in_s": 3})
    session.set_settings({"fade_in_s": 0})
    session.set_settings({"include_without_lyrics": False})
    assert [call for call in player.calls if call[0] == "set_fade_in"] == [
        ("set_fade_in", 3.0),
        ("set_fade_in", 0.0),
    ]


def test_a_stored_fade_in_that_makes_no_sense_counts_as_off(store, library):
    for stored in ({"fade_in_s": "long"}, {"fade_in_s": 999}, {"fade_in_s": -3}, "nonsense"):
        store.write_json(store.session_json, {"settings": stored})
        session, player = session_on(store, library)
        session.resume()
        assert session.document()["settings"]["fade_in_s"] == 0.0
        assert player.calls == []


def test_mono_is_off_until_it_is_turned_on_and_reaches_the_player_at_once(store, library):
    session, player = session_on(store, library)
    assert session.document()["settings"]["mono"] is False
    assert session.set_settings({"mono": True})["settings"]["mono"] is True
    assert saved(store)["settings"] == {"mono": True}
    session.set_settings({"mono": False})
    session.set_settings({"fade_in_s": 2})
    assert [call for call in player.calls if call[0] == "set_mono"] == [
        ("set_mono", True),
        ("set_mono", False),
    ]
    with pytest.raises(SettingError, match="true or false"):
        session.set_settings({"mono": 1})


def test_a_start_turns_mono_on_only_when_it_was_left_on(store, library):
    for stored, calls in (({"mono": True}, [("set_mono", True)]), ({"mono": "yes"}, [])):
        store.write_json(store.session_json, {"settings": stored})
        session, player = session_on(store, library)
        session.resume()
        assert player.calls == calls
    assert session.document()["settings"]["mono"] is False  # only a stored true is on


def test_a_player_that_cannot_mix_to_mono_does_not_stop_the_setting_being_kept(
    store, library, monkeypatch, capsys
):
    session, player = session_on(store, library)

    def refuse(on):
        raise PlaybackError("mpv could not mix to mono")

    monkeypatch.setattr(player, "set_mono", refuse)
    assert session.set_settings({"mono": True})["settings"]["mono"] is True
    assert "the mono setting is not in force" in capsys.readouterr().err


def test_loop_is_off_until_it_is_turned_on_and_a_start_puts_it_back(store, library):
    session, player = session_on(store, library)
    assert session.document()["settings"]["loop"] is False
    assert session.set_settings({"loop": True})["settings"]["loop"] is True
    assert saved(store)["settings"] == {"loop": True}
    session.set_settings({"mono": True})
    assert [call for call in player.calls if call[0] == "set_loop"] == [("set_loop", True)]

    session, player = session_on(store, library)
    session.resume()
    assert ("set_loop", True) in player.calls and ("set_mono", True) in player.calls


# ------------------------------ track filters ---------------------------- #

SYNCED_IDS = ["TRK0000", "TRK0001", "TRK0002"]  # TRK0003 has plain lyrics only


def listed(session, library) -> list[str]:
    return [t["id"] for t in session.arranged(library.playlist(PLAYLIST_ID))["tracks"]]


def test_every_track_is_included_until_a_filter_is_turned_off(store, library):
    session, _ = session_on(store, library)
    settings = session.document()["settings"]
    assert settings["include_without_lyrics"] is True
    assert settings["include_without_audio"] is True
    assert listed(session, library) == [*PLAYABLE, "TRK0004"]
    assert session.arranged(library.playlist(PLAYLIST_ID))["hidden"] == 0


def test_without_lyrics_off_leaves_those_tracks_out_of_the_list_and_the_queue(store, library):
    session, _ = session_on(store, library)
    session.set_settings({"include_without_lyrics": False})
    assert ids(session.queue_for(PLAYLIST_ID)) == SYNCED_IDS
    assert listed(session, library) == [*SYNCED_IDS, "TRK0004"]
    assert session.arranged(library.playlist(PLAYLIST_ID))["hidden"] == 1
    # It is kept, and a restart plays by it.
    assert saved(store)["settings"] == {"include_without_lyrics": False}
    assert ids(session_on(store, library)[0].queue_for(PLAYLIST_ID)) == SYNCED_IDS


def test_without_audio_off_only_changes_what_is_listed(store, library):
    session, _ = session_on(store, library)
    session.set_settings({"include_without_audio": False})
    assert listed(session, library) == PLAYABLE
    assert ids(session.queue_for(PLAYLIST_ID)) == PLAYABLE
    session.set_settings({"include_without_lyrics": False})
    assert listed(session, library) == SYNCED_IDS
    assert session.arranged(library.playlist(PLAYLIST_ID))["hidden"] == 2


def test_a_track_that_is_left_out_keeps_its_place_in_a_kept_order(store, library):
    session, _ = session_on(store, library)
    order = session.shuffle(PLAYLIST_ID)
    session.set_settings({"include_without_lyrics": False})
    assert ids(session.queue_for(PLAYLIST_ID)) == [t for t in order if t != "TRK0003"]
    assert saved(store)["orders"] == {PLAYLIST_ID: order}
    session.set_settings({"include_without_lyrics": True})
    assert ids(session.queue_for(PLAYLIST_ID)) == order


def test_changing_a_filter_brings_the_playing_queue_up_to_date(store, library):
    session, player = session_on(store, library)
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0001"))
    session.set_settings({"include_without_lyrics": False})
    assert player.calls[-1] == ("reorder", SYNCED_IDS)
    assert player.bus.current.track_id == "TRK0001"
    session.set_settings({"include_without_lyrics": True})
    assert player.calls[-1] == ("reorder", PLAYABLE)
    # A setting that is not a filter, or a filter set to what it was, leaves the queue alone.
    calls = len(player.calls)
    session.set_settings({"fade_in_s": 2, "include_without_lyrics": True})
    assert player.calls[calls:] == [("set_fade_in", 2.0)]  # the fade only, no queue change
    session.set_settings({"show_lyrics": False, "show_position": False})
    assert len(player.calls) == calls + 1


def test_the_page_switches_are_on_until_turned_off_and_then_kept(store, library):
    session, _ = session_on(store, library)
    settings = session.document()["settings"]
    assert settings["show_lyrics"] is True and settings["show_position"] is True
    changed = session.set_settings({"show_lyrics": False, "show_position": False})["settings"]
    assert changed["show_lyrics"] is False and changed["show_position"] is False
    assert saved(store)["settings"] == {"show_lyrics": False, "show_position": False}
    reloaded = session_on(store, library)[0].document()["settings"]
    assert reloaded["show_lyrics"] is False and reloaded["show_position"] is False
    with pytest.raises(SettingError, match="show_position must be true or false"):
        session.set_settings({"show_lyrics": True, "show_position": "off"})
    assert session.document()["settings"]["show_lyrics"] is False  # none of it applied


def test_the_song_that_is_playing_or_asked_for_stays_even_if_the_filter_leaves_it_out(
    store, library
):
    session, player = session_on(store, library)
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0003"))
    session.set_settings({"include_without_lyrics": False})
    assert player.calls[-1] == ("reorder", ["TRK0000", "TRK0001", "TRK0002", "TRK0003"])
    assert player.bus.current.track_id == "TRK0003"
    # So a restart can still resume it.
    assert ids(session.queue_for(PLAYLIST_ID, start_track_id="TRK0003"))[-1] == "TRK0003"


def test_a_playlist_with_nothing_left_to_play_is_refused_with_the_reason(store, library):
    session, _ = session_on(store, library)
    for tid in SYNCED_IDS:
        assets.clear_lyrics(store, tid)
    session.set_settings({"include_without_lyrics": False})
    with pytest.raises(NotInCacheError, match="left out by a setting"):
        session.queue_for(PLAYLIST_ID)


@pytest.mark.parametrize("value", [1, 0, "false", None])
def test_a_filter_is_true_or_false_and_nothing_else(store, library, value):
    session, _ = session_on(store, library)
    with pytest.raises(SettingError, match="must be true or false"):
        session.set_settings({"include_without_audio": value})
    assert session.document()["settings"]["include_without_audio"] is True


def test_a_stored_filter_that_makes_no_sense_counts_as_on(store, library):
    store.write_json(store.session_json, {"settings": {"include_without_lyrics": "no"}})
    session, _ = session_on(store, library)
    assert session.document()["settings"]["include_without_lyrics"] is True


# ------------------------------ turning off ------------------------------ #


def test_a_track_turned_off_is_listed_but_not_queued_and_stays_off_after_a_restart(store, library):
    session, _ = session_on(store, library)
    session.set_disabled("TRK0001", True)
    arranged = session.arranged(library.playlist(PLAYLIST_ID))
    assert [t["id"] for t in arranged["tracks"]] == [*PLAYABLE, "TRK0004"]
    assert [t["id"] for t in arranged["tracks"] if t["disabled"]] == ["TRK0001"]
    assert (arranged["disabled"], arranged["hidden"]) == (1, 0)
    assert ids(session.queue_for(PLAYLIST_ID)) == ["TRK0000", "TRK0002", "TRK0003"]
    # Kept with the track, not the session.
    assert assets.read_track_state(store, "TRK0001").disabled is True
    assert "disabled" not in json.dumps(saved(store))
    later, _ = session_on(store, library)
    assert ids(later.queue_for(PLAYLIST_ID)) == ["TRK0000", "TRK0002", "TRK0003"]
    later.set_disabled("TRK0001", False)
    assert ids(later.queue_for(PLAYLIST_ID)) == PLAYABLE


def test_a_track_turned_off_keeps_its_place_in_a_kept_order(store, library):
    session, _ = session_on(store, library)
    order = session.shuffle(PLAYLIST_ID)
    session.set_disabled(order[1], True)
    assert ids(session.queue_for(PLAYLIST_ID)) == [t for t in order if t != order[1]]
    assert saved(store)["orders"] == {PLAYLIST_ID: order}
    session.set_disabled(order[1], False)
    assert ids(session.queue_for(PLAYLIST_ID)) == order


def test_a_track_turned_off_that_is_asked_for_plays_anyway(store, library):
    session, _ = session_on(store, library)
    session.set_disabled("TRK0002", True)
    queue = session.queue_for(PLAYLIST_ID, start_track_id="TRK0002")
    assert ids(queue) == PLAYABLE and queue.start_index == 2


def test_turning_off_another_track_brings_the_playing_queue_up_to_date(store, library):
    session, player = session_on(store, library)
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0001"))
    session.set_disabled("TRK0002", True)
    assert player.calls[-1] == ("reorder", ["TRK0000", "TRK0001", "TRK0003"])
    assert player.bus.current.track_id == "TRK0001"


@pytest.mark.parametrize("paused", [False, True])
def test_turning_off_the_song_that_is_on_skips_to_the_next_one_still_on(store, library, paused):
    session, player = session_on(store, library)
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0001"), paused=paused)
    session.set_disabled("TRK0002", True)
    session.set_disabled("TRK0001", True)
    assert player.calls[-1] == ("load_queue", ["TRK0000", "TRK0003"], 1)
    assert player.bus.current.track_id == "TRK0003"
    assert player.paused is paused


@pytest.mark.parametrize(("loop", "paused"), [(False, True), (True, False)])
def test_turning_off_the_last_song_goes_back_to_the_first(store, library, loop, paused):
    session, player = session_on(store, library)
    session.set_settings({"loop": loop})
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0003"))
    session.set_disabled("TRK0003", True)
    assert player.calls[-1] == ("load_queue", ["TRK0000", "TRK0001", "TRK0002"], 0)
    assert player.paused is paused


def test_the_shortcut_turns_off_the_song_that_is_on(store, library):
    session, player = session_on(store, library)
    assert session.disable_current() is None
    assert session.tracks_version == 0
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0000"))
    heard: list[int] = []
    session.subscribe_tracks(lambda: heard.append(session.tracks_version))
    assert session.disable_current() == "TRK0000"
    assert assets.read_track_state(store, "TRK0000").disabled is True
    assert player.bus.current.track_id == "TRK0001"
    assert heard == [1]


def test_turning_off_the_last_track_that_could_play_is_refused_and_nothing_is_kept(store, library):
    session, player = session_on(store, library)
    for tid in PLAYABLE[1:]:
        session.set_disabled(tid, True)
    player.load_queue(session.queue_for(PLAYLIST_ID, start_track_id="TRK0000"))
    calls, version = len(player.calls), session.tracks_version
    with pytest.raises(NotInCacheError, match="turned off"):
        session.set_disabled("TRK0000", True)
    assert assets.read_track_state(store, "TRK0000").disabled is False
    assert len(player.calls) == calls and session.tracks_version == version
