"""The ``ciw`` command line interface."""

from __future__ import annotations

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


# ------------------------------------------------------------- podcast (stubs)


@app.command(context_settings=_STUB_CONTEXT)
def poll() -> None:
    """Poll feeds and record new episodes. (stub)"""
    _stub("T3")


@feeds_app.command("diff", context_settings=_STUB_CONTEXT)
def feeds_diff() -> None:
    """Compare the Patreon and public feeds. (stub)"""
    _stub("T3")


@app.command(context_settings=_STUB_CONTEXT)
def download() -> None:
    """Download episode audio. (stub)"""
    _stub("T4")


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
