"""A fake ASR backend for tests: no torch, no GPU, no models, no HF token."""

from __future__ import annotations

from pathlib import Path

from ciw.asr.base import ProgressFn, Transcriber
from ciw.models import StageStats, Transcript, Word
from ciw.vocab import current_prompt_hash


class FakeTranscriber(Transcriber):
    """Returns a canned transcript; records calls; can be told to fail for some files."""

    name = "fake"

    def __init__(self, cfg=None, *, model: str | None = None, diarize: bool | None = None, **_):
        self.cfg = cfg
        self._model = model or (cfg.asr.model if cfg else "fake-model")
        self.words: list[Word] | None = None  # canned words; default "Hello carbonation."
        self.diarize = True if diarize is None else diarize
        self.calls: list[Path] = []
        self.fail_on: set[str] = set()
        self.version_string = "fake 1.0"

    @property
    def model(self) -> str:
        return self._model

    @property
    def version(self) -> str:
        return self.version_string

    def vocab_hash(self) -> str | None:
        """Like the real backend: the hash of the prompt the configured vocabulary gives."""
        if self.cfg is None:
            return None
        return current_prompt_hash(
            self.cfg.asr.vocabulary_file, self.cfg.asr.initial_prompt_max_chars
        )

    def transcribe(
        self,
        audio_path: Path,
        *,
        initial_prompt: str | None = None,
        progress: ProgressFn | None = None,
    ) -> Transcript:
        self.calls.append(Path(audio_path))
        if Path(audio_path).stem in self.fail_on:
            raise RuntimeError(f"boom on {Path(audio_path).stem}")
        for stage in ("asr", "align"):
            if progress:
                progress(stage, None)
                progress(stage, 1.0)
        spk = "SPEAKER_00" if self.diarize else None
        words = self.words or [
            Word(w="Hello", start=0.0, end=0.5, speaker=spk),
            Word(w="carbonation.", start=0.6, end=1.4, speaker=spk),
        ]
        return Transcript(
            asr_model=self.model,
            asr_version=self.version,
            asr_backend=self.name,
            compute_type="float16",
            batch_size=8,
            diarization=self.diarize,
            diarization_skipped=None if self.diarize else "no_hf_token",
            num_speakers=1 if self.diarize else None,
            vocab_hash=self.vocab_hash(),
            audio_duration_s=1.5,
            runtime_s=0.5,
            stages=[StageStats(name="asr", seconds=0.3), StageStats(name="align", seconds=0.2)],
            words=words,
        )
