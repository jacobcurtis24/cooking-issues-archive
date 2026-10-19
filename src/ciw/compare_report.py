"""Blind review page and summary for a comparison run (``ciw compare report|score``).

``review.html`` is one static, self-contained file (no CDN, no framework). For each episode whose
every config has a result it shows the outputs under shuffled labels A, B, C... The shuffle is
seeded by (run id, episode id), so regenerating the page keeps the labels; the label -> config map
is in ``key.json`` and is only read by ``score``. Cost, tokens and latency are NOT on the page
(they would identify the model); the quality checks are. Ratings live in ``localStorage`` and are
exported as ``ratings.json``.
"""

from __future__ import annotations

import html
import json
import random
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciw.compare import (
    ComparePrompt,
    EpisodeInput,
    jaccard,
    load_episode,
    load_result,
    result_path,
    run_checks,
    run_dir,
)
from ciw.config import Config

NOTES_CHARS = 1500
_PAGE = (Path(__file__).parent / "compare_review.html").read_text(encoding="utf-8")
RATING_MAX = 5


def labels_for(run_id: str, episode_id: str, config_ids: list[str]) -> dict[str, str]:
    """label -> config id, deterministic for (run, episode) and independent of input order."""
    ids = sorted(config_ids)
    random.Random(f"{run_id}:{episode_id}").shuffle(ids)
    return dict(zip(string.ascii_uppercase, ids, strict=False))


def render_value(v: Any) -> str:
    """JSON as nested HTML. Everything is escaped: model output is never trusted as markup."""
    if isinstance(v, dict):
        rows = "".join(
            f"<dt>{html.escape(str(k))}</dt><dd>{render_value(x)}</dd>" for k, x in v.items()
        )
        return f"<dl>{rows}</dl>"
    if isinstance(v, list):
        if not v:
            return '<span class="empty">(none)</span>'
        return "<ul>" + "".join(f"<li>{render_value(x)}</li>" for x in v) + "</ul>"
    if v is None:
        return '<span class="empty">null</span>'
    return f'<span class="s">{html.escape(str(v))}</span>'


def _badges(checks: dict[str, Any]) -> str:
    out: list[str] = []
    if "schema_ok" in checks:
        ok = checks["schema_ok"]
        out.append(
            f'<span class="b {"good" if ok else "bad"}">schema {"ok" if ok else "FAIL"}</span>'
        )
    q = checks.get("quotes")
    if q and q["total"]:
        found = q["exact"] + q["fuzzy"]
        cls = "good" if q["missing"] == 0 else "bad"
        out.append(f'<span class="b {cls}">quotes {found}/{q["total"]} found</span>')
    t = checks.get("timestamps")
    if t and t["total"]:
        cls = "good" if t["out_of_range"] == 0 and t["unparseable"] == 0 else "bad"
        out.append(f'<span class="b {cls}">times {t["in_range"]}/{t["total"]} in range</span>')
    return " ".join(out)


@dataclass
class ReviewInfo:
    path: Path
    key_path: Path
    complete: list[str]
    pending: list[str]


def _checks_for(
    p: ComparePrompt, result: dict[str, Any], ep: EpisodeInput, reference: Any = None
) -> dict[str, Any]:
    data = result.get("result", {}).get("data")
    if data is None:
        return {}
    return run_checks(
        p, data, transcript_text=ep.plain_text, duration_s=ep.duration_s, reference=reference
    )


def build_review(cfg: Config, p: ComparePrompt, *, web_base: str) -> ReviewInfo:
    run_id = p.run_id
    ids = [c.id for c in p.matrix]
    complete: list[str] = []
    pending: list[str] = []
    cards: list[str] = []
    key: dict[str, dict[str, str]] = {}
    for episode_id in p.sample:
        results = {c: load_result(result_path(cfg, run_id, episode_id, c)) for c in ids}
        if any(r is None or r.get("status") == "error" for r in results.values()):
            pending.append(episode_id)
            continue
        ep = load_episode(cfg, episode_id)
        label_map = labels_for(run_id, episode_id, ids)
        key[episode_id] = label_map
        complete.append(episode_id)
        notes = html.escape(ep.show_notes[:NOTES_CHARS])
        outs: list[str] = []
        for label, cid in label_map.items():
            r = results[cid] or {}
            res = r.get("result", {})
            if r.get("status") == "ok" and res.get("data") is not None:
                body = render_value(res["data"])
            elif res.get("text"):
                body = f'<p class="bad">No valid output ({html.escape(str(r.get("status")))}).</p>'
                body += f"<pre>{html.escape(res['text'][:4000])}</pre>"
            else:
                body = f'<p class="bad">No output ({html.escape(str(r.get("status")))}).</p>'
            badges = _badges(_checks_for(p, r, ep))
            stars = "".join(
                f'<label><input type="radio" name="r-{html.escape(episode_id)}-{label}" '
                f'value="{n}" data-ep="{html.escape(episode_id)}" data-label="{label}">{n}</label>'
                for n in range(1, RATING_MAX + 1)
            )
            outs.append(
                f'<section class="card" id="c-{html.escape(episode_id)}-{label}">'
                f'<header><h3>Output {label}</h3><div class="badges">{badges}</div>'
                f'<div class="rate">Rating: {stars}</div>'
                f'<label class="best"><input type="radio" name="best-{html.escape(episode_id)}" '
                f'value="{label}" data-ep="{html.escape(episode_id)}"> best</label></header>'
                f'<div class="body">{body}</div></section>'
            )
        link = f"{web_base.rstrip('/')}/episode/{html.escape(episode_id)}"
        cards.append(
            f'<article class="ep" id="ep-{html.escape(episode_id)}">'
            f"<h2>{html.escape(ep.title)}</h2>"
            f'<p class="meta">{html.escape(ep.pub_date)} · {int(ep.duration_s // 60)} min · '
            f'{ep.n_words:,} words · <a href="{link}">open transcript</a></p>'
            f"<details><summary>Show notes</summary><p class='notes'>{notes}</p></details>"
            f'<div class="grid">{"".join(outs)}</div></article>'
        )
    base = run_dir(cfg, run_id)
    base.mkdir(parents=True, exist_ok=True)
    key_path = base / "key.json"
    existing = load_result(key_path) or {}
    key_path.write_text(json.dumps({**existing, **key}, indent=2) + "\n", encoding="utf-8")
    fills = {
        "__RUN_ID__": html.escape(run_id),
        "__BODY__": "".join(cards) or "<p>No episode has results for every config yet.</p>",
        "__PENDING__": html.escape(
            f"{len(pending)} episode(s) still waiting for results" if pending else ""
        ),
        "__TOTAL__": str(len(complete)),
    }
    # One pass, so model output inside the body is never scanned for placeholders.
    page = re.sub("|".join(fills), lambda m: fills[m.group(0)], _PAGE)
    out = base / "review.html"
    out.write_text(page, encoding="utf-8")
    return ReviewInfo(path=out, key_path=key_path, complete=complete, pending=pending)


# -------------------------------------------------------------------------------- score


def summarize(
    cfg: Config,
    p: ComparePrompt,
    ratings: dict[str, Any],
    *,
    backlog_episodes: int,
) -> dict[str, Any]:
    """Unblind ``ratings.json`` and combine it with cost, size and check results per config."""
    run_id = p.run_id
    if ratings.get("run_id") not in (None, run_id):
        raise ValueError(f"ratings are for {ratings.get('run_id')}, not {run_id}")
    key = load_result(run_dir(cfg, run_id) / "key.json") or {}
    rated: dict[str, dict[str, int]] = ratings.get("ratings", {})
    best: dict[str, str] = ratings.get("best", {})
    rows: dict[str, dict[str, Any]] = {
        c.id: {
            "config": c.id,
            "model": c.model,
            "effort": c.effort,
            "n": 0,
            "statuses": {},
            "cost": [],
            "out_tokens": [],
            "latency": [],
            "ratings": [],
            "wins": 0,
            "schema_fail": 0,
            "quotes_total": 0,
            "quotes_missing": 0,
            "ts_total": 0,
            "ts_bad": 0,
            "agreement": [],
        }
        for c in p.matrix
    }
    for episode_id, label_map in key.items():
        if episode_id not in p.sample:
            continue
        ep = load_episode(cfg, episode_id)
        ref = (
            (load_result(result_path(cfg, run_id, episode_id, p.reference)) or {})
            .get("result", {})
            .get("data")
        )
        for label, cid in label_map.items():
            r = load_result(result_path(cfg, run_id, episode_id, cid)) or {}
            row = rows[cid]
            res = r.get("result", {})
            row["n"] += 1
            row["statuses"][r.get("status")] = row["statuses"].get(r.get("status"), 0) + 1
            row["cost"].append(res.get("cost_usd", 0.0))
            row["out_tokens"].append(res.get("output_tokens", 0))
            row["latency"].append(res.get("latency_s", 0.0))
            checks = _checks_for(p, r, ep, ref if cid != p.reference else None)
            row["schema_fail"] += int(checks.get("schema_ok") is False or not checks)
            q = checks.get("quotes")
            if q:
                row["quotes_total"] += q["total"]
                row["quotes_missing"] += q["missing"]
            t = checks.get("timestamps")
            if t:
                row["ts_total"] += t["total"]
                row["ts_bad"] += t["out_of_range"] + t["unparseable"]
            row["agreement"].extend(
                a for a in checks.get("agreement", {}).values() if a is not None
            )
            score = rated.get(episode_id, {}).get(label)
            if score:
                row["ratings"].append(int(score))
            if best.get(episode_id) == label:
                row["wins"] += 1

    def mean(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    configs = []
    for row in rows.values():
        mc, mr = mean(row["cost"]), mean(row["ratings"])
        configs.append(
            {
                "config": row["config"],
                "model": row["model"],
                "effort": row["effort"],
                "episodes": row["n"],
                "statuses": row["statuses"],
                "mean_cost_usd": mc,
                "mean_output_tokens": mean(row["out_tokens"]),
                "mean_latency_s": mean(row["latency"]),
                "rated": len(row["ratings"]),
                "mean_rating": mr,
                "best_picks": row["wins"],
                "schema_failures": row["schema_fail"],
                "quotes_missing": f"{row['quotes_missing']}/{row['quotes_total']}",
                "timestamps_bad": f"{row['ts_bad']}/{row['ts_total']}",
                "agreement_with_reference": mean(row["agreement"]),
                "usd_per_rating_point": (mc / mr) if (mc is not None and mr) else None,
                "backlog_usd_standard": mc * backlog_episodes if mc is not None else None,
                "backlog_usd_batch": mc * backlog_episodes * 0.5 if mc is not None else None,
            }
        )
    configs.sort(key=lambda c: (-(c["mean_rating"] or 0), c["mean_cost_usd"] or 0))
    return {
        "run_id": run_id,
        "reference": p.reference,
        "episodes": sorted(key),
        "backlog_episodes": backlog_episodes,
        "configs": configs,
    }


def summary_markdown(s: dict[str, Any]) -> str:
    def f(v: Any, fmt: str) -> str:
        return "-" if v is None else format(v, fmt)

    lines = [
        f"# Comparison {s['run_id']}",
        "",
        f"{len(s['episodes'])} episodes; reference = `{s['reference']}`. Sorted by mean rating. "
        "Cost is an estimate from API usage. The backlog columns project the mean cost per "
        f"episode of this sample onto {s['backlog_episodes']} episodes (Batch = half price).",
        "",
        "| config | rated | mean rating | best picks | $/episode | out tokens | latency s "
        "| schema fails | quotes missing | times bad | agrees w/ ref | backlog $ std "
        "| backlog $ batch |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in s["configs"]:
        lines.append(
            f"| {c['config']} | {c['rated']} | {f(c['mean_rating'], '.2f')} | {c['best_picks']} "
            f"| {f(c['mean_cost_usd'], '.3f')} | {f(c['mean_output_tokens'], '.0f')} "
            f"| {f(c['mean_latency_s'], '.0f')} | {c['schema_failures']} | {c['quotes_missing']} "
            f"| {c['timestamps_bad']} | {f(c['agreement_with_reference'], '.2f')} "
            f"| {f(c['backlog_usd_standard'], '.2f')} | {f(c['backlog_usd_batch'], '.2f')} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "ReviewInfo",
    "build_review",
    "jaccard",
    "labels_for",
    "render_value",
    "summarize",
    "summary_markdown",
]
