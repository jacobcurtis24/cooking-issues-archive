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
    conn: sqlite3.Connection, source: str | None = None, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM episodes"
    args: list[object] = []
    if source:
        sql += " WHERE source = ?"
        args.append(source)
    sql += " ORDER BY pub_date DESC, episode_id"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return conn.execute(sql, args).fetchall()


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
    }
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown feeds columns: {sorted(bad)}")
    conn.execute("INSERT OR IGNORE INTO feeds (name) VALUES (?)", (name,))
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE feeds SET {sets} WHERE name = ?", (*fields.values(), name))
