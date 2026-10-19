"""`ciw compare`: no API call without --yes or with the placeholder prompt; end to end on fakes."""

import json
import subprocess
import sys

import pytest
from comparefake import setup, write_prompt
from llmfake import FakeClient, install
from typer.testing import CliRunner

from ciw import state
from ciw.cli import app

runner = CliRunner()


@pytest.fixture
def ready(tmp_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("CIW_ROOT", str(tmp_path))
    return setup(tmp_path)


def invoke(*args):
    return runner.invoke(app, ["compare", *args])


def test_help_lists_the_commands():
    out = invoke("--help").output
    for name in ("plan", "run", "status", "report", "score"):
        assert name in out


def test_plan_is_offline_and_prints_grid_and_estimate(ready):
    r = invoke("plan")  # no fake installed: any client construction would raise
    assert r.exit_code == 0, r.output
    assert "grid: 3 episodes x 3 configs = 9 calls; 0 already have a result, 9 to do" in r.output
    assert "ESTIMATE total $" in r.output and "input tokens: offline" in r.output
    assert "ledger: $0.00 spent this month of $25.00" in r.output


def test_plan_reports_problems_and_exits_1(tmp_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("CIW_ROOT", str(tmp_path))
    setup(tmp_path, system="TODO")
    r = invoke("plan")
    assert r.exit_code == 1 and "PROBLEM the prompt still contains 'TODO'" in r.output


def test_plan_count_tokens_uses_the_endpoint_only_when_asked(ready, monkeypatch):
    fake = install(monkeypatch, FakeClient())
    assert invoke("plan").exit_code == 0 and fake.count_calls == []
    r = invoke("plan", "--episodes", "1", "--count-tokens")
    assert r.exit_code == 0 and len(fake.count_calls) == 3 and fake.calls == []
    assert "12,345" in r.output and "count_tokens" in r.output


def test_run_needs_a_selector_and_yes_and_a_written_prompt(ready, tmp_path, monkeypatch):
    fake = install(monkeypatch, FakeClient())
    assert invoke("run").exit_code == 2
    assert invoke("run", "--all", "--episodes", "1").exit_code == 2
    r = invoke("run", "--all")
    assert r.exit_code == 2 and "add --yes" in r.output
    assert invoke("run", "--all", "--config", "nope", "--yes").exit_code == 2
    write_prompt(tmp_path / "prompts" / "compare.yaml", user="TODO {{transcript}}")
    r = invoke("run", "--all", "--yes")
    assert r.exit_code == 2 and "not ready" in r.output
    assert fake.calls == []  # none of the above reached the API


def test_run_stages_report_and_score_end_to_end(ready, monkeypatch):
    fake = install(monkeypatch, FakeClient())
    r = invoke("run", "--episodes", "1", "--yes")
    assert r.exit_code == 0, r.output
    assert len(fake.calls) == 3 and "finished: 3 calls" in r.output
    r = invoke("run", "--all", "--yes")  # stage 2 skips what stage 1 did
    assert r.exit_code == 0 and len(fake.calls) == 9
    assert "nothing to do" in invoke("run", "--all", "--yes").output and len(fake.calls) == 9

    st = invoke("status")
    assert st.exit_code == 0 and "run spend $" in st.output
    assert [line.split()[1] for line in st.output.splitlines() if line.startswith("haiku")] == ["3"]

    rp = invoke("report")
    assert rp.exit_code == 0 and "3 episodes, 0 pending" in rp.output
    from ciw.config import load_config

    cfg = load_config()
    run_dir = next(cfg.llm.compare_dir.iterdir())
    key = json.loads((run_dir / "key.json").read_text())
    ratings = {
        "run_id": run_dir.name,
        "ratings": {
            ep: {lab: (5 if cid == "opus-medium" else 3) for lab, cid in m.items()}
            for ep, m in key.items()
        },
        "best": {
            ep: next(lab for lab, cid in m.items() if cid == "opus-medium") for ep, m in key.items()
        },
    }
    f = run_dir / "ratings.json"
    f.write_text(json.dumps(ratings))
    sc = invoke("score", str(f))
    assert sc.exit_code == 0, sc.output
    assert "| opus-medium |" in sc.output and (run_dir / "summary.json").is_file()
    assert (
        json.loads((run_dir / "summary.json").read_text())["configs"][0]["config"] == "opus-medium"
    )
    assert invoke("score", str(run_dir / "missing.json")).exit_code == 2


def test_run_budget_stop_exits_4(ready, monkeypatch):
    install(monkeypatch, FakeClient())
    r = invoke("run", "--all", "--yes", "--max-usd", "0.01")
    assert r.exit_code == 4 and "stopped early: run budget" in r.output


def test_run_failures_exit_1(ready, monkeypatch):
    install(
        monkeypatch,
        FakeClient(lambda req: __import__("llmfake").message("nope", model=req["model"])),
    )
    r = invoke("run", "--episodes", "1", "--yes")
    assert r.exit_code == 1 and "invalid" in r.output


def test_importing_the_cli_does_not_load_the_sdk_or_torch():
    code = (
        "import sys, ciw.cli; "
        "bad = [m for m in ('anthropic', 'torch', 'whisperx', 'jsonschema') if m in sys.modules]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_state_is_untouched_by_plan(ready):
    invoke("plan")
    with state.connect(ready.state_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0
