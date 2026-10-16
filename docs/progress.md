# Progress log

Cooking Issues segment watcher (package `ciw`). Newest entries last. Plan: see the project plan; resume from this file plus `git log`.

## 2026-10-15 - T0: env, storage, fixture (branch `m0-setup`)

### What was done
- Deleted stray empty probe file `/mnt/ssd0/.w` (confirmed 0 bytes first).
- Created conda env `py312` (Python 3.12.15) from conda-forge with `ffmpeg` (9.0.2). Other envs (`py310`, `freerouting`, base) untouched.
- pip-installed into `py312`: flask 3.1.3, typer 0.27.3, pydantic 2.14.1, feedparser 6.0.14, httpx 0.28.1, requests 2.34.2, beautifulsoup4 4.15.0, lxml 6.1.3, pyyaml 6.0.3, anthropic 1.15.0, pytest 9.1.1, ruff 0.17.1. No torch/whisperx (that is T5).
- Created `/mnt/ssd0/ciw/audio` and `/mnt/ssd0/ciw/clips`.
- Downloaded the test episode "Biscuits, Bouillon and Beyond" (guid `69715223a6c658f1834fb3d6`) to `/mnt/ssd0/ciw/audio/69715223a6c658f1834fb3d6.mp3`.
- Cut a 60 s excerpt (from 00:10:00, libmp3lame -q:a 4) to `/mnt/ssd0/ciw/audio/test_excerpt_60s.mp3`.
- Wrote `.gitignore` (cooking_issues.db is intentionally NOT ignored).

### Verification (py312 binaries by absolute path, P=/home/jacob/miniconda3/envs/py312/bin)
- `ls /mnt/ssd0/.w` -> No such file or directory.
- `$P/python --version` -> Python 3.12.15
- `$P/ffmpeg -version | head -1` -> ffmpeg version 9.0.2
- `$P/python -c "import flask,typer,pydantic,feedparser,httpx,requests,bs4,lxml,yaml,anthropic"` -> ok
- `curl -L -C - -o /mnt/ssd0/ciw/audio/69715223a6c658f1834fb3d6.mp3 <acast media.mp3 url>` -> `stat -c %s` = 58960972 bytes (matches the server Content-Length: 58960972).
- `$P/ffprobe -v error -show_entries format=duration -of default=nw=1 <mp3>` -> duration=3685.041625 (1:01:25), 128 kb/s, 44.1 kHz stereo, tags match the episode.
- `$P/ffprobe ... test_excerpt_60s.mp3` -> duration=60.000000 (1040797 bytes).

### Open issues
- SIZE MISMATCH: the plan expected exactly 147403780 bytes; the Acast URL (302 redirect to stitcher2.acast.com) serves 58960972 bytes. Not forced. Duration matches the expected 3685 s, and 128 kb/s x 3685 s is about 59 MB, so the file is complete and the 147 MB figure (likely RSS enclosure length or a different rendition) is simply not what the server delivers now. Downstream tasks should not assert the 147403780 size; assert duration (about 3685 s) instead. Backfill storage estimate (~40 GB) is probably an overestimate.
- Acast redirect URLs are signed and expire (Expires param); always re-resolve via the sphinx URL.

Next: T1 characterization tests

## 2026-10-15 - T1: characterization tests (branch `m1-skeleton`, from `m0-setup`)

### What was done
Added `pytest.ini` (`testpaths = tests`, silences scrape.py's `datetime.utcnow` DeprecationWarning) and `tests/` (76 tests, ~0.4 s, no network). No source, template or DB changes.
- `tests/conftest.py`: puts the repo root on `sys.path`, autouse fixture `monkeypatch.chdir(REPO_ROOT)` (app.py's `DB_PATH` is cwd-relative), session fixture that SHA-256s `cooking_issues.db` before/after and fails if it changed, `client` and read-only `db` (sqlite `mode=ro`) fixtures. T2 must update only the path/import bits here.
- `tests/test_routes.py`: `/` (216 posts text, 8 primer cards, 15 recent), `/browse` (24 per page, 9 pages, page 2 disjoint from page 1, page 0 clamps to 1, page 999 empty 200, tag=`sous vide` gives 6 posts, unknown tag gives 0), `/post/<id>` (post 1, primer 217, 404 for 0 / 999999 / non-int), `/search` (`carbonation` = 9 results with `<mark>`, `sous vide`, nonsense gives "No results for", empty/blank q gives hint text), `/primers` (8 cards, all titles), base layout.
- `tests/test_filters.py`: `fmt_date` and `short_excerpt` (registration, ISO with/without tz, date only, empty/None, invalid -> first 10 chars, truncation at word boundary, exact-length, no-space word).
- `tests/test_scrape.py`: `fix_mojibake` (apostrophe, curly quotes incl. undefined byte 0x9d, dashes, ellipsis, e-acute, degree, NBSP; clean text unchanged; mixed; idempotent; None/empty), `_CP1252_REVERSE` completeness, `fix_encoding.fix_mojibake` agrees with scrape's copy, `clean_text`, `parse_post` against an inline HTML fixture and its URL-date fallback.
- `tests/test_db.py`: tables `posts`, `posts_fts`, `scraped_index_pages`; counts 216 post + 8 primer = 224; date range 2009-04-30..2013-11-29; no `â€` marker in any text column; FTS answers `carbonation`.

### Verification (P=/home/jacob/miniconda3/envs/py312/bin/python)
- `cd /home/jacob/cooking-issues-archive && $P -m pytest -q` -> `76 passed in 0.41s`
- `cd /tmp && $P -m pytest /home/jacob/cooking-issues-archive/tests -q` -> `76 passed in 0.39s`
- `git diff --stat -- cooking_issues.db` -> empty. `$P -m ruff check tests` -> All checks passed.
- Importing `app` and `scrape` has no side effects (`app.run` is under `__main__`; scrape's `main()` is too).

### Bugs / oddities found (NOT fixed)
- `/browse?page=abc` -> HTTP 500 (`int()` on the query arg is unguarded). Pinned in `test_browse_non_integer_page_is_a_500_known_bug`; flip that test if T2+ fixes it.
- `/search?q=!!!` (punctuation only): `safe_fts` returns `""`, FTS5 raises a syntax error, caught and shown inline as "Search error: fts5: syntax error..." with HTTP 200. Pinned.
- Stray `Â` in the data: 162 of 224 posts have `Â` (4795 occurrences in body_text, 12 posts in body_html), always as "Â " before a space (e.g. `18 F.Â I cont`). Cause: `scrape.clean_text` runs `re.sub(r"\s+", " ")`, which turns the `\xa0` of the mojibake pair `Â\xa0` (UTF-8 NBSP read as cp1252) into a plain space BEFORE `fix_mojibake` runs, so the pair can no longer be repaired (`fix_mojibake("Â ")` leaves it as is). Fix would be to run `fix_mojibake` before whitespace collapsing, then re-clean the DB. The `â€` marker test passes (0 hits), so the mojibake the project was built to fix is gone; only this NBSP remnant is left. Titles are clean.
- Post id 25 ("Museum of Food and Drink Fundraiser - Buy Tickets!") has an empty `body_text`. Pinned in `test_only_known_post_has_empty_body`.
- `fix_mojibake`/`_fix_chunk`/`_CP1252_REVERSE` are duplicated in `fix_encoding.py` and `scrape.py` (a test asserts they agree); T2 could dedupe.
- `scrape.py` uses deprecated `datetime.utcnow()` (warning filtered in pytest.ini).
- `compare.py` does `import scrape` after `sys.argv = [...]`; not covered by tests.

### Open issues (for later tasks)
- Acast RSS enclosure `length` (147403780 for the test episode) does NOT match the real downloaded size (58960972, which matches the HTTP Content-Length). The downloader in T4 must verify against the response Content-Length, not the feed's length.

### Notes for T2
- Tests import top-level modules `app`, `scrape`, `fix_encoding` (cwd = repo root, `sys.path` set in conftest). After moving into `src/ciw/`, only `tests/conftest.py` and the import lines in `tests/test_filters.py` / `tests/test_scrape.py` should need touching. The app's DB path must still resolve to the repo-root `cooking_issues.db` (tests chdir to the repo root, and assert the same 216/8 counts).
- Test expectations rely on stable HTML hooks (class names `primer-card`, `post-card`, `primer-full-card`, `result-item`, `result-count`, `pagination`, `current`, `site-nav`, `post-nav`) and strings in templates; moving templates unchanged keeps them valid.

Next: T2 restructure into src/ciw
