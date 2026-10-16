"""ASR registry, config section and state migration: no GPU, no torch."""

import sqlite3
import subprocess
import sys

import pytest
from asrfake import FakeTranscriber

from ciw import state
from ciw.asr import ASR_BACKENDS, available_backends, get_transcriber, register_backend
from ciw.asr.base import Transcriber, diarization_hint
from ciw.config import load_config
from ciw.models import Transcript, Word


@pytest.fixture
def fake_backend():
    register_backend("fake", FakeTranscriber)
    yield
    ASR_BACKENDS.pop("fake", None)


def with_backend(cfg, name):
    return cfg.model_copy(update={"asr": cfg.asr.model_copy(update={"backend": name})})


def test_registry_selects_the_configured_backend(tmp_cfg, fake_backend):
    tr = get_transcriber(with_backend(tmp_cfg, "fake"), model="tiny", diarize=False)
    assert isinstance(tr, FakeTranscriber) and isinstance(tr, Transcriber)
    assert (tr.name, tr.model, tr.diarize) == ("fake", "tiny", False)
    assert {"fake", "whisperx"} <= set(available_backends())


def test_unknown_backend_is_a_clear_error(tmp_cfg):
    with pytest.raises(ValueError, match="unknown asr.backend 'parakeet'.*whisperx"):
        get_transcriber(with_backend(tmp_cfg, "parakeet"))


def test_builtin_whisperx_builds_without_importing_torch():
    """Constructing the backend (as the CLI does to check staleness) must stay torch-free."""
    code = (
        "import sys; from ciw.config import load_config; from ciw.asr import get_transcriber;"
        "t = get_transcriber(load_config(use_dotenv=False));"
        "print(t.name, t.model, t.version);"
        "assert 'torch' not in sys.modules and 'whisperx' not in sys.modules"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.startswith("whisperx large-v3 ")


def test_matches_compares_backend_model_and_version(tmp_cfg):
    tr = FakeTranscriber(tmp_cfg, model="m1")
    mk = lambda **kw: Transcript(  # noqa: E731
        **{"asr_model": "m1", "asr_version": "fake 1.0", "asr_backend": "fake", **kw}
    )
    assert tr.matches(mk())
    assert not tr.matches(mk(asr_model="m2"))
    assert not tr.matches(mk(asr_version="fake 2.0"))
    assert not tr.matches(mk(asr_backend="whisperx"))


def test_asr_config_defaults_and_path_resolution(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "asr:\n  hf_home: caches/hf\n  vocabulary_file: vocab.txt\n  batch_size: 4\n"
    )
    cfg = load_config(tmp_path, use_dotenv=False)
    assert cfg.asr.hf_home == tmp_path / "caches" / "hf"
    assert cfg.asr.vocabulary_file == tmp_path / "vocab.txt"
    assert (cfg.asr.compute_type, cfg.asr.fallback_compute_type) == ("float16", "int8_float16")
    assert (cfg.asr.batch_size, cfg.asr.min_batch_size, cfg.asr.language) == (4, 2, "en")
    assert cfg.asr.diarize and cfg.asr.diarization_model.endswith("community-1")


def test_repo_config_keeps_model_caches_off_the_home_disk(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    cfg = load_config(use_dotenv=False)
    assert str(cfg.asr.hf_home) == "/mnt/ssd0/ciw/hf_cache"
    assert cfg.asr.vocabulary_file is None


def test_state_migration_3_adds_transcription_columns_to_an_old_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(db)
    for number in range(2):  # a DB that was left at schema v2 by T4
        conn.executescript(
            f"BEGIN;\n{state.MIGRATIONS[number]}\nPRAGMA user_version = {number + 1};\nCOMMIT;"
        )
    conn.execute(
        "INSERT INTO episodes (episode_id, source, title, pub_date, stage, created_at, updated_at)"
        " VALUES ('e1', 'acast', 'T', '2026-01-01T00:00:00Z', 'downloaded', 'x', 'x')"
    )
    conn.commit()
    conn.close()
    with state.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(state.MIGRATIONS) >= 3
        row = state.get_episode(conn, "e1")
        assert row["stage"] == "downloaded" and row["asr_model"] is None
        state.mark_transcribed(
            conn, "e1", asr_model="large-v3", asr_version="v", asr_runtime_s=12.5, diarization=False
        )
        row = state.get_episode(conn, "e1")
        assert (row["stage"], row["asr_model"], row["asr_runtime_s"]) == (
            "transcribed",
            "large-v3",
            12.5,
        )
        assert row["diarization"] == 0 and row["transcribed_at"] and row["error"] is None


def test_mark_transcribed_never_moves_an_episode_backwards(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        conn.execute(
            "INSERT INTO episodes (episode_id, source, title, pub_date, stage, created_at,"
            " updated_at) VALUES ('e1', 'acast', 'T', '2026-01-01T00:00:00Z', 'scored', 'x', 'x')"
        )
        state.mark_transcribed(
            conn, "e1", asr_model="m", asr_version="v", asr_runtime_s=1, diarization=True
        )
        assert state.get_episode(conn, "e1")["stage"] == "scored"


def test_diarization_hint_names_the_exact_steps_without_secrets():
    hint = diarization_hint("no_hf_token")
    assert "HF_TOKEN" in hint and ".env" in hint
    assert "huggingface.co/settings/tokens" in hint
    assert "huggingface.co/pyannote/speaker-diarization-community-1" in hint
    assert "\n" not in hint
    assert "refused" in diarization_hint("hf_access_denied")


def test_word_model_unchanged():
    assert Word(w="x", start=0, end=0).speaker is None
