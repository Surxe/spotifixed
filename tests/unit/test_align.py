"""Word alignment: the arithmetic, the run over tracks, and what reaches the API.

The engine here is a stand-in; the real one needs PyTorch and real singing to hear.
"""

from __future__ import annotations

import pytest

from spotifixed.align import MIN_CONFIDENCE, WordSpan, align_tracks, settle, spell, summary
from spotifixed.align import windows as line_windows
from spotifixed.align.plan import AUDIBLE_VOICE, strong_words, voice_share
from spotifixed.cache import assets
from spotifixed.errors import PlaylistPlayerError
from spotifixed.lyrics import parse_lrc
from spotifixed.models import AudioAsset, LyricLine
from spotifixed.server.library import Library
from tests.support.library import PLAYLIST_ID, SYNCED, build_library, track_id

# ----------------------------------- plan ---------------------------------- #


@pytest.mark.parametrize(
    ("word", "letters"),
    [("Love,", "love"), ("don't", "don't"), ("'Cause", "cause"), ("Señor", "senor"),
     ("pre-K!\"", "prek"), ("2", "*"), ("...", "*"), ("愛", "*")],
)  # fmt: skip
def test_a_word_is_spelled_in_the_letters_the_model_knows(word, letters):
    assert spell(word) == letters


def test_each_line_is_looked_for_around_its_own_span():
    lines = parse_lrc("[00:10.00] one two\n[00:12.00]\n[00:20.00] three\n")
    # 400 ms before the line to 600 ms after the next one starts, in 20 ms frames.
    assert line_windows(lines, 0, 5000) == [(480, 630), None, (980, 1780)]
    # The last line gets 15 s, or whatever the track has left.
    assert line_windows(lines, 0, 1200)[2] == (980, 1200)
    # Lyrics that run 1 s late against this audio are looked for 1 s earlier in it.
    assert line_windows(lines, 1000, 5000)[0] == (430, 580)
    # A line that would lie before the audio begins has nowhere to be looked for.
    assert line_windows(lines, 20_000, 5000)[0] is None


def spans(*triples):
    return [WordSpan(*triple) for triple in triples]


def test_a_word_lasts_until_the_next_one_starts():
    found = [spans((100, 105, 0.9), (120, 130, 0.9), (160, 170, 0.9))]
    assert settle(found, [0.0] * 400) == [((2000, 2400), (2400, 3200), (3200, 3400))]


def test_a_line_the_model_was_not_sure_of_is_left_out():
    sure = spans((10, 20, MIN_CONFIDENCE + 0.1), (30, 40, MIN_CONFIDENCE))
    unsure = spans((50, 60, MIN_CONFIDENCE - 0.05), (70, 80, MIN_CONFIDENCE))
    settled = settle([sure, unsure, None], [0.0] * 200)
    assert settled[0] is not None and settled[1] is None and settled[2] is None


def test_the_last_word_is_held_while_the_voice_stays_up():
    loudness = [0.0] * 400
    loudness[100:260] = [1.0] * 160  # the voice carries on to frame 260
    found = [spans((100, 110, 0.9), (150, 160, 0.9))]
    assert settle(found, loudness)[0][-1] == (3000, 5200)


def test_a_held_word_stops_where_the_next_line_begins():
    loudness = [1.0] * 400  # never quiet
    found = [spans((100, 110, 0.9)), None, spans((200, 210, 0.9))]
    first, _, last = settle(found, loudness)
    assert first == ((2000, 4000),)
    assert last == ((4000, 8000),)  # nothing follows: to the end of the track


# ----------------------------------- run ----------------------------------- #


class FakeEngine:
    """Places every word a second after the one before, from the start of its window."""

    name = "fake"
    built = 0
    level = 0.0  # the voice's level in every frame
    said = ""  # what a recognizer writes down of the voice
    transcribed = 0  # tracks it was asked to write down

    def __init__(self) -> None:
        type(self).built += 1
        self.heard: list[str] = []

    def align(self, audio, spelled, windows_for, *, transcribe=False):
        if audio.read_bytes() == b"unreadable":
            raise PlaylistPlayerError("ffmpeg could not decode it")
        self.heard.append(audio.name)
        found = []
        for words, window in zip(spelled, windows_for(1000), strict=True):
            if not words or window is None:
                found.append(None)
                continue
            start = window[0] + 20  # 400 ms of padding in: the line's own timestamp
            found.append(
                [WordSpan(start + 50 * k, start + 50 * k + 5, 0.9) for k in range(len(words))]
            )
        type(self).transcribed += transcribe
        return found, [self.level] * 1000, self.said if transcribe else None


@pytest.fixture
def library(store):
    FakeEngine.built = FakeEngine.transcribed = 0
    FakeEngine.level, FakeEngine.said = 0.0, ""
    build_library(store)
    return store


def run(store, ids, **kwargs):
    return align_tracks(store, ids, FakeEngine, FakeEngine.name, **kwargs)


def test_a_track_with_synced_lyrics_and_audio_gets_its_word_times(library):
    (report,) = run(library, [track_id(0)])
    assert (report.outcome, report.lines, report.placed) == ("aligned", 2, 2)
    stored = library.read_json(library.words_json(track_id(0)))
    assert stored["engine"] == "fake" and stored["audio"]["file"] == "audio.opus"
    # "an invented first line" is stamped 1.0 s; the pause line has no words.
    assert stored["lines"][0] == [[1000, 2000], [2000, 3000], [3000, 4000], [4000, 4100]]
    assert stored["lines"][1] is None


def test_tracks_that_cannot_be_aligned_are_skipped_and_say_why(library):
    plain, no_audio = run(library, [track_id(3), track_id(4)])
    assert (plain.outcome, plain.detail) == ("skipped", "no synced lyrics")
    assert (no_audio.outcome, no_audio.detail) == ("skipped", "no audio")
    assert FakeEngine.built == 0  # nothing to listen to: no model was loaded


def test_a_second_run_does_nothing_until_something_changes(library):
    ids = [track_id(0), track_id(1), track_id(0)]  # a playlist may repeat a track
    assert [r.outcome for r in run(library, ids)] == ["aligned", "aligned"]
    assert [r.outcome for r in run(library, ids)] == ["current", "current"]
    assert [r.outcome for r in run(library, ids, force=True)] == ["aligned", "aligned"]

    # A changed offset moves where the lines are looked for.
    state = assets.read_track_state(library, track_id(0))
    assets.write_track_state(library, track_id(0), type(state)(offset_ms=500))
    assert [r.outcome for r in run(library, ids)] == ["aligned", "current"]


def test_new_audio_drops_the_word_times_that_were_heard_in_the_old(library):
    run(library, [track_id(0)])
    assert library.has(library.words_json(track_id(0)))
    asset = AudioAsset(file="audio.opus", duration_s=5, source="test", codec="opus")
    assets.write_audio(library, track_id(0), asset)
    assert not library.has(library.words_json(track_id(0)))


def test_a_track_that_cannot_be_decoded_fails_alone(library):
    library.write_bytes(library.track_file(track_id(0), "audio.opus"), b"unreadable")
    failed, fine = run(library, [track_id(0), track_id(1)])
    assert failed.outcome == "failed" and "decode" in failed.detail
    assert fine.outcome == "aligned"
    assert summary([failed, fine]) == (
        "1 aligned, 2 of 2 lines placed (100%); 0 already current, 0 skipped, 1 failed"
    )


def test_the_api_sends_word_times_only_for_the_text_they_were_made_from(library):
    api = Library(library)
    assert all("words" not in line for line in api.lyrics(track_id(0))["lines"])

    run(library, [track_id(0)])
    first, pause, _ = api.lyrics(track_id(0))["lines"]
    assert first["words"] == [[1000, 2000], [2000, 3000], [3000, 4000], [4000, 4100]]
    assert "words" not in pause

    # Different lyrics for the same track: the stored times are for other words.
    info = assets.read_lyrics_info(library, track_id(0))
    assets.write_lyrics(library, track_id(0), info, SYNCED.replace("first", "changed"))
    assert all("words" not in line for line in api.lyrics(track_id(0))["lines"])
    assert PLAYLIST_ID  # the playlist itself is untouched by any of this


def test_a_line_keeps_its_word_times_through_json():
    line = LyricLine(1000, "two words", ((1000, 1400), (1400, 2000)))
    assert LyricLine.from_dict(line.to_dict()) == line
    assert LyricLine.from_dict({"t_ms": 5, "text": "x"}) == LyricLine(5, "x")


def test_voice_share_counts_the_frames_a_voice_is_heard_in():
    assert voice_share([]) == 0.0
    assert voice_share([0.0, AUDIBLE_VOICE, 0.5, AUDIBLE_VOICE / 2]) == 0.5


def test_an_aligned_track_records_how_much_of_it_has_a_voice(library):
    FakeEngine.level = 1.0
    try:
        (sung,) = run(library, [track_id(0)])
    finally:
        FakeEngine.level = 0.0
    assert assets.voice_share(library, track_id(0)) == 1.0 and sung.detail == ""

    (silent,) = run(library, [track_id(1)])
    assert assets.voice_share(library, track_id(1)) == 0.0
    assert silent.outcome == "aligned" and "no singing heard" in silent.detail


# ------------------------------- clean edits -------------------------------- #


@pytest.mark.parametrize(
    ("text", "count"),
    [("fuck this shit", 2), ("F**kin' perfect", 1), ("sh*t, b*tches, n***as", 3),
     ("motherfucker", 1), ("damn, what the hell", 0), ("fun bits, a pushy nigerian", 0)],
)  # fmt: skip
def test_strong_words_are_counted_also_as_written_with_asterisks(text, count):
    assert strong_words(text) == count


STRONG = "[00:01.00] shit, fuck, bitch\n[00:02.50]\n[00:04.00] one more fuck\n"


def with_strong_lyrics(store, tid):
    FakeEngine.level = 1.0  # sung: the voice is up throughout
    info = assets.read_lyrics_info(store, tid)
    assets.write_lyrics(store, tid, info, STRONG)


def test_lyrics_with_strong_words_are_listened_to_for_a_clean_edit(library):
    with_strong_lyrics(library, track_id(0))
    FakeEngine.said = "shit, mm, bitch, one more"
    plain, cleaned = run(library, [track_id(1), track_id(0)])
    assert FakeEngine.transcribed == 1  # lyrics with no strong words are not written down
    assert "strong_words" not in library.read_json(library.words_json(track_id(1)))
    assert assets.strong_words_heard(library, track_id(1)) is None

    stored = library.read_json(library.words_json(track_id(0)))
    assert stored["strong_words"] == {"in_lyrics": 4, "heard": 2}
    assert assets.strong_words_heard(library, track_id(0)) == 0.5
    assert assets.is_clean_edit(library, track_id(0))
    assert "clean edit" in cleaned.detail and plain.detail == ""


def test_audio_heard_saying_the_strong_words_is_not_a_clean_edit(library):
    with_strong_lyrics(library, track_id(0))
    FakeEngine.said = "Shit, f***, bitch, one more fuck"
    (report,) = run(library, [track_id(0)])
    assert not assets.is_clean_edit(library, track_id(0)) and report.detail == ""


def test_with_clean_edits_wanted_the_recognizer_is_never_asked(library):
    with_strong_lyrics(library, track_id(0))
    run(library, [track_id(0)], listen_for_clean_edits=False)
    assert FakeEngine.transcribed == 0
    assert assets.strong_words_heard(library, track_id(0)) is None
