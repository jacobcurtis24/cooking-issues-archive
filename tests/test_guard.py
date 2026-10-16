"""The test-safety guard: under pytest nothing may resolve to the real ``data/`` or /mnt/ssd0/ciw.

T4 and T5 each shipped a stub test that ran a real ``ciw download``/``ciw transcribe`` against the
real config. These tests prove that can no longer happen silently.
"""

import subprocess
import sys

import pytest
from conftest import DB_FILE, REPO_ROOT, write_config
from typer.testing import CliRunner

from ciw import download, state, transcribe
from ciw.cli import app
from ciw.config import (
    ALLOW_REAL_PATHS_ENV,
    Config,
    UnsafeTestPathError,
    guard_path,
    load_config,
)

runner = CliRunner()


def test_loading_the_real_repo_config_fails_loudly(monkeypatch):
    monkeypatch.delenv("CIW_ROOT", raising=False)
    with pytest.raises(UnsafeTestPathError, match="real runtime data"):
        load_config(use_dotenv=False)


def test_default_audio_and_clips_dirs_are_refused_even_in_a_tmp_root(tmp_path):
    with pytest.raises(UnsafeTestPathError, match="/mnt/ssd0/ciw"):
        load_config(tmp_path, use_dotenv=False)


def test_a_tmp_config_with_tmp_dirs_is_fine(tmp_path):
    write_config(tmp_path)
    cfg = load_config(tmp_path, use_dotenv=False)
    assert cfg.data_dir == tmp_path.resolve() / "data"


def test_the_blog_database_is_allowed_read_only(tmp_path):
    write_config(tmp_path, f"blog_db: {DB_FILE}\n")
    assert load_config(tmp_path, use_dotenv=False).blog_db == DB_FILE


@pytest.mark.parametrize("name", ["data_dir", "state_db", "audio_dir", "clips_dir"])
def test_each_runtime_path_is_guarded(tmp_path, name):
    write_config(tmp_path, f"{name}: {REPO_ROOT / 'data' / 'x'}\n")
    with pytest.raises(UnsafeTestPathError, match=name):
        load_config(tmp_path, use_dotenv=False)


def test_a_symlink_into_the_real_data_is_caught(tmp_path):
    link = tmp_path / "sneaky"
    link.symlink_to(REPO_ROOT / "data")
    with pytest.raises(UnsafeTestPathError):
        guard_path(link / "state.sqlite", "state database")


def test_guard_path_allows_tmp_and_other_repo_files(tmp_path):
    guard_path(tmp_path / "data" / "state.sqlite")
    guard_path(REPO_ROOT / "vocab" / "vocabulary.txt")  # tracked files in the repo are fine


def test_state_connect_refuses_the_real_state_db():
    with pytest.raises(UnsafeTestPathError):
        with state.connect(REPO_ROOT / "data" / "state.sqlite"):
            pass  # pragma: no cover


def test_locks_and_audio_dir_refuse_real_paths():
    cfg = Config(root=REPO_ROOT)  # defaults: real data dir and /mnt/ssd0/ciw
    with pytest.raises(UnsafeTestPathError):
        with transcribe.transcribe_lock(cfg):
            pass  # pragma: no cover
    with pytest.raises(UnsafeTestPathError):
        with download.download_lock(cfg):
            pass  # pragma: no cover


def test_a_real_command_against_the_real_config_cannot_run(monkeypatch):
    """The exact accident of T4/T5: `ciw transcribe --latest 3` with no tmp config."""
    monkeypatch.delenv("CIW_ROOT", raising=False)
    for args in (["transcribe", "--latest", "3"], ["download", "--latest", "3"], ["poll"]):
        result = runner.invoke(app, args)
        assert isinstance(result.exception, UnsafeTestPathError), args
        assert result.exit_code != 0


def test_the_guard_also_covers_subprocesses_started_by_a_test(monkeypatch):
    monkeypatch.delenv("CIW_ROOT", raising=False)
    code = "from ciw.config import load_config; load_config(use_dotenv=False)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode != 0 and "UnsafeTestPathError" in out.stderr


def test_explicit_opt_in_allows_the_real_paths(allow_real_paths, monkeypatch):
    monkeypatch.delenv("CIW_ROOT", raising=False)
    assert load_config(use_dotenv=False).data_dir == REPO_ROOT / "data"


@pytest.mark.real_paths
def test_marker_opt_in_is_the_same_thing(monkeypatch):
    import os

    assert os.environ.get(ALLOW_REAL_PATHS_ENV) == "1"


def test_guard_is_inert_outside_pytest(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    guard_path(REPO_ROOT / "data" / "state.sqlite")  # no exception: production behaviour
