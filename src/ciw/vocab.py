"""The jargon vocabulary: mining candidate terms, reading the curated file, building the prompt.

Pipeline: ``ciw vocab build`` mines every episode description (``meta.json``) and, as a second
corroborating source, the blog archive (read-only), with token/n-gram heuristics only (no LLM, no
network): capitalised words in mid-sentence, CamelCase, hyphenated words, tokens with digits,
acronyms, and words that are rare in everyday English (``wordfreq``'s Zipf scale). The ranked
result is ``vocab/candidates.csv``. A human curates ``vocab/vocabulary.txt`` from it (the control
knob: one term per line, most valuable first, because Whisper's prompt is cut at 600 characters).

Everything here is deterministic: the same inputs give byte-identical output.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CANDIDATE_COLUMNS = ["term", "variants", "count", "doc_freq", "sources", "score", "zipf", "flags"]

# A word at or above this Zipf frequency (wordfreq, English) counts as ordinary English.
COMMON_ZIPF = 3.0
# Words at or above this Zipf frequency are function words (never a term by themselves).
FUNCTION_ZIPF = 5.5
# A paragraph repeated verbatim in at least this many show notes is boilerplate, not content.
BOILERPLATE_MIN_DOCS = 4
MIN_PHRASE_DOC_FREQ = 3

# Words that are never useful as vocabulary even when rare or capitalised in show notes.
STOP_TERMS = frozenset(
    """patreon subscribe subscribed subscription simplecast acast podcast podcasts episode
    episodes apple spotify itunes stitcher facebook twitter instagram youtube http https www com
    org net html call listen listeners listener sponsored sponsor sponsors discount code promo
    monday tuesday wednesday thursday friday saturday sunday january february march april may june
    july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec
    plus also this that these those here there then than they them their what when where which who
    whom whose with without within would could should please thanks thank welcome today tonight
    yesterday tomorrow week weeks year years month months day days hour hours minute minutes
    mr mrs ms dr""".split()
)


# ----------------------------------------------------------------------------- English


@lru_cache(maxsize=1)
def _zipf_fn() -> Callable[[str], float]:
    from wordfreq import zipf_frequency

    return lambda word: zipf_frequency(word, "en")


def zipf(word: str) -> float:
    """Zipf frequency (0 = unseen, ~7 = 'the') of a lowercase word in everyday English."""
    return _zipf_fn()(word.lower())


@lru_cache(maxsize=1)
def _dictionary() -> frozenset[str]:
    """``/usr/share/dict`` word list (lowercase) if the machine has one, else empty."""
    for name in ("american-english", "words"):
        path = Path("/usr/share/dict") / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            return frozenset(w for w in text.split() if w.islower() and w.isalpha())
    return frozenset()


def in_dictionary(word: str) -> bool:
    return word.lower() in _dictionary()


@lru_cache(maxsize=65536)
def is_common_word(word: str) -> bool:
    """True for ordinary English words: frequent in ``wordfreq``, or in the system dictionary
    and at least occasionally seen (rare dictionary entries like "soxhlet" do not count)."""
    z = zipf(word)
    return z >= COMMON_ZIPF or (z >= 2.0 and in_dictionary(word))


# ----------------------------------------------------------------------------- text


@dataclass(frozen=True)
class Doc:
    """One mined document: an episode description or a blog post."""

    doc_id: str
    source: str  # acast | hrn | patreon | blog
    text: str
    labels: tuple[str, ...] = ()  # curated labels (blog title/tags/categories): whole-phrase terms


_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b\S+@\S+\.\S+|\b\S+\.(?:com|org|net|io|fm)\b\S*")
_PHONE = re.compile(r"\b\d{3}[.\- ]\d{3}[.\- ]\d{4}\b")
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[’'\-][A-Za-z0-9]+)*")
_SENT_SPLIT = re.compile(r"(?<=[.!?:;])\s+|\n+|\s[–—]\s|[()\[\]“”\"]")
_CAMEL = re.compile(r"[a-z][A-Z]")
_ROMAN_OR_ORD = re.compile(r"(?i)^(?:\d+(?:st|nd|rd|th)|[ivx]+)$")


def clean_text(text: str) -> str:
    text = _URL.sub(" ", text)
    text = _PHONE.sub(" ", text)
    return text.replace("’", "'").replace(" ", " ")


def norm_term(term: str) -> str:
    """Canonical key: casefolded, hyphens/spaces collapsed, apostrophes dropped."""
    t = term.casefold().replace("’", "").replace("'", "")
    return " ".join(re.sub(r"[-–—_/]+", " ", t).split())


def squash(term: str) -> str:
    """Letters and digits only, lowercase: the form compared across space/hyphen variants."""
    return re.sub(r"[^a-z0-9]", "", term.casefold())


def boilerplate_paragraphs(docs: Iterable[Doc], min_docs: int = BOILERPLATE_MIN_DOCS) -> set[str]:
    """Normalised paragraphs that occur verbatim in at least ``min_docs`` episode descriptions."""
    seen: dict[str, set[str]] = defaultdict(set)
    for d in docs:
        if d.source == "blog":
            continue
        for para in re.split(r"\n\s*\n|\n", d.text):
            key = " ".join(para.split()).casefold()
            if len(key) >= 20:
                seen[key].add(d.doc_id)
    return {k for k, ids in seen.items() if len(ids) >= min_docs}


def strip_boilerplate(text: str, boiler: set[str]) -> str:
    keep = []
    for para in re.split(r"\n", text):
        key = " ".join(para.split()).casefold()
        if key and key not in boiler:
            keep.append(para)
    return "\n".join(keep)


# ------------------------------------------------------------------------- extraction


@dataclass
class _Stat:
    variants: Counter = field(default_factory=Counter)
    count: int = 0
    docs: set = field(default_factory=set)
    by_source: dict = field(default_factory=lambda: defaultdict(set))
    flags: set = field(default_factory=set)

    def add(self, surface: str, doc: Doc, flag: str | None = None) -> None:
        self.variants[surface] += 1
        self.count += 1
        self.docs.add(doc.doc_id)
        self.by_source[doc.source].add(doc.doc_id)
        if flag:
            self.flags.add(flag)


@dataclass
class Candidate:
    term: str  # best surface form
    key: str
    variants: list[str]
    count: int
    doc_freq: int
    sources: dict[str, int]  # source -> number of documents
    score: float
    zipf: float
    flags: list[str]

    def row(self) -> dict[str, object]:
        return {
            "term": self.term,
            "variants": "|".join(self.variants),
            "count": self.count,
            "doc_freq": self.doc_freq,
            "sources": ";".join(f"{k}:{v}" for k, v in sorted(self.sources.items())),
            "score": f"{self.score:.3f}",
            "zipf": f"{self.zipf:.2f}",
            "flags": " ".join(self.flags),
        }


def _tokens(sentence: str) -> list[str]:
    toks = []
    for t in _TOKEN.findall(sentence):
        if t.lower().endswith(("'s", "'S")):
            t = t[:-2]
        if t:
            toks.append(t)
    return toks


def _is_cap(tok: str) -> bool:
    return tok[0].isupper() and not tok.isupper()


def _shape_flags(tok: str, first_in_sentence: bool) -> list[str]:
    flags = []
    if _CAMEL.search(tok):
        flags.append("camel")
    if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
        flags.append("digit")
    if "-" in tok:
        flags.append("hyphen")
    if tok.isupper() and 2 <= len(tok) <= 6 and tok.isalpha() and zipf(tok.lower()) < 3.5:
        flags.append("acronym")  # not "THE"/"FOOD" shouted in a headline
    if _is_cap(tok) and not first_in_sentence:
        flags.append("cap")
    return flags


def _word_zipf(term: str) -> float:
    """Zipf of a term: the smallest over its words (a rare word makes the term rare)."""
    parts = [p for p in re.split(r"[\s\-]+", term) if p]
    return min((zipf(p) for p in parts), default=0.0)


def extract(
    docs: Sequence[Doc],
    *,
    seeds: Iterable[str] = (),
    min_phrase_doc_freq: int = MIN_PHRASE_DOC_FREQ,
    boilerplate_min_docs: int = BOILERPLATE_MIN_DOCS,
) -> list[Candidate]:
    """Mine and rank candidate terms from ``docs``. Deterministic; best candidates first."""
    boiler = boilerplate_paragraphs(docs, boilerplate_min_docs)
    stats: dict[str, _Stat] = defaultdict(_Stat)
    ngram_stats: dict[str, _Stat] = defaultdict(_Stat)

    def usable(tok: str) -> bool:
        low = tok.lower()
        return (
            (len(tok) >= 3 or (tok.isupper() and len(tok) >= 2))
            and low not in STOP_TERMS
            and not tok.isdigit()
            and not _ROMAN_OR_ORD.match(tok)
        )

    for doc in docs:
        text = clean_text(doc.text if doc.source == "blog" else strip_boilerplate(doc.text, boiler))
        for sentence in _SENT_SPLIT.split(text):
            toks = _tokens(sentence)
            if not toks:
                continue
            # Unigram candidates.
            for i, tok in enumerate(toks):
                if not usable(tok):
                    continue
                low = tok.lower()
                flags = _shape_flags(tok, i == 0)
                rare = zipf(low) < 2.0 and tok.isalpha() and len(tok) >= 5
                sentence_start_rare = i == 0 and zipf(low) < 2.5 and _is_cap(tok)
                if flags or rare or sentence_start_rare:
                    if rare and "cap" not in flags:
                        flags.append("rare")
                    key = norm_term(tok)
                    stats[key].add(tok, doc, flags[0] if flags else None)
                    for f in flags[1:]:
                        stats[key].flags.add(f)
            # Capitalised runs: "Dave Arnold", "Heritage Radio Network" (2 to 4 words).
            i = 0
            while i < len(toks):
                if _is_cap(toks[i]) and usable(toks[i]):
                    j = i
                    while j + 1 < len(toks) and _is_cap(toks[j + 1]) and usable(toks[j + 1]):
                        j += 1
                    run = toks[i : j + 1]
                    if 2 <= len(run) <= 4:
                        stats[norm_term(" ".join(run))].add(" ".join(run), doc, "caprun")
                    i = j + 1
                else:
                    i += 1
            # Lower-case n-grams (2 to 3 words): kept later only when repeated across documents.
            content = [t if (usable(t) and zipf(t.lower()) < FUNCTION_ZIPF) else None for t in toks]
            for n in (2, 3):
                for i in range(len(toks) - n + 1):
                    window = content[i : i + n]
                    if None in window or any(not t.replace("-", "").isalpha() for t in window):
                        continue
                    surface = " ".join(window)
                    ngram_stats[norm_term(surface)].add(surface, doc, "ngram")
        for label in doc.labels:
            label = label.strip()
            if label and norm_term(label) not in STOP_TERMS:
                stats[norm_term(label)].add(label, doc, "label")

    for key, st in ngram_stats.items():
        if len(st.docs) >= min_phrase_doc_freq and " " in key:
            stats[key].variants.update(st.variants)
            stats[key].count = max(stats[key].count, st.count)
            stats[key].docs |= st.docs
            for src, ids in st.by_source.items():
                stats[key].by_source[src] |= ids
            stats[key].flags.add("ngram")

    seed_keys = {norm_term(s): s for s in seeds if s.strip()}
    for key, original in seed_keys.items():
        st = stats[key]
        st.flags.add("seed")
        if not st.variants:
            st.variants[original] += 0

    out: list[Candidate] = []
    for key, st in stats.items():
        if key in STOP_TERMS or not key:
            continue
        words = key.split()
        if all(w in STOP_TERMS for w in words):
            continue
        # Best surface form: prefer the most common variant; ties by case-sensitive text.
        variants = sorted(st.variants.items(), key=lambda kv: (-kv[1], kv[0]))
        best = variants[0][0]
        if "seed" in st.flags and key in seed_keys:
            best = seed_keys[key]
        z = _word_zipf(key)
        out.append(
            Candidate(
                term=best,
                key=key,
                variants=[v for v, _ in variants[:4]],
                count=st.count,
                doc_freq=len(st.docs),
                sources={s: len(ids) for s, ids in sorted(st.by_source.items())},
                score=_score(st, z),
                zipf=z,
                flags=sorted(st.flags),
            )
        )
    out.sort(key=lambda c: (-c.score, c.key))
    return out


def _score(st: _Stat, z: float) -> float:
    """Importance heuristic: spread across documents and sources, rarity, and shape."""
    df = len(st.docs)
    n_sources = len([s for s, ids in st.by_source.items() if ids])
    spread = math.log2(1 + df)
    corroboration = 1 + 0.6 * max(n_sources - 1, 0)
    rarity = max(0.15, min(1.0, (4.5 - z) / 4.5))
    shape = 1.0
    if st.flags & {"caprun"} or ("cap" in st.flags and z < 4.0):
        shape += 0.3
    if st.flags & {"camel", "digit", "hyphen", "acronym"}:
        shape += 0.4
    if "label" in st.flags:
        shape += 0.4
    if "rare" in st.flags:
        shape += 0.2
    score = spread * corroboration * (0.3 + rarity) * shape
    if "seed" in st.flags:
        score += 10.0
    return round(score, 6)


# --------------------------------------------------------------------------- loading


def episode_docs(episodes_dir: Path) -> list[Doc]:
    """One Doc per ``meta.json`` (sorted by episode id, so the order is stable)."""
    docs = []
    for path in sorted(Path(episodes_dir).glob("*/meta.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = " \n".join(
            t for t in (meta.get("title") or "", meta.get("description") or "") if t.strip()
        )
        if not text.strip():
            continue
        docs.append(Doc(path.parent.name, str(meta.get("source") or "unknown"), text))
    return docs


def blog_docs(blog_db: Path) -> list[Doc]:
    """One Doc per blog post (title, categories, tags, body); the database is opened read-only."""
    path = Path(blog_db)
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, title, categories, tags, body_text FROM posts ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    docs = []
    for pid, title, categories, tags, body in rows:
        labels = [t.strip() for t in re.split(r"[,;]", f"{categories or ''},{tags or ''}")]
        labels = [t for t in labels if t and t.lower() not in ("uncategorized", "uncategorised")]
        # The title is a label, not running text: Title Case would make every word "capitalised".
        labels = [(title or "").strip(), *labels]
        unique = tuple(dict.fromkeys(label for label in labels if label))
        docs.append(Doc(f"blog-{pid}", "blog", body or "", unique))
    return docs


def write_candidates_csv(path: Path, candidates: Sequence[Candidate]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CANDIDATE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for cand in candidates:
            writer.writerow(cand.row())
    tmp.replace(path)


def read_terms(path: Path | str | None) -> list[str]:
    """Terms from a vocabulary-style file: one per line, ``#`` comments, blank lines ignored.
    Duplicates (by :func:`norm_term`) are dropped, keeping the first (most important) one."""
    if path is None or not Path(path).is_file():
        return []
    seen: set[str] = set()
    terms = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        term = raw.split("#", 1)[0].strip()
        if term and norm_term(term) not in seen:
            seen.add(norm_term(term))
            terms.append(term)
    return terms


def read_aliases(path: Path | str | None) -> list[tuple[str, str]]:
    """``heard => correct`` pairs (one per line, ``#`` comments). Malformed lines are skipped."""
    if path is None or not Path(path).is_file():
        return []
    pairs = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=>" not in line:
            continue
        heard, _, correct = line.partition("=>")
        heard, correct = heard.strip(), correct.strip()
        if heard and correct:
            pairs.append((heard, correct))
    return pairs


def hash_text(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:12]


# ------------------------------------------------------------------------------ prompt


def load_vocabulary_prompt(path: Path | None, max_chars: int = 600) -> str | None:
    """Read a vocabulary file (one term per line, ``#`` comments) into an initial_prompt.

    Returns ``None`` when no file is configured or it holds no terms. The prompt is the terms
    joined by ", " (most valuable first) and cut at a term boundary to ``max_chars``: Whisper only
    attends to the last 224 tokens of a prompt anyway.
    """
    terms = read_terms(path)
    if not terms:
        return None
    prompt = ""
    for term in terms:
        piece = term if not prompt else f", {term}"
        if max_chars and len(prompt) + len(piece) > max_chars:
            break
        prompt += piece
    return prompt or None


def prompt_hash(prompt: str | None) -> str | None:
    """Identity of the prompt a transcript was made with (``Transcript.vocab_hash``)."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12] if prompt else None


def current_prompt_hash(vocabulary_file: Path | None, max_chars: int = 600) -> str | None:
    return prompt_hash(load_vocabulary_prompt(vocabulary_file, max_chars))


def configured_prompt(asr) -> str | None:  # asr: ciw.config.AsrConfig
    """The initial_prompt the configuration asks for: None unless ``asr.vocabulary_prompt``."""
    if not asr.vocabulary_prompt:
        return None
    return load_vocabulary_prompt(asr.vocabulary_file, asr.initial_prompt_max_chars)


def configured_prompt_hash(asr) -> str | None:
    return prompt_hash(configured_prompt(asr))
