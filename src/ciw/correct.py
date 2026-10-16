"""Post-ASR correction: fix mangled jargon in a finished transcript, conservatively.

Whisper hears "polydextrose" as "poly dextrose" or "Polly Dexter" and "Soxhlet" as "sockslet".
This pass compares the transcript's words with the curated vocabulary (``vocab/vocabulary.txt``)
and with explicit ``heard => correct`` aliases (``vocab/aliases.txt``) and fixes near-misses.

Rules, strongest first (the first rule that applies to a position wins):

1. ``alias``       an explicit ``heard => correct`` pair matched the run of 1 to 4 words. Always
                   applied (confidence 1.0): the user vouched for it.
2. ``merge``       2 or 3 adjacent words whose letters concatenate to exactly a vocabulary term
                   ("poly dextrose" -> "polydextrose"). Only for terms that are not everyday words.
3. ``normalize`` / ``case``  the words are already the term, but with the wrong apostrophes or
                   capitalisation ("Ultrasonic's" -> "Ultrasonics", "dave arnold" -> "Dave Arnold").
4. ``fuzzy-merge`` 2 or 3 adjacent words that nearly concatenate to a term (edit ratio and phonetic
                   key both close).
5. ``fuzzy``       one word that is close to a vocabulary term in spelling AND in sound
                   (Damerau-Levenshtein ratio and metaphone key).

Guard rails (rules 4 and 5; never for aliases): terms must be at least 6 letters and tokens at least
5; the first sounds must agree; tokens with digits are never touched; a word that is a vocabulary
term already is never touched; the confidence must reach ``min_confidence``; two equally good terms
mean no change; an ordinary English word (``vocab.is_common_word``) is only altered when the edit is
one character, the term is long (7+ letters), it is not a plain prefix/suffix extension of the word,
AND the term is named in this episode's show notes. Merges never span a sentence end or a pause of
0.75 s or more. Preferring a missed fix to a corrupted word is the design: tune with review.

The pass is deterministic and idempotent: it first undoes any earlier corrections (every change
keeps the original words in ``Transcript.corrections``), then applies the rules to the ASR's own
words. A transcript whose vocabulary is empty is returned untouched.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import jellyfish

from ciw import vocab as vocab_mod
from ciw.models import Correction, Transcript, Word

RULES_VERSION = "1"
DEFAULT_MIN_CONFIDENCE = 0.86

MIN_TOKEN_LEN = 5  # letters, for fuzzy rules (aliases have no minimum)
MIN_TERM_LEN = 7  # "Activia" (a yogurt) is one letter from "Activa": short terms need aliases
MIN_MERGE_TERM_LEN = 7
MAX_MERGE_WORDS = 3
MAX_ALIAS_WORDS = 4
MAX_GAP_S = 0.75  # a longer pause between two words forbids merging them
MIN_RATIO = 0.70
MIN_MERGE_RATIO = 0.84
MIN_PHONETIC = 0.75
AMBIGUITY_MARGIN = 0.03

_EDGE = re.compile(r"^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9]*)$", re.S)
_SENTENCE_END = re.compile(r"[.!?;:]")
_POSSESSIVE = re.compile(r"(['’]s)$", re.I)


# ----------------------------------------------------------------------------- helpers


@lru_cache(maxsize=65536)
def _metaphone(s: str) -> str:
    return jellyfish.metaphone(s)


def _ratio(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    if not longest:
        return 0.0
    return 1.0 - jellyfish.damerau_levenshtein_distance(a, b) / longest


def _phonetic(a: str, b: str) -> float:
    ca, cb = _metaphone(a), _metaphone(b)
    longest = max(len(ca), len(cb))
    if not longest:
        return 0.0
    return 1.0 - jellyfish.levenshtein_distance(ca, cb) / longest


def _same_start(a: str, b: str) -> bool:
    """The first sound agrees: same first letter, or same first metaphone letter."""
    if not a or not b:
        return False
    if a[0] == b[0]:
        return True
    ca, cb = _metaphone(a), _metaphone(b)
    return bool(ca and cb and ca[0] == cb[0])


def _is_prefix_or_suffix_extension(a: str, b: str) -> bool:
    """True if one word is the other plus extra letters at an end ("pokers"/"poker",
    "tandoori"/"tandoor", "of" glued after a name): a different word or an extra word, not a
    mis-hearing. A doubled last letter ("Pacojett") is the one exception: that is a typo."""
    short, long_ = sorted((a, b), key=len)
    if long_.startswith(short):
        extra = long_[len(short) :]
        return not (len(extra) == 1 and extra == short[-1])
    return long_.endswith(short)


def _apostrophes(text: str) -> str:
    return text.replace("’", "'")


@dataclass(frozen=True)
class Tok:
    """A transcript word split into surrounding punctuation and its body."""

    lead: str
    body: str
    trail: str
    key: str  # squashed (lowercase letters/digits) body, possessive included
    key_np: str  # same, with a trailing 's possessive removed
    possessive: str  # the removed suffix as written ("'s"), or ""

    @classmethod
    def parse(cls, w: str) -> Tok:
        m = _EDGE.match(w)
        assert m is not None
        lead, body, trail = m.groups()
        pos = _POSSESSIVE.search(body)
        stem = body[: pos.start()] if pos else body
        return cls(
            lead,
            body,
            trail,
            vocab_mod.squash(body),
            vocab_mod.squash(stem),
            pos.group(1) if pos else "",
        )


@dataclass(frozen=True)
class Term:
    text: str
    key: str  # squashed
    words: tuple[str, ...]  # normalised words (hyphens/apostrophes removed)
    proper: bool  # spelled with a capital anywhere: written exactly like this
    common: bool  # a single everyday English word


@dataclass(frozen=True)
class Alias:
    heard: tuple[str, ...]  # normalised words
    heard_text: str
    correct: str


@dataclass
class Vocabulary:
    terms: list[Term] = field(default_factory=list)
    aliases: list[Alias] = field(default_factory=list)
    hash: str = ""

    def __bool__(self) -> bool:
        return bool(self.terms or self.aliases)

    @classmethod
    def from_lists(
        cls, terms: Sequence[str] = (), aliases: Sequence[tuple[str, str]] = ()
    ) -> Vocabulary:
        built = []
        for text in terms:
            words = tuple(w for w in vocab_mod.norm_term(text).replace("'", "").split() if w)
            key = vocab_mod.squash(text)
            if not key:
                continue
            built.append(
                Term(
                    text=text,
                    key=key,
                    words=words,
                    proper=any(c.isupper() for c in text),
                    common=len(words) == 1 and vocab_mod.is_common_word(key),
                )
            )
        alias_list = []
        for heard, correct in aliases:
            words = tuple(vocab_mod.norm_term(heard).replace("'", "").split())
            if words and len(words) <= MAX_ALIAS_WORDS:
                alias_list.append(Alias(words, heard, correct))
        alias_list.sort(key=lambda a: (-len(a.heard), a.heard_text.casefold()))
        digest = vocab_mod.hash_text(
            RULES_VERSION,
            "\n".join(terms),
            "\n".join(f"{h}=>{c}" for h, c in aliases),
        )
        return cls(built, alias_list, digest)

    @classmethod
    def load(cls, vocabulary_file: Path | None, aliases_file: Path | None = None) -> Vocabulary:
        return cls.from_lists(
            vocab_mod.read_terms(vocabulary_file), vocab_mod.read_aliases(aliases_file)
        )


@dataclass
class _Match:
    count: int  # words replaced
    text: str  # replacement text (no punctuation)
    rule: str
    confidence: float
    term: str


def _gap_ok(words: Sequence[Word], i: int, k: int) -> bool:
    return all(words[j + 1].start - words[j].end < MAX_GAP_S for j in range(i, i + k - 1))


def _run_ok(toks: Sequence[Tok], words: Sequence[Word], i: int, k: int) -> bool:
    """Words i..i+k-1 may form one phrase: no punctuation between them, no long pause."""
    if i + k > len(toks) or not all(t.key for t in toks[i : i + k]):
        return False  # (a lone "&" or "–" is not a word)
    for j in range(i, i + k - 1):
        if toks[j].trail or toks[j + 1].lead or _SENTENCE_END.search(toks[j].body):
            return False
    return _gap_ok(words, i, k)


def _notes_key(show_notes: str | None) -> str:
    return vocab_mod.squash(show_notes or "")


# ------------------------------------------------------------------------------- engine


class Corrector:
    """Applies a :class:`Vocabulary` to word lists. Cheap to build, reusable across episodes."""

    def __init__(self, vocabulary: Vocabulary, *, min_confidence: float = DEFAULT_MIN_CONFIDENCE):
        self.vocabulary = vocabulary
        self.min_confidence = min_confidence
        self._by_key = {t.key: t for t in vocabulary.terms}
        self._fuzzy_terms = [
            t
            for t in vocabulary.terms
            if len(t.key) >= MIN_TERM_LEN and not any(c.isdigit() for c in t.key)
        ]
        self._aliases = vocabulary.aliases

    # -- public

    def correct(
        self, words: Sequence[Word], show_notes: str | None = None
    ) -> tuple[list[Word], list[Correction]]:
        """Return ``(new words, corrections)`` for ``words`` (which are never mutated)."""
        notes = _notes_key(show_notes)
        toks = [Tok.parse(w.w) for w in words]
        out: list[Word] = []
        changes: list[Correction] = []
        i = 0
        while i < len(words):
            match = self._best(words, toks, i, notes)
            if match is None:
                out.append(words[i])
                i += 1
                continue
            replaced = list(words[i : i + match.count])
            start = i == 0 or bool(re.search(r"[.!?]$", toks[i - 1].trail))
            new_words = self._build(replaced, toks[i : i + match.count], match, start)
            changes.append(
                Correction(
                    index=len(out),
                    original=" ".join(w.w for w in replaced),
                    corrected=" ".join(w.w for w in new_words),
                    start=replaced[0].start,
                    end=replaced[-1].end,
                    rule=match.rule,
                    confidence=round(match.confidence, 3),
                    term=match.term,
                    original_words=[w.model_copy() for w in replaced],
                )
            )
            out.extend(new_words)
            i += match.count
        return out, changes

    # -- matching

    def _best(
        self, words: Sequence[Word], toks: Sequence[Tok], i: int, notes: str
    ) -> _Match | None:
        for finder in (self._alias, self._exact, self._fuzzy_merge, self._fuzzy):
            match = finder(words, toks, i, notes)
            if match is not None:
                return match
        return None

    def _alias(self, words, toks, i, notes) -> _Match | None:
        for alias in self._aliases:
            k = len(alias.heard)
            if not _run_ok(toks, words, i, k):
                continue
            run = toks[i : i + k]
            if tuple(vocab_mod.norm_term(t.body).replace("'", "") for t in run) != alias.heard:
                continue
            if _apostrophes(" ".join(t.body for t in run)) == _apostrophes(alias.correct):
                continue  # already as desired
            return _Match(k, alias.correct, "alias", 1.0, alias.correct)
        return None

    def _exact(self, words, toks, i, notes) -> _Match | None:
        """Rules 2 and 3: the letters already spell a term (merged, re-cased, re-apostrophised)."""
        for k in (3, 2, 1):
            if not _run_ok(toks, words, i, k):
                continue
            run = toks[i : i + k]
            term = self._by_key.get("".join(t.key for t in run))
            if term is None or k < len(term.words):
                continue  # no match, or the term is several words and the ASR glued them
            surface = " ".join(t.body for t in run)
            if k > len(term.words):
                # "poly dextrose" -> "polydextrose": only into a single, uncommon word
                if len(term.words) == 1 and not term.common:
                    return _Match(k, term.text, "merge", 0.98, term.text)
                continue
            if _apostrophes(surface) == _apostrophes(term.text):
                continue
            plain = lambda t: t.replace("-", " ").casefold()  # noqa: E731
            if plain(_apostrophes(surface)) != plain(_apostrophes(term.text)):
                if plain(surface.replace("'", "")) == plain(term.text.replace("'", "")):
                    return _Match(k, term.text, "normalize", 0.95, term.text)
                continue
            if surface.replace("-", " ") == term.text.replace("-", " "):
                continue  # only hyphen vs space: equivalent
            if term.proper and (k >= 2 or not term.common):
                return _Match(k, term.text, "case", 0.99, term.text)
        return None

    def _fuzzy_merge(self, words, toks, i, notes) -> _Match | None:
        for k in (3, 2):
            if not _run_ok(toks, words, i, k):
                continue
            run = toks[i : i + k]
            key = "".join(t.key_np if j == k - 1 else t.key for j, t in enumerate(run))
            if any(any(c.isdigit() for c in t.key) for t in run) or len(key) < MIN_MERGE_TERM_LEN:
                continue
            if all(vocab_mod.is_common_word(t.key) for t in run):
                continue  # needs at least one odd word
            best = self._score_terms(key, notes, merge=True, common=False)
            if best is not None:
                term, conf = best
                alone = max(_ratio(t.key_np, term.key) for t in run)
                if _ratio(key, term.key) < alone + 0.1:
                    continue  # "hydrocolloid is": one word already fits; the other is just a word
                suffix = run[-1].possessive
                return _Match(k, term.text + suffix, "fuzzy-merge", conf, term.text)
        return None

    def _fuzzy(self, words, toks, i, notes) -> _Match | None:
        tok = toks[i]
        key = tok.key_np
        if len(key) < MIN_TOKEN_LEN or any(c.isdigit() for c in key):
            return None
        if tok.key in self._by_key or key in self._by_key:
            return None  # already a vocabulary term
        common = vocab_mod.is_common_word(key)
        best = self._score_terms(key, notes, merge=False, common=common)
        if best is None:
            return None
        term, conf = best
        return _Match(1, term.text + tok.possessive, "fuzzy", conf, term.text)

    def _score_terms(
        self, key: str, notes: str, *, merge: bool, common: bool
    ) -> tuple[Term, float] | None:
        """The one vocabulary term ``key`` is clearly a mis-hearing of, or ``None``."""
        scored: list[tuple[float, Term]] = []
        min_ratio = MIN_MERGE_RATIO if merge else MIN_RATIO
        for term in self._fuzzy_terms:
            if term.key == key or abs(len(term.key) - len(key)) > 2:
                continue
            if merge and term.common:
                continue
            if not _same_start(key, term.key) or _is_prefix_or_suffix_extension(key, term.key):
                continue  # (plurals, -ed/-ing, "tandoori" and dropped words are not errors)
            ratio = _ratio(key, term.key)
            if ratio < min_ratio:
                continue
            phon = _phonetic(key, term.key)
            if phon < MIN_PHONETIC:
                continue
            if common:
                # An everyday word: one letter off, a long term, no plain extension, and the
                # term must be named in this episode's show notes.
                if (
                    jellyfish.damerau_levenshtein_distance(key, term.key) != 1
                    or len(term.key) < MIN_MERGE_TERM_LEN
                    or _is_prefix_or_suffix_extension(key, term.key)
                    or not notes
                    or term.key not in notes
                ):
                    continue
            conf = 0.4 * ratio + 0.6 * phon
            if conf >= self.min_confidence:
                scored.append((conf, term))
        if not scored:
            return None
        scored.sort(key=lambda s: (-s[0], s[1].key))
        if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUITY_MARGIN:
            return None
        return scored[0][1], scored[0][0]

    # -- building the replacement words

    @staticmethod
    def _build(
        replaced: Sequence[Word], toks: Sequence[Tok], match: _Match, sentence_start: bool
    ) -> list[Word]:
        text = _format(match, sentence_start)
        original = " ".join(w.w for w in replaced)
        first, last = replaced[0], replaced[-1]
        lead, trail = toks[0].lead, toks[-1].trail
        parts = text.split()
        if len(parts) == 1:
            return [
                Word(
                    w=f"{lead}{parts[0]}{trail}",
                    start=first.start,
                    end=last.end,
                    speaker=first.speaker,
                    orig=original,
                )
            ]
        # Several output words over the span of the replaced ones: split time by letter count.
        total = sum(len(p) for p in parts) or 1
        span = last.end - first.start
        out, cursor = [], first.start
        for n, part in enumerate(parts):
            end = last.end if n == len(parts) - 1 else cursor + span * len(part) / total
            shown = f"{lead if n == 0 else ''}{part}{trail if n == len(parts) - 1 else ''}"
            out.append(Word(w=shown, start=cursor, end=end, speaker=first.speaker, orig=original))
            cursor = end
        return out


def _format(match: _Match, sentence_start: bool) -> str:
    """Replacement text: proper terms and aliases are written as given; an uncommon lower-case
    term only gets a capital where a sentence starts."""
    text = match.text
    if match.rule in ("alias", "case") or any(c.isupper() for c in text):
        return text
    return text[:1].upper() + text[1:] if sentence_start else text


# ------------------------------------------------------------------------- transcripts


def revert(transcript: Transcript) -> int:
    """Undo every correction in place (restoring the ASR's own words). Returns how many."""
    changes = sorted(transcript.corrections, key=lambda c: c.index, reverse=True)
    for change in changes:
        n = _corrected_span(change)
        transcript.words[change.index : change.index + n] = [
            w.model_copy() for w in change.original_words
        ]
    transcript.corrections = []
    transcript.correction_hash = None
    return len(changes)


def _corrected_span(change: Correction) -> int:
    """How many words in ``Transcript.words`` currently stand for ``change``."""
    return max(len(change.corrected.split()), 1)


def apply_correction(
    transcript: Transcript,
    vocabulary: Vocabulary,
    *,
    show_notes: str | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Correction]:
    """Correct ``transcript`` in place and return the corrections made.

    Idempotent (earlier corrections are reverted first). With an empty vocabulary nothing is
    touched, not even the audit fields.
    """
    if not vocabulary:
        return []
    revert(transcript)
    corrector = Corrector(vocabulary, min_confidence=min_confidence)
    transcript.words, transcript.corrections = corrector.correct(transcript.words, show_notes)
    transcript.correction_hash = vocabulary.hash
    return transcript.corrections


def preview(
    transcript: Transcript,
    vocabulary: Vocabulary,
    *,
    show_notes: str | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Correction]:
    """The corrections :func:`apply_correction` would make, without touching ``transcript``."""
    if not vocabulary:
        return []
    clone = transcript.model_copy(deep=True)
    return apply_correction(clone, vocabulary, show_notes=show_notes, min_confidence=min_confidence)


def original_words(transcript: Transcript) -> list[str]:
    """The ASR's own word strings (before correction), merged words expanded back."""
    out = [w.w for w in transcript.words]
    for change in sorted(transcript.corrections, key=lambda c: c.index, reverse=True):
        n = _corrected_span(change)
        out[change.index : change.index + n] = [w.w for w in change.original_words]
    return out
