"""Podcast episodes blueprint: list, episode page, audio streaming and transcript download.

Read-only: the state DB (``state_db``) and the transcript/meta files are only read. Show notes
come from ``meta.json`` and are sanitised before they reach a page (:mod:`ciw.htmlsafe`).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, render_template, request

from ciw import search_index as si
from ciw import state
from ciw.config import Config
from ciw.models import EpisodeMeta
from ciw.web import blog

bp = Blueprint("episodes", __name__)

PER_PAGE = 24
SOURCES = {"acast": "Acast", "hrn": "HRN", "patreon": "Patreon"}
STATUSES = ("all", "transcribed", "downloaded")
TRANSCRIBED_STAGES = ("transcribed", "segmented", "scored")


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
def state_conn():
    """Read-only state connection, or ``None`` when there is no (readable) state DB."""
    path = cfg().state_db
    if not path.is_file():
        yield None
        return
    try:
        with state.connect_readonly(path) as conn:
            yield conn
    except sqlite3.Error:
        yield None


@contextmanager
def index_conn():
    """Read-only search-index connection, or ``None`` when it is missing/incompatible."""
    path = cfg().search_db
    if not path.is_file():
        yield None
        return
    try:
        with si.open_readonly(path) as conn:
            yield conn
    except sqlite3.Error:
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


def fmt_clock(seconds: float) -> str:
    """``12:34`` or ``1:02:03``."""
    s = max(int(seconds), 0)
    return (
        f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    )


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
