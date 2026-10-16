"""Utterance grouping and the ``[mm:ss] SPEAKER_01: text`` renderer, plus Transcript helpers."""

import pytest

from ciw.models import Transcript, Word
from ciw.transcript import (
    UNKNOWN_SPEAKER,
    ends_sentence,
    format_timestamp,
    render_utterances,
    utterances,
)


def words(spec):
    """spec: (text, start, end, speaker) tuples."""
    return [Word(w=w, start=s, end=e, speaker=sp) for w, s, e, sp in spec]


def test_format_timestamp_has_unbounded_minutes():
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(59.9) == "00:59"
    assert format_timestamp(754.2) == "12:34"
    assert format_timestamp(3685.0) == "61:25"
    assert format_timestamp(-3) == "00:00"


@pytest.mark.parametrize(
    "token, expected",
    [("done.", True), ("what?", True), ("no!", True), ('said."', True), ("(yes.)", True),
     ("hello,", False), ("3.5", False), ("Dr", False), ("", False), ("...", True)],
)  # fmt: skip
def test_ends_sentence(token, expected):
    assert ends_sentence(token) is expected


def test_speaker_change_splits():
    ws = words([("Hi", 0, 0.5, "SPEAKER_00"), ("there.", 0.5, 1, "SPEAKER_00"),
                ("Hello", 1.1, 1.5, "SPEAKER_01")])  # fmt: skip
    assert render_utterances(ws) == "[00:00] SPEAKER_00: Hi there.\n[00:01] SPEAKER_01: Hello"


def test_long_pause_splits_but_short_does_not():
    ws = words([("a", 0, 1, "S"), ("b", 2.4, 3, "S"), ("c", 4.6, 5, "S")])  # gaps 1.4 and 1.6
    got = utterances(ws)
    assert [u.text for u in got] == ["a b", "c"]
    assert [u.text for u in utterances(ws, pause_s=2)] == ["a b c"]


def test_sentence_end_splits_only_after_the_length_floor():
    # Short sentences stay together; a monologue is cut at a sentence end once >= 15 s long.
    ws = words([("One.", 0, 1, "S"), ("Two.", 1.1, 2, "S")])
    assert len(utterances(ws)) == 1
    long = words([("Intro", 0, 10, "S"), ("goes", 10, 16, "S"), ("on.", 16, 17, "S"),
                  ("Next", 17.2, 18, "S"), ("bit.", 18, 19, "S")])  # fmt: skip
    assert [u.text for u in utterances(long)] == ["Intro goes on.", "Next bit."]
    # a comma is not a sentence end, however long the utterance
    comma = words([("Intro,", 0, 20, "S"), ("more", 20.1, 21, "S")])
    assert len(utterances(comma)) == 1


def test_no_speakers_anywhere_omits_labels():
    ws = words([("Hello", 0, 1, None), ("world", 70, 71, None)])
    assert render_utterances(ws) == "[00:00] Hello\n[01:10] world"


def test_partial_speakers_label_the_rest_unknown():
    ws = words([("a", 0, 1, "SPEAKER_00"), ("b", 1, 2, None)])
    assert render_utterances(ws) == f"[00:00] SPEAKER_00: a\n[00:01] {UNKNOWN_SPEAKER}: b"


def test_empty_input_renders_empty_string():
    assert render_utterances([]) == ""
    assert utterances([]) == []


def test_utterance_spans_and_transcript_helpers():
    t = Transcript(
        asr_model="m",
        asr_version="v",
        words=words([("Hello", 0.5, 1, "SPEAKER_01"), ("world.", 1, 2.5, "SPEAKER_01")]),
    )
    (u,) = t.utterances()
    assert (u.start, u.end, u.speaker, u.text) == (0.5, 2.5, "SPEAKER_01", "Hello world.")
    assert t.text() == "Hello world."
    assert t.render() == "[00:00] SPEAKER_01: Hello world."
    assert render_utterances(t) == t.render()


def test_transcript_json_is_compact_and_round_trips(tmp_path):
    t = Transcript(asr_model="m", asr_version="v", words=words([("a", 0, 1, None)]))
    assert "\n" not in t.to_json()
    path = tmp_path / "t.json"
    t.save(path)
    assert Transcript.load(path) == t
    assert not path.with_name("t.json.tmp").exists()


def test_old_style_transcript_still_loads():
    """T2-era files (only asr_model/asr_version/language/words/created_at) stay valid."""
    t = Transcript.from_json(
        '{"asr_model":"large-v3","asr_version":"3.1","language":"en","words":[],'
        '"created_at":"2026-01-01T00:00:00Z"}'
    )
    assert t.diarization is False and t.stages == [] and t.asr_backend == "whisperx"
