# Cooking Issues Archive

A local archive of [cookingissues.com](https://cookingissues.com) — 216 blog posts and 8 technical primers by Dave Arnold, stored in a searchable SQLite database with a web viewer.

A podcast pipeline (RSS ingest, WhisperX transcripts, Claude segment scoring, ntfy alerts, review pages) is being added to the same package, `ciw`. It is in progress: ingest, download and transcription work; the scoring/notify commands are still stubs. The design is in `Cooking Issues Segment Watcher — Plan.md` (PLAN) and progress is logged in [docs/progress.md](docs/progress.md).

## Requirements

- Python 3.11+ (developed in a conda env `py312`, Python 3.12)

## Install

```bash
pip install -e .          # add ".[dev]" for pytest and ruff
```

This installs the `ciw` command (also available as `python -m ciw`).

## Quick start

The database is already populated. Just start the viewer:

```bash
ciw web
```

Then open **http://localhost:5000**. Options: `--host`, `--port`, `--debug` (off by default). Defaults come from `config.yaml`.

---

## Web viewer

| Page | URL | Description |
|---|---|---|
| Home | `/` | Search bar, primers, 15 recent posts |
| Primers | `/primers` | All 8 technical reference guides |
| Browse | `/browse` | Full archive, 24 per page, filterable by tag |
| Post | `/post/<id>` | Full post with prev/next navigation |
| Search | `/search?q=…` | Full-text search with highlighted excerpts |

## CLI search

Search without starting the server:

```bash
ciw blog search "agar clarification"
ciw blog search "sous vide chicken" --limit 20
ciw blog stats
```

---

## Re-scraping

The scraper is resumable — already-scraped posts are skipped based on URL. Safe to re-run at any time:

```bash
ciw blog scrape
```

To scrape from scratch, delete `cooking_issues.db` first.

## Verifying the database

Fetches a sample of posts from the live site and compares them to the local database:

```bash
python -m ciw.blog.compare
```

## Fixing encoding (if needed)

If the database develops encoding artifacts (`â€™` instead of `'`), run:

```bash
ciw blog fix-encoding             # apply fixes
ciw blog fix-encoding --dry-run   # preview without changing anything
```

This is applied automatically during scraping, so it's only needed if you're patching an existing database.

## Podcast feeds

```bash
ciw poll                          # resolve + fetch Acast, HRN (and Patreon if configured), merge, record
ciw poll --source hrn             # one feed (acast | hrn | patreon | all)
ciw feeds diff                    # Patreon vs Acast, per episode (needs PATREON_FEED_URL in .env)
ciw feeds check-audio --source hrn --sample 3 [--range]   # HEAD / 1-byte Range probes, never downloads
ciw episodes list --source acast --limit 10 [--stage downloaded|discovered]
ciw download --latest 3 [--source acast|hrn|patreon]      # the 3 newest (already-downloaded ones are skipped)
ciw download --episode ID [--episode ID ...]              # specific episodes
ciw download --since 2026-01-01 | --all [--limit N]       # bulk (not-yet-downloaded only); one of these is required
ciw download --latest 3 --dry-run                         # preview: size from a Range probe, free disk
ciw download --episode ID --redownload | --verify         # replace the stored file | re-check it
```

`ciw download` streams to `<audio_dir>/<episode_id>.<ext>.part`, resumes after interruptions, checks the size against the server's own total and the duration with ffprobe, then renames atomically. A file that already exists but is not in state (the T0 fixture) is adopted after the same checks, without downloading. It is sequential (lock file `data/download.lock`), at most 2 requests/s, and refuses to start when `audio_dir`'s disk has less than `download.min_free_gb` free. Feed problems seen by `ciw poll` (401/403 or zero audio items) send one ntfy alert per status change when `NTFY_TOPIC` is set; otherwise they are only logged.

Episodes land in `data/state.sqlite` and `data/episodes/<episode_id>/meta.json` (git-ignored). A second `ciw poll` with unchanged feeds changes nothing.

## Transcription (WhisperX, local GPU)

The ASR stack is an optional extra, not a core dependency (it needs an NVIDIA GPU, about 8 GB of packages and 3.3 GB of models). In the `py312` env:

```bash
pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
pip install -e ".[asr]"
python -c "import torch; print(torch.cuda.is_available())"     # must print True
```

```bash
ciw transcribe --episode ID [--episode ID ...]            # one or more downloaded episodes
ciw transcribe --latest 2 | --all-downloaded              # a selector is required (no accidental backfill)
ciw transcribe --episode ID --dry-run                     # what would run; loads nothing
ciw transcribe --episode ID --force | --stale             # redo a current transcript | one made by another model/version
ciw transcribe --audio some.mp3 --out /tmp/t.json         # any file, no pipeline state touched
ciw transcribe --episode ID --no-diarize --model large-v3
```

Output is `data/episodes/<id>/transcript.json` (compact JSON, atomic write): words with start/end/speaker plus the model, versions, compute type, batch size, per-stage seconds and peak VRAM. `ciw.models.Transcript.render()` gives `[mm:ss] SPEAKER_01: text` lines. Model caches live on `/mnt/ssd0/ciw/hf_cache` (`asr.hf_home`). Speaker labels need a Hugging Face token: create a read token, accept the terms at https://huggingface.co/pyannote/speaker-diarization-community-1 and put `HF_TOKEN=...` in `.env`; without it transcription still runs, with no speakers and a one-line hint. One run at a time (`data/transcribe.lock`). `python -m pytest -m gpu` runs the real-model test on the 60 s excerpt.

### Vocabulary, correction and quality (T6)

Whisper mangles the show's jargon ("Polly Dexter" for polydextrose, "sockslet" for Soxhlet), and the scorer keys on those terms, so the transcripts get a vocabulary prompt and a conservative correction pass. `vocab/vocabulary.txt` is hand-curated (one term per line, most valuable first) and is the knob to edit; see `vocab/README.md`.

```bash
ciw vocab build                                  # mine vocab/candidates.csv from show notes + the blog (no LLM, no network)
ciw vocab correct EPISODE [--dry-run | --revert] # fix an existing transcript.json in place, no ASR re-run (idempotent)
ciw vocab corrections EPISODE [--json]           # the audit list of every change made
ciw vocab quality EPISODE... [--aggregate] [--json]   # term recall before/after correction, misses and what they were heard as, runtime, real-time factor
ciw transcribe --episode ID --no-correct --quality    # skip the correction pass / print recall afterwards
ciw episodes list --stale                        # transcripts made with another vocabulary prompt (STALE) or older rules (recorrect)
```

`asr.vocabulary_file` becomes Whisper's `initial_prompt` (cut at 600 characters); `vocab/aliases.txt` holds explicit `heard => correct` pairs. A transcript made with a different prompt is stale (`ciw transcribe --stale` redoes it).

## Configuration

Non-secret settings live in `config.yaml` (paths, feed IDs, models, web host/port). Relative paths resolve against the project root, so commands work from any directory (override the root with `CIW_ROOT`). Secrets go only in `.env` (git-ignored); copy `.env.example`.

## Development

```bash
python -m pytest
ruff check .
ruff format --check .
```

---

## Files

```
cooking-issues-archive/
├── pyproject.toml         # package `ciw`, console script, ruff/pytest config
├── config.yaml            # settings (secrets stay in .env)
├── interests.yaml         # interest profile for segment scoring
├── .env.example           # secret placeholders
├── cooking_issues.db      # SQLite database (224 posts)
├── CLAUDE.md              # notes for Claude Code sessions
├── README.md              # this file
├── summary.md             # technical decisions and architecture of the blog archive
├── docs/progress.md       # task-by-task progress log
├── tests/                 # pytest suite
└── src/ciw/
    ├── cli.py             # `ciw` command line (typer)
    ├── config.py          # config.yaml + .env loader, redact()
    ├── models.py          # pydantic models for the podcast JSON files
    ├── feeds.py           # Apple lookup, polite HTTP, feed parsing, episode ids
    ├── matching.py        # Acast <-> Patreon matching rules
    ├── ingest.py          # poll / diff / audio-probe orchestration
    ├── state.py           # data/state.sqlite (versioned migrations)
    ├── download.py        # resumable verified audio downloader, selection, lock
    ├── transcribe.py      # transcription orchestration: selection, staleness, lock, state
    ├── transcript.py      # utterance grouping + `[mm:ss] SPEAKER_01: text` renderer
    ├── vocab.py           # mine candidate jargon, read vocabulary/aliases, build the prompt
    ├── correct.py         # conservative post-ASR correction against the vocabulary
    ├── quality.py         # term-recall report; cli_vocab.py holds the `ciw vocab` commands
    ├── asr/               # Transcriber interface + registry, WhisperX backend (lazy torch)
    ├── notify.py          # minimal ntfy sender (T10 extends)
    ├── health.py          # feed health alerts (deduped per status change)
    ├── blog/              # scrape.py, encoding.py, fix_encoding.py, compare.py
    └── web/               # create_app() factory, blog blueprint, templates/
```
