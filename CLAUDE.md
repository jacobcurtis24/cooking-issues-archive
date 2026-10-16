# CLAUDE.md

Flask site serving a scraped 2009-2013 Cooking Issues blog archive (`cooking_issues.db`, tracked in git), being extended with a podcast pipeline (RSS ingest, WhisperX transcripts, Claude segment/score, ntfy + review pages). Package: `src/ciw/` (`web/`, `blog/`, `cli.py`, `config.py`, `models.py`). Stack: Python 3.11+, Flask, typer, pydantic v2, pyyaml, pytest, ruff.

## Environment

- Use the conda env `py312` by absolute path: `/home/jacob/miniconda3/envs/py312/bin/python` (install with `pip install -e .`). Do not touch other envs.
- Run `python -m pytest` and `ruff check .` (plus `ruff format --check .`) before every commit.
- One milestone per branch. Do not push or merge unless asked.

## Rules

- Never commit `data/`, `.env`, `*.sqlite`, or audio. Secrets only in `.env`; never log them (use `ciw.config.redact`).
- `cooking_issues.db` is tracked and read-only for tests; a session-wide test fails if it changes.
- Audio and clips live on `/mnt/ssd0/ciw/` (see `config.yaml`), transcripts/state in repo `data/` (git-ignored).
- Progress log: `docs/progress.md` (append a dated entry per task). Plan: `Cooking Issues Segment Watcher — Plan.md` (untracked; do not commit it).
- Known oddities in the blog code (e.g. `/browse?page=abc` 500, `/search?q=!!!` error, stray `Â` in text, empty body of post 25) are intentionally unfixed until scheduled; tests pin them. See docs/progress.md (T1).
