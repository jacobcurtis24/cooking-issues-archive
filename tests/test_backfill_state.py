"""T8 groundwork: config section, state migration 5, silent-gap statistics, failure bookkeeping."""

import sqlite3
from datetime import UTC, datetime

import pytest
from asrfake import FakeTranscriber
from conftest import write_config

from ciw import state, transcribe
from ciw.config import BackfillConfig, load_config
from ciw.ingest import meta_path
from ciw.models import EpisodeMeta, Transcript, Word
from ciw.transcript import gap_stats

EID = "a" * 24


def words(*spans, speaker=None):
    return [Word(w=f"w{i}", start=a, end=b, speaker=speaker) for i, (a, b) in enumerate(spans)]


def tr(**kw):
    return Transcript(asr_model="m", asr_version="v", **kw)


def test_backfill_defaults(tmp_cfg):
    b = tmp_cfg.backfill
    assert b.order == ["acast", "patreon", "hrn"] and b.prefetch == 2 and b.max_attempts == 3
    assert b.retry_backoff_minutes == [10, 60, 360] and b.suspect_gap_pct == 5
    assert b.log_dir == tmp_cfg.root / "data" / "logs"
    assert b.stop_file == tmp_cfg.root / "data" / "STOP"
    assert tmp_cfg.download.duration_tolerance_pct == 0.10


def test_real_config_yaml_has_the_documented_backfill_values(allow_real_paths):
    cfg = load_config(use_dotenv=False)
    assert cfg.download.duration_tolerance_pct == 0.10
    assert cfg.backfill == BackfillConfig(
        log_dir=cfg.backfill.log_dir, stop_file=cfg.backfill.stop_file
    )


def test_backfill_config_rejects_unknown_keys(tmp_path):
    write_config(tmp_path, "backfill:\n  prefech: 3\n")
    with pytest.raises(ValueError):
        load_config(tmp_path, use_dotenv=False)


def test_migration_5_adds_columns_to_an_old_db(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    for number in range(4):
        conn.executescript(
            f"BEGIN;\n{state.MIGRATIONS[number]}\nPRAGMA user_version = {number + 1};\nCOMMIT;"
        )
    conn.execute(
        "INSERT INTO episodes (episode_id, source, title, pub_date, created_at, updated_at) "
        "VALUES ('x', 'acast', 't', '2020-01-01T00:00:00Z', 'now', 'now')"
    )
    conn.commit()
    conn.close()
    with state.connect(path) as c:
        row = state.get_episode(c, "x")
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(state.MIGRATIONS) >= 5
        assert row["attempts"] == 0 and row["suspect"] == 0
        for col in ("last_error_stage", "next_retry_at", "asr_gap_s", "asr_gap_count"):
            assert row[col] is None


def test_gap_stats_counts_gaps_of_ten_seconds_and_flags_suspects():
    # 100 s of audio; two gaps (12 s and 11 s) = 23 s = 23%
    ws = words((0, 10), (22, 40), (51, 60))
    stats = gap_stats(tr(words=ws), 100.0, 5.0)
    assert (stats.gap_s, stats.count, stats.suspect) == (23.0, 2, True)
    # the same gaps in a 1000 s episode are 2.3%: not suspect
    assert gap_stats(tr(words=ws), 1000.0, 5.0).suspect is False
    # a 9.9 s pause is speech, not a gap; a gap before the first word is not counted either
    ok = gap_stats(tr(words=words((30, 31), (40.9, 41))), 60.0, 5.0)
    assert (ok.gap_s, ok.count, ok.suspect) == (0.0, 0, False)
    assert gap_stats(tr(words=[]), None, 5.0).suspect is False


def test_gap_stats_falls_back_to_the_transcript_duration():
    t = tr(words=words((0, 1), (21, 22)), audio_duration_s=100.0)
    assert gap_stats(t, None, 5.0).suspect is True  # 20% of 100 s


def add(cfg, eid=EID):
    meta = EpisodeMeta(
        guid=eid,
        guids={"acast": eid},
        source="acast",
        audio_source="acast",
        title="T",
        pub_date=datetime(2026, 1, 1, 8, tzinfo=UTC),
        duration_s=3.0,
        enclosure_url="https://audio.example/x.mp3",
    )
    meta.save(meta_path(cfg, eid))
    cfg.audio_dir.mkdir(parents=True, exist_ok=True)
    audio = cfg.audio_dir / f"{eid}.mp3"
    audio.write_bytes(b"x")
    with state.connect(cfg.state_db) as conn:
        state.upsert_episode(conn, meta)
        state.mark_downloaded(
            conn, eid, audio_path=str(audio), audio_bytes=1, audio_duration_s=100.0,
            audio_sha256="0" * 64, audio_downloaded_from="acast",
        )  # fmt: skip


@pytest.mark.parametrize(
    "spans,suspect",
    [(((0, 10), (22, 40), (51, 60)), 1), (((0, 10), (11, 40), (41, 60)), 0)],
)
def test_transcribe_episode_fills_the_gap_columns(tmp_cfg, spans, suspect):
    add(tmp_cfg)
    tr = FakeTranscriber(tmp_cfg)
    tr.words = words(*spans)
    with state.connect(tmp_cfg.state_db) as conn:
        res = transcribe.transcribe_episode(tmp_cfg, conn, state.get_episode(conn, EID), tr)
        assert res.outcome == "transcribed"
        row = state.get_episode(conn, EID)
    assert row["suspect"] == suspect
    assert row["asr_gap_count"] == (2 if suspect else 0)
    assert row["asr_gap_s"] == (23.0 if suspect else 0.0)


def test_failure_bookkeeping_and_its_reset(tmp_cfg):
    add(tmp_cfg)
    with state.connect(tmp_cfg.state_db) as conn:
        assert (
            state.record_failure(conn, EID, stage="download", error="e1", next_retry_at="Z1") == 1
        )
        assert (
            state.record_failure(conn, EID, stage="download", error="e2", next_retry_at="Z2") == 2
        )
        row = state.get_episode(conn, EID)
        assert (row["attempts"], row["last_error_stage"], row["next_retry_at"]) == (
            2,
            "download",
            "Z2",
        )
        assert row["error"] == "e2"
        # a successful download clears a *download* failure ...
        state.mark_downloaded(
            conn, EID, audio_path="/x", audio_bytes=1, audio_duration_s=1.0,
            audio_sha256=None, audio_downloaded_from="acast",
        )  # fmt: skip
        row = state.get_episode(conn, EID)
        assert (row["attempts"], row["last_error_stage"], row["next_retry_at"]) == (0, None, None)
        # ... but not a transcribe failure
        state.record_failure(conn, EID, stage="transcribe", error="boom", next_retry_at="Z3")
        state.mark_downloaded(
            conn, EID, audio_path="/x", audio_bytes=1, audio_duration_s=1.0,
            audio_sha256=None, audio_downloaded_from="acast",
        )  # fmt: skip
        assert state.get_episode(conn, EID)["attempts"] == 1
        state.clear_failure(conn, EID)
        assert state.get_episode(conn, EID)["attempts"] == 0
