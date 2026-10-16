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

## 2026-10-15 - T2: restructure into `src/ciw` + skeleton (branch `m1-skeleton`)

### What moved where (`git mv`, history follows)
- `app.py` -> `src/ciw/web/blog.py` (a `blog` Blueprint; routes `/`, `/browse`, `/post/<id>`, `/search`, `/primers` unchanged) plus `src/ciw/web/__init__.py` with `create_app(config=None)`. The factory sets `BLOG_DB` from config and registers the `fmt_date` / `short_excerpt` filters on the app (`add_template_filter`).
- `templates/` -> `src/ciw/web/templates/` (content unchanged except 3 nav lines, see Deviations).
- `scrape.py`, `fix_encoding.py`, `compare.py` -> `src/ciw/blog/`. `fix_mojibake`, `_fix_chunk`, `_CP1252_REVERSE` now live only in `src/ciw/blog/encoding.py` (AST-compared against both old copies first: identical code). `compare.py` imports `from ciw.blog import scrape` (the `sys.argv` hack was dropped, it was unneeded since `main()` is guarded).
- `scrape.py` / `fix_encoding.py` gained `run_scrape/run_search/run_stats` and `run(db_path, dry_run)` so the typer CLI can wrap them; their argparse `main()` still works (`python -m ciw.blog.scrape --stats`). Default DB path is now `<project root>/cooking_issues.db` instead of cwd-relative.
- Removed top-level `app.py`, `scrape.py`, `fix_encoding.py`, `compare.py`, `templates/`, `pytest.ini` (no shims). README and summary.md paths/run commands updated.
- New: `pyproject.toml` (package `ciw`, src layout, console script `ciw = ciw.cli:app`, ruff + pytest config), `config.yaml`, `.env.example`, `interests.yaml`, `CLAUDE.md`, `src/ciw/{__init__,__main__,cli,config,models}.py`, tests `test_config.py`, `test_models.py`, `test_cli.py`, `test_web_app.py`.
- Editable install: `/home/jacob/miniconda3/envs/py312/bin/python -m pip install -e .`.

### Design notes
- Config: pydantic models, `load_config(root=None)`. Root = `$CIW_ROOT` else nearest ancestor of the package with `pyproject.toml`. Relative paths in `config.yaml` resolve against root; absolute paths (`/mnt/ssd0/ciw/...`) are kept. Unknown keys are rejected. `.env` is parsed by a small built-in loader (no new dependency) and never overrides real env vars. Secrets live in `Config.secrets` as `SecretStr` (masked in repr). The ntfy topic is read from `NTFY_TOPIC` (`notify.topic_env`), exposed as `Config.ntfy_topic`. `ciw.config.redact(text, secrets=None)` masks known secret env values, any literals passed in, and `token=/key=/auth=/secret=/password=` URL params.
- Models (`ciw.models`): `EpisodeMeta`, `Word`, `Transcript`, `Segment`, `Segments`, `Score`, `Scores`, `InterestProfile`/`Topic`. All `extra="forbid"`, with `save(path)` (atomic) / `load(path)` / `to_json` / `from_json`. `Segments` and `Scores` are wrapper objects (guid, model, prompt_hash, created_at, list), since the design doc puts model + prompt hash on the file. Validators: word end >= start, segment end > start, segment ids unique and non-overlapping/ordered, `check_contiguous(tolerance, total)` for gaps (a helper, not enforced on load because LLM output may need a retry rather than a hard failure), score 0-10, unique score `segment_id`, `Scores.check_matches(segments)`.
- CLI: `ciw web [--host --port --debug]` (debug off by default), `ciw blog scrape|search|stats|fix-encoding`, `ciw feeds diff`, and stubs `poll` (T3), `feeds diff` (T3), `download` (T4), `transcribe` (T5), `vocab` (T6), `score` (T9), `eval` (T11), `notify` (T10), `run` (T12). Stubs print `not implemented yet (task Tn)` on stdout, exit 2, and accept/ignore extra args.

### Verification (P=/home/jacob/miniconda3/envs/py312/bin)
- `cd /home/jacob/cooking-issues-archive && $P/python -m pytest -q` -> `136 passed in 1.14s` (76 T1 + 60 new).
- `cd /tmp && $P/python -m pytest /home/jacob/cooking-issues-archive/tests -q` -> `136 passed in 1.15s`.
- `$P/python -m ruff check .` -> All checks passed. `$P/python -m ruff format --check .` -> 21 files already formatted.
- `$P/ciw --help` lists web, poll, download, transcribe, vocab, score, eval, notify, run, blog, feeds. `ciw blog stats` -> 224 posts (8 primers), 44 index pages.
- Old-vs-new HTML identity: rendered 15 URLs (`/`, `/browse`, page 2, tag filter, `/post/1`, `/217`, `/25`, `/0`, several `/search?q=`, `/primers`, `/browse?page=abc`) through the old `app.py` (from git HEAD~) and the new factory; status codes and SHA-256 of every body were identical (one-off script, not committed).
- Web smoke: `ciw web --port 5057` from `/tmp`: `/`, `/browse`, `/post/1`, `/search?q=carbonation`, `/primers` -> all 200; server stopped.
- `git diff --stat -- cooking_issues.db` empty; the session-wide DB-hash test passes.

### Deviations / notes
- Blueprint endpoints are `blog.index` etc. `base.html` compared `request.endpoint == 'index'|'primers'|'browse'` for the active nav class, so those 3 comparisons became `'blog.index'|'blog.primers'|'blog.browse'`. Rendered HTML is byte-identical (verified above; `test_web_app.py` also asserts the active class). All template links are hard-coded paths, no `url_for`.
- `ruff format` rewrote code blocks inside `summary.md` (ruff 0.17 formats markdown); reverted and `*.md` excluded in `[tool.ruff.format]`.
- `per-file-ignores` E501 for `src/ciw/blog/scrape.py` (long SQL strings kept verbatim). `zip(..., strict=False)` added in `fix_encoding.py` for B905 (same behavior). Moved code was otherwise ruff-formatted (separate commit).
- `ciw web` calls `Flask.run` (dev server) as before; no production server yet.
- README mentions the plan by name only; the Plan .md is untracked and was not copied into the repo.
- The oddities from T1 are still present and unfixed (page=abc 500, `!!!` search error, stray `Â`, empty post 25, `utcnow` warning).
- `ciw blog stats/search` and `scrape` still run `init_db` (CREATE IF NOT EXISTS) as before; it does not alter the existing DB (hash test passes).

### For T3
- Use `ciw.config.load_config()` for paths/apple ids (`cfg.apple_ids.acast/hrn`, `cfg.state_db`, `cfg.episodes_dir`, `cfg.audio_dir`, `cfg.secrets.patreon_feed_url`), and `ciw.config.redact()` when logging feed URLs. Fill in the existing `poll` and `feeds diff` stubs in `src/ciw/cli.py` (remove `_STUB_CONTEXT` there). `EpisodeMeta` in `ciw/models.py` is the meta.json schema; extend it rather than adding a parallel one. Web tests assume cwd-independent DB resolution via `Config.blog_db`.

Next: T3 feed parsing + state

## 2026-10-15 - T3: feed parsing, matching, state, poll/diff (branch `m2-ingest`, from `m1-skeleton`)

### What was built
- `src/ciw/feeds.py`: `HttpClient` (httpx, 30 s timeout, User-Agent, >= 0.5 s between requests, retries with exponential backoff for timeouts/transport errors/429/5xx, injectable transport), `resolve_feed_url(apple_id)` (iTunes lookup -> `results[0].feedUrl`), `fetch_feed` (conditional GET), `parse_feed` (feedparser -> `FeedItem`, `audio/*` enclosures only, counts skipped non-audio), `parse_duration` (`H:MM:SS`, `MM:SS`, seconds), `html_to_text`, `normalize_title`, `episode_id_for`. Typed errors: `FeedLookupError`, `FeedFetchError`, `FeedAuthError(401/403)`, `FeedEmptyError` (zero audio items). The `httpx`/`httpcore` loggers are pinned to WARNING (httpx logs full URLs at INFO, which would leak the Patreon token); the Patreon URL is never logged at all, other URLs only via `redact()`.
- `src/ciw/models.py`: new `FeedItem` (one feed's view of an item) and an extended `EpisodeMeta` (`guid` = episode_id, `guids`, `source` = identity, `audio_source`, `title_norm`, `description` (text), `description_html`, `duration_s`, `enclosure_url/type/length_hint`, `link`, `episode_number`, `items{source: FeedItem}`). `enclosure_length` was renamed `enclosure_length_hint` (only test_models used it).
- `src/ciw/matching.py`: Acast<->Patreon matching. Constants at the top: `DATE_WINDOW_DAYS = 3` (inclusive), `DURATION_TOLERANCE_PCT = 0.10`, `DURATION_TOLERANCE_S = 120` (tolerance = max of the two; unknown duration never vetoes), `TITLE_SIMILARITY = 0.90` (only when numbers in the titles are identical; otherwise normalized titles must be equal). Only a mutual 1:1 candidate pair is matched; an item with 2+ candidates puts the whole connected group in `ambiguous` (logged, reported by `poll`/`diff`, everything stays unmatched/standalone). Pairs already recorded in state are "locked" and stay paired.
- `src/ciw/state.py`: `data/state.sqlite`, WAL, `PRAGMA user_version` migrations (v1: `episodes`, `episode_guids`, `feeds`), `connect()` context manager (commit/rollback/close), idempotent `upsert_episode` (`updated_at` only moves on a real change; `stage`/`error` never touched).
- `src/ciw/ingest.py`: `poll` (acquire each feed, match, merge, store), `diff_feeds`, `check_audio`/`probe_audio`. Raw feed bodies are cached in `data/feeds/<name>.xml` (mode 600) so a 304, or a feed not requested this run, still takes part in matching. A feed that fails keeps whatever state already has (never downgrades a matched episode). `meta.json` is rewritten only when its content changed.
- CLI: `ciw poll [--source] [-v]`, `ciw feeds diff`, `ciw feeds check-audio [--source hrn|acast|patreon] [--sample N] [--range]`, `ciw episodes list [--source] [--limit]`. Other stubs unchanged. (`poll` and `feeds diff` removed from the stub tests.)

### Decisions
- episode_id: Acast-based episodes (Acast-only and Acast+Patreon) use the Acast guid (24 hex; three May-2021 Acast items have 8-digit guids, kept as is, they are path-safe; anything else would become `acast-<sha1[:16]>`); Patreon-only `patreon-<sha1(guid)[:16]>`; HRN `hrn-<guid>` for the ART19 UUID guids, else `hrn-<sha1(guid)[:16]>` (the 231 legacy guids are `http://s3.amazonaws.com/...mp3` URLs). IDs are STABLE once assigned: a Patreon-only episode whose Acast twin shows up later keeps its `patreon-...` id and gains the Acast guid (no rename; T4+ may already have audio under that folder). If two rows end up owning guids of one episode (a poll that could not see Acast), the Acast-id one wins and a still-`discovered` duplicate is deleted; a duplicate past `discovered` is left alone and counted as a conflict.
- A matched episode: `source=acast`, `audio_source=patreon`, title/show notes/pub_date/link from Acast, `enclosure_*` and `duration_s` from Patreon; the Acast item stays under `items.acast` as fallback audio.
- Patreon URL is kept out of sqlite (`feeds.feed_url` is NULL for patreon).
- The plan said HRN is Simplecast; it is actually hosted on ART19 (`https://rss.art19.com/cooking-issues`). Nothing depends on it.
- The public Acast feed embeds `<acast:importedFeed>https://www.patreon.com/rss/cookingissues?auth=<token>`, i.e. a Patreon feed auth token in a PUBLIC feed. It was NOT used, logged or committed (the fixture has it replaced by `auth=REDACTED`). The token is probably the show's own import feed, not the user's; worth knowing, but do not use it.

### Verification (P=/home/jacob/miniconda3/envs/py312/bin/python)
- `cd /home/jacob/cooking-issues-archive && $P -m pytest -q` -> `251 passed` (136 old + 115 new); same from `/tmp` (`$P -m pytest /home/jacob/cooking-issues-archive/tests -q`). `$P -m ruff check .` clean, `$P -m ruff format --check .` clean.
- New tests (no network, `tests/fakeweb.py` = httpx.MockTransport; fixtures in `tests/fixtures/feeds/`: trimmed real `acast.xml` and `hrn.xml` (8 items each), synthetic `patreon.xml` with 2 matched items (different title/date/duration; one exactly +3 days), Patreon-only posts, a non-audio image post and an enclosure-less post, and an ambiguous pair): lookup parsing and error cases, duration parsing, audio filtering, normalization, id rules and filesystem safety, tolerance edges (date +-3 d exactly / +1 s, duration 10% / 120 s), ambiguity, locked pairs, retries/backoff/throttle, 401/403 and empty-feed typed errors, redaction (token absent from caplog, state db, errors, CLI output), idempotency (second poll = 0 new/0 updated, meta.json mtimes and state rows identical, also after a forced full re-download), later Patreon appearance, duplicate merge, CLI poll/diff/check-audio/episodes list via CliRunner.
- Accidental discovery while building: `ciw poll` no longer a stub, so test_cli stub tests had to drop it (an earlier version hit the network from a test; fixed, the data/ it created was deleted before the real run).

### Real run (network, `cd /tmp && ciw poll`; Patreon unset)
```
feed      status         items   new  updated   same  non-audio
acast     ok               241   241        0      0          0
hrn       ok               453   453        0      0          0
patreon   skipped            0     0        0      0          0
total                      694   694        0      0          0
```
Second run: `acast not-modified 241 same`, `hrn not-modified 453 same`, 0 new / 0 updated (HTTP 304 via ETag / Last-Modified). A third run after clearing the stored etags (full 200 re-download and re-parse): 0 new, 0 updated, all 694 `meta.json` mtimes identical.
- Acast has 241 items, not ~230 (plan estimate): 2021-05-17 .. 2026-10-14, avg 61 min. HRN 453 items, 2010-06-29 .. 2021-04-30, durations 22-109 min, no missing dates/durations/enclosures, no duplicate guids or titles. HRN oddities: guids are UUIDs (222) or legacy `http://s3.amazonaws.com/hrn/audios/<n>/<name>.mp3` URLs (231), which is why they are hashed; pubDate offset is `-0000` (parsed as UTC); `itunes:summary` is double-escaped (we use `content:encoded`/description instead). Acast pubDates are exact; three early Acast items have 8-digit guids.
- Test episode: `episode_id 69715223a6c658f1834fb3d6`, 'Biscuits, Bouillon and Beyond', pub 2026-01-24T08:00:00Z, `duration_s 3685.0`, stage `discovered`, `enclosure_length_hint 147403780` (as the feed says; the real file is 58960972).
- Patreon skip path: `INFO patreon: no PATREON_FEED_URL configured` and `note: patreon skipped (PATREON_FEED_URL is not set)`, exit 0. `ciw feeds diff` without Patreon prints per-feed counts/date ranges and says it is not configured.
- Feed `length` hints are unreliable on HRN/Acast generally: 1-5% below the real sizes (e.g. HRN ep 1 hint 47649018 vs real 48276655; Acast newest 58711980 vs 61609074).

### HRN audio check (resolves the plan's open question): YES, HRN audio still resolves
`ciw feeds check-audio --source hrn --sample 3` (3 newest, 3 middle, 3 oldest = 9 episodes, one request each; no bodies read):
- HEAD: 9/9 `200 audio/mpeg`, 1 redirect, final host `rss.art19.com`, but NO content-length on HEAD.
- `--range` (1-byte `Range: bytes=0-0` GET, 9/9 `206 audio/mpeg`, 2 redirects, final host `content.production.cdn.art19.com`, total size from Content-Range): newest 73029791 / 62961515 / 79804428 bytes; middle 54418575 / 46818815 / 43795722; oldest (Episodes 3, 2, 1) 48034647 / 44571434 / 48276655. So all of 2010-2021 is downloadable, 43-80 MB each; 453 episodes is roughly 25 GB.
- Acast sample (`--source acast --sample 1`): `sphinx.acast.com` answers HEAD with a fake `200 text/plain` (2 bytes), so a HEAD "200" proves nothing there. `probe_audio` therefore accepts a HEAD only when the content type is audio-like (or octet-stream), otherwise falls back to the Range GET. With it: 3/3 `206 audio/mpeg`, 1 redirect to `stitcher2.acast.com`, 58-68 MB.

### Open issues / notes for T4
- Acast and ART19 enclosure URLs redirect (Acast to signed, expiring `stitcher2.acast.com` URLs): always start from the `meta.json` enclosure URL, follow redirects, and verify against the HTTP Content-Length / 206 Content-Range, not `enclosure_length_hint`. ART19 HEAD gives no Content-Length; use the Range GET (or the GET's own headers).
- Patreon was never exercised for real (URL not available): the Patreon feed's actual shape (guid form, enclosure URL form, durations, non-audio post types, 401 vs 403 vs an HTML 200 on bad tokens) is only covered by the synthetic fixture. `ciw feeds diff` on the real feed is still to be run by the user, and matching constants may need tuning from its output (watch the ambiguity list).
- T4 health check/ntfy alert hooks: `FeedReport.status` is `auth` / `empty` / `error`; the reason is also in `feeds.last_status` (`auth_error:403`, `empty`, `error: ...`). `poll` prints the warning to stderr; no notification yet.
- If an episode gets Patreon audio after an Acast download already exists, `meta.json` flips `audio_source` to patreon; T4 should decide whether to re-download. `meta.audio_file` is preserved across polls.
- A feed item that disappears from its feed is never deleted from state.
- `.env` is not read by `ciw poll` unless `PATREON_FEED_URL` is in `.env` or the environment (via `load_config`); nothing else needed.

Next: T4 downloader
