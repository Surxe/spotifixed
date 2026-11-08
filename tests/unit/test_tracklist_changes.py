"""Comparing two tracklists, and carrying settings to tracks given a new ID."""

from __future__ import annotations

from spotifixed.cache import assets
from spotifixed.models import Playlist, Track, TrackState
from spotifixed.sources.tracklist import Tracklist
from spotifixed.sync import report
from spotifixed.sync.changes import Relink, carry_over, compare


def track(track_id: str, title: str = "", artist: str = "Artist", duration_ms: int = 200_000, **kw):
    return Track(track_id, title or f"Song {track_id}", (artist,), duration_ms, **kw)


def tracklist(*tracks: Track) -> Tracklist:
    playlist = Playlist("PL1", "Playlist", tuple(t.id for t in tracks))
    return Tracklist(playlist, tuple(tracks))


def test_identical_tracklists_have_no_changes():
    a, b = track("A"), track("B")
    changes = compare(tracklist(a, b), tracklist(a, b))
    assert changes.empty
    assert report.changes_lines(changes) == ["Changes since the previous tracklist: none"]


def test_added_and_removed_tracks():
    a, b, c = track("A"), track("B"), track("C")
    changes = compare(tracklist(a, b), tracklist(a, c))
    assert changes.added == (c,) and changes.removed == (b,) and not changes.relinked


def test_the_same_song_under_a_new_id_is_a_relink():
    old = track("OLD", "Radioactive", "Imagine Dragons", 186_813)
    new = track("NEW", "Radioactive - Remastered 2020", "Imagine Dragons", 187_500)
    changes = compare(tracklist(track("A"), old), tracklist(track("A"), new))
    assert changes.relinked == (Relink(old, new),)
    assert not changes.added and not changes.removed


def test_a_same_titled_song_of_a_different_length_is_not_a_relink():
    old = track("OLD", "Song", duration_ms=200_000)
    new = track("NEW", "Song", duration_ms=260_000)
    changes = compare(tracklist(old), tracklist(new))
    assert not changes.relinked and changes.added == (new,) and changes.removed == (old,)


def test_changed_details_on_a_kept_id_are_reported_but_not_the_cover():
    old = track("A", "Song", album="One", cover_url="https://images.invalid/1.jpg")
    new = track("A", "Song (feat. X)", album="One", cover_url="https://images.invalid/2.jpg")
    (change,) = compare(tracklist(old), tracklist(new)).changed
    assert change.fields == ("title",)


def test_moved_counts_reordering_but_not_shifts_from_inserts():
    a, b, c, d = track("A"), track("B"), track("C"), track("D")
    assert compare(tracklist(a, b, c), tracklist(d, a, b, c)).moved == 0
    assert compare(tracklist(a, b, c), tracklist(c, a, b)).moved == 3


def test_carry_over_copies_state_and_cached_files_but_not_track_json(store):
    old, new = track("OLD"), track("NEW")
    assets.write_track_state(store, "OLD", TrackState(offset_ms=250, disabled=True))
    store.write_json(store.track_json("OLD"), old.to_dict())
    store.write_json(store.audio_json("OLD"), {"file": "audio.opus"})
    store.write_bytes(store.track_file("OLD", "audio.opus"), b"sound")

    (result,) = carry_over(store, (Relink(old, new),))

    assert result.state and result.files == 2
    assert assets.read_track_state(store, "NEW") == TrackState(offset_ms=250, disabled=True)
    assert store.track_file("NEW", "audio.opus").read_bytes() == b"sound"
    assert not store.track_json("NEW").exists()
    # The old ID keeps everything.
    assert store.has(store.track_state("OLD")) and store.has(store.track_file("OLD", "audio.opus"))


def test_carry_over_never_overwrites_what_the_new_id_has(store):
    old, new = track("OLD"), track("NEW")
    assets.write_track_state(store, "OLD", TrackState(offset_ms=250))
    assets.write_track_state(store, "NEW", TrackState(offset_ms=-100))
    store.write_json(store.audio_json("OLD"), {"file": "a"})
    store.write_json(store.audio_json("NEW"), {"file": "b"})

    (result,) = carry_over(store, (Relink(old, new),))

    assert not result.state and result.files == 0
    assert assets.read_track_state(store, "NEW").offset_ms == -100
    assert store.read_json(store.audio_json("NEW")) == {"file": "b"}


def test_report_names_hand_made_settings_of_removed_tracks():
    gone = track("B", "Gone Song")
    changes = compare(tracklist(track("A"), gone), tracklist(track("A")))
    lines = report.changes_lines(changes, states={"B": TrackState(offset_ms=-120, disabled=True)})
    assert "1 removed" in lines[0]
    assert any("Gone Song" in line and "turned off, offset -120 ms" in line for line in lines)
