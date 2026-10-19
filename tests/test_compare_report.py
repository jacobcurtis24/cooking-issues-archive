"""The blind review page, the key, and unblinded scoring."""

import json
import re

import pytest
from comparefake import EPISODES, MATRIX, setup
from llmfake import FakeClient, default_json, message

from ciw import compare as cmp
from ciw import compare_report as rep

CONFIG_IDS = [c["id"] for c in MATRIX]


@pytest.fixture
def done(tmp_cfg, tmp_path):
    cfg = setup(tmp_path)
    p = cmp.load_prompt(cfg.llm.prompt_file)
    return cfg, p


def run_all(cfg, p, client=None, episodes=None):
    jobs = cmp.select_jobs(p, episodes=episodes)
    return cmp.run_jobs(cfg, p, jobs, client=client or FakeClient(), max_usd=100)


def test_labels_are_a_stable_permutation_per_episode():
    a = rep.labels_for("run-x", "ep1", CONFIG_IDS)
    assert sorted(a) == ["A", "B", "C"] and sorted(a.values()) == sorted(CONFIG_IDS)
    assert a == rep.labels_for(
        "run-x", "ep1", list(reversed(CONFIG_IDS))
    )  # input order is irrelevant
    orders = {tuple(rep.labels_for("run-x", f"ep{i}", CONFIG_IDS).values()) for i in range(30)}
    assert len(orders) > 1  # not the same order for every episode


def test_render_value_escapes_everything():
    out = rep.render_value({"k<b>": ["<script>alert(1)</script>", {"a": None}], "n": 3})
    assert "<script>" not in out and "&lt;script&gt;" in out and "k&lt;b&gt;" in out


def test_review_page_hides_the_identity_cost_and_escapes_model_text(done):
    cfg, p = done

    def respond(req):
        d = default_json()
        d["summary"] = "<img src=x onerror=alert(1)> and __TOTAL__"
        return message(json.dumps(d), model=req["model"])

    run_all(cfg, p, FakeClient(respond))
    info = rep.build_review(cfg, p, web_base="http://127.0.0.1:5000/")
    page = info.path.read_text()
    assert info.complete == EPISODES and info.pending == []
    assert "<img src=x" not in page and "&lt;img src=x" in page
    assert "and __TOTAL__" in page  # model text is never re-scanned for template markers
    for secret in (*CONFIG_IDS, "haiku", "sonnet", "opus", "claude-", "xhigh", "$0."):
        assert secret not in page.replace("ciw-compare:" + p.run_id, "")
    assert 'href="http://127.0.0.1:5000/episode/ep1"' in page
    assert page.count("<h3>Output ") == 9 and "Show notes 1" in page
    assert (
        re.search(r"schema ok", page)
        and "quotes 1/1 found" in page
        and "times 1/1 in range" in page
    )
    assert info.key_path.is_file()


def test_key_maps_every_label_and_regeneration_keeps_labels(done):
    cfg, p = done
    run_all(cfg, p)
    first = rep.build_review(cfg, p, web_base="http://x")
    key1 = json.loads(first.key_path.read_text())
    page1 = first.path.read_text()
    assert set(key1) == set(EPISODES)
    assert all(sorted(v.values()) == sorted(CONFIG_IDS) for v in key1.values())
    second = rep.build_review(cfg, p, web_base="http://x")
    assert json.loads(second.key_path.read_text()) == key1 and second.path.read_text() == page1


def test_episodes_missing_a_config_or_with_errors_stay_pending(done):
    cfg, p = done
    run_all(cfg, p, episodes=2)
    cmp.result_path(cfg, p.run_id, "ep2", "opus-medium").write_text('{"status": "error"}')
    info = rep.build_review(cfg, p, web_base="http://x")
    assert info.complete == ["ep1"] and info.pending == ["ep2", "ep3"]
    assert "2 episode(s) still waiting" in info.path.read_text()


def test_failed_outputs_show_a_notice_not_markup(done):
    cfg, p = done

    def respond(req):
        if req["model"] == "claude-opus-5-5":
            return message("<b>half", model=req["model"], stop_reason="max_tokens")
        return message(json.dumps(default_json()), model=req["model"])

    run_all(cfg, p, FakeClient(respond), episodes=1)
    page = rep.build_review(cfg, p, web_base="http://x").path.read_text()
    assert "No valid output (truncated)" in page and "&lt;b&gt;half" in page


def ratings_for(cfg, p, scores):
    """ratings.json as the page would export it; ``scores`` maps config id -> rating."""
    key = json.loads((cmp.run_dir(cfg, p.run_id) / "key.json").read_text())
    ratings = {ep: {lab: scores[cid] for lab, cid in m.items()} for ep, m in key.items()}
    best = {
        ep: next(lab for lab, cid in m.items() if cid == "sonnet-high") for ep, m in key.items()
    }
    return {"run_id": p.run_id, "ratings": ratings, "best": best}


def test_summary_unblinds_ratings_and_projects_cost(done):
    cfg, p = done
    run_all(cfg, p)
    rep.build_review(cfg, p, web_base="http://x")
    s = rep.summarize(
        cfg,
        p,
        ratings_for(cfg, p, {"haiku-medium": 2, "sonnet-high": 5, "opus-medium": 4}),
        backlog_episodes=696,
    )
    by = {c["config"]: c for c in s["configs"]}
    assert [c["config"] for c in s["configs"]] == ["sonnet-high", "opus-medium", "haiku-medium"]
    assert by["sonnet-high"]["mean_rating"] == 5 and by["sonnet-high"]["best_picks"] == 3
    assert by["haiku-medium"]["best_picks"] == 0 and by["haiku-medium"]["rated"] == 3
    assert (
        by["opus-medium"]["mean_cost_usd"]
        > by["sonnet-high"]["mean_cost_usd"]
        > by["haiku-medium"]["mean_cost_usd"]
    )
    c = by["sonnet-high"]
    assert c["backlog_usd_standard"] == pytest.approx(c["mean_cost_usd"] * 696)
    assert c["backlog_usd_batch"] == pytest.approx(c["backlog_usd_standard"] / 2)
    assert c["usd_per_rating_point"] == pytest.approx(c["mean_cost_usd"] / 5)
    assert (
        c["quotes_missing"] == "0/3" and c["timestamps_bad"] == "0/3" and c["schema_failures"] == 0
    )
    assert by["haiku-medium"]["agreement_with_reference"] == 1.0
    md = rep.summary_markdown(s)
    assert "| sonnet-high |" in md and "696 episodes" in md


def test_summary_counts_check_failures(done):
    cfg, p = done

    def respond(req):
        d = default_json()
        if req["model"] == "claude-haiku-5-5":
            d["key_quotes"] = [
                {"timestamp": "9:99:99", "quote": "something nobody said at all here"}
            ]
        return message(json.dumps(d), model=req["model"])

    run_all(cfg, p, FakeClient(respond))
    rep.build_review(cfg, p, web_base="http://x")
    s = rep.summarize(cfg, p, {"run_id": p.run_id, "ratings": {}, "best": {}}, backlog_episodes=10)
    h = next(c for c in s["configs"] if c["config"] == "haiku-medium")
    assert h["quotes_missing"] == "3/3" and h["timestamps_bad"] == "3/3" and h["rated"] == 0
    assert h["mean_rating"] is None and h["usd_per_rating_point"] is None


def test_summary_rejects_ratings_from_another_run(done):
    cfg, p = done
    run_all(cfg, p, episodes=1)
    rep.build_review(cfg, p, web_base="http://x")
    with pytest.raises(ValueError, match="run-other"):
        rep.summarize(cfg, p, {"run_id": "run-other"}, backlog_episodes=1)
