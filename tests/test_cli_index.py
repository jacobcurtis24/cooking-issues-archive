"""``ciw index`` and the automatic re-index after ``ciw transcribe`` / ``ciw vocab correct``."""

import pytest
from test_cli_transcribe import A, B, eps, fake, invoke  # noqa: F401 - pytest fixtures
from test_cli_vocab import env, put_transcript, vc  # noqa: F401 - pytest fixtures
from typer.testing import CliRunner
from webfake import add_episode

from ciw import search_index as si
from ciw.cli import app

runner = CliRunner()


def idx(*args):
    return runner.invoke(app, ["index", *args])


def hits(cfg, fts):
    with si.open_readonly(cfg.search_db) as c:
        return sorted({h.episode_id for h in si.search_utterances(c, fts)})


def test_index_builds_updates_and_reports(tmp_cfg):
    add_episode(tmp_cfg, "ep-a", "Fizz", transcript="all about carbonation here", notes="bubbles")
    add_episode(tmp_cfg, "ep-b", "Notes only", notes="nothing")
    r = idx()
    assert r.exit_code == 0, r.output
    assert "transcripts: 1 indexed (1 utterances), 0 unchanged" in r.output
    assert "show notes: 2 indexed" in r.output and "index size:" in r.output
    again = idx("--all")
    assert "transcripts: 0 indexed" in again.output and "1 unchanged" in again.output
    assert hits(tmp_cfg, '"carbonation"*') == ["ep-a"]


def test_index_episode_and_rebuild_options(tmp_cfg):
    add_episode(tmp_cfg, "ep-a", "Fizz", transcript="carbonation")
    add_episode(tmp_cfg, "ep-b", "Gel", transcript="gellan")
    r = idx("--episode", "ep-a")
    assert r.exit_code == 0 and "1 indexed" in r.output
    assert hits(tmp_cfg, '"gellan"*') == []
    r = idx("--rebuild")
    assert r.exit_code == 0 and "rebuilt" in r.output and "2 indexed" in r.output
    assert hits(tmp_cfg, '"gellan"*') == ["ep-b"]
    assert idx("--rebuild", "--episode", "ep-a").exit_code != 0
    assert idx("--all", "--episode", "ep-a").exit_code != 0


def test_index_without_state_is_a_clear_error(tmp_cfg):
    r = idx()
    assert r.exit_code == 1 and "ciw poll" in r.output
    assert not tmp_cfg.search_db.exists()


def test_transcribe_indexes_the_new_transcript(eps):  # noqa: F811
    cfg, shared = eps
    shared["words"] = None
    r = invoke("--episode", A)
    assert r.exit_code == 0, r.output
    assert "search index: transcripts: 1 indexed" in r.output
    assert hits(cfg, '"carbonation"*') == [A]
    r = invoke("--episode", A)  # skipped: nothing changed, nothing printed
    assert "search index" not in r.output


def test_a_failing_index_does_not_fail_the_transcription(eps, monkeypatch):  # noqa: F811
    cfg, _ = eps
    monkeypatch.setattr(si, "update_index", lambda *a, **k: 1 / 0)
    r = invoke("--episode", A)
    assert r.exit_code == 0, r.output
    assert "transcribed 1" in r.output and "search index not updated" in r.output
    assert (cfg.episodes_dir / A / "transcript.json").is_file()


def test_vocab_correct_refreshes_the_index_but_dry_run_does_not(env):  # noqa: F811
    cfg, _ = env
    put_transcript(cfg, A)
    put_transcript(cfg, B)
    assert vc("correct", A, "--dry-run").exit_code == 0
    assert not cfg.search_db.exists()
    r = vc("correct", A)
    assert r.exit_code == 0 and "search index:" in r.output
    assert hits(cfg, '"polydextrose"*') == [A]  # corrected words are what is indexed
    assert B not in hits(cfg, '"polydextrose"*')  # only A was touched


@pytest.mark.parametrize("args", [["--episode", "nope"]])
def test_unknown_episode_indexes_nothing(tmp_cfg, args):
    add_episode(tmp_cfg, "ep-a", "Fizz", transcript="carbonation")
    r = idx(*args)
    assert r.exit_code == 0 and "0 indexed" in r.output
