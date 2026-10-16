"""Podcast episodes blueprint: list, episode page, audio streaming and transcript download.

Read-only: the state DB (``state_db``) and the transcript/meta files are only read. Show notes
come from ``meta.json`` and are sanitised before they reach a page (:mod:`ciw.htmlsafe`).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    render_template,
    request,
    send_file,
    url_for,
)
from markupsafe import Markup, escape

from ciw import search_index as si
from ciw import state
from ciw.config import Config
from ciw.htmlsafe import plain_to_html, sanitize_html
from ciw.models import EpisodeMeta, Transcript
from ciw.transcript import GAP_S, UTTERANCE_MAX_WORDS, format_timestamp, split_utterances
from ciw.web import blog

bp = Blueprint("episodes", __name__)

PER_PAGE = 24
SOURCES = {"acast": "Acast", "hrn": "HRN", "patreon": "Patreon"}
STATUSES = ("all", "transcribed", "downloaded")
TRANSCRIBED_STAGES = ("transcribed", "segmented", "scored")
AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def cfg() -> Config:
    return current_app.config["CIW"]


def int_arg(name: str, default: int, lo: int = 1, hi: int = 10**6) -> int:
    """A query-string integer, clamped; anything unparsable gives ``default``."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, lo), hi)


# ------------------------------------------------------------------------- data access


@contextmanager
def _optional(factory):
    """Yield the connection ``factory()`` opens, or None if it cannot be opened (missing,
    unreadable or incompatible file). Errors raised in the ``with`` body are not swallowed."""
    with ExitStack() as stack:
        try:
            conn = stack.enter_context(factory())
        except sqlite3.Error:
            conn = None
        yield conn


def state_conn():
    """Read-only state connection, or ``None`` when there is no (readable) state DB."""
    path = cfg().state_db
    return _optional(lambda: state.connect_readonly(path)) if path.is_file() else _none()


def index_conn():
    """Read-only search-index connection, or ``None`` when it is missing/incompatible."""
    path = cfg().search_db
    return _optional(lambda: si.open_readonly(path)) if path.is_file() else _none()


@contextmanager
def _none():
    yield None


_META_CACHE: dict[str, tuple[tuple[int, int], EpisodeMeta | None]] = {}


def load_meta(episode_id: str) -> EpisodeMeta | None:
    """meta.json of an episode (cached by size and mtime); None if absent or unreadable."""
    path = cfg().episodes_dir / episode_id / "meta.json"
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_size, st.st_mtime_ns)
    key = str(path)
    cached = _META_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    try:
        meta: EpisodeMeta | None = EpisodeMeta.load(path)
    except (OSError, ValueError):
        meta = None
    if len(_META_CACHE) > 512:
        _META_CACHE.clear()
    _META_CACHE[key] = (sig, meta)
    return meta


def show_notes_text(episode_id: str) -> str:
    meta = load_meta(episode_id)
    return (meta.description or "") if meta else ""


def stage_of(row: sqlite3.Row) -> str:
    """``transcribed`` | ``audio`` | ``notes``: what the episode has so far."""
    if row["stage"] in TRANSCRIBED_STAGES:
        return "transcribed"
    if row["audio_path"]:
        return "audio"
    return "notes"


STAGE_LABELS = {"transcribed": "Transcribed", "audio": "Audio only", "notes": "Show notes only"}


def duration_of(row: sqlite3.Row) -> float | None:
    """Real audio length when downloaded, else the feed's."""
    return row["audio_duration_s"] or row["duration_s"]


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    minutes = int(round(seconds / 60))
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


@dataclass
class Card:
    id: str
    title: str
    date: str
    source: str
    source_label: str
    duration: str
    stage: str
    stage_label: str
    excerpt: str


def make_card(row: sqlite3.Row) -> Card:
    stage = stage_of(row)
    return Card(
        id=row["episode_id"],
        title=row["title"],
        date=row["pub_date"],
        source=row["source"],
        source_label=SOURCES.get(row["source"], row["source"]),
        duration=fmt_duration(duration_of(row)),
        stage=stage,
        stage_label=STAGE_LABELS[stage],
        excerpt=blog.short_excerpt(show_notes_text(row["episode_id"]), 240),
    )


def like_pattern(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------------------------------------------------------------------------- list page


@bp.route("/episodes")
def index():
    source = request.args.get("source", "").strip().lower()
    source = source if source in SOURCES else ""
    year = request.args.get("year", "").strip()
    year = year if re.fullmatch(r"\d{4}", year) else ""
    status = request.args.get("status", "all").strip().lower()
    status = status if status in STATUSES else "all"
    q = request.args.get("q", "").strip()[:200]
    page = int_arg("page", 1)

    where, args = [], []  # type: list[str], list[Any]
    if source:
        where.append("source = ?")
        args.append(source)
    if year:
        where.append("substr(pub_date, 1, 4) = ?")
        args.append(year)
    if status == "transcribed":
        where.append(f"stage IN ({','.join('?' * len(TRANSCRIBED_STAGES))})")
        args.extend(TRANSCRIBED_STAGES)
    elif status == "downloaded":
        where.append("audio_path IS NOT NULL")

    notice = ""
    if q:
        clauses = ["title LIKE ? ESCAPE '\\'"]
        qargs: list[Any] = [like_pattern(q)]
        fts = blog.safe_fts(q)
        if fts:
            with index_conn() as ic:
                if ic is None:
                    notice = (
                        "Show notes are searchable once the index is built (`ciw index`); "
                        "matching titles only."
                    )
                else:
                    try:
                        ids = sorted(si.notes_match_ids(ic, fts))
                    except sqlite3.Error:
                        ids = []
                    if ids:
                        clauses.append(f"episode_id IN ({','.join('?' * len(ids))})")
                        qargs.extend(ids)
        where.append("(" + " OR ".join(clauses) + ")")
        args.extend(qargs)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    cards: list[Card] = []
    total = grand_total = 0
    years: list[str] = []
    with state_conn() as conn:
        if conn is not None:
            grand_total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            total = conn.execute(f"SELECT COUNT(*) FROM episodes{clause}", args).fetchone()[0]
            years = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT substr(pub_date, 1, 4) AS y FROM episodes ORDER BY y DESC"
                )
            ]
            total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
            page = min(page, total_pages)
            rows = conn.execute(
                f"SELECT * FROM episodes{clause} ORDER BY pub_date DESC, episode_id "
                "LIMIT ? OFFSET ?",
                [*args, PER_PAGE, (page - 1) * PER_PAGE],
            ).fetchall()
            cards = [make_card(r) for r in rows]
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    filters = {k: v for k, v in (("source", source), ("year", year), ("q", q)) if v}
    if status != "all":
        filters["status"] = status
    return render_template(
        "episodes.html",
        cards=cards,
        page=page,
        total_pages=total_pages,
        total=total,
        grand_total=grand_total,
        years=years,
        source=source,
        year=year,
        status=status,
        q=q,
        filters=filters,
        sources=SOURCES,
        notice=notice,
        no_state=grand_total == 0 and not filters,
    )


def audio_file_for(row: sqlite3.Row) -> Path | None:
    """The stored audio file of an episode, only if it exists inside ``audio_dir``.

    The path comes exclusively from the state row (never from the request) and is resolved
    (symlinks and ``..`` included) before the containment check."""
    raw = row["audio_path"]
    if not raw:
        return None
    try:
        path = Path(raw).resolve(strict=True)
        root = cfg().audio_dir.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not path.is_file() or not path.is_relative_to(root):
        return None
    return path


# ------------------------------------------------------------------- transcript display


@dataclass
class UttView:
    idx: int
    start: float
    end: float
    speaker: str
    html: Markup


@dataclass
class GapView:
    start: float
    end: float
    seconds: int


_TRANSCRIPT_CACHE: dict[str, tuple[tuple[int, int], Transcript]] = {}


def load_transcript(episode_id: str) -> Transcript | None:
    """transcript.json of an episode (cached by size+mtime); None if absent. Raises ValueError
    if the file exists but is unreadable."""
    path = cfg().episodes_dir / episode_id / "transcript.json"
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_size, st.st_mtime_ns)
    cached = _TRANSCRIPT_CACHE.get(str(path))
    if cached and cached[0] == sig:
        return cached[1]
    try:
        transcript = Transcript.load(path)
    except OSError as exc:
        raise ValueError(f"cannot read transcript: {exc.strerror}") from exc
    if len(_TRANSCRIPT_CACHE) >= 6:
        _TRANSCRIPT_CACHE.clear()
    _TRANSCRIPT_CACHE[str(path)] = (sig, transcript)
    return transcript


def _fold(text: str) -> str:
    """Lower-case with diacritics removed (matches the index's unicode61 folding)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def query_tokens(q: str) -> list[str]:
    return [t for t in re.findall(r"\w+", _fold(q))][:8]


def word_matches(word: str, tokens: list[str]) -> bool:
    """True if any alphanumeric piece of ``word`` starts with a query token (FTS prefix rule)."""
    if not tokens:
        return False
    return any(piece.startswith(t) for piece in re.findall(r"\w+", _fold(word)) for t in tokens)


def transcript_views(
    transcript: Transcript, q: str = ""
) -> tuple[list[UttView | GapView], int, int]:
    """Display items (utterances and untranscribed-gap markers), the number of utterances and
    the number of highlighted words for ``q``."""
    tokens = query_tokens(q)
    items: list[UttView | GapView] = []
    hits = n_utts = 0
    prev_end: float | None = None
    for i, utt in enumerate(split_utterances(transcript.words, max_words=UTTERANCE_MAX_WORDS)):
        if prev_end is not None and utt.start - prev_end >= GAP_S:
            items.append(GapView(prev_end, utt.start, int(round(utt.start - prev_end))))
        prev_end = utt.end
        parts = []
        for w in utt.words:
            piece = escape(w.w)
            if word_matches(w.w, tokens):
                piece = Markup('<mark class="hit">{}</mark>').format(piece)
                hits += 1
            if w.orig:
                piece = Markup('<span class="corr" title="{}">{}</span>').format(
                    f"heard: {w.orig}", piece
                )
            parts.append(piece)
        items.append(UttView(i, utt.start, utt.end, utt.speaker or "", Markup(" ").join(parts)))
        n_utts += 1
    return items, n_utts, hits


def clock(seconds: float) -> str:
    return format_timestamp(seconds)


def episode_row(episode_id: str) -> sqlite3.Row | None:
    with state_conn() as conn:
        return state.get_episode(conn, episode_id) if conn is not None else None


def transcript_status(row: sqlite3.Row) -> str:
    """The one-line explanation shown when an episode has no transcript yet."""
    eid = row["episode_id"]
    stage = stage_of(row)
    if stage == "transcribed":
        return (
            "This episode is marked transcribed but its transcript file is missing or unreadable. "
            f"Run `ciw transcribe --episode {eid} --force`."
        )
    if stage == "audio":
        return (
            "The audio is downloaded but not transcribed yet. "
            f"Run `ciw transcribe --episode {eid}`."
        )
    return (
        "Only the show notes are available: the audio has not been downloaded. "
        f"Run `ciw download --episode {eid}` and then `ciw transcribe --episode {eid}`."
    )


@bp.route("/episode/<episode_id>")
def episode(episode_id: str):
    row = episode_row(episode_id)
    if row is None:
        abort(404)
    meta = load_meta(episode_id)
    notes_html = ""
    if meta is not None:
        notes_html = sanitize_html(meta.description_html) or plain_to_html(meta.description)
    q = request.args.get("q", "").strip()[:200]

    audio = audio_file_for(row)
    audio_problem = ""
    if row["audio_path"] and audio is None:
        audio_problem = (
            "The audio file recorded for this episode is missing or outside the audio folder. "
            f"Run `ciw download --episode {episode_id} --redownload`."
        )

    transcript: Transcript | None = None
    transcript_error = ""
    try:
        transcript = load_transcript(episode_id)
    except ValueError as exc:
        transcript_error = f"The transcript could not be read ({exc})."
    items: list[UttView | GapView] = []
    n_utts = hits = 0
    if transcript is not None:
        items, n_utts, hits = transcript_views(transcript, q)

    stage = stage_of(row)
    return render_template(
        "episode.html",
        ep=row,
        meta=meta,
        source_label=SOURCES.get(row["source"], row["source"]),
        stage=stage,
        stage_label=STAGE_LABELS[stage],
        duration=fmt_duration(duration_of(row)),
        notes_html=Markup(notes_html),
        has_audio=audio is not None,
        audio_problem=audio_problem,
        transcript=transcript,
        transcript_error=transcript_error,
        status_note="" if transcript is not None else transcript_status(row),
        items=items,
        n_utts=n_utts,
        gap_count=sum(isinstance(i, GapView) for i in items),
        q=q,
        hits=hits,
        correction_count=len(transcript.corrections) if transcript else 0,
    )


@bp.route("/audio/<episode_id>")
def audio(episode_id: str):
    row = episode_row(episode_id)
    path = audio_file_for(row) if row is not None else None
    if path is None:
        abort(404)
    mimetype = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    resp = send_file(path, mimetype=mimetype, conditional=True, max_age=0)
    resp.headers["Cache-Control"] = "private, no-cache"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/episode/<episode_id>/transcript.txt")
def transcript_txt(episode_id: str):
    row = episode_row(episode_id)
    if row is None:
        abort(404)
    try:
        transcript = load_transcript(episode_id)
    except ValueError:
        abort(404)
    if transcript is None:
        abort(404)
    resp = Response(transcript.render() + "\n", mimetype="text/plain")
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{episode_id}.txt"'
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


# ------------------------------------------------------------------ unified search (episodes)

log = logging.getLogger("ciw.web.episodes")
SNIPPETS_PER_EPISODE = 3
MAX_EPISODE_GROUPS = 20


@dataclass
class HitView:
    start: float
    clock: str
    snippet: Markup
    href: str


@dataclass
class EpisodeGroup:
    episode_id: str
    title: str
    date: str
    source_label: str
    stage_label: str
    notes_snippet: Markup
    title_snippet: Markup
    hits: list[HitView]
    n_hits: int  # utterances matching in this episode (may exceed len(hits))
    href: str


@dataclass
class EpisodeSearch:
    status: str = "ok"  # ok | no_index | bad_query | error
    message: str = ""
    groups: list[EpisodeGroup] = field(default_factory=list)
    total_hits: int = 0  # matching utterances
    total_episodes: int = 0  # episodes with a matching utterance or show notes
    shown: int = 0

    @property
    def count(self) -> int:
        return self.total_episodes


def hit_href(episode_id: str, q: str, start: float) -> str:
    """Link to an utterance: the episode page with the query highlighted, seeking to ``start``."""
    return url_for("episodes.episode", episode_id=episode_id, q=q, _anchor=f"t={int(start)}")


def search_episodes(q: str) -> EpisodeSearch:
    """Transcript and show-notes hits for ``q``, grouped by episode. Never raises: problems
    come back as a friendly ``status``/``message``."""
    fts = blog.safe_fts(" ".join(q.split()[:8]))
    if not fts:
        return EpisodeSearch(
            status="bad_query", message="Use at least one letter or number to search episodes."
        )
    try:
        with index_conn() as ic:
            if ic is None:
                return EpisodeSearch(
                    status="no_index",
                    message=(
                        "Episode search is not available yet: the transcript index has not been "
                        "built. Run `ciw index`."
                    ),
                )
            utt_hits = si.search_utterances(ic, fts)
            total_hits, utt_episodes = si.count_utterance_hits(ic, fts)
            per_episode = {
                r[0]: r[1]
                for r in ic.execute(
                    "SELECT episode_id, COUNT(*) FROM utterances_fts "
                    "WHERE utterances_fts MATCH ? GROUP BY episode_id",
                    (fts,),
                )
            }
            note_hits = si.search_notes(ic, fts)
    except sqlite3.Error:
        log.exception("episode search failed for %r", q)
        return EpisodeSearch(status="error", message="Episode search is unavailable right now.")

    by_episode: dict[str, list[si.UtteranceHit]] = {}
    for h in utt_hits:  # already in rank order, so the first hit decides the episode's rank
        by_episode.setdefault(h.episode_id, []).append(h)
    notes = {h.episode_id: h for h in note_hits}
    order = list(by_episode) + [e for e in notes if e not in by_episode]

    rows: dict[str, sqlite3.Row] = {}
    with state_conn() as conn:
        if conn is not None:
            for eid in order:
                row = state.get_episode(conn, eid)
                if row is not None:
                    rows[eid] = row
    order = [e for e in order if e in rows]  # an index entry for a forgotten episode is skipped

    groups = []
    for eid in order[:MAX_EPISODE_GROUPS]:
        row = rows[eid]
        best = sorted(by_episode.get(eid, [])[:SNIPPETS_PER_EPISODE], key=lambda h: h.start)
        note = notes.get(eid)
        stage = stage_of(row)
        groups.append(
            EpisodeGroup(
                episode_id=eid,
                title=row["title"],
                date=row["pub_date"],
                source_label=SOURCES.get(row["source"], row["source"]),
                stage_label=STAGE_LABELS[stage],
                notes_snippet=Markup(note.notes_snippet) if note else Markup(""),
                title_snippet=Markup(note.title_snippet) if note else Markup(""),
                hits=[
                    HitView(
                        h.start,
                        format_timestamp(h.start),
                        Markup(h.snippet),
                        url_for(
                            "episodes.episode", episode_id=eid, q=q, _anchor=f"t={int(h.start)}"
                        ),
                    )
                    for h in best
                ],
                n_hits=per_episode.get(eid, 0),
                href=url_for("episodes.episode", episode_id=eid, q=q),
            )
        )
    return EpisodeSearch(
        groups=groups,
        total_hits=total_hits,
        total_episodes=max(len(order), utt_episodes),
        shown=len(groups),
    )
