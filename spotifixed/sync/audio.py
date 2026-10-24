"""The audio stage: one correctly matched, tagged audio file per track.

Goal: ``audio.<ext>`` in the track's cache directory with ``audio.json`` beside it.

Without a pin: search, score the candidates (``matching.rank_audio``), and download the
best one if it clears the review threshold. If the first search turns up no album-audio
upload, a second, narrower search asks for one by name. When nothing clears the
threshold the track is reported as ``needs_review`` with its three best candidates, so
one can be chosen by hand with the ``pin`` command.

With a pin (``audio_pin`` in the track's state file): download exactly that URL. No
scoring and no duration check; the person who pinned it has decided.

Audio that ``align`` found to have no singing in it (a karaoke backing or an instrumental
under a song's name, see ``assets.voice_share``) is not done: it is listed in
``rejected_audio.json``, and the next best candidate that is not on that list replaces
it. Only so many are tried, since a song that is instrumental in every version would
otherwise use up its candidates one run at a time. A pinned file is never replaced.

A clean edit (``align`` heard it leave out the lyrics' strong words, see
``assets.is_clean_edit``) is replaced the same way, unless ``prefer_explicit`` is off.
Once a track has had a clean edit rejected, a third search asks for the explicit version
by name, and candidates are ranked as for an explicit track (``rank_audio``).

The downloaded stream is measured, then copied (never re-encoded) into a container that
carries title and artist tags, and only then moved to its final name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ..cache import CacheStore, assets
from ..errors import SourceError
from ..models import AudioAsset, Track
from ..sources.audio import AudioSource, media
from .matching import ScoredCandidate, rank_audio
from .stages import StageResult

_SEARCH_LIMIT = 10
# How many acceptable candidates to try when a download fails (removed, region-locked).
_MAX_DOWNLOAD_ATTEMPTS = 3
_REVIEW_LISTING = 3
# Versions of one track found unfit (no singing, a clean edit) before the stage stops looking.
_MAX_REJECTED = 3
_NO_SINGING = "no singing"
_CLEAN_EDIT = "clean edit"


class AudioStage:
    name = "audio"

    def __init__(
        self,
        store: CacheStore,
        source: AudioSource,
        *,
        tolerance_s: float,
        reject_s: float,
        prefer_explicit: bool = True,
    ) -> None:
        self._store = store
        self._source = source
        self._tolerance_s = tolerance_s
        self._reject_s = reject_s
        self._prefer_explicit = prefer_explicit

    def is_done(self, track: Track) -> bool:
        asset = assets.read_audio(self._store, track.id)
        if asset is None:
            return False
        pin = assets.read_track_state(self._store, track.id).audio_pin
        if pin is not None:
            # A pin added or changed after the download means the file on disk is the wrong one.
            return asset.pinned and asset.url == pin
        return self._unfit(track.id, asset) is None

    def _unfit(self, track_id: str, asset: AudioAsset) -> tuple[str, dict[str, float]] | None:
        """Why another version of the audio should still be looked for, or ``None``.

        The reason comes with what it was measured as, for ``rejected_audio.json``.
        """
        if asset.pinned:
            return None
        found: tuple[str, dict[str, float]] | None = None
        share = assets.voice_share(self._store, track_id)
        heard = assets.strong_words_heard(self._store, track_id)
        if share is not None and share < assets.NO_VOICE_SHARE:
            found = (_NO_SINGING, {"voice_share": share})
        elif self._prefer_explicit and heard is not None and heard < assets.CLEAN_EDIT_SHARE:
            found = (_CLEAN_EDIT, {"strong_words_heard": heard})
        if found is None:
            return None
        rejected = assets.rejected_audio(self._store, track_id)
        if asset.source_id in {entry.get("source_id") for entry in rejected}:
            return None  # already looked for another version, and kept this one
        if found[0] == _CLEAN_EDIT and any(
            entry.get("reason") == _CLEAN_EDIT
            and entry.get("strong_words_heard", float("inf")) <= round(heard, 3)  # as stored
            for entry in rejected
        ):
            # No better heard than a version already turned down: the words are hard to
            # hear in this song (few of them, or a voice the recognizer misses), not cut.
            return None
        return found if len(rejected) < _MAX_REJECTED else None

    def run(self, track: Track) -> StageResult:
        pin = assets.read_track_state(self._store, track.id).audio_pin
        if pin:
            asset = self._fetch(track, pin, score=None, pinned=True)
            return StageResult.done(f"pinned, {asset.duration_s:.0f}s")

        current = assets.read_audio(self._store, track.id)
        unfit = None if current is None else self._unfit(track.id, current)
        if current is not None and unfit is not None:
            assets.reject_audio(self._store, track.id, current, *unfit)
        rejected = assets.rejected_audio(self._store, track.id)
        cleaned = any(entry.get("reason") == _CLEAN_EDIT for entry in rejected)
        skip = {entry.get("source_id") for entry in rejected}

        ranked = [
            item for item in self._candidates(track, cleaned) if item.candidate.id not in skip
        ]
        acceptable = [item for item in ranked if item.acceptable]
        if not acceptable:
            listing = [item.summary() for item in ranked[:_REVIEW_LISTING]]
            if not ranked:
                return StageResult.miss("not_found", "no candidate of the right length")
            return StageResult.miss("needs_review", detail=listing)

        errors: list[str] = []
        for item in acceptable[:_MAX_DOWNLOAD_ATTEMPTS]:
            try:
                asset = self._fetch(track, item.candidate.url, score=item.score, pinned=False)
            except SourceError as exc:
                errors.append(str(exc))
                continue
            kind = "album audio" if item.album_audio else "video"
            return StageResult.done(f"{kind}, {asset.duration_s - track.duration_s:+.1f}s")
        raise SourceError("; ".join(errors))

    def _candidates(self, track: Track, cleaned: bool) -> list[ScoredCandidate]:
        """Search results, ranked; ``cleaned``: a clean edit of the track was turned down."""
        # The explicit recording is wanted when the track is flagged explicit, or when one
        # of its downloads was heard to be a clean edit; never with prefer_explicit off.
        explicit = self._prefer_explicit and (cleaned or track.explicit)

        def rank(found):
            return rank_audio(
                track,
                found,
                tolerance_s=self._tolerance_s,
                reject_s=self._reject_s,
                explicit=explicit,
            )

        found = self._source.search(f"{' '.join(track.artists)} {track.title}", _SEARCH_LIMIT)
        ranked = rank(found)
        if not any(item.album_audio and item.acceptable for item in ranked):
            # Album uploads live on auto-generated "<artist> - Topic" channels, which a
            # plain search often ranks below the music video.
            found += self._source.search(
                f"{track.primary_artist} - Topic {track.title}", _SEARCH_LIMIT
            )
            ranked = rank(found)
        if self._prefer_explicit and cleaned:
            # A plain search tends to find the clean edit again, often on several channels.
            found += self._source.search(
                f"{' '.join(track.artists)} {track.title} explicit", _SEARCH_LIMIT
            )
            ranked = rank(found)
        return ranked

    def _fetch(self, track: Track, url: str, *, score: float | None, pinned: bool) -> AudioAsset:
        track_dir = self._store.track_dir(track.id)
        with self._store.work_dir(track_dir) as work:
            downloaded = self._source.download(url, work)
            raw = media.probe(downloaded.path)
            if not pinned and abs(raw.duration_s - track.duration_s) > self._reject_s:
                raise SourceError(
                    f"downloaded audio is {raw.duration_s:.0f}s, expected about "
                    f"{track.duration_s:.0f}s; discarded"
                )
            extension = media.tagged_extension(raw.codec, downloaded.path.suffix)
            tagged = work / f"tagged.{extension}"
            media.remux(
                downloaded.path,
                tagged,
                {"title": track.title, "artist": ", ".join(track.artists), "album": track.album},
            )
            info = media.probe(tagged)
            if info.codec != raw.codec:
                raise SourceError(f"rewrapping changed the codec ({raw.codec} to {info.codec})")

            name = f"audio.{extension}"
            self._remove_other_audio(track_dir, keep=name)
            self._store.adopt(tagged, self._store.track_file(track.id, name))

        found = downloaded.candidate
        asset = AudioAsset(
            file=name,
            duration_s=round(info.duration_s, 3),
            source=self._source.name,
            source_id=found.id or None,
            url=url if pinned else (found.url or url),
            title=found.title or None,
            channel=found.channel or None,
            codec=info.codec,
            score=None if score is None else round(score, 1),
            pinned=pinned,
            fetched_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        assets.write_audio(self._store, track.id, asset)
        return asset

    def _remove_other_audio(self, track_dir: Path, *, keep: str) -> None:
        # A re-download can arrive in a different container; never leave two audio files.
        for old in track_dir.glob("audio.*"):
            if old.name not in (keep, "audio.json"):
                self._store.remove(old)
