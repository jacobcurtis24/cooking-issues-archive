"""ciw.llm (request shape, cost, result handling, errors) and the llm_usage ledger."""

import json
from datetime import UTC, datetime

import anthropic
import httpx2
import pytest
from llmfake import FakeClient, install, message

from ciw import llm, state


def _err(cls, status, text="boom"):
    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://x"))
    return cls(text, response=resp, body=None)


# ----------------------------------------------------------------------------- pricing


def test_cost_per_model():
    assert llm.cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) == pytest.approx(12.0)
    assert llm.cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert llm.cost_usd("claude-haiku-5-5", 50_000, 1_000_000) == pytest.approx(0.505)


def test_cost_cache_batch_and_long_prompt_rate():
    base = llm.cost_usd("claude-sonnet-5-5", 0, 0, cache_read=1_000_000)
    assert base == pytest.approx(0.2)  # cache reads are 10% of the input price
    assert llm.cost_usd("claude-sonnet-5-5", 0, 0, cache_write=1_000_000) == pytest.approx(2.5)
    assert llm.cost_usd("claude-sonnet-5-5", 1_000_000, 0, batch=True) == pytest.approx(1.0)
    # Haiku's rate rises above 100K prompt tokens
    assert llm.cost_usd("claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert llm.cost_usd("claude-haiku-5-5", 100_001, 0) == pytest.approx(100_001 * 0.5 / 1e6)


def test_unknown_model_is_an_error_not_free():
    with pytest.raises(KeyError):
        llm.cost_usd("claude-typo", 1, 1)


# ------------------------------------------------------------------------- the request


def test_request_has_effort_schema_adaptive_thinking_and_nothing_forbidden():
    req = llm.build_request(
        model="claude-opus-5-5",
        system="s",
        user="u",
        effort="xhigh",
        schema={"type": "object"},
        max_tokens=1234,
    )
    assert req["output_config"] == {
        "effort": "xhigh",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert req["thinking"] == {"type": "adaptive"}
    assert req["max_tokens"] == 1234 and req["messages"] == [{"role": "user", "content": "u"}]
    for forbidden in ("temperature", "top_p", "top_k", "tool_choice", "fallbacks", "betas"):
        assert forbidden not in req
    assert req["messages"][-1]["role"] == "user"  # no assistant prefill


def test_bad_effort_rejected():
    with pytest.raises(ValueError):
        llm.build_request(
            model="m", system="", user="", effort="extreme", schema=None, max_tokens=10
        )


# --------------------------------------------------------------------------- the call


def test_call_returns_usage_cost_and_parsed_json():
    client = FakeClient(
        lambda req: message(
            json.dumps({"a": 1}),
            model=req["model"],
            input_tokens=1000,
            output_tokens=2000,
            cache_read=500,
            request_id="req_1",
        )
    )
    res = llm.call(
        client, model="claude-sonnet-5-5", system="s", user="u", effort="high", schema={"t": 1}
    )
    assert res.data == {"a": 1} and res.parse_error is None and res.ok
    assert (res.input_tokens, res.output_tokens, res.cache_read) == (1000, 2000, 500)
    assert res.request_id == "req_1" and res.model == "claude-sonnet-5-5"
    assert res.cost_usd == pytest.approx(llm.cost_usd("claude-sonnet-5-5", 1000, 2000, 500))
    assert client.calls[0]["output_config"]["effort"] == "high"


def test_invalid_json_truncation_and_refusal_are_results_not_exceptions():
    bad = FakeClient(lambda r: message("{not json", model=r["model"]))
    res = llm.call(bad, model="claude-haiku-5-5", system="", user="", schema={"t": 1})
    assert res.data is None and "invalid JSON" in res.parse_error and not res.ok

    cut = FakeClient(lambda r: message('{"a":', model=r["model"], stop_reason="max_tokens"))
    res = llm.call(cut, model="claude-haiku-5-5", system="", user="", schema={"t": 1})
    assert res.stop_reason == "max_tokens" and not res.ok

    refused = FakeClient(
        lambda r: message(
            "", model=r["model"], stop_reason="refusal", stop_details={"category": "bio"}
        )
    )
    res = llm.call(refused, model="claude-opus-5-5", system="", user="", schema={"t": 1})
    assert res.stop_reason == "refusal" and res.stop_details == {"category": "bio"}
    assert res.parse_error == "empty response"


def test_free_text_call_has_no_json_parse():
    client = FakeClient(lambda r: message("just text", model=r["model"]))
    res = llm.call(client, model="claude-haiku-5-5", system="", user="")
    assert res.text == "just text" and res.data is None and res.ok
    assert "output_config" not in client.calls[0]


@pytest.mark.parametrize(
    "exc, kind, fatal",
    [
        (lambda: _err(anthropic.AuthenticationError, 401), "auth", True),
        (lambda: _err(anthropic.PermissionDeniedError, 403), "permission", True),
        (lambda: _err(anthropic.RateLimitError, 429), "rate_limit", False),
        (lambda: _err(anthropic.InternalServerError, 500), "server", False),
        (lambda: _err(anthropic.BadRequestError, 400), "bad_request", False),
        (
            lambda: anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            "connection",
            False,
        ),
    ],
)
def test_api_errors_become_llm_errors(exc, kind, fatal):
    client = FakeClient(lambda r: exc())
    with pytest.raises(llm.LlmError) as info:
        llm.call(client, model="claude-haiku-5-5", system="", user="")
    assert info.value.kind == kind and info.value.fatal is fatal


def test_error_text_never_carries_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretvalue123")
    client = FakeClient(
        lambda r: _err(anthropic.AuthenticationError, 401, "bad key sk-ant-secretvalue123")
    )
    with pytest.raises(llm.LlmError) as info:
        llm.call(client, model="claude-haiku-5-5", system="", user="")
    assert "secretvalue123" not in str(info.value)


def test_count_tokens_uses_the_endpoint():
    client = FakeClient()
    assert llm.count_tokens(client, model="claude-haiku-5-5", system="s", user="u") == 12_345
    assert client.count_calls[0]["messages"] == [{"role": "user", "content": "u"}]


def test_make_client_cannot_build_the_real_client_under_pytest(tmp_cfg):
    with pytest.raises(RuntimeError, match="fake LLM client"):
        llm.make_client(tmp_cfg)


def test_make_client_returns_the_installed_fake(tmp_cfg, monkeypatch):
    fake = install(monkeypatch, FakeClient())
    assert llm.make_client(tmp_cfg) is fake


# ----------------------------------------------------------------------------- ledger


def test_migration_6_creates_the_ledger(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)")}
    assert {
        "ts",
        "purpose",
        "run_id",
        "model",
        "effort",
        "cost_usd",
        "request_id",
        "status",
    } <= cols
    assert len(state.MIGRATIONS) >= 6


def test_ledger_sums_this_month_and_run(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        state.record_llm_usage(
            conn, purpose="compare", model="m", status="ok", cost_usd=1.5, run_id="r1"
        )
        state.record_llm_usage(
            conn, purpose="compare", model="m", status="ok", cost_usd=0.5, run_id="r2"
        )
        conn.execute("UPDATE llm_usage SET ts='2020-01-05T00:00:00Z' WHERE run_id='r2'")
        assert state.month_spend_usd(conn) == pytest.approx(1.5)
        assert state.month_spend_usd(conn, now=datetime(2020, 1, 20, tzinfo=UTC)) == pytest.approx(
            0.5
        )
        assert state.run_spend_usd(conn, "r1") == pytest.approx(1.5)


def test_check_budget_raises_before_the_cap_is_passed(tmp_path):
    with state.connect(tmp_path / "s.sqlite") as conn:
        state.record_llm_usage(conn, purpose="compare", model="m", status="ok", cost_usd=24.0)
        assert state.check_budget(conn, 25.0, 1.0) == pytest.approx(24.0)  # exactly fits
        with pytest.raises(state.BudgetExceeded, match=r"\$24\.00 already spent"):
            state.check_budget(conn, 25.0, 1.01)
