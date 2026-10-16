"""state.sqlite: migrations, WAL, idempotent upserts, rollback."""

from datetime import UTC, datetime

import pytest

from ciw import state
from ciw.models import EpisodeMeta

T0 = datetime(2026, 1, 24, 8, 0, tzinfo=UTC)


def meta(**kw):
    base = dict(
        guid="a" * 24,
        guids={"acast": "a" * 24},
        source="acast",
        audio_source="acast",
        title="T",
        pub_date=T0,
        enclosure_url="https://x/y.mp3",
        duration_s=100.0,
    )
    base.update(kw)
    return EpisodeMeta(**base)


def test_schema_version_wal_and_directory_creation(tmp_path):
    db = tmp_path / "nested" / "state.sqlite"
    with state.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(state.MIGRATIONS)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"episodes", "episode_guids", "feeds"} <= tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)")}
        assert {"episode_id", "stage", "error", "audio_source", "created_at", "updated_at"} <= cols
    with state.connect(db) as conn:  # reopening does not re-migrate or fail
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(state.MIGRATIONS)


def test_newer_schema_is_refused(tmp_path):
    db = tmp_path / "s.sqlite"
    with state.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {len(state.MIGRATIONS) + 5}")
    with pytest.raises(RuntimeError), state.connect(db):
        pass


def test_upsert_is_idempotent(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        assert state.upsert_episode(conn, meta()) == "new"
        row = state.get_episode(conn, "a" * 24)
        assert row["stage"] == "discovered" and row["error"] is None
        assert state.upsert_episode(conn, meta()) == "unchanged"
        assert state.upsert_episode(conn, meta(title="T2")) == "updated"
        assert state.get_episode(conn, "a" * 24)["title"] == "T2"
        assert state.upsert_episode(conn, meta(title="T2")) == "unchanged"


def test_stage_is_not_overwritten_by_upsert(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        state.upsert_episode(conn, meta())
        conn.execute("UPDATE episodes SET stage='downloaded'")
        state.upsert_episode(conn, meta(title="changed"))
        assert state.get_episode(conn, "a" * 24)["stage"] == "downloaded"


def test_guid_added_counts_as_update_and_is_unique(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        state.upsert_episode(conn, meta())
        merged = meta(guids={"acast": "a" * 24, "patreon": "p1"})
        assert state.upsert_episode(conn, merged) == "updated"
        assert state.upsert_episode(conn, merged) == "unchanged"
        assert state.find_episode_ids(conn, [("patreon", "p1")]) == ["a" * 24]
        assert state.guid_owner(conn, "patreon", "p1") == "a" * 24
        other = meta(guid="b" * 24, guids={"acast": "b" * 24, "patreon": "p1"})
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            state.upsert_episode(conn, other)


def test_rollback_on_exception(tmp_path):
    db = tmp_path / "s.sqlite"
    with pytest.raises(ZeroDivisionError), state.connect(db) as conn:
        state.upsert_episode(conn, meta())
        raise ZeroDivisionError
    with state.connect(db) as conn:
        assert state.list_episodes(conn) == []


def test_feeds_partial_update_and_delete_cascade(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        state.save_feed(conn, "acast", apple_id=1, feed_url="https://f")
        state.save_feed(conn, "acast", etag="e1")
        row = state.get_feed(conn, "acast")
        assert (row["apple_id"], row["feed_url"], row["etag"]) == (1, "https://f", "e1")
        with pytest.raises(ValueError):
            state.save_feed(conn, "acast", bogus=1)
        state.upsert_episode(conn, meta())
        state.delete_episode(conn, "a" * 24)
        assert conn.execute("SELECT COUNT(*) FROM episode_guids").fetchone()[0] == 0


def test_migration_v2_upgrades_a_v1_database_in_place(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(f"BEGIN;\n{state.MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    raw.execute(
        "INSERT INTO episodes (episode_id, source, title, pub_date, created_at, updated_at) "
        "VALUES ('e1', 'acast', 'T', '2026-01-01T00:00:00Z', 'x', 'x')"
    )
    raw.execute("INSERT INTO feeds (name) VALUES ('acast')")
    raw.commit()
    raw.close()
    with state.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(state.MIGRATIONS) >= 2
        row = state.get_episode(conn, "e1")
        for col in (
            "audio_path",
            "audio_bytes",
            "audio_sha256",
            "audio_duration_s",
            "downloaded_at",
            "audio_downloaded_from",
        ):
            assert row[col] is None
        assert row["stage"] == "discovered"
        assert state.get_feed(conn, "acast")["alert_status"] is None


def test_mark_downloaded_survives_upsert_and_list_filters_by_stage(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        state.upsert_episode(conn, meta())
        state.upsert_episode(conn, meta(guid="b" * 24, guids={"acast": "b" * 24}, title="B"))
        state.set_error(conn, "a" * 24, "boom")
        state.mark_downloaded(
            conn,
            "a" * 24,
            audio_path="/x/a.mp3",
            audio_bytes=10,
            audio_duration_s=1.5,
            audio_sha256="ff",
            audio_downloaded_from="acast",
        )
        state.upsert_episode(conn, meta(title="renamed", audio_source="patreon"))
        row = state.get_episode(conn, "a" * 24)
        assert (row["stage"], row["error"], row["audio_path"]) == ("downloaded", None, "/x/a.mp3")
        assert row["audio_downloaded_from"] == "acast" and row["audio_source"] == "patreon"
        done = state.list_episodes(conn, stage="downloaded")
        todo = state.list_episodes(conn, stage="discovered")
        assert [r["episode_id"] for r in done] == ["a" * 24]
        assert [r["episode_id"] for r in todo] == ["b" * 24]
        assert len(state.list_episodes(conn)) == 2
