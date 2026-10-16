# CLAUDE.md

Flask site serving a scraped 2009-2013 Cooking Issues blog archive (`cooking_issues.db`, tracked in git), being extended with a podcast pipeline (RSS ingest, WhisperX transcripts, Claude segment/score, ntfy + review pages). Package: `src/ciw/` (`web/`, `blog/`, `cli.py`, `config.py`, `models.py`, `feeds.py`, `matching.py`, `ingest.py`, `state.py`, `download.py`, `transcribe.py`, `transcript.py`, `asr/`, `vocab.py`, `correct.py`, `quality.py`, `cli_vocab.py`, `search_index.py`, `related.py`, `htmlsafe.py`, `notify.py`, `health.py`). Stack: Python 3.11+, Flask, typer, pydantic v2, pyyaml, pytest, ruff.

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

## Podcast ingest (T3)

- `ciw poll [--source acast|hrn|patreon|all]`, `ciw feeds diff`, `ciw feeds check-audio`, `ciw episodes list`. State: `data/state.sqlite`; per-episode `data/episodes/<episode_id>/meta.json`; raw feed cache `data/feeds/<name>.xml` (mode 600).
- `episode_id` is stable once assigned: Acast guid (Acast-based, including Acast+Patreon matches), `patreon-<sha1[:16]>`, `hrn-<uuid>` or `hrn-<sha1[:16]>`. Matching constants live at the top of `ciw/matching.py`: Patreon is early access, so it may precede its Acast twin by up to 21 days (`PATREON_LEAD_DAYS`); recurring titles are resolved by near-identical durations (`TIGHT_DURATION_S`). A matched episode downloads the Patreon copy (`audio_source=patreon`). `feeds/pins.txt` (tracked) pins pairs the rules cannot find (`<acast guid> <patreon guid>  # note`; applied as locked pairs by poll and diff, warns if unused).
- Feed `enclosure length` is unreliable (stored as `enclosure_length_hint`); verify downloads against the HTTP Content-Length. Never log a feed URL except through `redact()`; the Patreon URL is not logged at all. Feed tests use `tests/fakeweb.py` (httpx.MockTransport) and `tests/fixtures/feeds/`.

## Downloader (T4)

- `ciw download` needs a selector (`--latest N` | `--episode ID` | `--since DATE` | `--all`); `--dry-run` previews; `--redownload` replaces, `--verify` re-checks. Files: `<audio_dir>/<episode_id>.<mp3|m4a>`, `.part` while downloading (never touch other files in `audio_dir`, e.g. `test_excerpt_60s.mp3`). State columns `audio_path/audio_bytes/audio_sha256/audio_duration_s/downloaded_at/audio_downloaded_from`; stage `downloaded`.
- Sizes: Acast stitches ads dynamically, so the served size/duration drifts (and real durations run up to ~5% above the feed's). Never compare against `enclosure_length_hint`; resume uses ETag/If-Range. Config `download.min_free_gb`, `download.duration_tolerance_*` (tolerance = max(120 s, 10%) since T8).
- Tests that invoke the CLI against the real repo config would hit the network and `/mnt/ssd0`: use `tmp_cfg` + a `config.yaml` with a tmp `audio_dir` (see `tests/test_cli_download.py`).
- Feed health alerts: `ciw.health` (called by `ciw poll`), `ciw.notify.send_ntfy` (no topic -> log only, never raises).

## Transcription (T5)

- `ciw transcribe` needs a selector (`--episode ID` | `--latest N` | `--all-downloaded`) or `--audio PATH --out PATH` (no state); `--dry-run`, `--force`, `--stale`, `--no-diarize`, `--model`. Writes `data/episodes/<id>/transcript.json`, sets stage `transcribed` (state columns `transcribed_at/asr_model/asr_version/asr_runtime_s/diarization`). Lock: `data/transcribe.lock`. A transcript is current when `asr_backend`/`asr_model`/`asr_version` equal the backend's; otherwise it is `STALE` (redo with `--stale`/`--force`).
- ASR deps are the optional `asr` extra (torch 2.8.0+cu128 from the PyTorch index, whisperx 3.8.x). Never import torch/whisperx at module level outside `ciw/asr/whisperx_backend.py`'s functions; `ciw --help` and the web app must stay torch-free (a test enforces it for the registry). Backends register in `ciw.asr` (`asr.backend` in config.yaml).
- Tests must never run a real `ciw transcribe` against the real config (T5's old stub test did, by accident, and transcribed an episode): use `tmp_cfg` + a tmp `config.yaml` with `asr.backend: fake` (`tests/asrfake.py`, `tests/test_cli_transcribe.py`). Default suite needs no GPU/network/HF token; `pytest -m gpu` runs the real-model test.
- Model caches: `HF_HOME=/mnt/ssd0/ciw/hf_cache`, `TORCH_HOME=<that>/torch` (set by the backend unless already in the environment). Speakers need `HF_TOKEN` in `.env` plus accepting the pyannote model terms; without them transcription runs with `speaker=None` and `diarization_skipped` set.
- Render transcripts for prompts/UI with `Transcript.render()` / `ciw.transcript.render_utterances`.

## Vocabulary, correction, quality (T6)

- `vocab/vocabulary.txt` (hand-curated, one term per line, most valuable first) is Whisper's `initial_prompt` (cut at a term boundary to 600 chars) and the correction pass's term list; `vocab/aliases.txt` holds `heard => correct` pairs; `vocab/candidates.csv` is machine-mined (`ciw vocab build`, no LLM, never touches vocabulary.txt); `vocab/README.md` explains it. Config: `asr.vocabulary_file`, `asr.aliases_file`, `asr.correct` (default true), `asr.correct_min_confidence`.
- `ciw/correct.py`: conservative post-ASR fixes (alias > exact merge/case/normalize > guarded fuzzy merge/fuzzy). Every change is in `Transcript.corrections` with the original words, `Word.orig` is set only on changed words, it is idempotent (reverts, then re-applies) and a transcript with no vocabulary is never touched. Prefer a missed fix to a corrupted word; tune with the false-positive stress test described in docs/progress.md (T6).
- Commands: `ciw vocab build|correct EPISODE [--dry-run|--revert]|corrections EPISODE|quality EPISODE... [--aggregate] [--json]`; `ciw transcribe --no-correct --quality`. A transcript made with a different prompt (`vocab_hash`) is STALE (redo with `ciw transcribe --stale`); `ciw episodes list` shows `ok|STALE|recorrect|-` and `--stale` filters. State columns `vocab_hash/correction_hash/corrections` (migration 4).
- Test safety guard (T6): under pytest `ciw.config.load_config`, `state.connect` and the locks raise `UnsafeTestPathError` if a path resolves into the real `data/` or `/mnt/ssd0/ciw`. Use `tmp_cfg` (its config.yaml has tmp audio/clips dirs), `write_config(tmp_path, extra)` from conftest for custom configs, `blog_cfg` when only the read-only blog DB is needed, and `allow_real_paths` / `@pytest.mark.real_paths` only for tests that must read the real paths.

## Transcript browser (T7)

- Routes (`ciw/web/episodes.py`, blueprint `episodes`): `/episodes`, `/episode/<id>` (`?q=` highlight, `#t=<s>` deep link), `/audio/<id>` (Range; path ONLY from the state row, must resolve inside `audio_dir`), `/episode/<id>/transcript.txt`. `/search` also searches episodes (`scope=all|blog|episodes`; blog markup unchanged; episode section never shows raw FTS errors). Post pages get a "Mentioned in episodes" box only when matches exist (otherwise their HTML is byte-identical to before). New routes parse `page` safely; the old blog oddities stay (tests pin them).
- `ciw index [--episode ID] [--all] [--rebuild]` builds `search_db` (`data/search.sqlite`, FTS5 `utterances_fts` + `episodes_fts` + `indexed` bookkeeping; `ciw/search_index.py`). Incremental by transcript size+mtime then content hash; utterances = `ciw.transcript.split_utterances` (<= 50 words), the same blocks the page shows (`#u<idx>`). `ciw transcribe` and `ciw vocab correct` call `index_best_effort` (never fails the command). The web app opens state and index read-only and degrades when either is missing.
- Show notes come from the feed (untrusted): always through `ciw.htmlsafe.sanitize_html` (allow-list p br ul ol li a[http(s)] strong em b i h2-h4 blockquote). Never mark feed HTML safe any other way.
- Frontend: `src/ciw/web/static/episode.js` + `episodes.css`, no frameworks/CDN. `tests/test_web_episode_page.py` pins the HTML hooks the JS uses (`#player`, `#transcript .utt[data-start][data-end]`, `a.ts`, `#find*`, `#follow`). Test fixtures: `tests/webfake.py` (synthetic episodes, transcripts, audio in tmp dirs).
- Related posts (`ciw/related.py`): vocabulary terms in the episode ranked by tf x idf over the blog FTS, boilerplate terms (in >30% of episodes) and near-universal ones (>40% of posts) skipped, capitalised-phrase fallback, one FTS5 OR query ranked by bm25.

## Backfill (T8)

- `ciw backfill plan|run|status|stop|retry` (`ciw/backfill.py`, `cli_backfill.py`). `run` needs `--yes` for more than 10 episodes or without any selector (`--source/--since/--until/--limit`); `--download-only` / `--transcribe-only` split the phases; `--max-hours H`; `--retry-failed` ignores cool-downs. Never start a bulk run from an agent session: the user starts the long run. Verification runs are capped by `--limit`.
- Order: `backfill.order` (acast, hrn), newest first (`--oldest-first`); `--limit` counts episodes that still need work. Done = transcript current + indexed (`done but not indexed` is just re-indexed). Failures: `attempts`, `last_error_stage`, `next_retry_at` (migration 5), backoff `backfill.retry_backoff_minutes`, parked after `max_attempts` until `ciw backfill retry`. A disk-guard failure stops the run (exit 4) without counting against the episode.
- It holds `data/transcribe.lock` and `data/download.lock` (exit 3 with a message if another run has them). Stop: SIGINT/SIGTERM or `data/STOP` finish the current episode; a second signal aborts at once (a watcher thread reads the signal wakeup fd, because the main thread is stuck inside torch). A STOP file older than the process is removed at start.
- State writes must not read before they write (SQLite stale-snapshot error when the downloader thread commits in between): `mark_transcribed` and `record_failure` are single UPDATEs first; `download_episode` ends its read transaction before the transfer.
- Tests use `tests/test_backfill.py`'s `env` fixture (fake ASR + `FakeDownloads`); never call the real downloader or transcriber against real paths. `download.duration_tolerance_pct` is a fraction (0.10); `backfill.suspect_gap_pct` is a percent (5).

