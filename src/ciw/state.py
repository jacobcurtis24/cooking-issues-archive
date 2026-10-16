"""Pipeline state in SQLite (``data/state.sqlite``): episodes, their guids, feed bookkeeping.

Schema changes are versioned migrations keyed on ``PRAGMA user_version``; add a new entry to
``MIGRATIONS`` rather than editing an old one. All writers are idempotent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ciw.models import EpisodeMeta

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE episodes (
        episode_id   TEXT PRIMARY KEY,
        source       TEXT NOT NULL,
        title        TEXT NOT NULL,
        pub_date     TEXT NOT NULL,
        duration_s   REAL,
        stage        TEXT NOT NULL DEFAULT 'discovered',
        error        TEXT,
        audio_source TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX episodes_pub_date ON episodes(pub_date);
    CREATE TABLE episode_guids (
        episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
        source     TEXT NOT NULL,
        guid       TEXT NOT NULL,
        UNIQUE(source, guid)
    );
    CREATE INDEX episode_guids_episode ON episode_guids(episode_id);
    CREATE TABLE feeds (
        name          TEXT PRIMARY KEY,
        apple_id      INTEGER,
        feed_url      TEXT,
        etag          TEXT,
        last_modified TEXT,
        last_checked  TEXT,
        last_status   TEXT,
        item_count    INTEGER
    );
    """,
    # 2: downloader columns (T4) and the last feed-health status an alert was sent for
    """
    ALTER TABLE episodes ADD COLUMN audio_path TEXT;
    ALTER TABLE episodes ADD COLUMN audio_bytes INTEGER;
    ALTER TABLE episodes ADD COLUMN audio_sha256 TEXT;
    ALTER TABLE episodes ADD COLUMN audio_duration_s REAL;
    ALTER TABLE episodes ADD COLUMN downloaded_at TEXT;
    ALTER TABLE episodes ADD COLUMN audio_downloaded_from TEXT;
    CREATE INDEX episodes_stage ON episodes(stage);
    ALTER TABLE feeds ADD COLUMN alert_status TEXT;
    """,
    # 3: transcription columns (T5)
    """
    ALTER TABLE episodes ADD COLUMN transcribed_at TEXT;
    ALTER TABLE episodes ADD COLUMN asr_model TEXT;
    ALTER TABLE episodes ADD COLUMN asr_version TEXT;
    ALTER TABLE episodes ADD COLUMN asr_runtime_s REAL;
    ALTER TABLE episodes ADD COLUMN diarization INTEGER;
    """,
]


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; return the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise RuntimeError(f"state.sqlite schema v{version} is newer than this code")
    for number in range(version + 1, len(MIGRATIONS) + 1):
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[number - 1]}\nPRAGMA user_version = {number};\nCOMMIT;"
        )
    return len(MIGRATIONS)


@contextmanager
def connect(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open (creating the directory and schema as needed) the state DB.

    Commits on a clean exit, rolls back on an exception, always closes. WAL mode.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # we manage transactions
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        migrate(conn)
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


# ------------------------------------------------------------------------ episodes


def get_episode(conn: sqlite3.Connection, episode_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)).fetchone()


def find_episode_ids(conn: sqlite3.Connection, guids: Iterable[tuple[str, str]]) -> list[str]:
    """episode_ids that already own any of the (source, guid) pairs, oldest row first."""
    found: dict[str, str] = {}
    for source, guid in guids:
        row = conn.execute(
            "SELECT g.episode_id, e.created_at FROM episode_guids g "
            "JOIN episodes e USING (episode_id) WHERE g.source = ? AND g.guid = ?",
            (source, guid),
        ).fetchone()
        if row:
            found[row["episode_id"]] = row["created_at"]
    return sorted(found, key=lambda k: (found[k], k))


def guid_owner(conn: sqlite3.Connection, source: str, guid: str) -> str | None:
    row = conn.execute(
        "SELECT episode_id FROM episode_guids WHERE source = ? AND guid = ?", (source, guid)
    ).fetchone()
    return row["episode_id"] if row else None


def episode_guids(conn: sqlite3.Connection, episode_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT source, guid FROM episode_guids WHERE episode_id = ?", (episode_id,)
    )
    return {r["source"]: r["guid"] for r in rows}


def upsert_episode(conn: sqlite3.Connection, meta: EpisodeMeta) -> str:
    """Insert or update the episode row and its guids. Returns ``new``, ``updated`` or
    ``unchanged``. ``stage``/``error`` are never touched here; ``updated_at`` only moves when a
    column really changes, so repeating the call with the same meta is a no-op."""
    pub = meta.pub_date.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = (meta.source, meta.title, pub, meta.duration_s, meta.audio_source)
    row = get_episode(conn, meta.guid)
    if row is None:
        stamp = now_iso()
        conn.execute(
            "INSERT INTO episodes (episode_id, source, title, pub_date, duration_s, audio_source, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (meta.guid, *values, stamp, stamp),
        )
        outcome = "new"
    else:
        current = (
            row["source"],
            row["title"],
            row["pub_date"],
            row["duration_s"],
            row["audio_source"],
        )
        outcome = "unchanged"
        if current != values:
            conn.execute(
                "UPDATE episodes SET source=?, title=?, pub_date=?, duration_s=?, audio_source=?, "
                "updated_at=? WHERE episode_id=?",
                (*values, now_iso(), meta.guid),
            )
            outcome = "updated"
    have = episode_guids(conn, meta.guid)
    for source, guid in meta.guids.items():
        if have.get(source) != guid:
            conn.execute(
                "DELETE FROM episode_guids WHERE episode_id = ? AND source = ?", (meta.guid, source)
            )
            conn.execute(
                "INSERT INTO episode_guids (episode_id, source, guid) VALUES (?, ?, ?)",
                (meta.guid, source, guid),
            )
            if outcome == "unchanged":
                outcome = "updated"
                conn.execute(
                    "UPDATE episodes SET updated_at = ? WHERE episode_id = ?",
                    (now_iso(), meta.guid),
                )
    return outcome


def delete_episode(conn: sqlite3.Connection, episode_id: str) -> None:
    conn.execute("DELETE FROM episodes WHERE episode_id = ?", (episode_id,))  # guids cascade


def list_episodes(
    conn: sqlite3.Connection,
    source: str | None = None,
    limit: int | None = None,
    stage: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM episodes"
    args: list[object] = []
    where = []
    if source:
        where.append("source = ?")
        args.append(source)
    if stage:
        where.append("stage = ?")
        args.append(stage)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY pub_date DESC, episode_id"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


def checkpoint(conn: sqlite3.Connection) -> None:
    """Commit what has been written so far and open a fresh transaction.

    ``connect`` wraps everything in one transaction; long-running jobs (downloads) call this after
    each state write so the write lock is not held while bytes are streaming."""
    conn.execute("COMMIT")
    conn.execute("BEGIN")


def mark_downloaded(
    conn: sqlite3.Connection,
    episode_id: str,
    *,
    audio_path: str,
    audio_bytes: int,
    audio_duration_s: float | None,
    audio_sha256: str | None,
    audio_downloaded_from: str | None,
) -> None:
    """Record a verified audio file: stage ``downloaded``, error cleared."""
    stamp = now_iso()
    conn.execute(
        "UPDATE episodes SET stage='downloaded', error=NULL, audio_path=?, audio_bytes=?, "
        "audio_duration_s=?, audio_sha256=?, audio_downloaded_from=?, downloaded_at=?, "
        "updated_at=? WHERE episode_id=?",
        (
            audio_path,
            audio_bytes,
            audio_duration_s,
            audio_sha256,
            audio_downloaded_from,
            stamp,
            stamp,
            episode_id,
        ),
    )


# Pipeline stages in order; a stage helper never moves an episode backwards.
STAGES = ("discovered", "downloaded", "transcribed", "segmented", "scored")


def mark_transcribed(
    conn: sqlite3.Connection,
    episode_id: str,
    *,
    asr_model: str,
    asr_version: str,
    asr_runtime_s: float | None,
    diarization: bool,
) -> None:
    """Record a finished transcript: stage ``transcribed`` (unless already further along)."""
    stamp = now_iso()
    row = get_episode(conn, episode_id)
    keep_stage = row is not None and row["stage"] in STAGES[STAGES.index("transcribed") :]
    conn.execute(
        "UPDATE episodes SET error=NULL, transcribed_at=?, asr_model=?, asr_version=?, "
        "asr_runtime_s=?, diarization=?, updated_at=? "
        + ("" if keep_stage else ", stage='transcribed' ")
        + "WHERE episode_id=?",
        (stamp, asr_model, asr_version, asr_runtime_s, int(diarization), stamp, episode_id),
    )


def set_error(
    conn: sqlite3.Connection, episode_id: str, error: str | None, *, stage: str | None = None
) -> None:
    """Record (or clear) an episode's last error; optionally also move it to ``stage``."""
    if stage is None:
        conn.execute(
            "UPDATE episodes SET error=?, updated_at=? WHERE episode_id=?",
            (error, now_iso(), episode_id),
        )
    else:
        conn.execute(
            "UPDATE episodes SET error=?, stage=?, updated_at=? WHERE episode_id=?",
            (error, stage, now_iso(), episode_id),
        )


def count_episodes(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT source, COUNT(*) AS n FROM episodes GROUP BY source")
    return {r["source"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- feeds


def get_feed(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM feeds WHERE name = ?", (name,)).fetchone()


def save_feed(conn: sqlite3.Connection, name: str, **fields: object) -> None:
    """Create or partially update a ``feeds`` row (only the given columns change)."""
    allowed = {
        "apple_id",
        "feed_url",
        "etag",
        "last_modified",
        "last_checked",
        "last_status",
        "item_count",
        "alert_status",
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown feeds columns: {sorted(bad)}")
    conn.execute("INSERT OR IGNORE INTO feeds (name) VALUES (?)", (name,))
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE feeds SET {sets} WHERE name = ?", (*fields.values(), name))


def locked_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(acast_guid, patreon_guid) of episodes that already own both guids."""
    rows = conn.execute(
        "SELECT a.guid AS a, p.guid AS p FROM episode_guids a "
        "JOIN episode_guids p ON a.episode_id = p.episode_id "
        "WHERE a.source = 'acast' AND p.source = 'patreon'"
    )
    return [(r["a"], r["p"]) for r in rows]


def episodes_with_guid_source(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    """Episodes that carry a guid from ``source`` (newest first)."""
    return conn.execute(
        "SELECT e.* FROM episodes e JOIN episode_guids g USING (episode_id) "
        "WHERE g.source = ? ORDER BY e.pub_date DESC, e.episode_id",
        (source,),
    ).fetchall()
