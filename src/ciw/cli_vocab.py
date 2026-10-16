"""``ciw vocab``: build the jargon vocabulary, correct transcripts against it, check quality."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ciw.config import load_config

vocab_app = typer.Typer(
    help="Jargon vocabulary: build candidates, correct transcripts, check term recall.",
    no_args_is_help=True,
)


@vocab_app.command("build")
def build(
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Where candidates.csv goes (default: <root>/vocab)."),
    ] = None,
    blog: Annotated[
        bool, typer.Option("--blog/--no-blog", help="Also mine the blog archive (read-only).")
    ] = True,
    top: Annotated[int, typer.Option("--top", min=1, help="Rows kept in candidates.csv.")] = 2500,
    show: Annotated[int, typer.Option("--show", min=0, help="Print the top N candidates.")] = 25,
) -> None:
    """Mine candidate jargon from episode show notes (and the blog) into vocab/candidates.csv.

    No LLM and no network. vocabulary.txt is curated by hand from the candidates and is never
    overwritten by this command.
    """
    from ciw import vocab

    cfg = load_config()
    out_dir = (out_dir or cfg.root / "vocab").expanduser().resolve()
    docs = vocab.episode_docs(cfg.episodes_dir)
    n_episodes = len(docs)
    if blog:
        docs += vocab.blog_docs(cfg.blog_db)
    if not docs:
        typer.echo(
            f"error: no show notes found under {cfg.episodes_dir} (run `ciw poll`)", err=True
        )
        raise typer.Exit(code=1)
    seeds = vocab.read_terms(out_dir / "seeds.txt")
    candidates = vocab.extract(docs, seeds=seeds)
    kept = candidates[:top]
    vocab.write_candidates_csv(out_dir / "candidates.csv", kept)

    by_source: dict[str, int] = {}
    for d in docs:
        by_source[d.source] = by_source.get(d.source, 0) + 1
    split = ", ".join(f"{k} {v}" for k, v in sorted(by_source.items()))
    typer.echo(
        f"mined {len(docs)} documents ({split}; {n_episodes} episodes), {len(candidates)} "
        f"candidates, {len(kept)} written to {out_dir / 'candidates.csv'}"
    )
    typer.echo(
        f"seeds: {len(seeds)} from seeds.txt; common-English test: wordfreq zipf >= "
        f"{vocab.COMMON_ZIPF:g} (or zipf >= 2 and in /usr/share/dict, "
        f"{'present' if vocab.in_dictionary('the') else 'absent'})"
    )
    for i, cand in enumerate(candidates[:show], 1):
        typer.echo(
            f"{i:>3}  {cand.term:<28} df {cand.doc_freq:>4}  score {cand.score:>7.2f}  "
            f"{';'.join(f'{k}:{v}' for k, v in sorted(cand.sources.items()))}"
        )
    typer.echo(
        f"curate {out_dir / 'vocabulary.txt'} by hand from the candidates (see vocab/README.md)"
    )
