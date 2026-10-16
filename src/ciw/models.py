"""Pydantic models for every on-disk JSON/YAML file in the pipeline.

Layout (one folder per episode under ``data/episodes/<guid>/``)::

    meta.json        EpisodeMeta
    transcript.json  Transcript   (words with timestamps and speakers)
    segments.json    Segments
    scores.json      Scores

plus ``interests.yaml`` (InterestProfile). Each model can be saved/loaded with
``save(path)`` / ``load(path)`` and round-trips exactly through JSON.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

Source = Literal["acast", "patreon", "hrn"]
SegmentKind = Literal["listener_question", "guest_interview", "tangent", "recipe_or_spec", "other"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_json(self, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, text: str | bytes) -> Self:
        return cls.model_validate_json(text)

    def save(self, path: Path) -> None:
        """Write JSON atomically (temp file + rename) so readers never see partial files."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_json() + "\n", encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.from_json(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------- meta.json


class FeedItem(_Model):
    """One audio item as published by a single feed (acast, patreon or hrn)."""

    source: Source
    guid: str = Field(min_length=1)
    title: str
    title_norm: str = ""
    description: str = ""  # HTML-stripped show notes
    description_html: str = ""  # raw HTML as published
    pub_date: datetime  # UTC
    duration_s: float | None = Field(None, ge=0)
    enclosure_url: str
    enclosure_type: str = ""
    # ``length`` attribute from the feed. UNRELIABLE (Acast says 147403780 for a file that is
    # really 58960972 bytes); never use it to validate a download.
    enclosure_length_hint: int | None = Field(None, ge=0)
    link: str | None = None
    episode_number: int | None = None


class EpisodeMeta(_Model):
    """An episode (meta.json), merged from one or two feed items.

    ``guid`` is the stable episode_id and the folder key; ``guids`` maps feed -> its guid.
    ``source`` is the episode's identity (``acast`` also for an Acast item matched to a Patreon
    one); ``audio_source`` is the feed whose enclosure is downloaded (Patreon when matched).
    The flat fields below come from that merge; ``items`` keeps every feed's own view.
    """

    guid: str = Field(min_length=1)
    guids: dict[str, str] = Field(default_factory=dict)
    source: Source
    audio_source: Source | None = None
    title: str
    title_norm: str = ""
    description: str = ""
    description_html: str = ""
    pub_date: datetime
    duration_s: float | None = Field(None, ge=0)  # of the audio source
    enclosure_url: str
    enclosure_type: str = ""
    enclosure_length_hint: int | None = Field(None, ge=0)  # unreliable, see FeedItem
    link: str | None = None
    episode_number: int | None = None
    audio_file: str = "audio.mp3"
    items: dict[str, FeedItem] = Field(default_factory=dict)


# ------------------------------------------------------------------ transcript.json


class Word(_Model):
    w: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str | None = None
    # What the ASR heard, set ONLY when the post-ASR correction pass changed this word (a merge
    # of "poly dextrose" into "polydextrose" stores "poly dextrose").
    orig: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_orig(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("orig") is None:
            data.pop("orig", None)  # ~10,000 words per episode: do not write `"orig":null` each
        return data

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"word {self.w!r}: end {self.end} < start {self.start}")
        return self


class Correction(_Model):
    """One change made by the correction pass (``ciw.correct``): the audit trail.

    ``index`` is the position of the first corrected word in ``Transcript.words`` as it is now;
    ``original_words`` are the replaced words exactly as the ASR produced them, so the change can
    be undone (and the pass re-run with a different vocabulary) without re-running the ASR.
    """

    index: int = Field(ge=0)
    original: str
    corrected: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    rule: str  # alias | merge | case | fuzzy | fuzzy-merge | normalize
    confidence: float = Field(ge=0, le=1)
    term: str  # the vocabulary term (or alias target) that was applied
    original_words: list[Word] = Field(default_factory=list)


class StageStats(_Model):
    """Wall time and GPU memory of one transcription stage (asr, align, diarize)."""

    name: str
    seconds: float = Field(ge=0)
    # Highest GPU memory in use on the device during the stage, above what was in use before it
    # started (so the desktop's own usage is excluded). Includes CTranslate2's allocations,
    # which torch's allocator statistics cannot see. None when no GPU is involved.
    peak_vram_mb: float | None = None
    # torch.cuda.max_memory_allocated() for the stage (torch tensors only).
    peak_torch_mb: float | None = None
    skipped: str | None = None  # reason, when the stage did not run


class Transcript(_Model):
    """transcript.json: the words of one episode plus how they were produced.

    ``asr_model`` is the model id (``large-v3``) and ``asr_version`` the software stack
    (``whisperx 3.8.6; faster-whisper 1.2.1; ctranslate2 4.8.3``); together with ``asr_backend``
    they decide whether a transcript is stale and the ASR stage should be re-run.
    """

    asr_model: str
    asr_version: str
    asr_backend: str = "whisperx"
    language: str = "en"
    device: str | None = None
    compute_type: str | None = None  # the one actually used (after any OOM fallback)
    batch_size: int | None = None  # the one actually used
    vad: str | None = None
    diarization: bool = False  # True when words carry real speaker labels
    diarization_model: str | None = None
    diarization_skipped: str | None = None  # why speakers are missing (no token, gated, ...)
    num_speakers: int | None = None
    initial_prompt: str | None = None
    vocab_hash: str | None = None
    audio_duration_s: float | None = Field(None, ge=0)
    runtime_s: float | None = Field(None, ge=0)
    stages: list[StageStats] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # fallbacks taken, interpolated words, ...
    words: list[Word] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    # Post-ASR correction (ciw.correct). `correction_hash` identifies the vocabulary + aliases +
    # rules that were applied (None = never corrected); `corrections` is the audit list.
    correction_hash: str | None = None
    corrections: list[Correction] = Field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max((w.end for w in self.words), default=0.0)

    def text(self) -> str:
        """The words joined with single spaces (no timestamps, no speakers)."""
        return " ".join(w.w for w in self.words)

    def utterances(self, **kwargs: Any) -> list[Any]:
        """Words grouped into utterances; see :func:`ciw.transcript.utterances`."""
        from ciw.transcript import utterances

        return utterances(self.words, **kwargs)

    def render(self, **kwargs: Any) -> str:
        """``[mm:ss] SPEAKER_01: text`` lines; see :func:`ciw.transcript.render_utterances`."""
        from ciw.transcript import render_utterances

        return render_utterances(self.words, **kwargs)

    def to_json(self, indent: int | None = None) -> str:
        # Compact by default: a one-hour episode is ~10k words and pretty-printing triples it.
        return self.model_dump_json(indent=indent)


# ------------------------------------------------------------------- segments.json


class Segment(_Model):
    id: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    title: str
    summary: str = ""
    speakers: list[str] = Field(default_factory=list)
    kind: SegmentKind = "other"

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end <= self.start:
            raise ValueError(f"segment {self.id}: end {self.end} <= start {self.start}")
        return self


class Segments(_Model):
    guid: str
    model: str | None = None
    prompt_hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    segments: list[Segment] = Field(default_factory=list)

    @field_validator("segments")
    @classmethod
    def _monotonic(cls, segs: list[Segment]) -> list[Segment]:
        """Ids unique; times strictly ordered with no overlaps."""
        check_monotonic(segs)
        return segs

    def check_contiguous(self, tolerance: float = 0.01, total: float | None = None) -> None:
        """Raise ValueError unless segments leave no gaps (and, if ``total`` is given, span it)."""
        check_contiguous(self.segments, tolerance=tolerance, total=total)


def check_monotonic(segments: list[Segment]) -> None:
    """Raise ValueError if ids repeat or a segment starts before the previous one ends."""
    seen: set[int] = set()
    prev: Segment | None = None
    for seg in segments:
        if seg.id in seen:
            raise ValueError(f"duplicate segment id {seg.id}")
        seen.add(seg.id)
        if prev is not None and seg.start < prev.end - 1e-9:
            raise ValueError(f"segment {seg.id} overlaps or precedes segment {prev.id}")
        prev = seg


def check_contiguous(
    segments: list[Segment], tolerance: float = 0.01, total: float | None = None
) -> None:
    """Raise ValueError if there is a gap larger than ``tolerance`` seconds between segments.

    With ``total`` (transcript duration), the first segment must begin and the last must end
    within ``tolerance`` of 0 and ``total`` respectively. Also enforces monotonic ordering.
    """
    check_monotonic(segments)
    if not segments:
        return
    if total is not None:
        if segments[0].start > tolerance:
            raise ValueError(f"first segment starts at {segments[0].start}, not 0")
        if abs(segments[-1].end - total) > tolerance:
            raise ValueError(f"last segment ends at {segments[-1].end}, expected {total}")
    for a, b in zip(segments, segments[1:], strict=False):
        if b.start - a.end > tolerance:
            raise ValueError(f"gap of {b.start - a.end:.2f}s between segments {a.id} and {b.id}")


# --------------------------------------------------------------------- scores.json


class Score(_Model):
    segment_id: int = Field(ge=0)
    score: float = Field(ge=0, le=10)
    matched_topics: list[str] = Field(default_factory=list)
    reason: str = ""
    actionable: bool = False


class Scores(_Model):
    guid: str
    model: str
    prompt_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    scores: list[Score] = Field(default_factory=list)

    @field_validator("scores")
    @classmethod
    def _unique_segment_ids(cls, scores: list[Score]) -> list[Score]:
        ids = [s.segment_id for s in scores]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate segment_id in scores")
        return scores

    def check_matches(self, segments: Segments) -> None:
        """Raise ValueError unless every score refers to a segment in ``segments``."""
        known = {s.id for s in segments.segments}
        missing = sorted({s.segment_id for s in self.scores} - known)
        if missing:
            raise ValueError(f"scores refer to unknown segment ids: {missing}")


# ------------------------------------------------------------------ interests.yaml


class Topic(_Model):
    name: str
    weight: float = Field(1.0, ge=0)
    examples: list[str] = Field(default_factory=list)


class InterestProfile(_Model):
    threshold: int = Field(7, ge=0, le=10)
    topics: list[Topic] = Field(default_factory=list)
    boost: dict[str, float] = Field(default_factory=dict)
    down_weight: list[str] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, text: str) -> Self:
        data: Any = yaml.safe_load(text) or {}
        return cls.model_validate(data)

    @classmethod
    def load_yaml(cls, path: Path) -> Self:
        return cls.from_yaml(path.read_text(encoding="utf-8"))

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
