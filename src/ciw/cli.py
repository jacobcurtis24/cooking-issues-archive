"""The ``ciw`` command line interface."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from ciw import __version__
from ciw.config import load_config

app = typer.Typer(
    name="ciw",
    help="Cooking Issues archive site and podcast segment watcher.",
    no_args_is_help=True,
    add_completion=False,
)
blog_app = typer.Typer(
    help="Blog archive tools (scrape, search, stats, encoding fixes).", no_args_is_help=True
)
feeds_app = typer.Typer(help="Podcast feed tools.", no_args_is_help=True)
app.add_typer(blog_app, name="blog")
app.add_typer(feeds_app, name="feeds")

_STUB_CONTEXT = {"allow_extra_args": True, "ignore_unknown_options": True}


def _stub(task: str) -> None:
    typer.echo(f"not implemented yet (task {task})")
    raise typer.Exit(code=2)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"ciw {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    """Cooking Issues archive site and podcast segment watcher."""


# ---------------------------------------------------------------------------- web


@app.command()
def web(
    host: Annotated[
        str | None, typer.Option(help="Bind address (default: config web.host).")
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port (default: config web.port).")] = None,
    debug: Annotated[
        bool, typer.Option("--debug", help="Enable Flask debug mode (default: off).")
    ] = False,
) -> None:
    """Serve the archive website."""
    from ciw.web import create_app

    cfg = load_config()
    flask_app = create_app(cfg)
    flask_app.run(
        host=host or cfg.web.host,
        port=port or cfg.web.port,
        debug=debug or cfg.web.debug,
    )


# ---------------------------------------------------------------------------- blog


@blog_app.command()
def scrape() -> None:
    """Scrape cookingissues.com into the blog database (resumable)."""
    from ciw.blog import scrape as scraper

    scraper.run_scrape(load_config().blog_db)


@blog_app.command()
def search(
    query: Annotated[str, typer.Argument(help="Full-text search query.")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results.")] = 10,
) -> None:
    """Full-text search the blog database from the terminal."""
    from ciw.blog import scrape as scraper

    scraper.run_search(query, limit, load_config().blog_db)


@blog_app.command()
def stats() -> None:
    """Show blog database statistics."""
    from ciw.blog import scrape as scraper

    scraper.run_stats(load_config().blog_db)


@blog_app.command("fix-encoding")
def fix_encoding(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would change without writing.")
    ] = False,
) -> None:
    """Repair cp1252 mojibake in the blog database."""
    from ciw.blog import fix_encoding as fixer

    fixer.run(Path(load_config().blog_db), dry_run)


# ---------------------------------------------------------------------- podcast

SourceOpt = Annotated[
    str, typer.Option("--source", help="acast, hrn, patreon or all.", show_default=True)
]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _sources(value: str) -> tuple[str, ...]:
    value = value.lower()
    if value == "all":
        return ("acast", "hrn", "patreon")
    if value not in ("acast", "hrn", "patreon"):
        raise typer.BadParameter("must be one of acast, hrn, patreon, all", param_hint="--source")
    return (value,)


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


@app.command()
def poll(
    source: SourceOpt = "all",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO.")] = False,
) -> None:
    """Resolve, fetch and merge the feeds, and record episodes in state."""
    from ciw import ingest

    _setup_logging(verbose)
    cfg = load_config()
    sources = _sources(source)
    with ingest.make_client(cfg) as client:
        report = ingest.poll(cfg, client, sources)  # type: ignore[arg-type]

    typer.echo(
        f"{'feed':<9} {'status':<13} {'items':>6} {'new':>5} {'updated':>8} "
        f"{'same':>6} {'non-audio':>10}"
    )
    tot = dict(items=0, new=0, updated=0, unchanged=0, skipped_non_audio=0)
    for rep in report.feeds.values():
        typer.echo(
            f"{rep.name:<9} {rep.status:<13} {rep.items:>6} {rep.new:>5} {rep.updated:>8} "
            f"{rep.unchanged:>6} {rep.skipped_non_audio:>10}"
        )
        for key in tot:
            tot[key] += getattr(rep, key)
    typer.echo(
        f"{'total':<9} {'':<13} {tot['items']:>6} {tot['new']:>5} {tot['updated']:>8} "
        f"{tot['unchanged']:>6} {tot['skipped_non_audio']:>10}"
    )
    typer.echo(
        f"matched Acast<->Patreon: {report.matched}; ambiguous: {len(report.ambiguous)}; "
        f"merged duplicates: {report.absorbed}; conflicts: {report.conflicts}"
    )
    for amb in report.ambiguous:
        typer.echo(f"AMBIGUOUS (left unmatched): {amb.describe()}")
    for rep in report.feeds.values():
        if rep.status == "skipped" and rep.name == "patreon" and rep.error:
            typer.echo(f"note: patreon skipped ({rep.error})")
        elif rep.failed:
            kind = {"auth": "authorisation failed", "empty": "no audio items"}.get(
                rep.status, "error"
            )
            typer.echo(f"WARNING: {rep.name} feed: {kind}: {rep.error}", err=True)
    from ciw import health, state

    with state.connect(cfg.state_db) as conn:
        for line in health.alert_feed_health(cfg, conn, report):
            typer.echo(f"ntfy: {line} sent")
    if report.all_failed:
        raise typer.Exit(code=1)


@feeds_app.command("diff")
def feeds_diff(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO.")] = False,
) -> None:
    """Compare the Patreon feed with the public Acast feed, episode by episode."""
    from ciw import ingest

    _setup_logging(verbose)
    cfg = load_config()
    with ingest.make_client(cfg) as client:
        rep = ingest.diff_feeds(cfg, client)

    typer.echo("Feeds")
    for name in ("acast", "hrn", "patreon"):
        if name in rep.counts:
            lo, hi = rep.ranges.get(name, ("-", "-"))
            typer.echo(f"  {name:<8} {rep.counts[name]:>4} audio items, {lo} .. {hi}")
        else:
            typer.echo(f"  {name:<8} not available")
    if not rep.patreon_configured:
        typer.echo(
            "\nPatreon feed not configured (PATREON_FEED_URL is unset in .env): "
            "no comparison possible."
        )
        return
    if "patreon" not in rep.counts:
        typer.echo(f"\nPatreon feed unavailable: {rep.patreon_error}")
        return
    if "acast" not in rep.counts:
        typer.echo("\nAcast feed unavailable: no comparison possible.")
        return

    typer.echo(f"\nMatched pairs ({len(rep.pairs)})")
    typer.echo(
        f"  {'date':<10} {'acast':>8} {'patreon':>8} {'delta s':>8} {'delta %':>8}"
        "  type (length hint a/p)  title"
    )
    for d in rep.pairs:
        ds, dp = d.delta_s, d.delta_pct
        typer.echo(
            f"  {d.acast.pub_date:%Y-%m-%d} {_fmt_dur(d.acast.duration_s):>8} "
            f"{_fmt_dur(d.patreon.duration_s):>8} "
            f"{'-' if ds is None else f'{ds:+.0f}':>8} {'-' if dp is None else f'{dp:+.1f}':>8}  "
            f"{d.acast.enclosure_type}/{d.patreon.enclosure_type} "
            f"({d.acast.enclosure_length_hint}/{d.patreon.enclosure_length_hint})  {d.acast.title}"
        )
    typer.echo(f"\nPatreon-only posts ({len(rep.patreon_only)})")
    for i in rep.patreon_only:
        typer.echo(f"  {i.pub_date:%Y-%m-%d} {_fmt_dur(i.duration_s):>8}  {i.title}")
    typer.echo(f"\nAcast-only items inside the Patreon date range ({len(rep.acast_only_in_range)})")
    for i in rep.acast_only_in_range:
        typer.echo(f"  {i.pub_date:%Y-%m-%d} {_fmt_dur(i.duration_s):>8}  {i.title}")
    if rep.ambiguous:
        typer.echo(f"\nAmbiguous (left unmatched) ({len(rep.ambiguous)})")
        for amb in rep.ambiguous:
            typer.echo(f"  {amb.describe()}")
    typer.echo("\nSummary")
    typer.echo(
        f"  matched {len(rep.pairs)}, patreon-only {len(rep.patreon_only)}, "
        f"acast-only in range {len(rep.acast_only_in_range)}, ambiguous {len(rep.ambiguous)}, "
        f"non-audio skipped {rep.skipped_non_audio}"
    )
    md, mp = rep.median_delta_s, rep.median_delta_pct
    if md is not None and mp is not None:
        typer.echo(f"  median duration delta (patreon - acast): {md:+.0f} s ({mp:+.1f}%)")
    typer.echo(f"  hint: {rep.hint()}")


@feeds_app.command("check-audio")
def feeds_check_audio(
    source: Annotated[str, typer.Option("--source", help="hrn, acast or patreon.")] = "hrn",
    sample: Annotated[
        int,
        typer.Option("--sample", "-n", min=1, help="Episodes per bucket (newest/middle/oldest)."),
    ] = 3,
    range_only: Annotated[
        bool, typer.Option("--range", help="Skip HEAD; use a 1-byte Range GET (gives total size).")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO.")] = False,
) -> None:
    """Probe sampled enclosures with HEAD (or a 1-byte Range GET). Never downloads audio."""
    from ciw import ingest

    _setup_logging(verbose)
    if source not in ("acast", "hrn", "patreon"):
        raise typer.BadParameter("must be one of acast, hrn, patreon", param_hint="--source")
    cfg = load_config()
    with ingest.make_client(cfg) as client:
        rows = ingest.check_audio(cfg, client, source, sample, range_only=range_only)  # type: ignore[arg-type]
    if not rows:
        typer.echo(f"no {source} episodes in state; run `ciw poll` first")
        raise typer.Exit(code=1)
    typer.echo(
        f"{'bucket':<7} {'status':>6} {'method':<9} {'type':<14} {'bytes':>11} "
        f"{'redir':>5}  final host / episode"
    )
    ok = 0
    for bucket, episode_id, title, res in rows:
        ok += res.ok
        size = res.content_length if res.content_length is not None else "-"
        typer.echo(
            f"{bucket:<7} {res.status if res.status is not None else 'ERR':>6} {res.method:<9} "
            f"{(res.content_type or '-')[:14]:<14} {size:>11} "
            f"{res.redirects:>5}  {res.final_host or res.error or '-'}  [{episode_id}] {title[:40]}"
        )
    typer.echo(f"{ok}/{len(rows)} probes resolved to audio ({source})")


episodes_app = typer.Typer(help="Inspect recorded episodes.", no_args_is_help=True)
app.add_typer(episodes_app, name="episodes")


@episodes_app.command("list")
def episodes_list(
    source: Annotated[str | None, typer.Option("--source", help="acast, hrn or patreon.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max rows.")] = 20,
    stage: Annotated[
        str | None, typer.Option("--stage", help="downloaded or discovered (default: all).")
    ] = None,
) -> None:
    """List episodes in state, newest first."""
    from ciw import state

    if stage not in (None, "discovered", "downloaded"):
        raise typer.BadParameter("must be downloaded or discovered", param_hint="--stage")
    cfg = load_config()
    with state.connect(cfg.state_db) as conn:
        rows = state.list_episodes(conn, source, limit, stage)
        counts = state.count_episodes(conn)
    for r in rows:
        typer.echo(
            f"{r['episode_id']:<42} {r['source']:<8} {r['pub_date'][:10]} "
            f"{_fmt_dur(r['duration_s']):>8} {r['stage']:<11} {r['title'][:60]}"
        )
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
    typer.echo(f"({len(rows)} shown; in state: {summary})")


def _fmt_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def _fmt_speed(bps: float) -> str:
    return f"{_fmt_bytes(bps)}/s" if bps else "-"


@app.command()
def download(
    latest: Annotated[
        int | None,
        typer.Option(
            "--latest", min=1, help="The N newest episodes (downloaded ones are skipped)."
        ),
    ] = None,
    episode: Annotated[
        list[str] | None, typer.Option("--episode", help="An episode id (repeatable).")
    ] = None,
    source: Annotated[
        str | None, typer.Option("--source", help="Only acast, hrn or patreon episodes.")
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="Not-yet-downloaded episodes from YYYY-MM-DD on.")
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Every not-yet-downloaded episode (large!).")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Cap the number of episodes.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be fetched; change nothing.")
    ] = False,
    redownload: Annotated[
        bool, typer.Option("--redownload", help="Fetch again and replace the stored file.")
    ] = False,
    verify: Annotated[
        bool, typer.Option("--verify", help="Re-check downloaded files (size, hash, ffprobe).")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO.")] = False,
) -> None:
    """Download episode audio to the audio directory (resumable, verified, sequential)."""
    import sys
    from datetime import date

    from ciw import download as dl
    from ciw import state

    _setup_logging(verbose)
    if source is not None and source not in ("acast", "hrn", "patreon"):
        raise typer.BadParameter("must be one of acast, hrn, patreon", param_hint="--source")
    since_date = None
    if since is not None:
        try:
            since_date = date.fromisoformat(since)
        except ValueError:
            raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--since") from None
    if latest is None and not episode and not all_ and since_date is None:
        typer.echo(
            "Nothing selected. Choose episodes with --latest N, --episode ID, --since YYYY-MM-DD "
            "or --all\n(add --dry-run to preview; --source/--limit narrow the choice).",
            err=True,
        )
        raise typer.Exit(code=2)

    cfg = load_config()
    with state.connect(cfg.state_db) as conn:
        rows, unknown = dl.select_episodes(
            conn,
            latest=latest,
            episode_ids=tuple(episode or ()),
            source=source,
            since=since_date,
            all_=all_,
            limit=limit,
            include_done=redownload or verify,
        )
        for eid in unknown:
            typer.echo(f"unknown episode id (not in state): {eid}", err=True)
        if not rows:
            typer.echo("nothing to do (no matching episodes)")
            raise typer.Exit(code=1 if unknown else 0)
        if dry_run:
            _download_dry_run(cfg, rows, redownload, verify)
            raise typer.Exit(code=1 if unknown else 0)

        tty = sys.stderr.isatty()

        def progress(eid: str, done: int, total: int | None, bps: float) -> None:
            if tty:
                of = f"/{_fmt_bytes(total)}" if total else ""
                typer.echo(
                    f"\r  {eid[:24]:<24} {_fmt_bytes(done)}{of}  {_fmt_speed(bps)}   ",
                    nl=False,
                    err=True,
                )

        def show(row, res) -> None:
            if tty:
                typer.echo("\r" + " " * 70 + "\r", nl=False, err=True)
            label = {"downloaded": "downloaded", "adopted": "adopted", "skipped": "skipped"}.get(
                res.outcome, "FAILED"
            )
            extra = ""
            if res.outcome == "downloaded":
                extra = f" in {res.elapsed_s:.1f}s ({_fmt_speed(res.speed)})"
            detail = (
                res.message
                if res.outcome in ("failed", "adopted") or verify
                else _fmt_bytes(res.size)
            )
            typer.echo(
                f"{label:<10} {row['episode_id']:<30} {row['pub_date'][:10]} "
                f"{_fmt_dur(res.duration_s):>8}  {detail}{extra}  {row['title'][:36]}",
                err=res.outcome == "failed",
            )

        try:
            summary = dl.download_batch(
                cfg,
                conn,
                rows,
                force=redownload,
                verify=verify,
                progress=progress,
                on_result=show,
            )
        except dl.DownloadLocked as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=3) from None

    moved = sum(r.fetched for r in summary.results)
    secs = sum(r.elapsed_s for r in summary.results if r.fetched)
    failed = summary.failed + len(unknown)
    if not (summary.count("downloaded") or summary.count("adopted") or failed):
        typer.echo(f"nothing to do ({summary.count('skipped')} already downloaded)")
        return
    typer.echo(
        f"downloaded {summary.count('downloaded')}, adopted {summary.count('adopted')}, "
        f"skipped {summary.count('skipped')}, failed {failed}; "
        f"transferred {_fmt_bytes(moved)}"
        + (f" in {secs:.1f}s ({_fmt_speed(moved / secs)})" if moved and secs else "")
    )
    if failed:
        raise typer.Exit(code=1)


def _download_dry_run(cfg, rows, redownload: bool, verify: bool) -> None:
    import shutil

    from ciw import download as dl
    from ciw.ingest import probe_audio

    probe = len(rows) <= 10
    client = dl.make_client(cfg) if probe else None
    total = 0
    try:
        for row in rows:
            plan, meta = dl.plan_episode(cfg, row, force=redownload, verify=verify)
            size = "-"
            if meta is not None and plan.action in ("download", "redownload"):
                hint = meta.enclosure_length_hint
                res = probe_audio(client, meta.enclosure_url, range_only=True) if client else None
                if res is not None and res.ok and res.content_length:
                    size, total = _fmt_bytes(res.content_length), total + res.content_length
                elif hint:
                    size, total = f"~{_fmt_bytes(hint)} (feed hint)", total + hint
            typer.echo(
                f"would {plan.action:<10} {row['episode_id']:<30} {row['pub_date'][:10]} "
                f"{row['source']:<6} {size:>22}  {row['title'][:44]}"
                + (f"  [{plan.detail}]" if plan.detail else "")
            )
    finally:
        if client:
            client.close()
    probe_dir = cfg.audio_dir
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    free = shutil.disk_usage(probe_dir).free
    typer.echo(
        f"{len(rows)} episode(s), about {_fmt_bytes(total)} to fetch; free on {cfg.audio_dir}: "
        f"{free / 1024**3:.1f} GiB (min_free_gb {cfg.download.min_free_gb:g})"
    )


@app.command(context_settings=_STUB_CONTEXT)
def transcribe() -> None:
    """Transcribe episodes with WhisperX. (stub)"""
    _stub("T5")


@app.command(context_settings=_STUB_CONTEXT)
def vocab() -> None:
    """Build the jargon vocabulary from show notes. (stub)"""
    _stub("T6")


@app.command(context_settings=_STUB_CONTEXT)
def score() -> None:
    """Segment and score an episode with Claude. (stub)"""
    _stub("T9")


@app.command("eval", context_settings=_STUB_CONTEXT)
def eval_() -> None:
    """Evaluate scoring against the gold set. (stub)"""
    _stub("T11")


@app.command(context_settings=_STUB_CONTEXT)
def notify() -> None:
    """Send notifications via ntfy. (stub)"""
    _stub("T10")


@app.command(context_settings=_STUB_CONTEXT)
def run() -> None:
    """Run the whole pipeline unattended. (stub)"""
    _stub("T12")


if __name__ == "__main__":
    app()
