"""The listening: isolate the voice, hear its letters, and place each line's words.

This is the only module that needs PyTorch, torchaudio and Demucs (``requirements-
align.txt``); they are imported when an engine is built, so the rest of the program runs
without them. Two models do the work:

* Demucs (``htdemucs``) separates the voice from the music. The acoustic model was
  trained on speech, and hears a bare voice far better than one under a band.
* MMS forced-alignment (wav2vec 2.0, CTC) gives, for every 20 ms of that voice, how
  likely each letter is. One pass over the track; each line is then placed inside its
  own window of those frames by the standard CTC alignment.

A third model is loaded only for a track that asks for it: Whisper (``large-v3-turbo``,
through ``transformers``) writes down what the separated voice says, so that ``run`` can
count the strong words that were really sung (see :func:`~.plan.strong_words`).

Audio is decoded by ffmpeg, which the audio stage already depends on.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..errors import PlaylistPlayerError
from .plan import ANY, FRAME_MS, WordSpan

NAME = "mms_fa+htdemucs"
_SEPARATION_RATE = 44_100
_MODEL_RATE = 16_000
_CHUNK_S = 30
_RECOGNIZER = "openai/whisper-large-v3-turbo"


class TorchEngine:
    name = NAME

    def __init__(self, *, device: str | None = None, ffmpeg: str = "ffmpeg") -> None:
        try:
            import numpy
            import torch
            import torchaudio
            from demucs.apply import apply_model
            from demucs.pretrained import get_model
        except ImportError as exc:
            raise PlaylistPlayerError(
                f"word alignment needs extra packages ({exc.name} is missing); install them "
                "with: .venv/bin/pip install -r requirements-align.txt"
            ) from exc
        self._np, self._torch, self._audio = numpy, torch, torchaudio
        self._apply_model = apply_model
        self._ffmpeg = ffmpeg
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._recognizer: Any = None
        bundle = torchaudio.pipelines.MMS_FA
        self._letters: dict[str, int] = bundle.get_dict(star=ANY)
        self._acoustic = bundle.get_model(with_star=True).to(self.device).eval()
        self._separator = get_model("htdemucs").to(self.device).eval()

    # ------------------------------- listening ------------------------------ #

    def _decode(self, path: Path) -> Any:
        """The file as a ``[2, samples]`` float tensor at the separator's rate."""
        done = subprocess.run(
            [self._ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2",
             "-ar", str(_SEPARATION_RATE), "-"],
            capture_output=True,
        )  # fmt: skip
        if done.returncode != 0 or not done.stdout:
            detail = done.stderr.decode(errors="replace").strip()[-300:]
            raise PlaylistPlayerError(f"ffmpeg could not decode {path.name}: {detail}")
        samples = self._np.frombuffer(done.stdout, dtype=self._np.float32).copy()
        return self._torch.from_numpy(samples).view(-1, 2).T

    def _voice(self, path: Path) -> Any:
        """The voice alone, mono, at the acoustic model's rate: ``[1, samples]``."""
        torch = self._torch
        mix = self._decode(path)
        level = mix.mean(0)
        mix = (mix - level.mean()) / (level.std() + 1e-8)
        # The track stays in main memory and goes to the device a segment at a time: a long
        # track whole on a GPU that something else is also using does not fit.
        with torch.no_grad():
            stems = self._apply_model(
                self._separator, mix[None], device=self.device, split=True, overlap=0.1
            )[0]
        voice = stems[self._separator.sources.index("vocals")].mean(0, keepdim=True)
        return self._audio.functional.resample(voice, _SEPARATION_RATE, _MODEL_RATE)

    def _hear(self, voice: Any) -> Any:
        """Log-probability of every letter in every frame: ``[frames, letters]``."""
        torch = self._torch
        heard = []
        with torch.inference_mode():
            for piece in voice.split(_CHUNK_S * _MODEL_RATE, dim=1):
                if piece.shape[1] < _MODEL_RATE // 10:
                    break  # too short for the model's first layer
                heard.append(self._acoustic(piece.to(self.device))[0][0].cpu())
        return torch.cat(heard) if heard else torch.empty(0, len(self._letters))

    def _transcribe(self, voice: Any) -> str:
        """What the voice says, as Whisper writes it down."""
        if self._recognizer is None:
            try:
                from transformers import pipeline
            except ImportError as exc:
                raise PlaylistPlayerError(
                    "listening for a clean edit needs transformers; install it with: "
                    ".venv/bin/pip install -r requirements-align.txt"
                ) from exc
            on_gpu = self.device != "cpu"
            self._recognizer = pipeline(
                "automatic-speech-recognition",
                model=_RECOGNIZER,
                dtype=self._torch.float16 if on_gpu else self._torch.float32,
                device=self.device,
            )
        heard = self._recognizer(
            {"raw": voice[0].cpu().numpy(), "sampling_rate": _MODEL_RATE},
            chunk_length_s=_CHUNK_S,
            batch_size=8,
            generate_kwargs={"language": "en", "task": "transcribe"},
        )
        return heard["text"]

    def _loudness(self, voice: Any, frames: int) -> list[float]:
        """The voice's level (RMS) in each of the model's frames."""
        size = _MODEL_RATE * FRAME_MS // 1000
        usable = voice[0, : frames * size].cpu()
        level = usable.view(-1, size).pow(2).mean(1).sqrt().tolist()
        return level + [0.0] * (frames - len(level))

    # -------------------------------- placing ------------------------------- #

    def align(
        self,
        audio: Path,
        spelled: Sequence[Sequence[str]],
        windows_for: Any,
        *,
        transcribe: bool = False,
    ) -> tuple[list[list[WordSpan] | None], list[float], str | None]:
        """Where each line's words are, the voice's level in every frame, and its words.

        ``spelled`` is each line's words as :func:`~.plan.spell` gives them, and
        ``windows_for(frames)`` each line's window once the track's length is known. The
        words the voice says are written down only with ``transcribe``, else ``None``.
        """
        try:
            voice = self._voice(audio)
            heard = self._hear(voice)
            said = self._transcribe(voice) if transcribe else None
        except self._torch.OutOfMemoryError as exc:
            # One track too many for the memory free right now must not end the run.
            self._torch.cuda.empty_cache()
            raise PlaylistPlayerError(f"out of GPU memory on {audio.name}") from exc
        frames = heard.shape[0]
        found = [
            self._place(heard, words, window) if words and window else None
            for words, window in zip(spelled, windows_for(frames), strict=True)
        ]
        return found, self._loudness(voice, frames), said

    def _place(
        self, heard: Any, words: Sequence[str], window: tuple[int, int]
    ) -> list[WordSpan] | None:
        torch, forced = self._torch, self._audio.functional
        first, last = window
        tokens = [[self._letters[letter] for letter in word] for word in words]
        flat = [token for word in tokens for token in word]
        if last - first < 2 * len(flat) + 1:
            return None  # more letters than the window has room for
        path, scores = forced.forced_align(
            heard[first:last][None], torch.tensor([flat], dtype=torch.int32), blank=0
        )
        letters = forced.merge_tokens(path[0], scores[0].exp())
        spans, at = [], 0
        for word in tokens:
            mine = letters[at : at + len(word)]
            at += len(word)
            held = sum(letter.end - letter.start for letter in mine)
            score = sum(letter.score * (letter.end - letter.start) for letter in mine) / held
            spans.append(WordSpan(first + mine[0].start, first + mine[-1].end, float(score)))
        return spans
