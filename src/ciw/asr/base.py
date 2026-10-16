"""The ``Transcriber`` interface and the registry that maps ``asr.backend`` to an implementation."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ciw.models import Transcript

if TYPE_CHECKING:
    from ciw.config import Config

# Reasons stored in Transcript.diarization_skipped when speakers are not available.
SKIP_DISABLED = "disabled (--no-diarize or asr.diarize: false)"
SKIP_NO_TOKEN = "no_hf_token"
SKIP_GATED = "hf_access_denied"
SKIP_ERROR = "diarization_failed"


def diarization_hint(
    reason: str | None, model: str = "pyannote/speaker-diarization-community-1"
) -> str:
    """One line telling the user how to get speakers (no secrets in it)."""
    steps = (
        f"create a read token at https://huggingface.co/settings/tokens, accept the model terms "
        f"at https://huggingface.co/{model}, then put HF_TOKEN=<token> in .env"
    )
    if reason == SKIP_NO_TOKEN:
        return f"speakers skipped: HF_TOKEN is not set. To enable speaker labels: {steps}"
    if reason == SKIP_GATED:
        return (
            "speakers skipped: Hugging Face refused access (401/403). Check that HF_TOKEN in .env "
            f"is valid and that you accepted the terms: {steps}"
        )
    return f"speakers skipped ({reason})"


# progress(stage, fraction): ``fraction`` is 0..1 within the stage, or None when the stage has
# just started and its length is unknown. Stages: "asr", "align", "diarize".
ProgressFn = Callable[[str, float | None], None]

# A factory takes the loaded Config plus overrides and returns a ready (but still cheap, no GPU
# touched yet) Transcriber.
BackendFactory = Callable[..., "Transcriber"]

# Built-in backends are imported lazily, by "module:attribute".
_BUILTIN: dict[str, str] = {
    "whisperx": "ciw.asr.whisperx_backend:WhisperXTranscriber",
}
ASR_BACKENDS: dict[str, BackendFactory] = {}


class Transcriber(ABC):
    """Turns one audio file into a :class:`~ciw.models.Transcript`.

    Constructing a transcriber must be cheap and must not import torch or touch the GPU: the CLI
    builds one just to compare ``name``/``model``/``version`` with stored transcripts. All
    heavy work happens inside :meth:`transcribe`.
    """

    #: registry key and the value stored in ``Transcript.asr_backend``
    name: str

    @property
    @abstractmethod
    def model(self) -> str:
        """The model id stored in ``Transcript.asr_model`` (e.g. ``large-v3``)."""

    @property
    @abstractmethod
    def version(self) -> str:
        """The software stack stored in ``Transcript.asr_version``."""

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        *,
        initial_prompt: str | None = None,
        progress: ProgressFn | None = None,
    ) -> Transcript:
        """Transcribe ``audio_path``. ``initial_prompt`` biases recognition toward jargon."""

    def vocab_hash(self) -> str | None:
        """Identity of the vocabulary prompt this backend would use now (None = no prompt).

        Compared with ``Transcript.vocab_hash``: a transcript made with another prompt is stale.
        """
        return None

    def describe(self) -> str:
        """One line for ``--dry-run`` / logs: what this backend would do."""
        return f"{self.name} {self.model}"

    def matches(self, transcript: Transcript) -> bool:
        """True if ``transcript`` was produced by this backend/model/version (i.e. is current)."""
        return (
            transcript.asr_backend == self.name
            and transcript.asr_model == self.model
            and transcript.asr_version == self.version
        )


def register_backend(name: str, factory: BackendFactory) -> None:
    """Register (or replace) a backend; ``factory(cfg, **overrides) -> Transcriber``."""
    ASR_BACKENDS[name] = factory


def available_backends() -> list[str]:
    return sorted(set(_BUILTIN) | set(ASR_BACKENDS))


def get_transcriber(cfg: Config, **overrides: Any) -> Transcriber:
    """Build the backend named by ``cfg.asr.backend``.

    ``overrides`` (``model=``, ``diarize=``) are passed through to the backend.
    """
    name = cfg.asr.backend
    factory = ASR_BACKENDS.get(name)
    if factory is None and name in _BUILTIN:
        module, _, attr = _BUILTIN[name].partition(":")
        factory = getattr(importlib.import_module(module), attr)
    if factory is None:
        raise ValueError(
            f"unknown asr.backend {name!r} (available: {', '.join(available_backends())})"
        )
    return factory(cfg, **overrides)
