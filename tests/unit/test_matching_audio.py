"""Audio candidate scoring over synthetic candidate lists."""

from __future__ import annotations

import pytest

from spotifixed.models import Track
from spotifixed.sync.matching import (
    AUDIO_REVIEW_THRESHOLD,
    is_album_audio,
    rank_audio,
    score_audio,
)
from tests.support.audio import candidate

TRACK = Track(
    id="T1",
    title="Invented Song 0 (feat. A Guest)",
    artists=("Made Up Artist 0", "A Guest"),
    duration_ms=200_000,
)
BOUNDS = {"tolerance_s": 3.0, "reject_s": 7.0}


def score(cand, track=TRACK):
    return score_audio(track, cand, **BOUNDS)


def test_album_audio_on_a_topic_channel_gets_every_point():
    result = score(candidate("a", 200, title="Invented Song 0", channel="Made Up Artist 0 - Topic"))
    assert result.score == 110 and result.album_audio and result.acceptable
    assert result.duration_delta_s == 0


def test_album_audio_is_also_recognized_by_the_labels_credit():
    by_credit = candidate("a", 200, description="Provided to YouTube by Some Label\n\nInvented")
    assert is_album_audio(by_credit) and score(by_credit).score == 110
    assert not is_album_audio(candidate("b", 200, description="Not provided to YouTube by anyone"))
    assert not is_album_audio(candidate("c", 200, channel="Topic Fans"))


def test_an_official_video_of_the_right_length_is_acceptable_but_ranks_below_album_audio():
    video = score(candidate("v", 200))
    assert video.score == 85 and not video.album_audio and video.acceptable
    ranked = rank_audio(
        TRACK,
        [candidate("v", 200), candidate("a", 201, channel="Made Up Artist 0 - Topic")],
        **BOUNDS,
    )
    assert [item.candidate.id for item in ranked] == ["a", "v"]


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(200, 40), (203, 40), (197, 40), (205, 20), (195, 20), (207, 0), (193, 0)],
)
def test_duration_points_are_full_inside_tolerance_and_slide_to_zero_at_the_reject_bound(
    duration, expected
):
    assert score(candidate("x", duration)).score == pytest.approx(expected + 30 + 15)


@pytest.mark.parametrize("duration", [None, 207.5, 192.4, 400, 30])
def test_no_duration_or_beyond_the_reject_bound_is_rejected_outright(duration):
    assert score(candidate("x", duration)) is None


@pytest.mark.parametrize(
    "label",
    [
        "Live",
        "Remix",
        "Cover",
        "Karaoke",
        "Instrumental",
        "Sped Up",
        "Slowed",
        "Nightcore",
        "8D",
        "Reverb",
        "Acoustic",
        "Reaction",
    ],  # fmt: skip
)
def test_each_other_version_label_drops_a_candidate_below_the_threshold(label):
    result = score(candidate("x", 200, title=f"Made Up Artist 0 - Invented Song 0 ({label})"))
    assert result.score == 85 - 60 and not result.acceptable
    assert f"labelled {label.lower()}" in result.reasons


def test_several_labels_cost_the_same_as_one():
    result = score(candidate("x", 200, title="Made Up Artist 0 - Invented Song 0 slowed reverb"))
    assert result.score == 25 and "labelled slowed, reverb" in result.reasons


def test_a_label_the_track_itself_carries_is_not_penalized():
    live_track = Track(id="T2", title="Invented Song 0 - Live", artists=("Made Up Artist 0",),
                       duration_ms=200_000)  # fmt: skip
    live = candidate("x", 200, title="Made Up Artist 0 - Invented Song 0 (Live)")
    assert score(live, live_track).score == 85


def test_labels_match_whole_words_only_and_lyrics_is_neutral():
    assert (
        score(candidate("x", 200, title="Made Up Artist 0 - Invented Song 0 (Lyrics)")).score == 85
    )
    # "alive" and "discovery" contain "live" and "cover" but are not labels.
    alive = candidate("x", 200, title="Made Up Artist 0 - Invented Song 0 alive discovery")
    assert score(alive).score == 85


def test_a_different_title_needs_review_even_as_album_audio():
    other = candidate("x", 200, title="Some Other Song", channel="Made Up Artist 0 - Topic")
    result = score(other)
    assert result.score == 40 - 30 + 15 + 25 and not result.acceptable
    assert "title differs" in result.reasons


def test_artist_name_counts_in_the_title_or_the_channel_and_any_artist_will_do():
    in_title = candidate("a", 200, title="Made Up Artist 0 - Invented Song 0", channel="Uploader")
    in_channel = candidate("b", 200, title="Invented Song 0", channel="Made Up Artist 0")
    guest = candidate("c", 200, title="Invented Song 0", channel="A Guest")
    nobody = candidate("d", 200, title="Invented Song 0", channel="Uploader")
    assert score(in_title).score == score(in_channel).score == score(guest).score == 85
    assert score(nobody).score == 70 and "no artist name" in score(nobody).reasons


def test_title_matching_ignores_decoration_on_either_side():
    decorated = candidate("x", 200, title="MADE UP ARTIST 0 – Inventéd Song 0 [Official]")
    assert score(decorated).score == 85


def test_a_title_with_brackets_in_the_middle_still_matches():
    # Stripping the bracket leaves "odd remix", which is not a phrase of the upload's title.
    track = Track(
        id="T3", title="Odd (B$$) Remix", artists=("Made Up Artist 0",), duration_ms=200_000
    )
    upload = candidate("x", 200, title="Odd (B$$) (Remix)", channel="Made Up Artist 0 - Topic")
    result = score(upload, track)
    assert result.score == 110 and "title differs" not in result.reasons


def test_the_review_threshold():
    # Right length, right title, unknown uploader: just acceptable.
    assert score(candidate("a", 200, title="Invented Song 0", channel="x")).score == 70
    # Five seconds off with no artist name: 20 + 30 = 50, flagged for review.
    weak = score(candidate("b", 205, title="Invented Song 0", channel="x"))
    assert weak.score == 50 < AUDIO_REVIEW_THRESHOLD and not weak.acceptable


def test_ranking_is_best_first_stable_and_deduplicated():
    found = [
        candidate("first", 200),
        candidate("second", 200),
        candidate("first", 200),
        candidate("rejected", 300),
        candidate("best", 200, channel="Made Up Artist 0 - Topic"),
    ]
    assert [item.candidate.id for item in rank_audio(TRACK, found, **BOUNDS)] == [
        "best",
        "first",
        "second",
    ]


def test_an_audio_upload_edges_out_a_video_of_the_same_length():
    audio = candidate("a", 200, title="Made Up Artist 0 - Invented Song 0 (Official Audio)")
    assert score(audio).score == 90 and "audio upload" in score(audio).reasons
    ranked = rank_audio(TRACK, [candidate("v", 200), audio], **BOUNDS)
    assert [item.candidate.id for item in ranked] == ["a", "v"]
    # Album audio does not collect the label bonus on top.
    topic = candidate("t", 200, title="Invented Song 0 (Audio)", channel="Made Up Artist 0 - Topic")
    assert score(topic).score == 110


def test_equal_scores_go_to_the_closer_duration():
    ranked = rank_audio(TRACK, [candidate("far", 203), candidate("near", 201)], **BOUNDS)
    assert [item.candidate.id for item in ranked] == ["near", "far"]


def test_summary_lists_what_a_person_needs_to_pin_by_hand():
    summary = score(
        candidate("x", 204, title="Made Up Artist 0 - Invented Song 0 (Live)")
    ).summary()
    assert summary["url"] == "https://video.invalid/watch?v=x" and summary["duration_s"] == 204
    assert summary["reasons"] == ["duration +4s", "labelled live"]
    assert summary["score"] == pytest.approx(30 + 30 + 15 - 60)


@pytest.mark.parametrize(
    "credit",
    ["Originally Performed By", "Made Famous by", "made popular by", "In the Style of",
     "as performed by", "originally by"],
)  # fmt: skip
def test_a_karaoke_credit_drops_a_candidate_and_earns_no_artist_points(credit):
    upload = candidate(
        "k", 200, title=f"Invented Song 0 ({credit} Made Up Artist 0)", channel="Sing Along - Topic"
    )
    result = score(upload)
    # The artist is named only as the song's original performer: no artist points.
    assert result.score == 40 + 30 + 25 - 60 and not result.acceptable
    assert "no artist name" in result.reasons
    assert f"labelled {credit.lower()}" in result.reasons


def test_a_karaoke_channel_drops_a_candidate_whose_title_looks_like_the_song():
    upload = candidate("k", 200, title="Invented Song 0", channel="Hit The Karaoke Button - Topic")
    result = score(upload)
    assert result.score == 40 + 30 + 25 - 60 and "labelled karaoke" in result.reasons
    credit = candidate("c", 200, title="Invented Song 0", channel="Made famous by Made Up Artist 0")
    assert not score(credit).acceptable and "no artist name" in score(credit).reasons


def test_only_karaoke_words_mark_a_channel_and_a_credit_in_the_track_title_is_not_penalized():
    live_channel = candidate(
        "l", 200, title="Invented Song 0", channel="Live Nation Acoustic Covers"
    )
    assert score(live_channel).score == 70
    tribute = Track(id="T4", title="Invented Song 0 (Made Famous by Someone)",
                    artists=("Made Up Artist 0",), duration_ms=200_000)  # fmt: skip
    same = candidate("t", 200, title="Made Up Artist 0 - Invented Song 0 (Made Famous by Someone)")
    assert score(same, tribute).score == 85


@pytest.mark.parametrize("label", ["TV Mix", "Backing Track", "Off Vocal"])
def test_backing_track_labels_drop_a_candidate(label):
    result = score(candidate("x", 200, title=f"Made Up Artist 0 - Invented Song 0 ({label})"))
    assert result.score == 85 - 60 and f"labelled {label.lower()}" in result.reasons


# ------------------------------ explicit tracks ----------------------------- #

EXPLICIT = Track(
    id="T2",
    title="Invented Song 0",
    artists=("Made Up Artist 0",),
    duration_ms=200_000,
    explicit=True,
)


@pytest.mark.parametrize("label", ["Clean", "Clean Version", "Edited", "Radio Edit", "Censored"])
def test_for_an_explicit_track_a_clean_edit_is_another_version(label):
    cleaned = candidate("c", 200, title=f"Invented Song 0 ({label})")
    assert not score(cleaned, EXPLICIT).acceptable
    assert score(cleaned, TRACK).score == score(candidate("p", 200, title="Invented Song 0")).score


def test_for_an_explicit_track_a_title_promising_the_explicit_version_scores():
    plain = score(candidate("p", 200, title="Invented Song 0"), EXPLICIT)
    for label in ("Explicit", "Dirty", "Uncensored"):
        promised = score(candidate("e", 200, title=f"Invented Song 0 [{label}]"), EXPLICIT)
        assert promised.score > plain.score and "labelled explicit" in promised.reasons
    # Not for a track that is not explicit, and not when the explicit one is not wanted.
    promise = candidate("e", 200, title="Invented Song 0 (feat. A Guest) [Explicit]")
    assert score(promise).score == score(candidate("p", 200, title="Invented Song 0")).score
    wanted = score_audio(EXPLICIT, candidate("c", 200, title="Invented Song 0 (Clean)"),
                         explicit=False, **BOUNDS)  # fmt: skip
    assert wanted.acceptable


def test_a_clean_label_in_an_artists_name_is_not_a_clean_edit():
    band = Track(id="T3", title="Invented Song 0", artists=("Clean Bandit",), duration_ms=200_000,
                 explicit=True)  # fmt: skip
    assert score(candidate("b", 200, title="Clean Bandit - Invented Song 0"), band).acceptable
