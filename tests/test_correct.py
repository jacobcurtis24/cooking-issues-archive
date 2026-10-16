"""The post-ASR correction pass: rules, guard rails, audit trail, idempotency, regression tables.

Everything uses small synthetic transcripts and a fixed synthetic vocabulary (the real
vocabulary.txt is a hand-edited file and must not be able to break these tests).
"""

import pytest

from ciw import correct
from ciw.models import Transcript, Word

TERMS = [
    "Cooking Issues",
    "Dave Arnold",
    "Searzall",
    "Spinzall",
    "polydextrose",
    "methylcellulose",
    "kansui",
    "Flavacol",
    "Soxhlet",
    "rotovap",
    "hydrocolloids",
    "compensator",
    "carbonator",
    "carbonation",
    "sous vide",
    "red hot poker",
    "Seattle Ultrasonics",
    "Ashmead's Kernel",
    "Wylie Dufresne",
    "Nastassia",
    "Transglutaminase",
    "Pacojet",
    "tandoor",
    "French 75",
    "Activa",
    "Jack",
    "pu-erh",
    "refractometer",
    "xanthan gum",
    "Heritage Radio Network",
]
ALIASES = [
    ("Soxlet", "Soxhlet"),
    ("sockslet", "Soxhlet"),
    ("Polly Dexter", "polydextrose"),
    ("Ashmeet's Colonel", "Ashmead's Kernel"),
    ("sears all", "Searzall"),
]
NOTES = "Soxhlet extraction, the Searzall and a refractometer."


def vocab(terms=TERMS, aliases=ALIASES):
    return correct.Vocabulary.from_lists(terms, aliases)


def words(text: str, gap: float = 0.1, dur: float = 0.3) -> list[Word]:
    out, t = [], 1.0
    for tok in text.split():
        out.append(Word(w=tok, start=round(t, 3), end=round(t + dur, 3)))
        t += dur + gap
    return out


def transcript(text: str) -> Transcript:
    return Transcript(asr_model="m", asr_version="v", words=words(text))


def fix(text: str, notes: str | None = NOTES, **kw) -> str:
    t = transcript(text)
    correct.apply_correction(t, vocab(**kw.pop("v", {})), show_notes=notes, **kw)
    return t.text()


# ------------------------------------------------------------------------------- rules


def test_no_vocabulary_leaves_the_transcript_completely_untouched():
    t = transcript("Polly Dexter and sockslet")
    before = t.model_dump_json()
    assert correct.apply_correction(t, correct.Vocabulary()) == []
    assert t.model_dump_json() == before and t.correction_hash is None
    assert correct.preview(t, correct.Vocabulary()) == []


def test_alias_is_applied_with_punctuation_and_case_of_the_alias():
    assert fix("a vacuum sockslet. Then SOXLET, ok") == "a vacuum Soxhlet. Then Soxhlet, ok"


def test_multi_word_alias_merges_timestamps_and_keeps_audit_data():
    t = transcript("so Polly Dexter works")
    (c,) = correct.apply_correction(t, vocab())
    assert t.text() == "so polydextrose works"
    merged = t.words[1]
    assert (merged.start, merged.end) == (1.4, 2.1)  # start of "Polly", end of "Dexter"
    assert merged.orig == "Polly Dexter" and t.words[0].orig is None and t.words[2].orig is None
    assert (c.index, c.original, c.corrected, c.rule, c.confidence) == (
        1, "Polly Dexter", "polydextrose", "alias", 1.0,
    )  # fmt: skip
    assert (c.start, c.end, c.term) == (1.4, 2.1, "polydextrose")
    assert [w.w for w in c.original_words] == ["Polly", "Dexter"]


def test_alias_does_not_cross_punctuation_or_a_long_pause():
    assert fix("Polly, Dexter said") == "Polly, Dexter said"
    t = Transcript(
        asr_model="m",
        asr_version="v",
        words=[Word(w="Polly", start=1, end=1.3), Word(w="Dexter", start=3.0, end=3.4)],
    )
    correct.apply_correction(t, vocab())
    assert t.text() == "Polly Dexter"


def test_exact_merge_into_an_uncommon_term_and_capital_only_at_sentence_start():
    assert fix("I love poly dextrose, really") == "I love polydextrose, really"
    assert fix("We use. Poly dextrose is great") == "We use. Polydextrose is great"
    assert fix("poly dextrose first") == "Polydextrose first"
    assert fix("a Poly dextrose here") == "a polydextrose here"  # no capital mid-sentence
    assert fix("my tan door, which") == "my tandoor, which"
    assert fix("Paco jet time") == "Pacojet time"


def test_merge_never_forms_an_everyday_word():
    assert fix("any one of you", v={"terms": ["anyone", "Searzall"]}) == "any one of you"


def test_merge_handles_three_words():
    assert fix("a meth yl cellulose gel") == "a methylcellulose gel"


def test_fuzzy_single_word_needs_spelling_and_sound():
    assert fix("the Searzol is hot") == "the Searzall is hot"
    assert fix("a Pacojett machine") == "a Pacojet machine"
    assert fix("Transglutaminace works") == "Transglutaminase works"
    assert fix("the Searzall's flame") == "the Searzall's flame"  # exact: untouched
    assert fix("the Searzalls flame") == "the Searzalls flame"  # a plural is not a mis-hearing


def test_fuzzy_keeps_a_possessive_and_surrounding_punctuation():
    assert fix("(the Searzol's) flame") == "(the Searzall's) flame"
    assert fix("Searzol, hot") == "Searzall, hot"


def test_fuzzy_fixes_a_near_miss_of_a_multi_word_term():
    assert fix("a Xantham gum slurry") == "a xanthan gum slurry"
    assert (
        fix("Wiley Dufresne cooks") == "Wiley Dufresne cooks"
    )  # too far: alias-only (see aliases)
    assert fix("Dave Arnolt says") == "Dave Arnold says"
    assert fix("Heritage Radio Netwerk") == "Heritage Radio Network"


def test_normalize_and_case_rules():
    assert fix("Seattle Ultrasonic's knife") == "Seattle Ultrasonics knife"
    assert fix("dave arnold says") == "Dave Arnold says"
    assert fix("our cooking issues. Hi") == "our Cooking Issues. Hi"
    assert fix("Ashmead’s kernel apple") == "Ashmead's Kernel apple"


def test_case_rule_leaves_everyday_single_words_alone():
    assert fix("get a jack for the car") == "get a jack for the car"
    assert fix("Jack and Dave Arnold") == "Jack and Dave Arnold"


def test_hyphen_and_space_variants_are_equivalent_not_corrected():
    assert fix("a Pu-erh and puerh and pu erh") == "a Pu-erh and puerh and pu erh"
    assert fix("sous-vide and sous vide") == "sous-vide and sous vide"


def test_exact_terms_are_never_touched():
    text = "Searzall Spinzall polydextrose kansui Flavacol rotovap carbonator carbonation"
    assert fix(text) == text


# ------------------------------------------------------------------------- guard rails


def test_never_alters_inflections_or_everyday_neighbours():
    assert fix("Red Hot Pokers, again") == "Red Hot Pokers, again"  # plural of a term
    assert fix("hydrocolloid is a word") == "hydrocolloid is a word"
    assert (
        fix("carbonate and carbonated and carbonates") == "carbonate and carbonated and carbonates"
    )
    assert fix("a compensate step") == "a compensate step"
    assert fix("spinach and spinal") == "spinach and spinal"


def test_short_terms_and_digit_terms_are_alias_only():
    assert fix("Activia is a yogurt") == "Activia is a yogurt"  # 1 letter from Activa (6 letters)
    assert fix("so Frenchy it up") == "so Frenchy it up"
    assert fix("a French 75 base") == "a French 75 base"


def test_ordinary_english_words_only_change_with_show_notes_and_one_edit():
    v = {"terms": ["marinate", "Searzall"], "aliases": []}
    assert fix("let it marinade", notes=None, v=v) == "let it marinade"
    assert fix("let it marinade", notes="Why we marinate", v=v) == "let it marinate"
    assert fix("let it marinade", notes="Searzall only", v=v) == "let it marinade"
    # two edits away: never, even with notes
    assert fix("let it marinades", notes="Why we marinate", v=v) == "let it marinades"


def test_ambiguity_between_two_terms_means_no_change():
    v = {"terms": ["Searzall", "Searzell"], "aliases": []}
    assert fix("a Searzull here", v=v) == "a Searzull here"


def test_confidence_threshold_applies_to_fuzzy_but_never_to_aliases():
    t = transcript("the Searzol and sockslet")
    correct.apply_correction(t, vocab(), min_confidence=0.99)
    assert t.text() == "the Searzol and Soxhlet"
    t2 = transcript("the Searzol and sockslet")
    (c1, c2) = correct.apply_correction(t2, vocab(), min_confidence=0.5)
    assert t2.text() == "the Searzall and Soxhlet"
    assert (c1.rule, c2.rule) == ("fuzzy", "alias") and 0.86 <= c1.confidence < 1


def test_punctuation_only_tokens_are_not_words():
    assert fix("& Nastassia ( rotovap") == "& Nastassia ( rotovap"


def test_merge_requires_adjacency_in_time_and_in_text():
    assert fix("poly. dextrose") == "poly. dextrose"


def test_fuzzy_never_touches_tokens_with_digits_or_short_tokens():
    assert fix("kans1 and kan") == "kans1 and kan"


# ------------------------------------------------------------ idempotency and reverting


def test_apply_is_idempotent():
    t = transcript("so Polly Dexter and sockslet and Searzol, dave arnold")
    first = correct.apply_correction(t, vocab())
    snapshot = t.model_dump_json()
    second = correct.apply_correction(t, vocab())
    assert t.model_dump_json() == snapshot
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]


def test_revert_restores_the_asr_words_exactly():
    original = transcript("so Polly Dexter and sockslet and Searzol, dave arnold")
    t = original.model_copy(deep=True)
    correct.apply_correction(t, vocab())
    assert t.words != original.words and t.corrections and t.correction_hash
    assert correct.original_words(t) == [w.w for w in original.words]
    n = len(t.corrections)
    assert correct.revert(t) == n
    assert t.words == original.words and t.corrections == [] and t.correction_hash is None


def test_changed_vocabulary_is_a_fresh_pass_over_the_original_words():
    t = transcript("so Polly Dexter and sockslet")
    correct.apply_correction(t, vocab())
    narrower = vocab(aliases=[("sockslet", "Soxhlet")])
    correct.apply_correction(t, narrower)
    fresh = transcript("so Polly Dexter and sockslet")
    correct.apply_correction(fresh, narrower)
    assert t.text() == fresh.text() == "so Polly Dexter and Soxhlet"
    assert t.model_dump(exclude={"created_at"}) == fresh.model_dump(exclude={"created_at"})
    assert t.correction_hash == narrower.hash != vocab().hash


def test_preview_changes_nothing():
    t = transcript("sockslet here")
    before = t.model_dump_json()
    assert len(correct.preview(t, vocab())) == 1
    assert t.model_dump_json() == before


def test_corrections_survive_a_json_round_trip_and_can_still_be_reverted(tmp_path):
    t = transcript("so Polly Dexter works")
    correct.apply_correction(t, vocab())
    path = tmp_path / "t.json"
    t.save(path)
    loaded = Transcript.load(path)
    assert loaded == t and loaded.corrections[0].original_words[0].w == "Polly"
    correct.revert(loaded)
    assert loaded.text() == "so Polly Dexter works"
    assert '"orig"' not in transcript("plain words").to_json()  # only written when changed


def test_multi_word_replacements_spread_the_time_over_the_span():
    v = correct.Vocabulary.from_lists([], [("so vide", "sous vide")])
    t = transcript("do so vide cooking")
    correct.apply_correction(t, v)
    assert t.text() == "do sous vide cooking"
    a, b = t.words[1], t.words[2]
    assert a.start == 1.4 and b.end == pytest.approx(2.1) and a.end <= b.start + 1e-9
    assert a.orig == b.orig == "so vide"


def test_speaker_of_the_first_word_is_kept_on_a_merge():
    ws = [Word(w="Polly", start=1, end=1.3, speaker="S1"), Word(w="Dexter", start=1.4, end=1.8)]
    t = Transcript(asr_model="m", asr_version="v", words=ws)
    correct.apply_correction(t, vocab())
    assert t.words[0].speaker == "S1" and len(t.words) == 1


# --------------------------------------------------------------------- regression tables

# Real mis-hearings found in the two T6 transcripts and in the blog/show-notes stress test
# (heard => corrected), with an episode-style show-notes string so the guard can see the terms.
REAL_FIXES = [
    ("a vacuum sockslet, which is", "a vacuum Soxhlet, which is"),
    ("uses for a Soxlet extractor", "uses for a Soxhlet extractor"),
    ("the Seattle Ultrasonic's new knife", "the Seattle Ultrasonics new knife"),
    ("I have my tan door, which", "I have my tandoor, which"),
    ("kernels like ashmeet's Colonel?", "kernels like Ashmead's Kernel?"),
    ("put a sears all on a stick", "put a Searzall on a stick"),
    ("host of cooking issues coming to you", "host of Cooking Issues coming to you"),
    ("immersion Carageenan gels", "immersion carrageenan gels"),
    ("the Paco Jet again", "the Pacojet again"),
    ("Tranglutaminase glue", "Transglutaminase glue"),
    ("Nastasia and Dave", "Nastassia and Dave"),
    ("with a Spinsall centrifuge", "with a Spinzall centrifuge"),
    ("with Xantham Gum, again", "with xanthan gum, again"),
]
REAL_VOCAB = TERMS + ["carrageenan", "immersion circulator"]


@pytest.mark.parametrize("heard, expected", REAL_FIXES)
def test_real_mishearings_are_corrected(heard, expected):
    t = transcript(heard)
    correct.apply_correction(
        t, correct.Vocabulary.from_lists(REAL_VOCAB, ALIASES), show_notes=NOTES
    )
    assert t.text() == expected


# Correct (or merely unusual) text that must NEVER change: ordinary words that resemble a term,
# real names, listener handles, spoken forms, inflections.
MUST_NOT_CHANGE = [
    "the spinach and the spinal cord",
    "Activia is a probiotic yogurt",
    "hydrocolloid is a gel",
    "a Red Hot Pokers article",
    "carbonate carbonated carbonates carbonating",
    "compensate for it",
    "Polymax writes in, or maybe Polymex",
    "Sazerick wants to know",
    "I love poly D in sorbet",
    "sear all sides until brown",
    "Sears and all that stuff",
    "my Wiley friend",
    "the flavor of the method",
    "a German silent normal conversation",
    "Frenchy it up a French 75",
    "poly, dextrose and sugar",
    "a pearl of wisdom",
    "kansas city and Kansui and kans",
    "the Nastassia show with Jack in the box",
    "heritage radio network",
]


@pytest.mark.parametrize("text", MUST_NOT_CHANGE)
def test_ordinary_text_is_left_alone(text):
    t = transcript(text)
    correct.apply_correction(
        t, correct.Vocabulary.from_lists(REAL_VOCAB, ALIASES), show_notes=NOTES
    )
    expected = text.replace("heritage radio network", "Heritage Radio Network")
    assert t.text() == expected, [
        c.model_dump(include={"original", "corrected", "rule"}) for c in t.corrections
    ]
