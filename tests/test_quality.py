"""Term-recall quality check on synthetic transcripts (no real data)."""

import json

import pytest

from ciw import correct, quality
from ciw.models import Transcript, Word

TERMS = ["Searzall", "polydextrose", "Soxhlet", "sous vide", "pu-erh", "kansui", "carbonator"]
NOTES = "Title\nA Searzall demo, polydextrose, Soxhlet extraction, sous-vide eggs and pu-erh tea."


def transcript(text: str, runtime=30.0, audio=600.0) -> Transcript:
    ws = [Word(w=t, start=i * 0.4, end=i * 0.4 + 0.3) for i, t in enumerate(text.split())]
    return Transcript(
        asr_model="large-v3", asr_version="v", words=ws, runtime_s=runtime, audio_duration_s=audio
    )


def test_terms_named_in_the_show_notes_only():
    named = quality.terms_in_notes(TERMS, NOTES)
    assert named == ["Searzall", "polydextrose", "Soxhlet", "sous vide", "pu-erh"]
    assert quality.terms_in_notes(TERMS, "") == []


@pytest.mark.parametrize(
    "term, text, hits",
    [
        ("sous vide", "we sous vide and Sous-Vide and sous vides it", 3),
        (
            "sous vide",
            "sousvide",
            0,
        ),  # a closed compound is NOT declared equivalent for a spaced term
        ("pu-erh", "Pu-erh, puerh and pu erh", 3),  # hyphenated terms also match closed/spaced
        ("Searzall", "the SEARZALL, Searzalls and Searzall's", 3),  # case, plural, possessive
        ("polydextrose", "poly dextrose and polydextrose", 1),  # the very mangling: no match
        ("Soxhlet", "Soxhlets", 1),
        ("Soxhlet", "Soxlet sockslet", 0),
        ("kansui", "kansuis kansui", 2),
        ("carbonator", "carbonation carbonators", 1),
    ],
)
def test_mention_matching_rules(term, text, hits):
    assert len(quality.mentions(term, quality.tokens(text))) == hits


VOCAB = correct.Vocabulary.from_lists(TERMS, [("sockslet", "Soxhlet")])
TEXT = "a Searzall and poly dextrose with a vacuum sockslet and sous vide eggs"


def test_recall_without_correction_lists_what_each_miss_was_heard_as():
    res = quality.measure(transcript(TEXT), TERMS, NOTES, episode_id="e1", title="T")
    assert res.n == 5 and res.found_after == ["Searzall", "sous vide"]
    assert res.recall_after == res.recall_before == 0.4
    assert [m.term for m in res.missed] == ["polydextrose", "Soxhlet", "pu-erh"]
    heard = {m.term: m.heard_as for m in res.missed}
    assert "poly dextrose" in heard["polydextrose"] and "sockslet" in heard["Soxhlet"]
    assert heard["pu-erh"] == []
    assert res.suspects_after == {
        "polydextrose": {"poly dextrose": 1},
        "Soxhlet": {"sockslet": 1},
    }
    assert res.mentions_after == 2 and res.n_suspects_after == 2
    assert res.mention_recall_after == 0.5


def test_recall_improves_after_correction_and_before_is_the_asr_words():
    t = transcript(TEXT)
    correct.apply_correction(t, VOCAB)
    res = quality.measure(t, TERMS, NOTES, episode_id="e1")
    assert res.found_before == ["Searzall", "sous vide"] and res.recall_before == 0.4
    assert set(res.found_after) == {"Searzall", "polydextrose", "Soxhlet", "sous vide"}
    assert res.recall_after == 0.8 and [m.term for m in res.missed] == ["pu-erh"]
    assert res.corrections == 2 and res.suspects_after == {}
    assert res.n_suspects_before == 2 and res.mention_recall_before == 0.5
    assert res.mention_recall_after == 1.0


def test_runtime_and_real_time_factor_come_from_the_transcript():
    res = quality.measure(transcript(TEXT, runtime=30, audio=600), TERMS, NOTES)
    assert (res.runtime_s, res.audio_s) == (30, 600)
    assert res.rtf == pytest.approx(0.05) and res.to_dict()["speed_x"] == pytest.approx(20)
    none = quality.measure(Transcript(asr_model="m", asr_version="v"), TERMS, NOTES)
    assert none.rtf is None and none.recall_after == 0
    assert "runtime unknown" in quality.render(none)


def test_no_terms_in_the_notes_is_not_a_division_by_zero():
    res = quality.measure(transcript(TEXT), TERMS, "Nothing technical here.")
    assert res.n == 0 and res.recall_after is None and res.mention_recall_after is None
    assert "no vocabulary terms" in quality.render(res)


def test_aggregate_pools_terms_and_runtime():
    a = quality.measure(transcript(TEXT, runtime=30, audio=600), TERMS, NOTES, episode_id="a")
    t = transcript(TEXT, runtime=10, audio=300)
    correct.apply_correction(t, VOCAB)
    b = quality.measure(t, TERMS, NOTES, episode_id="b")
    agg = quality.aggregate([a, b])
    assert (agg.episodes, agg.terms) == (2, 10)
    assert (agg.found_before, agg.found_after) == (4, 6)
    assert agg.recall_before == 0.4 and agg.recall_after == 0.6
    assert agg.rtf == pytest.approx(40 / 900) and agg.runtime_s == 40
    text = quality.render_aggregate(agg)
    assert "6/10 terms = 60% after correction" in text and "4/10 = 40% before" in text
    assert quality.aggregate([]).recall_after is None


def test_render_names_missed_terms_and_suspects():
    out = quality.render(quality.measure(transcript(TEXT), TERMS, NOTES, episode_id="e1"))
    assert "term recall: 2/5 = 40% after correction, 2/5 = 40% before" in out
    assert "missed 'Soxhlet': heard as 'sockslet'" in out
    assert "suspect for 'polydextrose'" in out and "large-v3" in out


def test_json_is_serialisable_and_complete():
    res = quality.measure(transcript(TEXT), TERMS, NOTES, episode_id="e1")
    data = json.loads(json.dumps(res.to_dict()))
    assert data["recall_after"] == 0.4 and data["n_terms"] == 5
    assert data["missed"][0]["term"] == "polydextrose"
    assert data["missed"][0]["heard_as"][0] == "poly dextrose"  # the closest first
    assert data["realtime_factor"] == pytest.approx(0.05)


def test_ordinary_look_alikes_are_not_suspects():
    t = transcript("carbonation and carbonate and carbonators in a carbonator")
    res = quality.measure(t, ["carbonator"], "A carbonator.", episode_id="e")
    assert res.mentions_after == 2 and res.n_suspects_after == 0  # carbonation is a real word
