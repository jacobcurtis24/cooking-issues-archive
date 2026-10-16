"""Pydantic models: JSON round-trips for every model, validators, YAML profile."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ciw.models import (
    EpisodeMeta,
    InterestProfile,
    Score,
    Scores,
    Segment,
    Segments,
    Transcript,
    Word,
    check_contiguous,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 1, 24, 6, 0, tzinfo=UTC)


def make_meta():
    return EpisodeMeta(
        guid="69715223a6c658f1834fb3d6",
        guids={"acast": "69715223a6c658f1834fb3d6", "patreon": "12345"},
        source="acast",
        title="Biscuits, Bouillon and Beyond",
        description="Home carbonation and more",
        pub_date=T0,
        enclosure_url="https://sphinx.acast.com/x/media.mp3",
        enclosure_length_hint=147403780,
        duration_s=3685.04,
    )


def make_transcript():
    return Transcript(
        asr_model="large-v3",
        asr_version="whisperx 3.x",
        words=[
            Word(w="Hello", start=0.0, end=0.4, speaker="SPEAKER_00"),
            Word(w="there", start=0.4, end=0.9),
        ],
        created_at=T0,
    )


def make_segments():
    return Segments(
        guid="g1",
        model="claude-sonnet-5-5",
        prompt_hash="abc123",
        created_at=T0,
        segments=[
            Segment(id=0, start=0, end=10, title="Intro", speakers=["SPEAKER_00"], kind="tangent"),
            Segment(
                id=1, start=10, end=30.5, title="Carbonation", summary="s", kind="recipe_or_spec"
            ),
        ],
    )


def make_scores():
    return Scores(
        guid="g1",
        model="claude-sonnet-5-5",
        prompt_hash="abc123",
        created_at=T0,
        scores=[
            Score(segment_id=0, score=2),
            Score(
                segment_id=1, score=9.5, matched_topics=["carbonation"], reason="r", actionable=True
            ),
        ],
    )


FACTORIES = [make_meta, make_transcript, make_segments, make_scores]


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda f: f.__name__)
def test_json_round_trip(factory):
    obj = factory()
    assert type(obj).from_json(obj.to_json()) == obj
    assert type(obj).model_validate(obj.model_dump(mode="json")) == obj


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda f: f.__name__)
def test_save_load_round_trip(factory, tmp_path):
    obj = factory()
    path = tmp_path / "sub" / "file.json"
    obj.save(path)
    assert type(obj).load(path) == obj
    assert not path.with_name("file.json.tmp").exists()


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        Word(w="x", start=0, end=1, bogus=1)


def test_word_end_before_start_rejected():
    with pytest.raises(ValidationError):
        Word(w="x", start=2, end=1)


def test_transcript_helpers():
    t = make_transcript()
    assert t.duration_s == 0.9
    assert t.text() == "Hello there"


@pytest.mark.parametrize("bad", [-0.1, 10.1, 11])
def test_score_range(bad):
    with pytest.raises(ValidationError):
        Score(segment_id=0, score=bad)


@pytest.mark.parametrize("ok", [0, 5, 10])
def test_score_range_ok(ok):
    assert Score(segment_id=0, score=ok).score == ok


def test_segment_end_must_follow_start():
    with pytest.raises(ValidationError):
        Segment(id=0, start=5, end=5, title="x")


def test_segments_overlap_and_duplicate_ids_rejected():
    a = Segment(id=0, start=0, end=10, title="a")
    with pytest.raises(ValidationError):
        Segments(guid="g", segments=[a, Segment(id=1, start=9, end=20, title="b")])
    with pytest.raises(ValidationError):
        Segments(guid="g", segments=[a, Segment(id=0, start=10, end=20, title="b")])


def test_segments_out_of_order_rejected():
    with pytest.raises(ValidationError):
        Segments(
            guid="g",
            segments=[
                Segment(id=0, start=10, end=20, title="b"),
                Segment(id=1, start=0, end=10, title="a"),
            ],
        )


def test_check_contiguous():
    segs = make_segments()
    segs.check_contiguous()
    segs.check_contiguous(total=30.5)
    with pytest.raises(ValueError, match="last segment"):
        segs.check_contiguous(total=60)
    gappy = Segments(
        guid="g",
        segments=[
            Segment(id=0, start=0, end=10, title="a"),
            Segment(id=1, start=12, end=20, title="b"),
        ],
    )
    with pytest.raises(ValueError, match="gap"):
        gappy.check_contiguous()
    gappy.check_contiguous(tolerance=2.5)
    check_contiguous([])  # empty is fine


def test_scores_unique_and_match_segments():
    with pytest.raises(ValidationError):
        Scores(
            guid="g",
            model="m",
            prompt_hash="h",
            scores=[Score(segment_id=1, score=1), Score(segment_id=1, score=2)],
        )
    make_scores().check_matches(make_segments())
    bad = Scores(guid="g", model="m", prompt_hash="h", scores=[Score(segment_id=7, score=1)])
    with pytest.raises(ValueError, match="unknown"):
        bad.check_matches(make_segments())


def test_episode_meta_requires_known_source():
    with pytest.raises(ValidationError):
        EpisodeMeta(guid="g", source="spotify", title="t", pub_date=T0, enclosure_url="u")


def test_interests_yaml_loads():
    prof = InterestProfile.load_yaml(REPO_ROOT / "interests.yaml")
    assert prof.threshold == 7
    assert len(prof.topics) == 6
    assert prof.topics[0].name == "carbonation and cocktail technique"
    assert prof.topics[-1].examples == []
    assert prof.boost == {"actionable": 1.5}
    assert len(prof.down_weight) == 2


def test_interest_profile_yaml_round_trip():
    prof = InterestProfile.load_yaml(REPO_ROOT / "interests.yaml")
    assert InterestProfile.from_yaml(prof.to_yaml()) == prof
    assert InterestProfile.from_json(prof.to_json()) == prof


def test_interest_threshold_range():
    with pytest.raises(ValidationError):
        InterestProfile(threshold=11)
