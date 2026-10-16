"""The ``ciw`` typer CLI: help lists everything, stubs exit 2, blog stats reads the real DB."""

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from ciw.cli import app

runner = CliRunner()

TOP_LEVEL = [
    "web", "blog", "feeds", "poll", "download", "transcribe", "vocab",
    "score", "eval", "notify", "run",
]  # fmt: skip

STUBS = [
    (["vocab"], "T6"),
    (["score"], "T9"),
    (["eval"], "T11"),
    (["notify"], "T10"),
    (["run"], "T12"),
]


def test_help_lists_every_subcommand():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in TOP_LEVEL:
        assert name in result.output, name


def test_blog_and_feeds_help_list_their_commands():
    out = runner.invoke(app, ["blog", "--help"]).output
    for name in ["scrape", "search", "stats", "fix-encoding"]:
        assert name in out
    assert "diff" in runner.invoke(app, ["feeds", "--help"]).output


@pytest.mark.parametrize(
    "args, task", STUBS, ids=lambda v: " ".join(v) if isinstance(v, list) else v
)
def test_stubs_exit_2(args, task):
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert f"not implemented yet (task {task})" in result.output


def test_stub_ignores_extra_arguments():
    # Never use a real command here: this runs against the real repo config (T4's `download
    # --latest 3` really downloaded, T5's `transcribe --latest 3` really transcribed).
    result = runner.invoke(app, ["vocab", "--latest", "3"])
    assert result.exit_code == 2
    assert "not implemented yet" in result.output


def test_blog_stats_against_real_db():
    result = runner.invoke(app, ["blog", "stats"])
    assert result.exit_code == 0, result.output
    assert "Total posts scraped : 224 (8 primers)" in result.output
    assert "Index pages done    : 44" in result.output


def test_blog_search_against_real_db():
    result = runner.invoke(app, ["blog", "search", "carbonation", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "2 result(s) for 'carbonation'" in result.output


def test_fix_encoding_dry_run_changes_nothing():
    result = runner.invoke(app, ["blog", "fix-encoding", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "[DRY RUN]" in result.output
    assert "Posts with changes : 0" in result.output


def test_web_defaults_debug_off(monkeypatch):
    calls = {}

    def fake_run(self, host=None, port=None, debug=None, **kw):
        calls.update(host=host, port=port, debug=debug)

    monkeypatch.setattr("flask.Flask.run", fake_run)
    assert runner.invoke(app, ["web"]).exit_code == 0
    assert calls == {"host": "127.0.0.1", "port": 5000, "debug": False}
    assert (
        runner.invoke(app, ["web", "--port", "5057", "--host", "0.0.0.0", "--debug"]).exit_code == 0
    )
    assert calls == {"host": "0.0.0.0", "port": 5057, "debug": True}


def test_python_dash_m_ciw():
    out = subprocess.run(
        [sys.executable, "-m", "ciw", "--help"], capture_output=True, text=True, check=True
    ).stdout
    assert "blog" in out and "transcribe" in out
