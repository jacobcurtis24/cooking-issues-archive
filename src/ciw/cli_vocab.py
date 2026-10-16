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


# --------------------------------------------------------------------------- correction


def _episode_transcript(cfg, episode_id: str, override: Path | None = None):
    """``(path, Transcript)`` for an episode (or a given file); exits with a message if absent."""
    from pydantic import ValidationError

    from ciw import transcribe as tx
    from ciw.models import Transcript

    path = override.expanduser().resolve() if override else tx.transcript_path(cfg, episode_id)
    if not path.is_file():
        typer.echo(f"error: no transcript for {episode_id} ({path})", err=True)
        raise typer.Exit(code=1)
    try:
        return path, Transcript.load(path)
    except (ValidationError, ValueError, OSError) as exc:
        typer.echo(f"error: cannot read {path}: {type(exc).__name__}", err=True)
        raise typer.Exit(code=1) from None


def _episode_ids(cfg, episodes: list[str] | None, all_: bool) -> list[str]:
    """The ids named on the command line, or every episode that has a transcript.json."""
    ids = list(episodes or [])
    if all_:
        found = sorted(p.parent.name for p in cfg.episodes_dir.glob("*/transcript.json"))
        ids += [i for i in found if i not in ids]
    return ids


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    return (
        f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    )


def _show_corrections(corrections, context=None) -> None:
    for c in corrections:
        typer.echo(
            f"{_fmt_time(c.start):>8}  {c.rule:<12} {c.confidence:.2f}  "
            f"{c.original!r} => {c.corrected!r}"
        )


@vocab_app.command("correct")
def correct_cmd(
    episodes: Annotated[list[str] | None, typer.Argument(help="Episode id(s).")] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Every episode that has a transcript.json.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List the corrections; write nothing.")
    ] = False,
    revert: Annotated[
        bool, typer.Option("--revert", help="Undo all corrections (restore the ASR's words).")
    ] = False,
    min_confidence: Annotated[
        float | None,
        typer.Option("--min-confidence", min=0, max=1, help="Fuzzy bar (default: config)."),
    ] = None,
    transcript: Annotated[
        Path | None,
        typer.Option("--transcript", help="Use this transcript.json (one episode id for notes)."),
    ] = None,
) -> None:
    """Correct jargon in existing transcripts against the vocabulary (no ASR is run).

    Idempotent: earlier corrections are undone first, so changing vocabulary.txt or aliases.txt
    and running this again gives the same result as a fresh pass. Writes atomically.
    """
    from ciw import correct as correct_mod
    from ciw import state
    from ciw import transcribe as tx

    cfg = load_config()
    ids = _episode_ids(cfg, episodes, all_)
    if not ids:
        typer.echo("Nothing selected. Give an EPISODE id, or --all.", err=True)
        raise typer.Exit(code=2)
    if transcript is not None and len(ids) != 1:
        raise typer.BadParameter("--transcript needs exactly one episode id", param_hint="EPISODE")
    vocabulary = correct_mod.Vocabulary.load(cfg.asr.vocabulary_file, cfg.asr.aliases_file)
    if not vocabulary and not revert:
        typer.echo(
            f"error: no vocabulary ({cfg.asr.vocabulary_file}) and no aliases; nothing to apply",
            err=True,
        )
        raise typer.Exit(code=1)
    threshold = cfg.asr.correct_min_confidence if min_confidence is None else min_confidence
    total = 0
    failed = False
    for eid in ids:
        path, tr = _episode_transcript(cfg, eid, transcript)
        notes = tx.show_notes_text(cfg, eid)
        if revert:
            before = len(tr.corrections)
            if not dry_run:
                correct_mod.revert(tr)
            typer.echo(f"{eid}: {'would revert' if dry_run else 'reverted'} {before} corrections")
            changes = []
        else:
            changes = (
                correct_mod.preview(tr, vocabulary, show_notes=notes, min_confidence=threshold)
                if dry_run
                else correct_mod.apply_correction(
                    tr, vocabulary, show_notes=notes, min_confidence=threshold
                )
            )
            typer.echo(
                f"{eid}: {len(changes)} corrections"
                + (" (dry run, nothing written)" if dry_run else "")
            )
            _show_corrections(changes)
        total += len(changes)
        if dry_run:
            continue
        try:
            tr.save(path)
        except OSError as exc:
            typer.echo(f"error: cannot write {path}: {exc}", err=True)
            failed = True
            continue
        if transcript is None and cfg.state_db.is_file():
            with state.connect(cfg.state_db) as conn:
                if state.get_episode(conn, eid) is not None:
                    state.set_correction(
                        conn,
                        eid,
                        correction_hash=tr.correction_hash,
                        corrections=len(tr.corrections),
                    )
    if len(ids) > 1:
        typer.echo(f"{total} corrections in {len(ids)} transcripts")
    if failed:
        raise typer.Exit(code=1)


@vocab_app.command("corrections")
def corrections_cmd(
    episode: Annotated[str, typer.Argument(help="Episode id.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    transcript: Annotated[
        Path | None, typer.Option("--transcript", help="Read this transcript.json instead.")
    ] = None,
) -> None:
    """Show every correction stored in an episode's transcript (the audit list)."""
    cfg = load_config()
    _, tr = _episode_transcript(cfg, episode, transcript)
    if as_json:
        import json

        typer.echo(
            json.dumps(
                {
                    "episode": episode,
                    "correction_hash": tr.correction_hash,
                    "corrections": [
                        c.model_dump(exclude={"original_words"}) for c in tr.corrections
                    ],
                },
                indent=2,
            )
        )
        return
    typer.echo(
        f"{episode}: {len(tr.corrections)} corrections"
        + (f" (vocabulary/rules hash {tr.correction_hash})" if tr.correction_hash else "")
    )
    _show_corrections(tr.corrections)


# ------------------------------------------------------------------------------ quality


@vocab_app.command("quality")
def quality_cmd(
    episodes: Annotated[list[str] | None, typer.Argument(help="Episode id(s).")] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Every episode that has a transcript.json.")
    ] = False,
    aggregate: Annotated[
        bool, typer.Option("--aggregate", help="Add a pooled summary across the episodes.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    transcript: Annotated[
        Path | None,
        typer.Option("--transcript", help="Score this transcript.json (one episode id for notes)."),
    ] = None,
) -> None:
    """Term recall: how many vocabulary terms named in the show notes the transcript contains.

    Reports recall before and after the correction pass, what each missed term was heard as, and
    the ASR runtime and real-time factor.
    """
    import json

    from ciw import quality
    from ciw import transcribe as tx
    from ciw import vocab as vocab_mod

    cfg = load_config()
    ids = _episode_ids(cfg, episodes, all_)
    if not ids:
        typer.echo("Nothing selected. Give an EPISODE id, or --all.", err=True)
        raise typer.Exit(code=2)
    if transcript is not None and len(ids) != 1:
        raise typer.BadParameter("--transcript needs exactly one episode id", param_hint="EPISODE")
    terms = vocab_mod.read_terms(cfg.asr.vocabulary_file)
    if not terms:
        typer.echo(f"error: no vocabulary terms in {cfg.asr.vocabulary_file}", err=True)
        raise typer.Exit(code=1)
    results = []
    for eid in ids:
        _, tr = _episode_transcript(cfg, eid, transcript)
        notes = tx.show_notes_text(cfg, eid)
        if notes is None:
            typer.echo(f"error: no meta.json (show notes) for {eid}", err=True)
            raise typer.Exit(code=1)
        title = notes.split("\n", 1)[0]
        results.append(quality.measure(tr, terms, notes, episode_id=eid, title=title))
    agg = quality.aggregate(results)
    if as_json:
        payload: dict = {"episodes": [r.to_dict() for r in results]}
        if aggregate or len(results) > 1:
            payload["aggregate"] = agg.to_dict()
        typer.echo(json.dumps(payload, indent=2))
        return
    for res in results:
        typer.echo(quality.render(res))
    if aggregate:
        typer.echo(quality.render_aggregate(agg))
