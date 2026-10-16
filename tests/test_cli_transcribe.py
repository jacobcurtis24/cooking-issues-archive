"""``ciw transcribe`` via CliRunner with a fake ASR backend, tmp config and tmp data dirs.

Nothing here touches the real state, the real audio directory, the GPU or the network.
"""

import fcntl
import os
from datetime import UTC, datetime

import pytest
from asrfake import FakeTranscriber
from conftest import write_config
from typer.testing import CliRunner

from ciw import state, transcribe
from ciw.asr import ASR_BACKENDS, register_backend
from ciw.cli import app
from ciw.config import load_config
from ciw.ingest import meta_path
from ciw.models import EpisodeMeta, Transcript

runner = CliRunner()
A, B, C, D = ("a" * 24, "b" * 24, "c" * 24, "d" * 24)


@pytest.fixture
def fake(tmp_cfg, tmp_path):
    """Fake backend registered as ``fake`` and selected by a tmp config.yaml."""
    write_config(tmp_path, "asr:\n  backend: fake\n  model: fake-model\n")
    cfg = load_config(tmp_path, use_dotenv=False)
    assert str(cfg.audio_dir).startswith(str(tmp_path))  # never touch /mnt/ssd0 in tests
    assert str(cfg.state_db).startswith(str(tmp_path))
    instances: list[FakeTranscriber] = []

    def factory(c, **kw):
        tr = FakeTranscriber(c, **kw)
        tr.fail_on = set(shared["fail_on"])
        tr.version_string = shared["version"]
        instances.append(tr)
        return tr

    shared = {"fail_on": set(), "version": "fake 1.0", "instances": instances}
    register_backend("fake", factory)
    yield cfg, shared
    ASR_BACKENDS.pop("fake", None)


def calls(shared):
    return [p.stem for tr in shared["instances"] for p in tr.calls]


def add_episode(cfg, eid, pub, *, downloaded=True):
    meta = EpisodeMeta(
        guid=eid,
        guids={"acast": eid},
        source="acast",
        audio_source="acast",
        title=f"Episode {eid[:2]}",
        pub_date=datetime(*pub, 8, tzinfo=UTC),
        duration_s=3.0,
        enclosure_url="https://audio.example/x.mp3",
    )
    meta.save(meta_path(cfg, eid))
    with state.connect(cfg.state_db) as conn:
        state.upsert_episode(conn, meta)
        if downloaded:
            cfg.audio_dir.mkdir(parents=True, exist_ok=True)
            path = cfg.audio_dir / f"{eid}.mp3"
            path.write_bytes(b"audio")
            state.mark_downloaded(
                conn, eid, audio_path=str(path), audio_bytes=5, audio_duration_s=1.5,
                audio_sha256="0" * 64, audio_downloaded_from="acast",
            )  # fmt: skip


def row(cfg, eid):
    with state.connect(cfg.state_db) as conn:
        return dict(state.get_episode(conn, eid))


@pytest.fixture
def eps(fake):
    cfg, shared = fake
    add_episode(cfg, A, (2026, 3, 1))
    add_episode(cfg, B, (2026, 2, 1))
    add_episode(cfg, C, (2026, 1, 1))
    add_episode(cfg, D, (2025, 12, 1), downloaded=False)
    return cfg, shared


def invoke(*args):
    return runner.invoke(app, ["transcribe", *args])


def test_no_selector_is_a_usage_error_not_a_bulk_run(eps):
    cfg, shared = eps
    result = invoke()
    assert result.exit_code == 2
    assert "--episode ID" in result.output and "--all-downloaded" in result.output
    assert shared["instances"] == [] and not transcribe.transcript_path(cfg, A).exists()
    assert invoke("--force").exit_code == 2


def test_episode_is_transcribed_saved_and_the_state_row_updated(eps):
    cfg, shared = eps
    result = invoke("--episode", A)
    assert result.exit_code == 0, result.output
    assert f"transcribed  {A}" in result.output
    assert "transcribed 1, skipped 0, stale 0, failed 0" in result.output
    assert "backend: fake fake-model" in result.output
    path = cfg.data_dir / "episodes" / A / "transcript.json"
    t = Transcript.load(path)
    assert t.asr_model == "fake-model" and t.text() == "Hello carbonation."
    r = row(cfg, A)
    assert (r["stage"], r["asr_model"], r["asr_version"]) == (
        "transcribed",
        "fake-model",
        "fake 1.0",
    )
    assert r["asr_runtime_s"] == 0.5 and r["diarization"] == 1 and r["transcribed_at"]
    assert row(cfg, B)["stage"] == "downloaded"  # only the selected episode
    assert not list(path.parent.glob("*.tmp"))


def test_second_run_skips_everything_and_does_no_work(eps):
    cfg, shared = eps
    assert invoke("--episode", A).exit_code == 0
    again = invoke("--episode", A)
    assert again.exit_code == 0
    assert f"skipped      {A}" in again.output and "up to date" in again.output
    assert calls(shared) == [A]  # the backend ran once in total


def test_latest_picks_the_newest_downloaded_and_skips_done_ones(eps):
    cfg, shared = eps
    result = invoke("--latest", "2")
    assert result.exit_code == 0, result.output
    assert calls(shared) == [A, B]  # newest first; D (newest-but-not-downloaded) is ignored
    assert row(cfg, C)["stage"] == "downloaded"
    again = invoke("--latest", "2")
    assert "skipped 2" in again.output and calls(shared) == [A, B]  # no creep onto C


def test_all_downloaded(eps):
    cfg, shared = eps
    result = invoke("--all-downloaded")
    assert result.exit_code == 0 and "transcribed 3, skipped 0" in result.output
    assert sorted(calls(shared)) == [A, B, C] and row(cfg, D)["stage"] == "discovered"


def test_changed_model_or_version_is_stale_and_only_rerun_on_request(eps):
    cfg, shared = eps
    invoke("--episode", A)
    shared["version"] = "fake 2.0"
    result = invoke("--episode", A)
    assert result.exit_code == 0
    assert "STALE" in result.output and "fake 1.0" in result.output and "fake 2.0" in result.output
    assert "stale 1" in result.output and calls(shared) == [A]
    assert row(cfg, A)["asr_version"] == "fake 1.0"
    redo = invoke("--episode", A, "--stale")
    assert "transcribed 1" in redo.output and calls(shared) == [A, A]
    assert row(cfg, A)["asr_version"] == "fake 2.0"
    # a different --model counts the same way
    other = invoke("--episode", A, "--model", "fake-turbo")
    assert "STALE" in other.output and calls(shared) == [A, A]
    assert "transcribed 1" in invoke("--episode", A, "--model", "fake-turbo", "--force").output


def test_force_redoes_a_current_transcript(eps):
    cfg, shared = eps
    invoke("--episode", A)
    result = invoke("--episode", A, "--force")
    assert "transcribed 1" in result.output and calls(shared) == [A, A]


def test_dry_run_changes_nothing(eps):
    cfg, shared = eps
    invoke("--episode", C)
    before = row(cfg, A)
    result = invoke("--latest", "3", "--episode", D, "--dry-run")
    assert result.exit_code == 0, result.output
    assert result.output.count("would transcribe") == 2
    assert "would skip" in result.output and "would blocked" in result.output
    assert "not downloaded" in result.output
    assert calls(shared) == [C]
    assert row(cfg, A) == before and not transcribe.transcript_path(cfg, A).exists()


def test_one_failure_does_not_stop_the_batch_and_is_recorded(eps):
    cfg, shared = eps
    shared["fail_on"] = {B}
    result = invoke("--all-downloaded")
    assert result.exit_code == 1
    assert f"FAILED       {B}" in result.output and "boom on" in result.output
    assert "transcribed 2, skipped 0, stale 0, failed 1" in result.output
    assert row(cfg, A)["stage"] == row(cfg, C)["stage"] == "transcribed"
    r = row(cfg, B)
    assert r["stage"] == "downloaded" and "boom" in r["error"] and r["asr_model"] is None
    assert not transcribe.transcript_path(cfg, B).exists()
    shared["fail_on"] = set()  # fixed: the retry succeeds and clears the error
    assert invoke("--all-downloaded").exit_code == 0
    assert row(cfg, B)["stage"] == "transcribed" and row(cfg, B)["error"] is None


def test_empty_transcript_is_a_failure_not_a_file(eps, monkeypatch):
    cfg, shared = eps
    monkeypatch.setattr(
        FakeTranscriber,
        "transcribe",
        lambda self, p, **kw: Transcript(asr_model="fake-model", asr_version="fake 1.0", words=[]),
    )
    result = invoke("--episode", A)
    assert result.exit_code == 1 and "no words" in result.output
    assert not transcribe.transcript_path(cfg, A).exists() and "no words" in row(cfg, A)["error"]


def test_undownloaded_and_unknown_episodes_are_errors(eps):
    cfg, shared = eps
    blocked = invoke("--episode", D)
    assert (
        blocked.exit_code == 1 and "BLOCKED" in blocked.output and "ciw download" in blocked.output
    )
    unknown = invoke("--episode", "f" * 24)
    assert unknown.exit_code == 1 and "unknown episode id" in unknown.output
    assert shared["instances"] == [] or calls(shared) == []


def test_missing_audio_file_is_blocked(eps):
    cfg, shared = eps
    (cfg.audio_dir / f"{A}.mp3").unlink()
    result = invoke("--episode", A)
    assert result.exit_code == 1 and "audio file is missing" in result.output


def test_no_speakers_prints_the_one_line_hint(eps):
    cfg, shared = eps
    result = invoke("--episode", A, "--no-diarize")
    assert result.exit_code == 0
    assert "HF_TOKEN" in result.output and ".env" in result.output
    assert "huggingface.co/pyannote/speaker-diarization-community-1" in result.output
    assert row(cfg, A)["diarization"] == 0
    # once per run even with several episodes
    result = invoke("--all-downloaded", "--no-diarize", "--force")
    assert result.output.count("speakers skipped") == 1


def test_a_skipped_transcript_without_speakers_says_so(eps):
    cfg, shared = eps
    invoke("--episode", A, "--no-diarize")
    again = invoke("--episode", A, "--no-diarize")
    assert "up to date; no speakers (no_hf_token)" in again.output


def test_state_is_repaired_when_a_matching_transcript_exists_but_the_row_lags(eps):
    cfg, shared = eps
    invoke("--episode", A)
    with state.connect(cfg.state_db) as conn:
        conn.execute("UPDATE episodes SET stage='downloaded' WHERE episode_id=?", (A,))
    result = invoke("--episode", A)
    assert "state repaired" in result.output and calls(shared) == [A]
    assert row(cfg, A)["stage"] == "transcribed"


def test_lock_prevents_two_runs(eps):
    cfg, shared = eps
    lock = cfg.data_dir / "transcribe.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        result = invoke("--episode", A)
        assert result.exit_code == 3 and "already running" in result.output
        assert calls(shared) == [] and row(cfg, A)["stage"] == "downloaded"
    finally:
        os.close(fd)
    assert invoke("--episode", A).exit_code == 0  # lock released


# ---------------------------------------------------------------- --audio / --out mode


def test_audio_mode_writes_the_file_and_never_touches_state(fake, tmp_path):
    cfg, shared = fake
    audio = tmp_path / "excerpt.mp3"
    audio.write_bytes(b"x")
    out = tmp_path / "scratch" / "t.json"
    result = invoke("--audio", str(audio), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert Transcript.load(out).text() == "Hello carbonation."
    assert f"-> {out}" in result.output
    assert not cfg.state_db.exists()  # no state row, not even a database
    assert calls(shared) == ["excerpt"]
    again = invoke("--audio", str(audio), "--out", str(out))
    assert again.exit_code == 0 and "skipped" in again.output and calls(shared) == ["excerpt"]
    assert "transcribed" in invoke("--audio", str(audio), "--out", str(out), "--force").output
    assert calls(shared) == ["excerpt", "excerpt"]


def test_audio_mode_dry_run_and_failures(fake, tmp_path):
    cfg, shared = fake
    audio = tmp_path / "excerpt.mp3"
    audio.write_bytes(b"x")
    out = tmp_path / "t.json"
    dry = invoke("--audio", str(audio), "--out", str(out), "--dry-run")
    assert "would transcribe" in dry.output and not out.exists() and calls(shared) == []
    assert invoke("--audio", str(tmp_path / "missing.mp3"), "--out", str(out)).exit_code == 1
    shared["fail_on"] = {"excerpt"}
    failed = invoke("--audio", str(audio), "--out", str(out))
    assert failed.exit_code == 1 and "FAILED" in failed.output and not out.exists()


def test_audio_and_out_go_together_and_exclude_selectors(fake, tmp_path):
    assert invoke("--audio", str(tmp_path / "a.mp3")).exit_code == 2
    assert invoke("--out", str(tmp_path / "a.json")).exit_code == 2
    both = invoke("--audio", "a.mp3", "--out", "b.json", "--latest", "1")
    assert both.exit_code == 2


def test_unknown_backend_is_a_usage_error(tmp_cfg, tmp_path):
    write_config(tmp_path, "asr:\n  backend: nope\n")
    result = invoke("--latest", "1")
    assert result.exit_code == 2 and "unknown asr.backend" in result.output


# ------------------------------------------------------------------ module-level pieces


def test_transcript_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "e" / "transcript.json"
    first = Transcript(asr_model="m", asr_version="v1", words=[])
    first.save(path)
    second = Transcript(asr_model="m", asr_version="v2", words=[])

    def explode(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(type(path), "replace", explode)
    with pytest.raises(OSError):
        second.save(path)
    monkeypatch.undo()
    assert Transcript.load(path).asr_version == "v1"  # the old file is intact, never half-written


def test_select_episodes_unit(eps):
    cfg, shared = eps
    with state.connect(cfg.state_db) as conn:
        rows, unknown = transcribe.select_episodes(
            conn, latest=1, episode_ids=(D, "zz"), all_downloaded=False
        )
        assert [r["episode_id"] for r in rows] == [A, D] and unknown == ["zz"]
        rows, _ = transcribe.select_episodes(conn, all_downloaded=True)
        assert [r["episode_id"] for r in rows] == [A, B, C]


def test_episodes_list_stage_filter_accepts_every_pipeline_stage(eps):
    cfg, shared = eps
    assert invoke("--episode", A).exit_code == 0

    def listing(stage):
        return runner.invoke(app, ["episodes", "list", "--stage", stage])

    for stage in state.STAGES:  # one source of truth: no stage may be rejected
        assert listing(stage).exit_code == 0, stage
    done = listing("transcribed")
    assert A in done.output and B not in done.output
    assert B in listing("downloaded").output and A not in listing("downloaded").output
    assert D in listing("discovered").output
    bad = listing("bogus")
    assert bad.exit_code == 2 and "must be one of" in " ".join(bad.output.split())
