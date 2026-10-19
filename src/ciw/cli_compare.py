"""``ciw compare``: run one prompt across models/efforts on sampled episodes (T9 step 0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from ciw import compare as cmp
from ciw import llm, state
from ciw.config import Config, load_config, redact

compare_app = typer.Typer(
    help="Compare Claude models and effort levels on one prompt (blind review + checks).",
    no_args_is_help=True,
)

EXIT_USAGE = 2
EXIT_BUDGET = 4

EpisodesOpt = Annotated[
    int | None, typer.Option("--episodes", min=1, help="Only the first N sample episodes.")
]
ConfigOpt = Annotated[
    list[str] | None, typer.Option("--config", help="Only this matrix id (repeatable).")
]


def _fail(msg: str, code: int = EXIT_USAGE) -> typer.Exit:
    typer.echo(f"error: {msg}", err=True)
    return typer.Exit(code=code)


def _load(cfg: Config) -> cmp.ComparePrompt:
    try:
        return cmp.load_prompt(cfg.llm.prompt_file)
    except ValueError as exc:
        raise _fail(redact(exc)) from None


def _unknown_configs(p: cmp.ComparePrompt, only: list[str] | None) -> None:
    bad = sorted(set(only or []) - {c.id for c in p.matrix})
    if bad:
        raise _fail(f"unknown --config {bad}; matrix ids: {[c.id for c in p.matrix]}")


def _print_estimate(cfg: Config, p: cmp.ComparePrompt, ests: list[cmp.Estimate]) -> None:
    by_cfg: dict[str, list[cmp.Estimate]] = {}
    for e in ests:
        by_cfg.setdefault(e.job.spec.id, []).append(e)
    typer.echo(
        f"{'config':<16}{'model':<20}{'effort':<8}{'calls':>6}{'in tok/call':>13}{'USD':>20}"
    )
    lo = hi = worst = 0.0
    for spec in p.matrix:
        es = by_cfg.get(spec.id, [])
        if not es:
            continue
        spec_lo, spec_hi = sum(e.usd_lo for e in es), sum(e.usd_hi for e in es)
        lo, hi, worst = lo + spec_lo, hi + spec_hi, worst + sum(e.worst_usd for e in es)
        avg_in = sum(e.input_tokens for e in es) // len(es)
        typer.echo(
            f"{spec.id:<16}{spec.model:<20}{spec.effort:<8}{len(es):>6}{avg_in:>13,}"
            f"{f'${spec_lo:.2f} - ${spec_hi:.2f}':>20}"
        )
    basis = {e.basis for e in ests}
    observed = cmp.observed_output_tokens(cfg, p)
    typer.echo(
        f"ESTIMATE total ${lo:.2f} - ${hi:.2f}  (worst case, every call at max_tokens: "
        f"${worst:.2f}; input tokens: {'/'.join(sorted(basis))}; output tokens: "
        f"{f'observed in this run for {len(observed)} config(s), else ' if observed else ''}"
        f"assumed per effort; prices verified {llm.PRICES_VERIFIED})"
    )


def _month_line(cfg: Config) -> str:
    with state.connect(cfg.state_db) as conn:
        spent = state.month_spend_usd(conn)
    return f"ledger: ${spent:.2f} spent this month of ${cfg.llm.monthly_budget_usd:.2f}"


@compare_app.command("plan")
def plan_cmd(
    episodes: EpisodesOpt = None,
    config: ConfigOpt = None,
    count_tokens: Annotated[
        bool,
        typer.Option(
            "--count-tokens",
            help="Exact input counts from the (free) count_tokens endpoint: this makes API "
            "calls, so it is never the default.",
        ),
    ] = False,
) -> None:
    """Show the grid, the prompt problems and the cost estimate. No spend; no API call
    unless --count-tokens."""
    cfg = load_config()
    p = _load(cfg)
    _unknown_configs(p, config)
    jobs = cmp.select_jobs(p, episodes=episodes, only_configs=config)
    todo = cmp.pending_jobs(cfg, p, jobs)
    typer.echo(
        f"prompt file: {cfg.llm.prompt_file}   run: {p.run_id}   hash: {p.prompt_hash()[:12]}"
    )
    problems = p.lint()
    for line in problems:
        typer.echo(f"PROBLEM {line}")
    typer.echo(
        f"grid: {len({j.episode_id for j in jobs})} episodes x {len(p.matrix)} configs = "
        f"{len(jobs)} calls; {len(jobs) - len(todo)} already have a result, {len(todo)} to do"
    )
    token_counts = None
    inputs: dict[str, cmp.EpisodeInput] = {}
    try:
        if count_tokens:
            if problems:
                raise _fail("fix the prompt problems before counting tokens")
            client = llm.make_client(cfg)
            token_counts = {}
            for job in todo:
                ep = inputs.setdefault(job.episode_id, cmp.load_episode(cfg, job.episode_id))
                system, user = ep.prompt(p)
                token_counts[job.key] = llm.count_tokens(
                    client, model=job.spec.model, system=system, user=user
                )
        if todo:
            _print_estimate(
                cfg, p, cmp.estimate(cfg, p, todo, inputs=inputs, token_counts=token_counts)
            )
    except (llm.LlmError, ValueError) as exc:
        raise _fail(redact(exc)) from None
    typer.echo(_month_line(cfg))
    if problems:
        raise typer.Exit(code=1)


@compare_app.command("run")
def run_cmd(
    episodes: EpisodesOpt = None,
    all_: Annotated[bool, typer.Option("--all", help="Every sample episode.")] = False,
    config: ConfigOpt = None,
    max_usd: Annotated[
        float | None, typer.Option("--max-usd", min=0, help="Stop before this run passes $X.")
    ] = None,
    retry_bad: Annotated[
        bool, typer.Option("--retry-bad", help="Also redo truncated, refused and invalid results.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Go ahead and spend (required).")] = False,
) -> None:
    """Call the API for every (episode, config) without a result. Needs --yes."""
    if (episodes is None) == (not all_):
        raise _fail("choose exactly one of --episodes N or --all")
    cfg = load_config()
    p = _load(cfg)
    _unknown_configs(p, config)
    problems = p.lint()
    if problems:
        for line in problems:
            typer.echo(f"PROBLEM {line}", err=True)
        raise _fail("the prompt file is not ready; no API call was made")
    jobs = cmp.select_jobs(p, episodes=episodes, only_configs=config)
    todo = cmp.pending_jobs(cfg, p, jobs, retry_bad=retry_bad)
    if not todo:
        typer.echo("nothing to do: every selected (episode, config) already has a result")
        return
    cap = cfg.llm.max_usd_per_run if max_usd is None else max_usd
    inputs: dict[str, cmp.EpisodeInput] = {}
    try:
        ests = cmp.estimate(cfg, p, todo, inputs=inputs)
    except ValueError as exc:
        raise _fail(redact(exc)) from None
    typer.echo(f"run {p.run_id}: {len(todo)} calls, run cap ${cap:.2f}")
    _print_estimate(cfg, p, ests)
    typer.echo(_month_line(cfg))
    if not yes:
        raise _fail("add --yes to make these API calls (nothing was sent)")
    try:
        client = llm.make_client(cfg)
    except llm.LlmError as exc:
        raise _fail(redact(exc)) from None
    summary = cmp.run_jobs(
        cfg, p, todo, client=client, max_usd=cap, progress=typer.echo, inputs=inputs
    )
    typer.echo(
        f"finished: {summary.done} calls, ${summary.spent_usd:.2f} spent, "
        f"statuses {summary.by_status or '{}'}"
    )
    for line in summary.errors[:10]:
        typer.echo(f"  error: {line}", err=True)
    if summary.stopped:
        typer.echo(f"stopped early: {summary.stopped}", err=True)
        raise typer.Exit(code=EXIT_BUDGET if "budget" in summary.stopped else 1)
    if summary.errors or any(s != "ok" for s in summary.by_status):
        raise typer.Exit(code=1)


@compare_app.command("status")
def status_cmd() -> None:
    """Results so far per config, and what this run and this month have cost."""
    cfg = load_config()
    p = _load(cfg)
    typer.echo(f"run {p.run_id} in {cmp.run_dir(cfg, p.run_id)}")
    typer.echo(
        f"{'config':<16}"
        + "".join(f"{s:>11}" for s in ("ok", "truncated", "refusal", "invalid", "error", "missing"))
    )
    for spec in p.matrix:
        counts = dict.fromkeys(("ok", "truncated", "refusal", "invalid", "error", "missing"), 0)
        for ep in p.sample:
            r = cmp.load_result(cmp.result_path(cfg, p.run_id, ep, spec.id))
            counts[r["status"] if r and r["status"] in counts else "missing"] += 1
        typer.echo(f"{spec.id:<16}" + "".join(f"{counts[k]:>11}" for k in counts))
    with state.connect(cfg.state_db) as conn:
        typer.echo(f"run spend ${state.run_spend_usd(conn, p.run_id):.2f}; " + _month_line(cfg))


@compare_app.command("report")
def report_cmd(
    web_base: Annotated[
        str | None, typer.Option("--web-base", help="Base URL of `ciw web` for transcript links.")
    ] = None,
) -> None:
    """Write the blind review page (and key.json) for the episodes that are complete."""
    from ciw.compare_report import build_review

    cfg = load_config()
    p = _load(cfg)
    base = web_base or f"http://{cfg.web.host}:{cfg.web.port}"
    info = build_review(cfg, p, web_base=base)
    typer.echo(
        f"review page: {info.path}  ({len(info.complete)} episodes, {len(info.pending)} pending)"
    )
    typer.echo("open it in a browser, rate, 'Export ratings.json', then `ciw compare score FILE`")


@compare_app.command("score")
def score_cmd(
    ratings: Annotated[Path, typer.Argument(help="ratings.json exported from the review page")],
) -> None:
    """Unblind the ratings and write summary.md / summary.json next to the results."""
    from ciw.compare_report import summarize, summary_markdown

    cfg = load_config()
    p = _load(cfg)
    try:
        data = json.loads(ratings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _fail(f"cannot read {ratings}: {exc}") from None
    with state.connect(cfg.state_db) as conn:
        backlog = sum(state.count_episodes(conn).values())
    try:
        s = summarize(cfg, p, data, backlog_episodes=backlog or 696)
    except ValueError as exc:
        raise _fail(str(exc)) from None
    out = cmp.run_dir(cfg, p.run_id)
    (out / "summary.json").write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    md = summary_markdown(s)
    (out / "summary.md").write_text(md, encoding="utf-8")
    sys.stdout.write(md)
