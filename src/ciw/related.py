"""Related blog posts for an episode, and episodes that mention a blog post's topic.

Method (no LLM, no network; the blog database is opened read-only):

1. Key terms. Every vocabulary term (``vocab/vocabulary.txt``) that occurs in the episode's show
   notes or transcript is a candidate. Its weight is ``(1 + ln(1 + tf)) * ln(1 + N / df)`` times
   1.5 if it is in the title and 1.25 if it is in the show notes, where ``tf`` is the number of
   mentions in the transcript (in the show notes when there is no transcript), ``df`` the number
   of blog posts matching the term (FTS5 phrase) and ``N`` the number of posts. Terms with
   ``df == 0`` (nothing to find) or ``df / N > 0.4`` (nearly every post: "Dave Arnold") are
   dropped. When fewer than ``MIN_TERMS`` vocabulary terms survive, capitalised phrases from the
   title and show notes ("Ashmead's Kernel", "Wylie Dufresne") fill up to the same budget under
   the same weighting. The best ``max_terms`` (3-8) are kept.
2. Posts. One FTS5 query ORs the quoted terms (prefix on the last word, so plurals match) and
   ranks with ``bm25`` (title counts 10x, categories and tags 2x). BM25 already prefers posts
   matching rarer terms and more of them. The ``limit`` best posts are returned with a highlighted
   snippet and the key terms each one actually contains.
"""

from __future__ import annotations

import html
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ciw.quality import _variants, tokens

MAX_DF_FRACTION = 0.4
MIN_TERMS = 3
MAX_TERMS = 8
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"
_SUFFIXES = ("", "s", "es")

_CAP_PHRASE = re.compile(r"\b[A-Z][\w’'\-]*(?:\s+[A-Z][\w’'\-]*){0,2}")
_STOP_CAPS = frozenset(
    "the a an and or but of in on at to for with from by this that these those it its is are was "
    "were be been we our you your i my he she they their his her there here what when where who "
    "how why if as not no yes so then than also just very more most some any all each every one "
    "two three listen subscribe patreon episode show notes today week welcome thanks thank "
    "monday tuesday wednesday thursday friday saturday sunday january february march april may "
    "june july august september october november december".split()
)


@dataclass
class KeyTerm:
    term: str
    tf: int
    df: int
    score: float
    source: str  # vocabulary | capitalized


@dataclass
class RelatedPost:
    id: int
    title: str
    date: str
    post_type: str
    snippet: str  # HTML-safe, with <mark>
    terms: list[str]  # key terms this post contains
    score: float  # bm25 (lower is better)


@dataclass
class Related:
    posts: list[RelatedPost] = field(default_factory=list)
    terms: list[KeyTerm] = field(default_factory=list)


# ---------------------------------------------------------------------------- FTS helpers


def fts_phrase(term: str) -> str:
    """An FTS5 phrase query for ``term`` (prefix on the last word), or '' if it has no words."""
    words = re.findall(r"\w+", term)
    return '"' + " ".join(words) + '"*' if words else ""


def connect_blog(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def make_df(conn: sqlite3.Connection) -> Callable[[str], int]:
    """A cached ``term -> number of posts matching it`` function over the blog FTS table."""
    cache: dict[str, int] = {}

    def df(term: str) -> int:
        q = fts_phrase(term)
        if not q:
            return 0
        if q not in cache:
            try:
                cache[q] = conn.execute(
                    "SELECT COUNT(*) FROM posts_fts WHERE posts_fts MATCH ?", (q,)
                ).fetchone()[0]
            except sqlite3.Error:
                cache[q] = 0
        return cache[q]

    return df


# ------------------------------------------------------------------------- term counting


def token_index(toks: Sequence[str]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for i, t in enumerate(toks):
        index.setdefault(t, []).append(i)
    return index


def count_mentions(term: str, toks: Sequence[str], index: dict[str, list[int]]) -> int:
    """Occurrences of ``term`` (same equivalences as ``ciw.quality.mentions``: hyphen = space,
    a plural/-es on the last word) using a prebuilt token index instead of a full scan."""
    found: set[int] = set()
    for seq in _variants(term):
        n, last = len(seq), seq[-1]
        starts: Iterable[int]
        if n == 1:
            starts = [i for suffix in _SUFFIXES for i in index.get(last + suffix, [])]
        else:
            starts = index.get(seq[0], [])
        for i in starts:
            if n == 1:
                found.add(i)
                continue
            if i + n > len(toks) or list(toks[i : i + n - 1]) != list(seq[:-1]):
                continue
            if toks[i + n - 1] in {last + s for s in _SUFFIXES}:
                found.add(i)
    return len(found)


def capitalized_phrases(text: str) -> Counter[str]:
    """Capitalised word runs in ``text`` (stop words trimmed from the ends), by surface form."""
    out: Counter[str] = Counter()
    for m in _CAP_PHRASE.finditer(text):
        words = m.group(0).replace("’", "'").split()
        while words and words[0].casefold() in _STOP_CAPS:
            words.pop(0)
        while words and words[-1].casefold() in _STOP_CAPS:
            words.pop()
        words = [w[:-2] if w.endswith("'s") else w for w in words]
        if not words:
            continue
        phrase = " ".join(words)
        if len(words) == 1 and (len(phrase) < 4 or _is_common(phrase)):
            continue
        out[phrase] += 1
    return out


def _is_common(word: str) -> bool:
    from ciw import vocab

    return vocab.is_common_word(word.lower())


# ---------------------------------------------------------------------------- key terms


def _weight(tf: int, df: int, n_posts: int, in_title: bool, in_notes: bool) -> float:
    boost = 1.5 if in_title else 1.25 if in_notes else 1.0
    return (1 + math.log(1 + tf)) * math.log(1 + n_posts / max(df, 1)) * boost


def key_terms(
    *,
    title: str,
    notes: str,
    transcript_text: str | None,
    vocabulary: Sequence[str],
    df: Callable[[str], int],
    n_posts: int,
    max_terms: int = MAX_TERMS,
    min_terms: int = MIN_TERMS,
    skip: Callable[[str], bool] | None = None,
) -> list[KeyTerm]:
    """The terms that characterise an episode, best first (see the module docstring)."""
    title_toks, notes_toks = tokens(title), tokens(notes)
    tr_toks = tokens(transcript_text) if transcript_text else []
    title_idx, notes_idx, tr_idx = (token_index(t) for t in (title_toks, notes_toks, tr_toks))

    def usable(term: str) -> int | None:
        if skip is not None and skip(term):
            return None
        d = df(term)
        return d if 0 < d <= MAX_DF_FRACTION * n_posts else None

    terms: dict[str, KeyTerm] = {}
    for term in vocabulary:
        in_title = count_mentions(term, title_toks, title_idx) > 0
        n_notes = count_mentions(term, notes_toks, notes_idx)
        n_tr = count_mentions(term, tr_toks, tr_idx) if tr_toks else 0
        if not (in_title or n_notes or n_tr):
            continue
        d = usable(term)
        if d is None:
            continue
        tf = n_tr if tr_toks else n_notes + int(in_title)
        terms[term.casefold()] = KeyTerm(
            term, tf, d, _weight(tf, d, n_posts, in_title, n_notes > 0), "vocabulary"
        )

    ranked = sorted(terms.values(), key=lambda k: (-k.score, k.term.casefold()))[:max_terms]
    if len(ranked) < min_terms:  # fall back to capitalised phrases from the title and notes
        have = {k.term.casefold() for k in ranked}
        counts = capitalized_phrases(title) + capitalized_phrases(notes)
        extra: list[KeyTerm] = []
        for phrase, n in counts.items():
            if phrase.casefold() in have or any(
                phrase.casefold() in h or h in phrase.casefold() for h in have
            ):
                continue
            d = usable(phrase)
            if d is None:
                continue
            in_title = bool(count_mentions(phrase, title_toks, title_idx))
            tf = count_mentions(phrase, tr_toks, tr_idx) if tr_toks else n
            extra.append(
                KeyTerm(phrase, tf, d, _weight(tf, d, n_posts, in_title, True), "capitalized")
            )
        extra.sort(key=lambda k: (-k.score, k.term.casefold()))
        ranked = (ranked + extra)[:max_terms]
    return ranked


def show_boilerplate(
    index_conn: sqlite3.Connection, threshold: float = 0.3
) -> Callable[[str], bool]:
    """A ``term -> bool`` test for terms that appear in more than ``threshold`` of all episodes'
    title/show notes ("Cooking Issues", "Heritage Radio Network"): they say nothing about one
    episode. Needs the search index; without one nothing is skipped."""
    cache: dict[str, bool] = {}
    try:
        total = index_conn.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0]
    except sqlite3.Error:
        return lambda term: False

    def skip(term: str) -> bool:
        q = fts_phrase(term)
        if not q or total < 10:
            return False
        if q not in cache:
            try:
                n = index_conn.execute(
                    "SELECT COUNT(*) FROM episodes_fts WHERE episodes_fts MATCH ?", (q,)
                ).fetchone()[0]
            except sqlite3.Error:
                n = 0
            cache[q] = n / total > threshold
        return cache[q]

    return skip


# ------------------------------------------------------------------------ related posts


def render_snippet(raw: str) -> str:
    return html.escape(raw, quote=False).replace(MARK_OPEN, "<mark>").replace(MARK_CLOSE, "</mark>")


def related_posts(
    blog_db: Path | str,
    *,
    title: str,
    notes: str = "",
    transcript_text: str | None = None,
    vocabulary: Sequence[str] = (),
    limit: int = 5,
    max_terms: int = MAX_TERMS,
    skip: Callable[[str], bool] | None = None,
) -> Related:
    """Up to ``limit`` blog posts related to an episode (see the module docstring)."""
    conn = connect_blog(blog_db)
    try:
        n_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        if not n_posts:
            return Related()
        terms = key_terms(
            title=title,
            notes=notes,
            transcript_text=transcript_text,
            vocabulary=vocabulary,
            df=make_df(conn),
            n_posts=n_posts,
            max_terms=max_terms,
            skip=skip,
        )
        queries = [q for q in (fts_phrase(t.term) for t in terms) if q]
        if not queries:
            return Related(terms=terms)
        rows = conn.execute(
            "SELECT p.id, p.title, p.date, p.post_type, p.body_text, "
            "bm25(posts_fts, 10.0, 1.0, 2.0, 2.0) AS score, "
            "snippet(posts_fts, 1, ?, ?, '…', 30) AS snip "
            "FROM posts_fts JOIN posts p ON p.id = posts_fts.rowid "
            "WHERE posts_fts MATCH ? ORDER BY score, p.id LIMIT ?",
            (MARK_OPEN, MARK_CLOSE, " OR ".join(queries), limit),
        ).fetchall()
    except sqlite3.Error:
        return Related()
    finally:
        conn.close()
    posts = []
    for r in rows:
        toks = tokens(f"{r['title']} {r['body_text'] or ''}")
        idx = token_index(toks)
        matched = [t.term for t in terms if count_mentions(t.term, toks, idx)]
        posts.append(
            RelatedPost(
                id=r["id"],
                title=r["title"],
                date=r["date"] or "",
                post_type=r["post_type"],
                snippet=render_snippet(r["snip"]),
                terms=matched,
                score=r["score"],
            )
        )
    return Related(posts=posts, terms=terms)


# -------------------------------------------------------------- episodes mentioning a post


@dataclass
class EpisodeMention:
    episode_id: str
    mentions: int  # transcript passages
    in_notes: bool
    term: str  # the post term that matched best


def post_terms(
    title: str, tags: str, categories: str, vocabulary: Sequence[str], max_terms: int = 3
) -> list[str]:
    """Vocabulary terms named in a blog post's title, tags or categories (the post's topic)."""
    text = f"{title} {tags} {categories}"
    toks = tokens(text)
    idx = token_index(toks)
    hits = [(count_mentions(t, toks, idx), t) for t in vocabulary]
    return [t for n, t in sorted(((n, t) for n, t in hits if n), key=lambda x: -x[0])][:max_terms]


def episodes_mentioning(
    index_conn: sqlite3.Connection, terms: Sequence[str], limit: int = 5
) -> list[EpisodeMention]:
    """Episodes whose transcript or show notes mention any of ``terms``, most mentions first."""
    queries = [q for q in (fts_phrase(t) for t in terms) if q]
    if not queries:
        return []
    fts = " OR ".join(queries)
    counts: dict[str, EpisodeMention] = {}
    try:
        for row in index_conn.execute(
            "SELECT episode_id, COUNT(*) FROM utterances_fts WHERE utterances_fts MATCH ? "
            "GROUP BY episode_id",
            (fts,),
        ):
            counts[row[0]] = EpisodeMention(row[0], row[1], False, terms[0])
        for row in index_conn.execute(
            "SELECT episode_id FROM episodes_fts WHERE episodes_fts MATCH ?", (fts,)
        ):
            m = counts.setdefault(row[0], EpisodeMention(row[0], 0, False, terms[0]))
            m.in_notes = True
    except sqlite3.Error:
        return []
    ranked = sorted(counts.values(), key=lambda m: (-(m.mentions + 5 * m.in_notes), m.episode_id))
    return ranked[:limit]
