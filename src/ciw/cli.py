"""The ``ciw`` command line interface."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Annotated

import typer

from ciw import __version__, state
from ciw.cli_backfill import backfill_app
from ciw.cli_vocab import vocab_app
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
app.add_typer(vocab_app, name="vocab")
app.add_typer(backfill_app, name="backfill")

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
    from ciw import health

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
        str | None,
        typer.Option("--stage", help=f"{', '.join(state.STAGES)} (default: all)."),
    ] = None,
    stale: Annotated[
        bool,
        typer.Option("--stale", help="Only transcripts whose vocabulary or corrections are stale."),
    ] = False,
) -> None:
    """List episodes in state, newest first."""
    if stage is not None and stage not in state.STAGES:
        raise typer.BadParameter(f"must be one of {', '.join(state.STAGES)}", param_hint="--stage")
    cfg = load_config()
    with state.connect(cfg.state_db) as conn:
        rows = state.list_episodes(conn, source, None if stale else limit, stage)
        counts = state.count_episodes(conn)
    from ciw import correct as correct_mod
    from ciw import vocab as vocab_mod

    now_prompt = vocab_mod.configured_prompt_hash(cfg.asr)
    now_correction = None
    if cfg.asr.correct:
        vocabulary = correct_mod.Vocabulary.load(cfg.asr.vocabulary_file, cfg.asr.aliases_file)
        now_correction = vocabulary.hash if vocabulary else None
    shown = []
    for r in rows:
        flag = _vocab_flag(r, now_prompt, now_correction)
        if stale and flag not in ("STALE", "recorrect"):
            continue
        if len(shown) >= limit:
            break
        shown.append((r, flag))
        typer.echo(
            f"{r['episode_id']:<42} {r['source']:<8} {r['pub_date'][:10]} "
            f"{_fmt_dur(r['duration_s']):>8} {r['stage']:<11} {flag:<9} {r['title'][:50]}"
        )
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
    typer.echo(f"({len(shown)} shown; in state: {summary})")


def _vocab_flag(row, now_prompt: str | None, now_correction: str | None) -> str:
    """Vocabulary status of a stored transcript: - (none), ok, STALE (made with another Whisper
    prompt: redo with `ciw transcribe --stale`) or recorrect (`ciw vocab correct`)."""
    if row["stage"] in ("discovered", "downloaded") or not row["asr_model"]:
        return "-"
    if row["vocab_hash"] != now_prompt:
        return "STALE"
    if row["correction_hash"] != now_correction:
        return "recorrect"
    return "ok"


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


def _fmt_mmss(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return (
        f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    )


def _transcribe_detail(res) -> str:
    """The numbers after a finished transcript: speed, words, speakers, per-stage times."""
    t = res.transcript
    if t is None:
        return res.message
    audio = t.audio_duration_s or t.duration_s
    run = t.runtime_s or res.elapsed_s
    speed = f"{audio / run:.1f}x realtime" if run else "-"
    speakers = f"{t.num_speakers} speakers" if t.diarization else "no speakers"
    stages = []
    for st in t.stages:
        if st.skipped:
            stages.append(f"{st.name} skipped")
        else:
            vram = f" +{st.peak_vram_mb / 1024:.1f}G" if st.peak_vram_mb is not None else ""
            stages.append(f"{st.name} {st.seconds:.0f}s{vram}")
    return (
        f"{_fmt_mmss(audio)} audio in {_fmt_mmss(run)} ({speed}), {len(t.words)} words, "
        f"{speakers}, {t.compute_type} batch {t.batch_size} | " + ", ".join(stages)
    )


def _print_quality(cfg, episode_ids: list[str]) -> None:
    """``ciw transcribe --quality``: term recall for each selected episode that has a transcript."""
    from ciw import quality as q
    from ciw import transcribe as tx
    from ciw import vocab as vocab_mod
    from ciw.models import Transcript

    terms = vocab_mod.read_terms(cfg.asr.vocabulary_file)
    results = []
    for eid in episode_ids:
        path = tx.transcript_path(cfg, eid)
        notes = tx.show_notes_text(cfg, eid)
        if terms and notes is not None and path.is_file():
            results.append(
                q.measure(Transcript.load(path), terms, notes, episode_id=eid,
                          title=notes.split("\n", 1)[0])
            )  # fmt: skip
    if not results:
        typer.echo("quality: no transcribed episodes with show notes and a vocabulary")
        return
    for res in results:
        typer.echo(q.render(res))
    if len(results) > 1:
        typer.echo(q.render_aggregate(q.aggregate(results)))


@app.command()
def transcribe(
    episode: Annotated[
        list[str] | None, typer.Option("--episode", help="An episode id (repeatable).")
    ] = None,
    latest: Annotated[
        int | None,
        typer.Option("--latest", min=1, help="The N newest downloaded episodes."),
    ] = None,
    all_downloaded: Annotated[
        bool,
        typer.Option("--all-downloaded", help="Every downloaded episode (hours of GPU time!)."),
    ] = False,
    audio: Annotated[
        Path | None,
        typer.Option("--audio", help="Transcribe this file instead (needs --out; no state)."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Where --audio writes its transcript.json.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="ASR model (default: config asr.model).")
    ] = None,
    no_diarize: Annotated[
        bool, typer.Option("--no-diarize", help="Skip speaker labels (no HF token needed).")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Redo even if the transcript is current.")
    ] = False,
    stale: Annotated[
        bool,
        typer.Option(
            "--stale",
            help="Also redo transcripts made by another model, version or vocabulary prompt.",
        ),
    ] = False,
    no_correct: Annotated[
        bool,
        typer.Option("--no-correct", help="Skip the post-ASR vocabulary correction pass."),
    ] = False,
    quality: Annotated[
        bool,
        typer.Option("--quality", help="Afterwards print term recall for the selected episodes."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would run; load nothing, change nothing.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO.")] = False,
) -> None:
    """Transcribe downloaded episodes with WhisperX (local GPU), one at a time."""
    import sys

    from ciw import transcribe as tx
    from ciw.asr import get_transcriber
    from ciw.asr.base import SKIP_DISABLED, diarization_hint

    _setup_logging(verbose)
    if (audio is None) != (out is None):
        raise typer.BadParameter("--audio and --out go together", param_hint="--audio/--out")
    if audio is not None and (episode or latest is not None or all_downloaded):
        raise typer.BadParameter("--audio cannot be combined with episode selectors")
    if audio is None and not episode and latest is None and not all_downloaded:
        typer.echo(
            "Nothing selected. Choose episodes with --episode ID, --latest N or --all-downloaded,\n"
            "or a file with --audio PATH --out PATH (add --dry-run to preview).",
            err=True,
        )
        raise typer.Exit(code=2)

    cfg = load_config()
    correction = tx.load_correction(cfg, enabled=not no_correct)
    try:
        tr = get_transcriber(cfg, model=model, diarize=False if no_diarize else None)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"backend: {tr.describe()} [{tr.version}]")
    typer.echo(
        "vocabulary: "
        + (
            f"whisper prompt {tr.vocab_hash()}"
            if tr.vocab_hash()
            else "no whisper prompt (asr.vocabulary_prompt is off)"
            if cfg.asr.vocabulary_file
            else "none"
        )
        + "; correction "
        + (
            f"on ({len(correction.vocabulary.terms)} terms, "
            f"{len(correction.vocabulary.aliases)} aliases)"
            if correction
            else "off"
        )
    )

    tty = sys.stderr.isatty()
    last_tick: dict[str, tuple[str, int]] = {}

    def progress(eid: str, stage: str, frac: float | None) -> None:
        """A live ``stage NN%`` line, only on a TTY and only when the 5% step changes."""
        tick = (stage, -1 if frac is None else int(frac * 20))
        if tty and last_tick.get(eid) != tick:
            last_tick[eid] = tick
            pct = "" if frac is None else f" {frac * 100:3.0f}%"
            typer.echo(f"\r  {eid[:24]:<24} {stage:<8}{pct}   ", nl=False, err=True)

    hints: set[str] = set()

    def show(label: str, name: str, res, extra: str = "") -> None:
        if tty:
            typer.echo("\r" + " " * 60 + "\r", nl=False, err=True)
        detail = _transcribe_detail(res) if res.outcome == "transcribed" else res.message
        if res.transcript is not None and res.transcript.correction_hash:
            detail += f"; {len(res.transcript.corrections)} corrections"
        typer.echo(
            f"{label:<12} {name:<30} {extra}{detail}",
            err=res.outcome in ("failed", "blocked"),
        )
        t = res.transcript
        if t is not None and t.diarization_skipped and t.diarization_skipped != SKIP_DISABLED:
            hints.add(diarization_hint(t.diarization_skipped, cfg.asr.diarization_model))

    labels = {
        "transcribed": "transcribed",
        "skipped": "skipped",
        "stale": "STALE",
        "blocked": "BLOCKED",
        "failed": "FAILED",
    }
    started = time.perf_counter()

    if audio is not None:
        assert out is not None
        audio, out = audio.expanduser().resolve(), out.expanduser().resolve()
        if not audio.is_file():
            typer.echo(f"error: no such audio file: {audio}", err=True)
            raise typer.Exit(code=1)
        if dry_run:
            plan = tx.plan_for_output(out, tr, force=force, stale=stale)
            typer.echo(
                f"would {plan.action:<12} {audio} -> {out}"
                + (f"  [{plan.detail}]" if plan.detail else "")
            )
            return
        try:
            with tx.transcribe_lock(cfg):
                res = tx.transcribe_file(
                    audio, out, tr, force=force, stale=stale, correction=correction,
                    progress=lambda stage, frac: progress("file", stage, frac),
                )  # fmt: skip
        except tx.TranscribeLocked as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=3) from None
        show(
            labels[res.outcome],
            audio.name,
            res,
            f"-> {out}  " if res.outcome == "transcribed" else "",
        )
        for hint in sorted(hints):
            typer.echo(hint, err=True)
        if res.outcome == "failed":
            raise typer.Exit(code=1)
        return

    with state.connect(cfg.state_db) as conn:
        rows, unknown = tx.select_episodes(
            conn, latest=latest, episode_ids=tuple(episode or ()), all_downloaded=all_downloaded
        )
        for eid in unknown:
            typer.echo(f"unknown episode id (not in state): {eid}", err=True)
        if not rows:
            typer.echo("nothing to do (no matching episodes)")
            raise typer.Exit(code=1 if unknown else 0)
        if dry_run:
            for row in rows:
                plan = tx.plan_episode(cfg, row, tr, force=force, stale=stale)
                typer.echo(
                    f"would {plan.action:<12} {row['episode_id']:<30} {row['pub_date'][:10]} "
                    f"{_fmt_dur(row['audio_duration_s']):>8}  {row['title'][:40]}"
                    + (f"  [{plan.detail}]" if plan.detail else "")
                )
            raise typer.Exit(code=1 if unknown else 0)
        try:
            summary = tx.transcribe_batch(
                cfg, conn, rows, tr, force=force, stale=stale, progress=progress,
                correction=correction,
                on_result=lambda row, res: show(
                    labels[res.outcome], row["episode_id"], res, f"{row['pub_date'][:10]}  "
                ),
            )  # fmt: skip
        except tx.TranscribeLocked as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=3) from None

    for hint in sorted(hints):
        typer.echo(hint, err=True)
    if done_ids := [r.episode_id for r in summary.results if r.outcome == "transcribed"]:
        from ciw import search_index as si

        si.index_best_effort(cfg, done_ids, echo=typer.echo)
    if quality:
        _print_quality(cfg, [r["episode_id"] for r in rows])
    failed = summary.failed + len(unknown)
    done = [r for r in summary.results if r.outcome == "transcribed" and r.transcript]
    audio_s = sum(r.transcript.audio_duration_s or 0 for r in done)
    wall = time.perf_counter() - started
    typer.echo(
        f"transcribed {summary.count('transcribed')}, skipped {summary.count('skipped')}, "
        f"stale {summary.count('stale')}, failed {failed}"
        + (
            f"; {_fmt_mmss(audio_s)} of audio in {_fmt_mmss(wall)} ({audio_s / wall:.1f}x realtime)"
            if done and wall
            else ""
        )
    )
    if failed:
        raise typer.Exit(code=1)


@app.command("index")
def index_cmd(
    episode: Annotated[
        list[str] | None, typer.Option("--episode", help="Only this episode id (repeatable).")
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Every episode (the default; unchanged ones are skipped).")
    ] = False,
    rebuild: Annotated[
        bool,
        typer.Option("--rebuild", help="Throw the index away and build it again from scratch."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log at INFO.")] = False,
) -> None:
    """Build or update the transcript + show-notes search index (`search_db`).

    Incremental: an episode is re-indexed only when its transcript.json or show notes changed,
    and rows of transcripts that were deleted are dropped. `ciw transcribe` and
    `ciw vocab correct` call this for the episodes they change.
    """
    from ciw import search_index as si

    _setup_logging(verbose)
    if all_ and episode:
        raise typer.BadParameter("--all and --episode are exclusive", param_hint="--all/--episode")
    cfg = load_config()
    if not cfg.state_db.is_file():
        typer.echo(f"error: no state database at {cfg.state_db}; run `ciw poll` first", err=True)
        raise typer.Exit(code=1)
    if rebuild and episode:
        raise typer.BadParameter("--rebuild rebuilds everything", param_hint="--rebuild")
    try:
        if rebuild:
            report = si.rebuild_index(cfg)
        else:
            report = si.update_index(cfg, episode_ids=episode or None, force=bool(episode))
    except Exception as exc:  # noqa: BLE001 - a clear message beats a traceback here
        typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"{'rebuilt' if rebuild else 'updated'} {cfg.search_db}")
    typer.echo(report.summary())
    for err in report.errors:
        typer.echo(f"  FAILED {err}", err=True)
    if cfg.search_db.is_file():
        typer.echo(f"index size: {cfg.search_db.stat().st_size / 1024 / 1024:.1f} MiB")
    if report.errors:
        raise typer.Exit(code=1)


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
