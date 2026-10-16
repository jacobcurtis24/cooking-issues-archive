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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class EpisodeMeta(_Model):
    """A feed item (meta.json). ``guid`` is the folder key; ``guids`` maps feed -> its guid."""

    guid: str = Field(min_length=1)
    guids: dict[str, str] = Field(default_factory=dict)
    source: Source
    title: str
    description: str = ""
    pub_date: datetime
    enclosure_url: str
    enclosure_length: int | None = Field(None, ge=0)
    duration_s: float | None = Field(None, ge=0)
    link: str | None = None
    episode_number: int | None = None
    audio_file: str = "audio.mp3"


# ------------------------------------------------------------------ transcript.json


class Word(_Model):
    w: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"word {self.w!r}: end {self.end} < start {self.start}")
        return self


class Transcript(_Model):
    asr_model: str
    asr_version: str
    language: str = "en"
    words: list[Word] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def duration_s(self) -> float:
        return max((w.end for w in self.words), default=0.0)

    @property
    def text(self) -> str:
        return " ".join(w.w for w in self.words)


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
