"""Term-recall quality check: do the jargon terms named in an episode's show notes come through?

For an episode: take every vocabulary term that the show notes (title + description) mention, and
count how many appear in the transcript. Matching is case-insensitive and whole-token/phrase;
the equivalences are declared here and nowhere else:

* a plural or possessive ("carbonators", "Searzall's") counts: the last word may carry an
  ``s``/``es`` ending;
* hyphen and space are the same ("sous vide" = "sous-vide");
* a term written with a hyphen in the vocabulary ("pu-erh", "Ultra-Sperse") also matches its closed
  compound ("puerh", "Ultrasperse");
* nothing else: "poly dextrose" is NOT "polydextrose" (that is the very mangling the correction
  pass exists to fix).

Two numbers per episode:

* **term recall**: of the terms the show notes name, how many appear at least once;
* **mention recall** (a heuristic): exact mentions / (exact mentions + *suspects*), where a suspect
  is a word, or two or three adjacent words, that is not an exact mention but is close in spelling
  to the term and is not an ordinary English word ("sockslet", "Soxlet" for "Soxhlet"). Term recall
  alone hides a term that came out right 3 times and wrong 5; suspects expose it. Suspects are
  counted, never confirmed, so read the list.

Both are reported before correction (the ASR's own words) and after it, plus the ASR runtime and
real-time factor from the transcript's metadata.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from ciw import correct as correct_mod
from ciw import vocab as vocab_mod
from ciw.models import Transcript
from ciw.transcript import GAP_S, silent_gaps  # GAP_S: dropped-audio threshold (10 s)

_WORD = re.compile(r"[a-z0-9]+")
_SUFFIXES = ("", "s", "es")


def tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens; apostrophes vanish ("Searzall's" -> "searzalls")."""
    return _WORD.findall(text.casefold().replace("’", "").replace("'", ""))


def _variants(term: str) -> list[list[str]]:
    """The token sequences that count as ``term``: spaced/hyphenated, plus closed if hyphenated."""
    base = tokens(term.replace("-", " "))
    if not base:
        return []
    out = [base]
    if "-" in term and len(base) > 1:
        out.append(["".join(base)])
    return out


def _find(seq: Sequence[str], toks: Sequence[str]) -> list[int]:
    """Start positions where ``seq`` occurs in ``toks`` (last word may take an s/es ending)."""
    n = len(seq)
    hits = []
    for i in range(len(toks) - n + 1):
        if list(toks[i : i + n - 1]) != list(seq[:-1]):
            continue
        if toks[i + n - 1] in {seq[-1] + suffix for suffix in _SUFFIXES}:
            hits.append(i)
    return hits


def mentions(term: str, toks: Sequence[str]) -> list[int]:
    """Start indexes of every occurrence of ``term`` in the token list."""
    seen: set[int] = set()
    for variant in _variants(term):
        seen.update(_find(variant, toks))
    return sorted(seen)


def terms_in_notes(terms: Sequence[str], show_notes: str) -> list[str]:
    """The vocabulary terms that the show notes name (in vocabulary order)."""
    notes = tokens(show_notes)
    return [t for t in terms if mentions(t, notes)]


# ----------------------------------------------------------------------------- report


@dataclass
class Missed:
    term: str
    heard_as: list[str] = field(default_factory=list)  # nearest transcript tokens/phrases


def suspects(
    term: str, words: Sequence[str], known: frozenset[str] = frozenset(), min_ratio: float = 0.72
) -> Counter[str]:
    """Words/phrases that look like a mangled ``term`` but are not it (surface -> count).

    ``known`` holds the squashed forms of every vocabulary term: another term is never a suspect.
    """
    want = vocab_mod.squash(term)
    found: Counter[str] = Counter()
    if len(want) < 5 or any(c.isdigit() for c in want):
        return found
    clean = [w.strip(".,;:!?\"'()[]{}“”–—-") for w in words]
    for i in range(len(clean)):
        for span in (1, 2, 3):
            if i + span > len(clean):
                break
            chunk = clean[i : i + span]
            key = vocab_mod.squash("".join(chunk))
            if not key or abs(len(key) - len(want)) > 3 or not correct_mod._same_start(key, want):
                continue
            if span == 1 and key == want:
                continue  # a closed spelling ("cookingissues" in a URL), not a mis-hearing
            if key != want and (key in known or key.startswith(want) or want.startswith(key)):
                continue  # another term, a plural/-ed form of this one, or a truncation
            if mentions(term, tokens(" ".join(chunk))):
                continue  # an exact mention (under the declared equivalences)
            ratio = correct_mod._ratio(key, want)
            if span == 1:
                ok = (
                    ratio >= min_ratio or (ratio >= 0.6 and correct_mod._phonetic(key, want) >= 0.8)
                ) and not vocab_mod.is_common_word(key)
            else:
                ok = ratio >= 0.85 and not all(c.isdigit() for c in key)
            if ok:
                found[" ".join(chunk)] += 1
    return found


@dataclass
class EpisodeQuality:
    episode_id: str
    title: str = ""
    terms: list[str] = field(default_factory=list)  # named in the show notes
    found_after: list[str] = field(default_factory=list)
    found_before: list[str] = field(default_factory=list)
    missed: list[Missed] = field(default_factory=list)  # still missed after correction
    mentions_after: int = 0
    mentions_before: int = 0
    suspects_after: dict[str, dict[str, int]] = field(default_factory=dict)  # term -> surface -> n
    suspects_before: dict[str, dict[str, int]] = field(default_factory=dict)
    corrections: int = 0
    asr_model: str | None = None
    runtime_s: float | None = None
    audio_s: float | None = None
    vocab_hash: str | None = None
    words: int = 0
    # Silent stretches >= GAP_S between two words: Whisper sometimes drops whole 30 s windows
    # (seen with a vocabulary prompt), and those show up as holes in the word timeline.
    gaps: int = 0
    gap_seconds: float = 0.0
    longest_gap_s: float = 0.0

    @property
    def n(self) -> int:
        return len(self.terms)

    @property
    def recall_after(self) -> float | None:
        return len(self.found_after) / self.n if self.n else None

    @property
    def recall_before(self) -> float | None:
        return len(self.found_before) / self.n if self.n else None

    @staticmethod
    def _count(by_term: dict[str, dict[str, int]]) -> int:
        return sum(n for surfaces in by_term.values() for n in surfaces.values())

    @property
    def n_suspects_after(self) -> int:
        return self._count(self.suspects_after)

    @property
    def n_suspects_before(self) -> int:
        return self._count(self.suspects_before)

    @property
    def mention_recall_after(self) -> float | None:
        total = self.mentions_after + self.n_suspects_after
        return self.mentions_after / total if total else None

    @property
    def mention_recall_before(self) -> float | None:
        total = self.mentions_before + self.n_suspects_before
        return self.mentions_before / total if total else None

    @property
    def rtf(self) -> float | None:
        """Real-time factor: processing seconds per audio second (0.03 = 33x realtime)."""
        if self.runtime_s and self.audio_s:
            return self.runtime_s / self.audio_s
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(
            n_terms=self.n,
            recall_before=self.recall_before,
            recall_after=self.recall_after,
            mention_recall_before=self.mention_recall_before,
            mention_recall_after=self.mention_recall_after,
            suspects_before_total=self.n_suspects_before,
            suspects_after_total=self.n_suspects_after,
            realtime_factor=self.rtf,
            speed_x=(1 / self.rtf) if self.rtf else None,
        )
        return d


def _nearest(term: str, words: Sequence[str], limit: int = 3) -> list[str]:
    """Transcript words or word pairs that look most like ``term``: what it was heard as."""
    want = vocab_mod.squash(term)
    scored: dict[str, float] = {}
    for i in range(len(words)):
        for span in (1, 2):
            if i + span > len(words):
                continue
            chunk = words[i : i + span]
            key = vocab_mod.squash("".join(chunk))
            if len(key) < 3 or abs(len(key) - len(want)) > max(3, len(want) // 3):
                continue
            ratio = SequenceMatcher(None, key, want).ratio()
            if ratio >= 0.55:
                shown = " ".join(c.strip(".,;:!?\"'()") for c in chunk)
                if shown.casefold() != term.casefold():
                    scored[shown] = max(scored.get(shown, 0.0), ratio)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [text for text, _ in ranked[:limit]]


def measure(
    transcript: Transcript,
    terms: Sequence[str],
    show_notes: str,
    *,
    episode_id: str = "",
    title: str = "",
) -> EpisodeQuality:
    """Recall of the vocabulary terms named in ``show_notes``, before and after correction."""
    named = terms_in_notes(terms, show_notes)
    known = frozenset(vocab_mod.squash(t) for t in terms)
    after = [w.w for w in transcript.words]
    before = correct_mod.original_words(transcript)
    if after == before:
        before = after  # no corrections: one list, one scan
    toks_after, toks_before = tokens(" ".join(after)), tokens(" ".join(before))
    result = EpisodeQuality(
        episode_id=episode_id,
        title=title,
        terms=list(named),
        corrections=len(transcript.corrections),
        asr_model=transcript.asr_model,
        runtime_s=transcript.runtime_s,
        audio_s=transcript.audio_duration_s or transcript.duration_s or None,
        vocab_hash=transcript.vocab_hash,
        words=len(transcript.words),
    )
    holes = [end - start for start, end in silent_gaps(transcript.words, GAP_S)]
    result.gaps, result.gap_seconds = len(holes), round(sum(holes), 1)
    result.longest_gap_s = round(max(holes, default=0.0), 1)
    for term in named:
        n_after, n_before = len(mentions(term, toks_after)), len(mentions(term, toks_before))
        result.mentions_after += n_after
        result.mentions_before += n_before
        if n_after:
            result.found_after.append(term)
        else:
            result.missed.append(Missed(term, _nearest(term, before)))
        if n_before:
            result.found_before.append(term)
        for words, store in ((after, result.suspects_after), (before, result.suspects_before)):
            sus = suspects(term, words, known)
            if sus:
                store[term] = dict(sus.most_common())
    return result


@dataclass
class Aggregate:
    episodes: int
    terms: int
    found_before: int
    found_after: int
    runtime_s: float
    audio_s: float
    mentions_before: int = 0
    mentions_after: int = 0
    suspects_before: int = 0
    suspects_after: int = 0
    gaps: int = 0
    gap_seconds: float = 0.0

    @property
    def mention_recall_before(self) -> float | None:
        total = self.mentions_before + self.suspects_before
        return self.mentions_before / total if total else None

    @property
    def mention_recall_after(self) -> float | None:
        total = self.mentions_after + self.suspects_after
        return self.mentions_after / total if total else None

    @property
    def recall_before(self) -> float | None:
        return self.found_before / self.terms if self.terms else None

    @property
    def recall_after(self) -> float | None:
        return self.found_after / self.terms if self.terms else None

    @property
    def rtf(self) -> float | None:
        return self.runtime_s / self.audio_s if self.audio_s else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(
            recall_before=self.recall_before,
            recall_after=self.recall_after,
            mention_recall_before=self.mention_recall_before,
            mention_recall_after=self.mention_recall_after,
            realtime_factor=self.rtf,
            speed_x=(1 / self.rtf) if self.rtf else None,
        )
        return d


def aggregate(results: Sequence[EpisodeQuality]) -> Aggregate:
    """Pooled (micro-averaged) recall across episodes: every named term counts once."""
    timed = [r for r in results if r.runtime_s and r.audio_s]
    return Aggregate(
        episodes=len(results),
        terms=sum(r.n for r in results),
        found_before=sum(len(r.found_before) for r in results),
        found_after=sum(len(r.found_after) for r in results),
        runtime_s=sum(r.runtime_s or 0.0 for r in timed),
        audio_s=sum(r.audio_s or 0.0 for r in timed),
        mentions_before=sum(r.mentions_before for r in results),
        mentions_after=sum(r.mentions_after for r in results),
        suspects_before=sum(r.n_suspects_before for r in results),
        suspects_after=sum(r.n_suspects_after for r in results),
        gaps=sum(r.gaps for r in results),
        gap_seconds=sum(r.gap_seconds for r in results),
    )


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def render(result: EpisodeQuality) -> str:
    """A human-readable report for one episode."""
    head = f"{result.episode_id}  {result.title}".rstrip()
    lines = [head]
    if not result.n:
        lines.append("  no vocabulary terms are named in the show notes")
    else:
        lines.append(
            f"  term recall: {len(result.found_after)}/{result.n} = {_pct(result.recall_after)} "
            f"after correction, {len(result.found_before)}/{result.n} = "
            f"{_pct(result.recall_before)} before ({result.corrections} corrections; "
            f"{result.mentions_after} mentions)"
        )
        lines.append(
            f"  mention recall (exact / exact + suspects): {result.mentions_after} exact, "
            f"{result.n_suspects_after} suspect = {_pct(result.mention_recall_after)} after; "
            f"{result.mentions_before} exact, {result.n_suspects_before} suspect = "
            f"{_pct(result.mention_recall_before)} before"
        )
        for miss in result.missed:
            heard = ", ".join(repr(h) for h in miss.heard_as) or "nothing close"
            lines.append(f"    missed {miss.term!r}: heard as {heard}")
        for term, surfaces in result.suspects_after.items():
            shown = ", ".join(f"{s!r} x{n}" for s, n in surfaces.items())
            lines.append(f"    suspect for {term!r} (after correction): {shown}")
    run = (
        f"{result.runtime_s:.0f} s for {(result.audio_s or 0) / 60:.1f} min of audio "
        f"(RTF {result.rtf:.3f}, {1 / result.rtf:.0f}x realtime)"
        if result.rtf
        else "runtime unknown"
    )
    lines.append(
        f"  asr: {result.asr_model or '?'}, {run}; {result.words} words; "
        f"prompt hash {result.vocab_hash or 'none'}"
    )
    if result.gaps:
        lines.append(
            f"  WARNING: {result.gaps} silent gaps >= {GAP_S:g} s in the transcript "
            f"({result.gap_seconds:.0f} s in all, longest {result.longest_gap_s:.0f} s): "
            "audio may have been dropped"
        )
    return "\n".join(lines)


def render_aggregate(agg: Aggregate) -> str:
    speed = f", RTF {agg.rtf:.3f} ({1 / agg.rtf:.0f}x realtime)" if agg.rtf else ""
    return (
        f"aggregate over {agg.episodes} episodes: {agg.found_after}/{agg.terms} terms = "
        f"{_pct(agg.recall_after)} after correction, {agg.found_before}/{agg.terms} = "
        f"{_pct(agg.recall_before)} before; mentions {agg.mentions_after} exact + "
        f"{agg.suspects_after} suspect = {_pct(agg.mention_recall_after)} after, "
        f"{agg.mentions_before} + {agg.suspects_before} = {_pct(agg.mention_recall_before)} "
        f"before; asr {agg.runtime_s:.0f} s for {agg.audio_s / 60:.1f} min{speed}"
        + (f"; {agg.gaps} gaps >= {GAP_S:g} s ({agg.gap_seconds:.0f} s)" if agg.gaps else "")
    )
