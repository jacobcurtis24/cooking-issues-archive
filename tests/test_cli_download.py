"""``ciw download`` and ``ciw episodes list --stage`` via CliRunner (mocked audio host)."""

import hashlib

import pytest
from audiofake import AudioWeb, ffmpeg_exe, make_mp3
from test_download import add_episode
from typer.testing import CliRunner

from ciw import download, state
from ciw.cli import app
from ciw.config import load_config

pytestmark = pytest.mark.skipif(ffmpeg_exe() is None, reason="ffmpeg is not installed")
runner = CliRunner()
A, B, C = "a" * 24, "b" * 24, "c" * 24


@pytest.fixture(scope="module")
def mp3(tmp_path_factory):
    return make_mp3(tmp_path_factory.mktemp("clip") / "tone.mp3")


@pytest.fixture
def cfg(tmp_cfg, tmp_path):
    (tmp_path / "config.yaml").write_text(
        f"audio_dir: {tmp_path / 'audio'}\ndownload:\n  min_free_gb: 0\n"
    )
    cfg = load_config(tmp_path, use_dotenv=False)
    assert str(cfg.audio_dir).startswith(str(tmp_path))  # never touch /mnt/ssd0 in tests
    return cfg


@pytest.fixture
def web(cfg, mp3, monkeypatch):
    fake = AudioWeb(mp3)
    monkeypatch.setattr(download, "make_client", lambda c: fake.client(c.secrets.secret_values()))
    add_episode(cfg, A, pub=(2026, 3, 1))
    add_episode(cfg, B, pub=(2026, 2, 1))
    add_episode(cfg, C, pub=(2026, 1, 1))
    add_episode(cfg, "hrn-1", pub=(2015, 1, 1), source="hrn", audio_source="hrn")
    return fake


def invoke(*args):
    return runner.invoke(app, ["download", *args])


def test_no_selector_is_a_usage_error_not_a_bulk_download(cfg, web):
    result = invoke()
    assert result.exit_code == 2
    assert "--latest N" in result.output and "--all" in result.output
    assert web.requests == []
    assert invoke("--source", "hrn", "--limit", "5").exit_code == 2


def test_latest_downloads_then_second_run_does_nothing(cfg, web, mp3):
    result = invoke("--latest", "2")
    assert result.exit_code == 0, result.output
    lines = [
        ln for ln in result.output.splitlines() if ln.startswith("downloaded ") and "," not in ln
    ]
    assert [ln.split()[1] for ln in lines] == [A, B]
    assert "downloaded 2, adopted 0, skipped 0, failed 0" in result.output
    assert "\r" not in result.output  # quiet progress when not a TTY
    for eid in (A, B):
        assert (cfg.audio_dir / f"{eid}.mp3").read_bytes() == mp3
    assert not (cfg.audio_dir / f"{C}.mp3").exists()
    web.requests.clear()
    again = invoke("--latest", "2")
    assert again.exit_code == 0 and "nothing to do" in again.output
    assert web.requests == []


def test_dry_run_changes_nothing_and_reports_size_and_disk(cfg, web, mp3):
    result = invoke("--latest", "2", "--dry-run")
    assert result.exit_code == 0, result.output
    assert result.output.count("would download") == 2
    assert A in result.output and B in result.output and C not in result.output
    assert "free on" in result.output and "min_free_gb 20" not in result.output
    assert "KB" in result.output  # server size from the Range probe
    assert not cfg.audio_dir.exists()
    assert all(r.headers.get("range") == "bytes=0-0" for r in web.requests if r.method == "GET")
    with state.connect(cfg.state_db) as conn:
        assert {r["stage"] for r in state.list_episodes(conn)} == {"discovered"}


def test_dry_run_shows_adopt_and_skip(cfg, web, mp3):
    invoke("--episode", A)
    cfg.audio_dir.mkdir(exist_ok=True)
    (cfg.audio_dir / f"{B}.mp3").write_bytes(mp3)
    result = invoke("--episode", A, "--episode", B, "--dry-run")
    assert "would skip" in result.output and "would adopt" in result.output


def test_source_since_and_all_selectors(cfg, web):
    result = invoke("--all", "--source", "hrn")
    assert result.exit_code == 0 and "downloaded 1" in result.output
    assert (cfg.audio_dir / "hrn-1.mp3").exists() and not (cfg.audio_dir / f"{A}.mp3").exists()
    result = invoke("--since", "2026-02-15")
    assert "downloaded 1" in result.output and (cfg.audio_dir / f"{A}.mp3").exists()
    assert not (cfg.audio_dir / f"{B}.mp3").exists()
    result = invoke("--all", "--limit", "1")
    assert "downloaded 1" in result.output and (cfg.audio_dir / f"{B}.mp3").exists()
    assert not (cfg.audio_dir / f"{C}.mp3").exists()


def test_bad_options(cfg, web):
    assert invoke("--latest", "1", "--source", "bogus").exit_code != 0
    assert invoke("--since", "yesterday").exit_code != 0
    assert web.requests == []


def test_failures_set_exit_code_but_do_not_stop_the_batch(cfg, web):
    web.statuses = [404]  # the first episode's body request fails
    result = invoke("--latest", "2")
    assert result.exit_code == 1
    assert "FAILED" in result.output and "failed 1" in result.output
    assert "downloaded 1" in result.output
    assert (cfg.audio_dir / f"{B}.mp3").exists()
    # the failed one is retried next time
    again = invoke("--latest", "2")
    assert again.exit_code == 0 and "downloaded 1" in again.output


def test_unknown_episode_id_is_a_failure(cfg, web):
    result = invoke("--episode", "nope")
    assert result.exit_code == 1 and "unknown episode id" in result.output


def test_lock_contention_exits_3(cfg, web):
    with download.download_lock(cfg):
        result = invoke("--latest", "1")
    assert result.exit_code == 3 and "already running" in result.output
    assert web.requests == []


def test_redownload_and_verify_flags(cfg, web, mp3):
    invoke("--latest", "1")
    web.requests.clear()
    verified = invoke("--latest", "1", "--verify")
    assert verified.exit_code == 0 and "verified ok" in verified.output
    assert web.body_requests() == []
    web.body = mp3  # same bytes again
    again = invoke("--episode", A, "--redownload")
    assert again.exit_code == 0 and "downloaded 1" in again.output
    assert hashlib.sha256((cfg.audio_dir / f"{A}.mp3").read_bytes()).hexdigest()


def test_episodes_list_stage_filter(cfg, web):
    invoke("--latest", "1")
    done = runner.invoke(app, ["episodes", "list", "--stage", "downloaded"])
    assert done.exit_code == 0 and A in done.output and B not in done.output
    todo = runner.invoke(app, ["episodes", "list", "--stage", "discovered"])
    assert A not in todo.output and B in todo.output and C in todo.output
    assert runner.invoke(app, ["episodes", "list", "--stage", "bogus"]).exit_code != 0
