"""Full-text index over transcripts and show notes: ``data/search.sqlite`` (config ``search_db``).

A separate SQLite file (never ``cooking_issues.db``) that is always rebuildable from
``data/episodes/<id>/{meta.json,transcript.json}`` and the state DB. Two FTS5 tables:

``utterances_fts``  one row per utterance of every transcript: ``text`` (corrected words) is the
                    only indexed column; ``episode_id, utt_idx, start_s, end_s, speaker`` are
                    stored UNINDEXED. Utterances are the ones the transcript page shows
                    (:func:`ciw.transcript.split_utterances`, at most 50 words), so a hit lands
                    near the right timestamp and ``utt_idx`` is the page's ``#u<idx>`` anchor.
``episodes_fts``    one row per episode: ``title`` and ``notes`` (show notes), so an episode
                    without a transcript is still findable.

``indexed`` records, per episode, what was indexed (transcript size+mtime and content hash, show
notes hash, the rowid range of its utterances) so a run only touches episodes whose transcript or
show notes changed, and deletes rows of transcripts that disappeared. Tokenizer: unicode61 with
diacritics removed and prefix indexes (2-4 characters) so ``"word"*`` queries are fast.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ciw import state
from ciw.config import Config, guard_path
from ciw.models import EpisodeMeta, Transcript
from ciw.transcript import UTTERANCE_MAX_WORDS, split_utterances

log = logging.getLogger("ciw.search_index")

SCHEMA_VERSION = 1
TOKENIZE = "unicode61 remove_diacritics 2"
PREFIXES = "2 3 4"
# snippet() highlight sentinels: never present in text, replaced by <mark> after HTML-escaping.
MARK_OPEN, MARK_CLOSE = "\x02", "\x03"

DDL = f"""
CREATE TABLE indexed (
    episode_id       TEXT PRIMARY KEY,
    transcript_sig   TEXT,            -- "<size>:<mtime_ns>" of transcript.json when indexed
    transcript_sha1  TEXT,            -- content hash: a touched but identical file is skipped
    notes_hash       TEXT,
    has_transcript   INTEGER NOT NULL DEFAULT 0,
    n_utterances     INTEGER NOT NULL DEFAULT 0,
    first_rowid      INTEGER,         -- utterances_fts rowids of this episode (contiguous)
    notes_rowid      INTEGER,         -- episodes_fts rowid
    indexed_at       TEXT NOT NULL
);
CREATE VIRTUAL TABLE utterances_fts USING fts5(
    text,
    episode_id UNINDEXED, utt_idx UNINDEXED, start_s UNINDEXED, end_s UNINDEXED,
    speaker UNINDEXED,
    tokenize = '{TOKENIZE}', prefix = '{PREFIXES}'
);
CREATE VIRTUAL TABLE episodes_fts USING fts5(
    title, notes, episode_id UNINDEXED,
    tokenize = '{TOKENIZE}', prefix = '{PREFIXES}'
);
"""


# ------------------------------------------------------------------------ database


def index_exists(path: Path | str) -> bool:
    return Path(path).is_file()


def _create(conn: sqlite3.Connection) -> None:
    conn.executescript(f"BEGIN;\n{DDL}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")


def open_index(path: Path | str, *, create: bool = True) -> sqlite3.Connection:
    """Open the writable index (creating file and schema). Autocommit; callers use BEGIN."""
    path = Path(path)
    guard_path(path, "search index")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        if not create:
            conn.close()
            raise FileNotFoundError(path)
        conn.execute("PRAGMA journal_mode=WAL")
        _create(conn)
    elif version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"{path} has index schema v{version}, this code needs v{SCHEMA_VERSION}: "
            "run `ciw index --rebuild`"
        )
    return conn


@contextmanager
def open_readonly(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Read-only connection for the web app. Raises ``sqlite3.Error`` if missing/incompatible."""
    path = Path(path)
    guard_path(path, "search index")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"index schema v{version}, expected v{SCHEMA_VERSION}")
        yield conn
    finally:
        conn.close()


# ----------------------------------------------------------------------- documents


@dataclass
class IndexedUtterance:
    idx: int
    start: float
    end: float
    speaker: str | None
    text: str


def build_utterances(
    transcript: Transcript, max_words: int = UTTERANCE_MAX_WORDS
) -> list[IndexedUtterance]:
    """The utterances of a transcript as they are indexed (and displayed)."""
    return [
        IndexedUtterance(i, u.start, u.end, u.speaker, u.text)
        for i, u in enumerate(split_utterances(transcript.words, max_words=max_words))
    ]


def _sha1(data: bytes | str) -> str:
    return hashlib.sha1(data if isinstance(data, bytes) else data.encode()).hexdigest()


def transcript_signature(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{st.st_size}:{st.st_mtime_ns}"


def episode_notes(cfg: Config, episode_id: str) -> str:
    """Plain-text show notes from meta.json ('' if there is none)."""
    try:
        meta = EpisodeMeta.load(cfg.episodes_dir / episode_id / "meta.json")
    except (OSError, ValueError):
        return ""
    return meta.description or ""


# ---------------------------------------------------------------------- indexing


@dataclass
class IndexReport:
    transcripts_indexed: int = 0
    transcripts_unchanged: int = 0
    transcripts_removed: int = 0
    utterances: int = 0  # utterances written in this run
    notes_indexed: int = 0
    notes_unchanged: int = 0
    episodes_removed: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    total_utterances: int = 0
    total_episodes: int = 0
    total_transcripts: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.transcripts_indexed
            or self.transcripts_removed
            or self.notes_indexed
            or self.episodes_removed
        )

    def summary(self) -> str:
        parts = [
            f"transcripts: {self.transcripts_indexed} indexed ({self.utterances} utterances), "
            f"{self.transcripts_unchanged} unchanged, {self.transcripts_removed} removed",
            f"show notes: {self.notes_indexed} indexed, {self.notes_unchanged} unchanged",
        ]
        if self.episodes_removed:
            parts.append(f"{self.episodes_removed} episodes removed")
        parts.append(
            f"index now holds {self.total_utterances} utterances from "
            f"{self.total_transcripts} transcripts, {self.total_episodes} episodes"
        )
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return "; ".join(parts) + f" ({self.elapsed_s:.1f}s)"


def _delete_utterances(conn: sqlite3.Connection, row: sqlite3.Row | None) -> None:
    if row is not None and row["first_rowid"] is not None and row["n_utterances"]:
        conn.execute(
            "DELETE FROM utterances_fts WHERE rowid BETWEEN ? AND ?",
            (row["first_rowid"], row["first_rowid"] + row["n_utterances"] - 1),
        )


def _next_utterance_rowid(conn: sqlite3.Connection) -> int:
    # from the bookkeeping table: MAX(rowid) on an FTS5 table is a scan
    return conn.execute(
        "SELECT COALESCE(MAX(first_rowid + n_utterances), 1) FROM indexed WHERE n_utterances > 0"
    ).fetchone()[0]


def _next_notes_rowid(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(notes_rowid), 0) + 1 FROM indexed").fetchone()[0]


def _index_one(
    conn: sqlite3.Connection,
    cfg: Config,
    episode_id: str,
    title: str,
    report: IndexReport,
    *,
    force: bool,
) -> None:
    from ciw.models import utcnow

    row = conn.execute("SELECT * FROM indexed WHERE episode_id = ?", (episode_id,)).fetchone()
    tpath = cfg.episodes_dir / episode_id / "transcript.json"
    sig = transcript_signature(tpath)
    notes = episode_notes(cfg, episode_id)
    notes_hash = _sha1(f"{title}\0{notes}")
    now = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    new = {
        "transcript_sig": row["transcript_sig"] if row else None,
        "transcript_sha1": row["transcript_sha1"] if row else None,
        "has_transcript": row["has_transcript"] if row else 0,
        "n_utterances": row["n_utterances"] if row else 0,
        "first_rowid": row["first_rowid"] if row else None,
        "notes_rowid": row["notes_rowid"] if row else None,
        "notes_hash": row["notes_hash"] if row else None,
    }
    touched = False

    # --- show notes + title
    if force or row is None or row["notes_hash"] != notes_hash or row["notes_rowid"] is None:
        if row is not None and row["notes_rowid"] is not None:
            conn.execute("DELETE FROM episodes_fts WHERE rowid = ?", (row["notes_rowid"],))
        rid = _next_notes_rowid(conn)
        conn.execute(
            "INSERT INTO episodes_fts (rowid, title, notes, episode_id) VALUES (?, ?, ?, ?)",
            (rid, title, notes, episode_id),
        )
        new["notes_rowid"], new["notes_hash"] = rid, notes_hash
        report.notes_indexed += 1
        touched = True
    else:
        report.notes_unchanged += 1

    # --- transcript
    if sig is None:
        if row is not None and row["has_transcript"]:
            _delete_utterances(conn, row)
            new.update(
                transcript_sig=None,
                transcript_sha1=None,
                has_transcript=0,
                n_utterances=0,
                first_rowid=None,
            )
            report.transcripts_removed += 1
            touched = True
    elif not force and row is not None and row["has_transcript"] and row["transcript_sig"] == sig:
        report.transcripts_unchanged += 1
    else:
        data = tpath.read_bytes()
        digest = _sha1(data)
        if (
            not force
            and row is not None
            and row["has_transcript"]
            and row["transcript_sha1"] == digest
        ):
            new["transcript_sig"] = sig  # touched, not changed
            report.transcripts_unchanged += 1
            touched = True
        else:
            transcript = Transcript.from_json(data)
            utts = build_utterances(transcript)
            _delete_utterances(conn, row)
            first = _next_utterance_rowid(conn)
            conn.executemany(
                "INSERT INTO utterances_fts "
                "(rowid, text, episode_id, utt_idx, start_s, end_s, speaker) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (first + u.idx, u.text, episode_id, u.idx, u.start, u.end, u.speaker or "")
                    for u in utts
                ),
            )
            new.update(
                transcript_sig=sig,
                transcript_sha1=digest,
                has_transcript=1,
                n_utterances=len(utts),
                first_rowid=first if utts else None,
            )
            report.transcripts_indexed += 1
            report.utterances += len(utts)
            touched = True

    if touched or row is None:
        conn.execute(
            "INSERT OR REPLACE INTO indexed (episode_id, transcript_sig, transcript_sha1, "
            "notes_hash, has_transcript, n_utterances, first_rowid, notes_rowid, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id,
                new["transcript_sig"],
                new["transcript_sha1"],
                new["notes_hash"],
                new["has_transcript"],
                new["n_utterances"],
                new["first_rowid"],
                new["notes_rowid"],
                now,
            ),
        )


def _state_episodes(cfg: Config) -> list[tuple[str, str]]:
    """(episode_id, title) of every episode in the state DB ([] if there is no state DB)."""
    if not cfg.state_db.is_file():
        return []
    with state.connect_readonly(cfg.state_db) as conn:
        return [(r["episode_id"], r["title"]) for r in state.list_episodes(conn)]


def _remove_episode(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    _delete_utterances(conn, row)
    if row["notes_rowid"] is not None:
        conn.execute("DELETE FROM episodes_fts WHERE rowid = ?", (row["notes_rowid"],))
    conn.execute("DELETE FROM indexed WHERE episode_id = ?", (row["episode_id"],))


def update_index(
    cfg: Config,
    *,
    episode_ids: Iterable[str] | None = None,
    force: bool = False,
    path: Path | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> IndexReport:
    """Bring the index up to date. ``episode_ids`` limits the work to those episodes (no removal
    pass for the rest); ``force`` re-indexes the selected episodes even if unchanged."""
    started = time.perf_counter()
    report = IndexReport()
    episodes = _state_episodes(cfg)
    titles = dict(episodes)
    wanted = list(titles) if episode_ids is None else [e for e in episode_ids if e in titles]
    conn = open_index(path or cfg.search_db)
    try:
        for n, eid in enumerate(wanted, 1):
            conn.execute("BEGIN")
            try:
                _index_one(conn, cfg, eid, titles[eid], report, force=force)
            except Exception as exc:  # one bad transcript must not stop the run
                conn.execute("ROLLBACK")
                report.errors.append(f"{eid}: {type(exc).__name__}: {exc}")
                log.warning("indexing %s failed: %s", eid, exc)
            else:
                conn.execute("COMMIT")
            if progress:
                progress(eid, n, len(wanted))
        if episode_ids is None:  # full pass: forget episodes that left the state DB
            conn.execute("BEGIN")
            for row in conn.execute("SELECT * FROM indexed").fetchall():
                if row["episode_id"] not in titles:
                    _remove_episode(conn, row)
                    report.episodes_removed += 1
            conn.execute("COMMIT")
        report.total_utterances = conn.execute(
            "SELECT COALESCE(SUM(n_utterances), 0) FROM indexed"
        ).fetchone()[0]
        report.total_transcripts = conn.execute(
            "SELECT COUNT(*) FROM indexed WHERE has_transcript = 1"
        ).fetchone()[0]
        report.total_episodes = conn.execute("SELECT COUNT(*) FROM indexed").fetchone()[0]
    finally:
        conn.close()
    report.elapsed_s = time.perf_counter() - started
    return report


def rebuild_index(
    cfg: Config, *, progress: Callable[[str, int, int], None] | None = None
) -> IndexReport:
    """Build a fresh index beside the old one and swap it in (readers never see a half-built
    file)."""
    target = cfg.search_db
    guard_path(target, "search index")
    tmp = target.with_name(target.name + ".new")
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    try:
        report = update_index(cfg, path=tmp, progress=progress)
        conn = sqlite3.connect(tmp, isolation_level=None)
        try:
            conn.execute("INSERT INTO utterances_fts(utterances_fts) VALUES('optimize')")
            conn.execute("INSERT INTO episodes_fts(episodes_fts) VALUES('optimize')")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        for suffix in ("-wal", "-shm", "-journal"):  # the old file's side files must not be
            Path(str(target) + suffix).unlink(missing_ok=True)  # applied to the new one
        os.replace(tmp, target)
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
    return report


def index_best_effort(
    cfg: Config, episode_ids: Iterable[str], echo: Callable[[str], None] | None = None
) -> IndexReport | None:
    """Re-index these episodes after their transcript changed. Never raises: a failure only
    logs/echoes a warning (the transcript itself is already safely written)."""
    try:
        report = update_index(cfg, episode_ids=list(episode_ids))
    except Exception as exc:
        msg = f"warning: search index not updated ({type(exc).__name__}: {exc}); run `ciw index`"
        log.warning(msg)
        if echo:
            echo(msg)
        return None
    if echo and (report.changed or report.errors):
        echo(f"search index: {report.summary()}")
    return report


# ----------------------------------------------------------------------- querying


@dataclass
class UtteranceHit:
    episode_id: str
    utt_idx: int
    start: float
    end: float
    speaker: str
    snippet: str  # HTML-safe, with <mark>
    rank: float


@dataclass
class NotesHit:
    episode_id: str
    title_snippet: str
    notes_snippet: str
    rank: float


def render_snippet(raw: str) -> str:
    """Escape an FTS5 ``snippet()`` made with the MARK sentinels and turn them into ``<mark>``."""
    escaped = html.escape(raw, quote=False)
    return escaped.replace(MARK_OPEN, "<mark>").replace(MARK_CLOSE, "</mark>")


def count_utterance_hits(conn: sqlite3.Connection, fts: str) -> tuple[int, int]:
    """(utterances matching, episodes they belong to)."""
    row = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT episode_id) FROM utterances_fts "
        "WHERE utterances_fts MATCH ?",
        (fts,),
    ).fetchone()
    return row[0], row[1]


def search_utterances(
    conn: sqlite3.Connection, fts: str, *, limit: int = 400
) -> list[UtteranceHit]:
    rows = conn.execute(
        "SELECT episode_id, utt_idx, start_s, end_s, speaker, rank, "
        "snippet(utterances_fts, 0, ?, ?, '…', 24) AS snip "
        "FROM utterances_fts WHERE utterances_fts MATCH ? ORDER BY rank LIMIT ?",
        (MARK_OPEN, MARK_CLOSE, fts, limit),
    ).fetchall()
    return [
        UtteranceHit(
            r["episode_id"],
            int(r["utt_idx"]),
            float(r["start_s"]),
            float(r["end_s"]),
            r["speaker"] or "",
            render_snippet(r["snip"]),
            r["rank"],
        )
        for r in rows
    ]


def search_notes(conn: sqlite3.Connection, fts: str, *, limit: int = 100) -> list[NotesHit]:
    """Episodes whose title or show notes match. A snippet is only returned for a column that
    actually contains a match (FTS5 would otherwise return the column's first words)."""
    rows = conn.execute(
        "SELECT episode_id, rank, "
        "snippet(episodes_fts, 0, ?, ?, '…', 60) AS t, "
        "snippet(episodes_fts, 1, ?, ?, '…', 28) AS n "
        "FROM episodes_fts WHERE episodes_fts MATCH ? ORDER BY rank LIMIT ?",
        (MARK_OPEN, MARK_CLOSE, MARK_OPEN, MARK_CLOSE, fts, limit),
    ).fetchall()

    def marked(raw: str) -> str:
        return render_snippet(raw) if MARK_OPEN in raw else ""

    return [NotesHit(r["episode_id"], marked(r["t"]), marked(r["n"]), r["rank"]) for r in rows]


def notes_match_ids(conn: sqlite3.Connection, fts: str) -> set[str]:
    rows = conn.execute("SELECT episode_id FROM episodes_fts WHERE episodes_fts MATCH ?", (fts,))
    return {r[0] for r in rows}


def safe_term(text: str) -> str:
    """A string usable inside an FTS5 double-quoted phrase."""
    return re.sub(r'["\x00-\x1f]', " ", text).strip()
