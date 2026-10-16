"""``ciw backfill``: plan, run, monitor, stop and retry the download + transcribe queue."""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Annotated

import typer

from ciw import state
from ciw.config import Config, load_config

backfill_app = typer.Typer(
    help="Resumable backfill: download and transcribe every episode that still needs it.",
    no_args_is_help=True,
)

CONFIRM_ABOVE = 10  # `run` needs --yes for more episodes than this

SourceOpt = Annotated[
    str | None,
    typer.Option(
        "--source",
        help="acast, hrn, patreon or all (default: backfill.order from config.yaml, "
        "i.e. acast then hrn).",
    ),
]
SinceOpt = Annotated[str | None, typer.Option("--since", help="Only episodes on/after YYYY-MM-DD.")]
UntilOpt = Annotated[
    str | None, typer.Option("--until", help="Only episodes on/before YYYY-MM-DD.")
]
LimitOpt = Annotated[
    int | None, typer.Option("--limit", min=1, help="At most N episodes that still need work.")
]
OldestOpt = Annotated[
    bool, typer.Option("--oldest-first", help="Oldest first within a source (default: newest).")
]
RetryOpt = Annotated[
    bool,
    typer.Option(
        "--retry-failed", help="Also try episodes that are cooling down or out of attempts."
    ),
]
StaleOpt = Annotated[
    bool,
    typer.Option("--stale", help="Also redo transcripts made by another model/version/prompt."),
]
DownloadOnlyOpt = Annotated[
    bool, typer.Option("--download-only", help="Only fetch audio (no GPU, no transcription).")
]
TranscribeOnlyOpt = Annotated[
    bool, typer.Option("--transcribe-only", help="Only transcribe episodes whose audio is on disk.")
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO.")]


def _date(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=name) from None


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _selection(
    cfg: Config,
    source: str | None,
    since: str | None,
    until: str | None,
    limit: int | None,
    oldest_first: bool,
    retry_failed: bool,
    stale: bool,
    download_only: bool,
    transcribe_only: bool,
):
    from ciw.backfill import SOURCES, Selection

    if download_only and transcribe_only:
        raise typer.BadParameter(
            "--download-only and --transcribe-only exclude each other",
            param_hint="--download-only/--transcribe-only",
        )
    if source is None or source.lower() == "all":
        sources = tuple(cfg.backfill.order)
    elif source.lower() in SOURCES:
        sources = (source.lower(),)
    else:
        raise typer.BadParameter("must be one of acast, hrn, patreon, all", param_hint="--source")
    return Selection(
        sources=sources,
        since=_date(since, "--since"),
        until=_date(until, "--until"),
        limit=limit,
        oldest_first=oldest_first,
        retry_failed=retry_failed,
        stale=stale,
        mode="download-only" if download_only else "transcribe-only" if transcribe_only else "all",
    )


def _transcriber(cfg: Config):
    from ciw.asr import get_transcriber

    try:
        return get_transcriber(cfg)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _make_plan(cfg: Config, selection):
    from ciw.backfill import plan_backfill

    tr = _transcriber(cfg)
    with state.connect(cfg.state_db) as conn:
        return plan_backfill(cfg, conn, selection, tr), tr


@backfill_app.command("plan")
def plan_cmd(
    source: SourceOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    limit: LimitOpt = None,
    oldest_first: OldestOpt = False,
    retry_failed: RetryOpt = False,
    stale: StaleOpt = False,
    download_only: DownloadOnlyOpt = False,
    transcribe_only: TranscribeOnlyOpt = False,
    first: Annotated[int, typer.Option("--first", min=0, help="Episodes to list.")] = 8,
) -> None:
    """Show what a run would do (counts, GB, GPU hours, disk). No side effects, no network."""
    from ciw.backfill import format_plan

    cfg = load_config()
    selection = _selection(
        cfg, source, since, until, limit, oldest_first, retry_failed, stale, download_only,
        transcribe_only,
    )  # fmt: skip
    plan, _ = _make_plan(cfg, selection)
    typer.echo(format_plan(plan, show_first=first))


@backfill_app.command("run")
def run_cmd(
    source: SourceOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    limit: LimitOpt = None,
    oldest_first: OldestOpt = False,
    retry_failed: RetryOpt = False,
    stale: StaleOpt = False,
    download_only: DownloadOnlyOpt = False,
    transcribe_only: TranscribeOnlyOpt = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help=f"Confirm: needed for more than {CONFIRM_ABOVE} episodes or "
            "when no selector is given.",
        ),
    ] = False,  # fmt: skip
    max_hours: Annotated[
        float | None,
        typer.Option("--max-hours", min=0.01, help="Start no new work after this many hours."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the plan and exit.")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Download and transcribe the planned episodes; Ctrl-C/SIGTERM or `ciw backfill stop` ends
    the run after the current episode, and running it again continues where it stopped."""
    from ciw import backfill as bf

    started = time.time()
    _setup_logging(verbose)
    cfg = load_config()
    selection = _selection(
        cfg, source, since, until, limit, oldest_first, retry_failed, stale, download_only,
        transcribe_only,
    )  # fmt: skip
    has_selector = any(x is not None for x in (source, since, until, limit))
    plan, tr = _make_plan(cfg, selection)
    typer.echo(bf.format_plan(plan))
    if not plan.items and not (plan.index_only and selection.mode != "download-only"):
        typer.echo("nothing to do")
        return
    if dry_run:
        typer.echo("(dry run: nothing started)")
        return
    if not yes and (len(plan.items) > CONFIRM_ABOVE or not has_selector):
        why = (
            "no selector (--source/--since/--until/--limit) was given"
            if not has_selector
            else f"{len(plan.items)} episodes are selected (more than {CONFIRM_ABOVE})"
        )
        typer.echo(f"\nRefusing to start: {why}. Review the plan above and add --yes.", err=True)
        raise typer.Exit(code=bf.EXIT_USAGE)
    if plan.disk and not plan.disk.ok:
        typer.echo(
            "warning: the estimated downloads do not fit with the free-space floor; the run "
            "will stop cleanly when the disk guard trips",
            err=True,
        )
    try:
        summary, _ = bf.run_backfill(
            cfg,
            plan,
            transcriber=None if selection.mode == "download-only" else tr,
            echo=typer.echo,
            max_hours=max_hours,
            started_at=started,
        )
    except bf.BackfillLocked as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=bf.EXIT_LOCKED) from None
    raise typer.Exit(code=summary.exit_code)


@backfill_app.command("status")
def status_cmd() -> None:
    """Counts per source and stage, failures, suspect episodes, ETA, disk and log usage."""
    from ciw import backfill as bf

    cfg = load_config()
    tr = _transcriber(cfg)
    with state.connect(cfg.state_db) as conn:
        bf.fill_gap_stats(cfg, conn)  # transcripts from before the gap statistics existed
        st = bf.gather_status(cfg, conn, tr)
    typer.echo(bf.format_status(cfg, st))


@backfill_app.command("stop")
def stop_cmd() -> None:
    """Ask a running backfill to finish the current episode and exit (writes the STOP file)."""
    from ciw import backfill as bf

    cfg = load_config()
    path = bf.request_stop(cfg)
    running = bf.lock_holder(cfg.data_dir / "transcribe.lock") or bf.lock_holder(
        cfg.data_dir / "download.lock"
    )
    typer.echo(
        f"stop requested ({path})."
        + (
            f" The run (pid {running}) exits after its current episode."
            if running
            else " No run is active; the next `ciw backfill run` removes this stale file first."
        )
    )


@backfill_app.command("retry")
def retry_cmd(
    episode: Annotated[
        list[str] | None, typer.Option("--episode", help="Only this episode id (repeatable).")
    ] = None,
) -> None:
    """Clear cool-downs and attempt counts of failed episodes so the next run tries them again."""
    from ciw import backfill as bf

    cfg = load_config()
    with state.connect(cfg.state_db) as conn:
        n = bf.retry_failed(conn, list(episode) if episode else None)
    typer.echo(f"reset {n} failed episode(s); the next run will retry them")
