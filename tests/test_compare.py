"""ciw.compare: prompt file, templates, planning, the runner (caps, resume, failures), checks."""

import json
import threading

import anthropic
import httpx2
import pytest
from comparefake import MATRIX, SCHEMA, setup, write_prompt
from llmfake import QUOTE, TRANSCRIPT_TEXT, FakeClient, default_json, message

from ciw import compare as cmp
from ciw import llm, state


@pytest.fixture
def env(tmp_cfg, tmp_path):
    cfg = setup(tmp_path)
    return cfg, cmp.load_prompt(cfg.llm.prompt_file)


def run(cfg, p, client, *, jobs=None, max_usd=100.0, **kw):
    jobs = jobs if jobs is not None else cmp.select_jobs(p)
    return cmp.run_jobs(cfg, p, jobs, client=client, max_usd=max_usd, **kw)


def ledger(cfg):
    with state.connect(cfg.state_db) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_usage ORDER BY id")]


# ------------------------------------------------------------------------ prompt file


def test_load_and_lint_a_ready_prompt(env):
    _, p = env
    assert p.lint() == []
    assert [c.id for c in p.matrix] == [c["id"] for c in MATRIX]
    assert p.run_id.startswith("run-") and len(p.prompt_hash()) == 64


def test_the_shipped_prompt_file_loads_but_is_not_ready():
    from conftest import REPO_ROOT

    p = cmp.load_prompt(REPO_ROOT / "prompts" / "compare.yaml")
    assert any("TODO" in line for line in p.lint())
    assert len(p.matrix) == 7 and len(p.sample) == 10 and p.reference == "opus-medium"
    assert {(c.model, c.effort) for c in p.matrix} == {
        (m, e)
        for m in ("claude-haiku-5-5", "claude-sonnet-5-5")
        for e in ("medium", "high", "xhigh")
    } | {("claude-opus-5-5", "medium")}


def test_placeholder_and_unknown_placeholder_are_problems(tmp_path):
    f = write_prompt(tmp_path / "p.yaml", system="TODO write me")
    assert any("TODO" in x for x in cmp.load_prompt(f).lint())
    f = write_prompt(tmp_path / "q.yaml", user="{{transcript}} {{speaker_names}}")
    assert any("speaker_names" in x for x in cmp.load_prompt(f).lint())
    f = write_prompt(tmp_path / "r.yaml", user="no transcript here")
    assert any("never uses" in x for x in cmp.load_prompt(f).lint())


def test_schema_lint_requires_closed_objects_and_required_properties(tmp_path):
    open_schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    p = cmp.load_prompt(write_prompt(tmp_path / "a.yaml", schema=open_schema))
    assert any("additionalProperties" in x for x in p.lint())
    partial = {**open_schema, "additionalProperties": False, "required": []}
    p = cmp.load_prompt(write_prompt(tmp_path / "b.yaml", schema=partial))
    assert any("not listed in required" in x for x in p.lint())
    p = cmp.load_prompt(write_prompt(tmp_path / "c.yaml", schema={"type": "wrong"}))
    assert any("not a valid JSON Schema" in x for x in p.lint())
    p = cmp.load_prompt(write_prompt(tmp_path / "d.yaml", schema=None))
    assert any("schema: missing" in x for x in p.lint())


@pytest.mark.parametrize(
    "over",
    [
        {"reference": "nope"},
        {"sample": []},
        {"sample": ["a", "a"]},
        {"matrix": MATRIX + [MATRIX[0]]},
        {"matrix": [{"id": "x", "model": "gpt-9", "effort": "high"}], "reference": "x"},
        {"matrix": [{"id": "x", "model": "claude-haiku-5-5", "effort": "huge"}], "reference": "x"},
        {"max_tokens": 10},
    ],
)
def test_structurally_invalid_prompt_files_raise(tmp_path, over):
    with pytest.raises(ValueError):
        cmp.load_prompt(write_prompt(tmp_path / "p.yaml", **over))


def test_missing_or_garbled_prompt_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        cmp.load_prompt(tmp_path / "nope.yaml")
    (tmp_path / "bad.yaml").write_text("a: [unclosed")
    with pytest.raises(ValueError, match="invalid YAML"):
        cmp.load_prompt(tmp_path / "bad.yaml")
    (tmp_path / "list.yaml").write_text("- 1\n- 2\n")
    with pytest.raises(ValueError, match="mapping"):
        cmp.load_prompt(tmp_path / "list.yaml")


def test_templates_are_plain_replacement_so_json_braces_survive():
    out = cmp.render_template(
        'Reply {"a": {{title}}} then {{ transcript }}', {"title": "T", "transcript": "X"}
    )
    assert out == 'Reply {"a": T} then X'
    with pytest.raises(ValueError, match="nope"):
        cmp.render_template("{{nope}}", {})


def test_prompt_hash_covers_prompt_and_schema_not_the_matrix(tmp_path):
    base = cmp.load_prompt(write_prompt(tmp_path / "a.yaml"))
    same = cmp.load_prompt(
        write_prompt(
            tmp_path / "b.yaml",
            matrix=MATRIX[:1],
            reference="haiku-medium",
            sample=["x"],
            max_tokens=9999,
        )
    )
    assert base.run_id == same.run_id
    assert base.run_id != cmp.load_prompt(write_prompt(tmp_path / "c.yaml", system="other")).run_id
    other_schema = {**SCHEMA, "required": ["summary"]}
    assert (
        base.run_id
        != cmp.load_prompt(write_prompt(tmp_path / "d.yaml", schema=other_schema)).run_id
    )


def test_episode_input_renders_what_the_model_sees(env):
    cfg, p = env
    ep = cmp.load_episode(cfg, "ep1")
    system, user = ep.prompt(p)
    assert system == "You summarise podcast transcripts."
    assert "Title: Episode 1" in user and "Notes: Show notes 1" in user
    assert "[00:00] SPEAKER_00: so today we are talking" in user
    assert "TODO" not in user and "{{" not in user
    assert TRANSCRIPT_TEXT in ep.plain_text
    with pytest.raises(ValueError, match="no transcript"):
        cmp.load_episode(cfg, "missing")


# ----------------------------------------------------------------------- jobs / plan


def test_select_jobs_orders_by_sample_and_filters(env):
    _, p = env
    jobs = cmp.select_jobs(p)
    assert len(jobs) == 9 and [j.episode_id for j in jobs[:3]] == ["ep1"] * 3
    assert {j.episode_id for j in cmp.select_jobs(p, episodes=2)} == {"ep1", "ep2"}
    only = cmp.select_jobs(p, only_configs=["opus-medium"])
    assert [j.spec.id for j in only] == ["opus-medium"] * 3


def test_offline_estimate_and_observed_output_replace_assumptions(env):
    cfg, p = env
    jobs = cmp.select_jobs(p, episodes=1)
    first = cmp.estimate(cfg, p, jobs)
    assert all(e.basis == "offline" and e.input_tokens > 50 for e in first)
    assert first[2].usd_hi > first[0].usd_hi  # opus costs more than haiku
    assert (first[0].output_lo, first[0].output_hi) == cmp.OUTPUT_TOKEN_RANGE["medium"]
    run(cfg, p, FakeClient(), jobs=jobs)  # 3000 output tokens each
    again = cmp.estimate(cfg, p, cmp.select_jobs(p, episodes=2)[3:])
    assert (again[0].output_lo, again[0].output_hi) == (2100, 3900)
    exact = cmp.estimate(cfg, p, jobs, token_counts={("ep1", "haiku-medium"): 777})
    assert exact[0].input_tokens == 777 and exact[0].basis == "count_tokens"
    assert exact[0].worst_usd == pytest.approx(llm.cost_usd("claude-haiku-5-5", 777, p.max_tokens))


# -------------------------------------------------------------------------------- run


def test_run_writes_results_run_json_and_ledger(env):
    cfg, p = env
    client = FakeClient()
    s = run(cfg, p, client)
    assert s.done == 9 and s.by_status == {"ok": 9} and s.stopped is None and s.errors == []
    assert len(client.calls) == 9
    r = cmp.load_result(cmp.result_path(cfg, p.run_id, "ep2", "sonnet-high"))
    assert r["status"] == "ok" and r["prompt_hash"] == p.prompt_hash()
    assert r["model_requested"] == "claude-sonnet-5-5" and r["effort"] == "high"
    assert r["result"]["data"]["tags"] == ["carbonation", "sous vide"]
    meta = json.loads((cmp.run_dir(cfg, p.run_id) / "run.json").read_text())
    assert meta["user"] == p.user and meta["schema"] == SCHEMA
    rows = ledger(cfg)
    assert len(rows) == 9 and {r["purpose"] for r in rows} == {"compare"}
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(s.spent_usd)
    assert {r["run_id"] for r in rows} == {p.run_id}


def test_requests_carry_each_configs_model_and_effort(env):
    cfg, p = env
    client = FakeClient()
    run(cfg, p, client, jobs=cmp.select_jobs(p, episodes=1))
    seen = {(c["model"], c["output_config"]["effort"]) for c in client.calls}
    assert seen == {(m["model"], m["effort"]) for m in MATRIX}
    assert all("temperature" not in c and "tool_choice" not in c for c in client.calls)


def test_resume_does_not_repeat_finished_calls_but_retries_errors(env):
    cfg, p = env
    client = FakeClient()
    run(cfg, p, client, jobs=cmp.select_jobs(p, episodes=1))
    assert cmp.pending_jobs(cfg, p, cmp.select_jobs(p, episodes=1)) == []
    todo = cmp.pending_jobs(cfg, p, cmp.select_jobs(p))
    assert len(todo) == 6 and {j.episode_id for j in todo} == {"ep2", "ep3"}
    # an error result is pending again
    path = cmp.result_path(cfg, p.run_id, "ep1", "haiku-medium")
    path.write_text(json.dumps({"status": "error", "error": "x"}))
    assert [j.key for j in cmp.pending_jobs(cfg, p, cmp.select_jobs(p, episodes=1))] == [
        ("ep1", "haiku-medium")
    ]


def test_refusal_truncation_and_invalid_json_are_recorded_and_the_run_continues(env):
    cfg, p = env

    def respond(req):
        m = req["model"]
        if m == "claude-haiku-5-5":
            return message("", model=m, stop_reason="refusal", stop_details={"category": "cyber"})
        if m == "claude-opus-5-5":
            return message('{"summary":', model=m, stop_reason="max_tokens", output_tokens=4096)
        return message("not json", model=m)

    s = run(cfg, p, FakeClient(respond), jobs=cmp.select_jobs(p, episodes=1))
    assert s.done == 3 and s.stopped is None
    assert s.by_status == {"refusal": 1, "truncated": 1, "invalid": 1}
    statuses = {
        c["id"]: cmp.load_result(cmp.result_path(cfg, p.run_id, "ep1", c["id"]))["status"]
        for c in MATRIX
    }
    assert statuses == {
        "haiku-medium": "refusal",
        "sonnet-high": "invalid",
        "opus-medium": "truncated",
    }
    # bad results are kept (not re-requested) unless --retry-bad
    jobs = cmp.select_jobs(p, episodes=1)
    assert cmp.pending_jobs(cfg, p, jobs) == []
    assert len(cmp.pending_jobs(cfg, p, jobs, retry_bad=True)) == 3
    assert {r["status"] for r in ledger(cfg)} == {"refusal", "truncated", "invalid"}


def _status_error(status):
    cls = {429: anthropic.RateLimitError, 500: anthropic.InternalServerError}[status]
    return cls(
        "x",
        response=httpx2.Response(status, request=httpx2.Request("POST", "https://x")),
        body=None,
    )


def test_transient_api_errors_become_error_results_and_do_not_stop_the_run(env):
    cfg, p = env
    client = FakeClient(
        lambda req: (
            _status_error(500)
            if req["model"] == "claude-opus-5-5"
            else message(json.dumps(default_json()), model=req["model"])
        )
    )
    s = run(cfg, p, client, jobs=cmp.select_jobs(p, episodes=1))
    assert s.by_status == {"ok": 2, "error": 1} and s.stopped is None
    assert len(s.errors) == 1 and "opus-medium" in s.errors[0]
    assert cmp.load_result(cmp.result_path(cfg, p.run_id, "ep1", "opus-medium"))["kind"] == "server"
    assert [r["status"] for r in ledger(cfg)].count("error") == 1
    assert [j.spec.id for j in cmp.pending_jobs(cfg, p, cmp.select_jobs(p, episodes=1))] == [
        "opus-medium"
    ]


def test_a_bad_key_stops_the_whole_run(env):
    cfg, p = env
    err = anthropic.AuthenticationError(
        "invalid x-api-key",
        response=httpx2.Response(401, request=httpx2.Request("POST", "https://x")),
        body=None,
    )
    client = FakeClient(lambda req: err)
    s = run(cfg, p, client)
    assert s.stopped and "fatal API error" in s.stopped
    assert len(client.calls) <= cfg.llm.concurrency  # nothing further was submitted


def test_repeated_rate_limits_stop_the_run(env):
    cfg, p = env
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"concurrency": 1})})
    client = FakeClient(lambda req: _status_error(429))
    s = run(cfg, p, client)
    assert s.stopped == "3 rate-limit failures in a row" and len(client.calls) == 3


def test_secrets_are_redacted_in_stored_errors(env, monkeypatch):
    cfg, p = env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-topsecret999")
    err = anthropic.InternalServerError(
        "oops sk-ant-topsecret999",
        response=httpx2.Response(500, request=httpx2.Request("POST", "https://x")),
        body=None,
    )
    run(cfg, p, FakeClient(lambda req: err), jobs=cmp.select_jobs(p, episodes=1)[:1])
    blob = "".join(f.read_text() for f in cmp.run_dir(cfg, p.run_id).rglob("*.json"))
    assert "topsecret999" not in blob and blob


def test_run_budget_is_a_hard_cap_using_the_worst_case(env):
    cfg, p = env
    cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"concurrency": 1})})
    ests = cmp.estimate(cfg, p, cmp.select_jobs(p))
    cap = ests[0].worst_usd + ests[1].worst_usd + 1e-6  # room for two calls' worst cases only
    client = FakeClient()
    s = run(cfg, p, client, max_usd=cap)
    assert s.stopped and "run budget" in s.stopped
    assert 1 <= len(client.calls) < 9 and s.spent_usd <= cap


def test_monthly_budget_stops_before_a_call_that_would_pass_it(env):
    cfg, p = env
    with state.connect(cfg.state_db) as conn:
        state.record_llm_usage(conn, purpose="other", model="m", status="ok", cost_usd=24.999)
    client = FakeClient()
    s = run(cfg, p, client)
    assert s.stopped and "monthly LLM budget" in s.stopped and client.calls == []


def test_concurrency_is_bounded(env):
    cfg, p = env
    client = FakeClient(delay=0.05)
    run(cfg, p, client)
    assert 1 < client.max_inflight <= cfg.llm.concurrency


def test_stop_event_stops_submitting_but_finishes_in_flight(env):
    cfg, p = env
    stop = threading.Event()
    seen = []

    def progress(msg):
        seen.append(msg)
        stop.set()

    s = run(cfg, p, FakeClient(), progress=progress, stop=stop)
    assert s.stopped == "stopped" and 1 <= s.done < 9


# ------------------------------------------------------------------------------ checks


def checks(p, data, ref=None, duration=60.0):
    return cmp.run_checks(
        p, data, transcript_text=TRANSCRIPT_TEXT, duration_s=duration, reference=ref
    )


def test_schema_check(env):
    _, p = env
    assert checks(p, default_json())["schema_ok"] is True
    bad = checks(p, {"summary": 1, "tags": [], "key_quotes": [], "extra": True})
    assert bad["schema_ok"] is False and bad["schema_errors"]


def test_quote_check_exact_fuzzy_and_missing(env):
    _, p = env
    data = default_json()
    data["key_quotes"] = [
        {
            "timestamp": "00:01",
            "quote": "Chill the liquid, to thirty-four degrees!",
        },  # punctuation/case
        {
            "timestamp": "00:02",
            "quote": "sous vide brisket for thirty six hours changes the texture a whole lot",
        },  # fuzzy
        {"timestamp": "00:03", "quote": "the cat sat on the mat and refused all offers"},
    ]
    q = checks(p, data)["quotes"]
    assert (q["total"], q["exact"], q["fuzzy"], q["missing"]) == (3, 1, 1, 1)
    assert q["missing_examples"] == ["the cat sat on the mat and refused all offers"]


def test_timestamp_check(env):
    _, p = env
    data = default_json()
    data["key_quotes"] = [
        {"timestamp": "00:05", "quote": QUOTE},
        {"timestamp": "1:00:00", "quote": QUOTE},  # past a 60 s episode
        {"timestamp": "00:02", "quote": QUOTE},  # out of order
        {"timestamp": "soon", "quote": QUOTE},
    ]
    t = checks(p, data)["timestamps"]
    assert t == {"total": 4, "in_range": 2, "out_of_range": 1, "unparseable": 1, "out_of_order": 1}


def test_parse_timestamps():
    assert cmp.parse_timestamps("12:34") == [754.0]
    assert cmp.parse_timestamps("1:02:03") == [3723.0]
    assert cmp.parse_timestamps("12:34-13:00") == [754.0, 780.0]
    assert cmp.parse_timestamps(90) == [90.0] and cmp.parse_timestamps("45.5") == [45.5]
    assert cmp.parse_timestamps("later") is None and cmp.parse_timestamps(True) is None


def test_list_agreement_with_the_reference(env):
    _, p = env
    ref = default_json()
    other = default_json()
    other["tags"] = ["Carbonation", "BBQ"]
    c = checks(p, other, ref)
    assert c["agreement"]["tags"] == pytest.approx(1 / 3)
    assert cmp.jaccard(set(), set()) is None


def test_checks_only_run_for_fields_the_prompt_names(tmp_path):
    p = cmp.load_prompt(write_prompt(tmp_path / "p.yaml", checks={}))
    assert set(checks(p, default_json())) == {"schema_ok", "schema_errors"}
    free = cmp.load_prompt(write_prompt(tmp_path / "q.yaml", schema=None, checks={}))
    assert checks(free, {"anything": 1}) == {}


def test_find_values_walks_nested_keys():
    data = {"a": [{"quote": "x"}, {"b": {"quote": ["y", "z"]}}], "quote": "w"}
    assert cmp.find_values(data, {"quote"}) == ["x", "y", "z", "w"]
