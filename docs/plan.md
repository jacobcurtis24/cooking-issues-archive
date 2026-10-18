# Plan: Cooking Issues Segment Watcher (`ciw`)

The roadmap for the podcast pipeline that is being added to the blog-archive site. This is the tracked, maintained copy of the plan; the original design document, `Cooking Issues Segment Watcher — Plan.md`, stays untracked in the repo root and is not edited. What was actually done, with commands and numbers, is in [progress.md](progress.md). The conventions for working in the repo are in `CLAUDE.md`.

Last updated: 2026-10-18.

## Goal

Poll the Cooking Issues podcast feeds, download new episodes, transcribe them locally (WhisperX, with speaker labels), have Claude split each transcript into topical segments and score them against an interest profile, then push the best ones to a phone (ntfy) with a clip and a review page. The same transcripts also make the whole back catalog searchable on the existing site, next to the 2009–2013 blog archive.

```
RSS poll -> download -> WhisperX (+speakers) -> vocabulary correction -> search index / browser
                                                         |
                                  Claude: segment + score vs interests.yaml
                                                         |
                                   review page + clips + ntfy push -> ratings -> eval
```

## Decisions

| Area | Decision |
|---|---|
| Web | A Flask blueprint inside the existing app (not FastAPI). Debug is configurable and off by default. Bind to localhost or Tailscale only. |
| Layout | Installable package `src/ciw/` (`pyproject.toml`), CLI `ciw` (typer), `config.yaml` for settings, `.env` for secrets. |
| Python | One conda env, `py312` (Python 3.12), at `/home/jacob/miniconda3/envs/py312`. Other envs are not touched. |
| UI | Unified nav, an Episodes section, transcripts included in search, episodes cross-linked to blog posts through the existing FTS5. |
| Notifier | ntfy.sh with an unguessable topic (`NTFY_TOPIC`). |
| Sources | Acast (public), Patreon (private URL in `.env`; early access of the same weekly episodes plus a few exclusives), Heritage Radio Network archive (2013 onwards). |
| ASR | WhisperX `large-v3` plus pyannote `speaker-diarization-community-1`. Speakers are on (decision of 2026-10-16: "Dave vs. everyone else is fine"). |
| Storage | Audio and clips on `/mnt/ssd0/ciw/`. Transcripts, per-episode JSON and `state.sqlite` in repo `data/` (git-ignored). |
| LLM | The Anthropic API (decided 2026-10-18), model `claude-sonnet-5-5` unless the model comparison in T9 says otherwise. Batch API for the backlog. Hard cap of $25 per month, enforced in code. |
| Process | One task at a time, a branch per milestone, a dated entry in `progress.md` per task, `pytest` + `ruff check` + `ruff format --check` before every commit. No push or merge without being asked. |

## Status

| Task | What | Status | Branch |
|---|---|---|---|
| T0 | Env, storage, fixture | done | `m0-setup` |
| T1 | Characterization tests of the blog site | done | `m1-skeleton` |
| T2 | Restructure into `src/ciw`, CLI skeleton, config, models | done | `m1-skeleton` |
| T3 | Feed parsing, Acast/Patreon matching, state, `ciw poll` | done | `m2-ingest` |
| T4 | Resumable downloader, feed health alerts | done | `m2-ingest` |
| T5 | ASR environment and WhisperX backend (with speakers) | done | `m3-transcribe` |
| T6 | Vocabulary, correction pass, quality report | done | `m3-transcribe` |
| T7 | Transcript browser, transcript search, related posts | done | `m3-transcribe` |
| T8 | Resumable backfill runner | done (the long run is in progress, see below) | `m3-transcribe` |
| T9 | LLM segment + score | next; blocked on `ANTHROPIC_API_KEY` | `m5-score` |
| T10 | Review pages, clips, ntfy notifier | not started | |
| T11 | Gold set and `ciw eval` | not started | |
| T12 | Unattended `ciw run`, systemd timer, retention | not started | |
| T13 | Extensions (optional) | not started | |

`m3-transcribe` carries T5–T8 plus the Patreon work and is about 60 commits ahead of `main`; nothing has been pushed or merged.

### State of the data (2026-10-18)

- 696 episodes known: 241 Acast (each with a Patreon twin), 453 HRN, 2 Patreon-only exclusives.
- 469 transcribed with speakers, 0 failures, 0 suspect transcripts. 227 HRN episodes (2010–2016) are still being processed by the user's backfill run (`ciw backfill run --yes --max-hours 8`), about 8 GPU hours at roughly 24x realtime.
- Audio so far is about 47 GB on `/mnt/ssd0` (367 GB free). `/` is 90+% full (about 10 GB free), so nothing bulky belongs there.
- 878 tests pass, 1 deselected (`gpu`).

## Tasks

### T0 — Environment, storage, fixture (done)
Conda env `py312` with ffmpeg and the app dependencies; `/mnt/ssd0/ciw/{audio,clips}`; `.gitignore`; the fixture episode "Biscuits, Bouillon and Beyond" (guid `69715223a6c658f1834fb3d6`, 1:01:25) plus a 60 s excerpt for tests. The fixture is gold-set episode 1. Note: the Acast copy is 59 MB, not the 147 MB the first plan assumed (that was the Patreon size).

### T1 — Characterization tests (done)
Pytest tests pinning the existing routes, filters, scraper helpers and database counts before any code moved. The blog's known oddities are pinned, not fixed.

### T2 — Restructure and skeleton (done)
`src/ciw/` with `web/` (app factory, blog blueprint, templates), `blog/` (scrape, encoding fixes, compare), `config.py`, `models.py` (pydantic models for transcript, segments, scores, meta), the `ciw` CLI, README and `CLAUDE.md`. T1 tests kept passing unchanged.

### T3 — Feeds, matching, state (done)
Apple lookup to feed URL; Acast, HRN and Patreon parsing; `state.sqlite` with versioned migrations; `ciw poll`, `ciw feeds diff|check-audio`, `ciw episodes list`. Acast and Patreon items are paired by `matching.py`: titles equal or near-identical, Patreon up to 21 days early, durations within a few seconds, recurring titles told apart by duration, anything unclear left ambiguous (never guessed). `feeds/pins.txt` pins the six pairs whose titles differ. A matched episode keeps the Acast id and downloads the Patreon copy.

### T4 — Downloader (done)
`ciw download`: sequential, at most 2 requests/s, resumable, size checked against the server's own total and the duration against ffprobe, atomic rename, free-disk guard. Feed problems (401/403, zero audio items) send one ntfy alert per status change when `NTFY_TOPIC` is set.

### T5 — WhisperX backend (done)
Optional `asr` extra (torch 2.8.0+cu128, whisperx 3.8). `ciw transcribe` with selectors, `--dry-run`, `--force`, `--stale`, OOM back-off, model and version stored in `transcript.json`, speaker labels through pyannote. About 24x realtime with diarization on the RTX 3080.

### T6 — Vocabulary, correction, quality (done)
`vocab/vocabulary.txt` (hand-curated) is Whisper's `initial_prompt` and the term list for a conservative post-ASR correction pass; `vocab/aliases.txt` holds known mis-hearings. `ciw vocab build|correct|corrections|quality`. Every change is recorded and reversible; a missed fix is preferred to a corrupted word.
Open: the aliases were fitted on the first two episodes; re-mine candidates from all transcripts once the backfill is finished, and review `vocabulary.txt`.

### T7 — Transcript browser (done)
`/episodes`, `/episode/<id>` (sanitised show notes, audio with Range support, click-to-seek transcript, find box, `#t=` deep links), `/search` across blog and episodes, "Mentioned in episodes" boxes on blog posts, related posts for an episode. `ciw index` builds the FTS5 index.

### T8 — Backfill runner (done)
`ciw backfill plan|run|status|stop|retry`: prefetching downloader plus one GPU transcriber, failure isolation with cool-downs, `--max-hours`, graceful stop (a second signal aborts), JSONL logs, suspect-transcript flag. Order is `acast`, then Patreon-only exclusives, then `hrn`, newest first. The user starts long runs themselves; agent sessions never do.
Remaining: finish the current run, then a short follow-up if `--max-hours` ran out.

### T9 — LLM segment + score (next)
Branch `m5-score`. Needs `ANTHROPIC_API_KEY` in `.env` (the user supplies it).

0. **Model comparison first.** Before the production prompt is fixed, run the same user-supplied prompt on 5–6 sampled transcripts through Haiku 5.5, Sonnet 5.5 and Opus 5.5 and compare. Opus is the reference, not the truth. The review is blind (model labels shuffled) and is backed by automatic checks: schema-valid JSON, quoted phrases found in the transcript, timestamps inside the episode and in order, segment coverage, tokens and dollars per model. Expected cost is under $1. The sample should cover the fixture, a short and a long episode, an old HRN episode, a Patreon exclusive and one with weaker ASR. The prompt comes from the user; no API call is made before the key and prompt are in and the user says go.
1. Prompt templates in `prompts/`, `interests.yaml` as the interest profile, a Claude backend with pydantic validation and retry, prompt caching for the shared prefix.
2. A cost ledger in `state.sqlite` and a hard monthly cap of $25. A cost estimate (exact input counts from `count_tokens`) is printed before any backlog is queued.
3. The backlog goes through the Batch API (50% off, results within hours); new weekly episodes go through the normal API.

Cost reference (per 1M tokens, input / output): Haiku 5.5 $0.10 / $0.50, Sonnet 5.5 $2 / $10, Opus 5.5 $4 / $20. The backlog is about 10–12M input tokens (696 episodes, about 11,200 words each). Estimates for the whole backlog: Sonnet standard $30–45 (over the cap), Sonnet Batch $15–23, Haiku about $2, Opus $60–85. A new episode costs a few cents.
*Done when:* `ciw score <guid>` writes valid `segments.json` and `scores.json` for the fixture episode, and the model for production use has been chosen from the comparison.

### T10 — Review and notify
Clips cut to `/mnt/ssd0/ciw/clips`; a segment view with media-fragment players; a ratings POST route; the ntfy notifier (a digest plus a push with a clip for scores of 9 and above).
*Done when:* a test push reaches the user's phone and its clip plays over Tailscale. Needs the phone with the ntfy app and a topic.

### T11 — Gold set and eval
Five gold episodes (the fixture is the first), labeling through the thumbs UI, `ciw eval` with a threshold sweep.
*Done when:* the user has picked a threshold. Needs the user's labeling.

### T12 — Unattended operation
`ciw run` orchestrator with a configurable stage list, a systemd service and timer (`Linger` is off here, so enable it or use another supervisor), structured logs, retry and backoff, 90-day audio retention.
*Done when:* a new episode goes end to end without anyone touching it.

### T13 — Extensions (optional)
A local LLM backend, Parakeet, semantic search, named speakers.

## Gates that need the user

| Task | Needs |
|---|---|
| T5 | Hugging Face token and accepting the pyannote terms (done) |
| T9 | `ANTHROPIC_API_KEY`, the comparison prompt, approval to spend |
| T10 | A phone with ntfy, and `NTFY_TOPIC` |
| T11 | Labeling the gold set |

## Open items outside the numbered tasks

- Finish the backfill, then re-mine vocabulary candidates and review `vocab/vocabulary.txt`.
- Decide when to merge or split the `m3-transcribe` branch; nothing is pushed yet.
- `NTFY_TOPIC` is unset, so alerts are only logged.
- The Acast feed URL carries an `auth=` token; it is never logged (see `CLAUDE.md`).
- The blog's known oddities (`/browse?page=abc` 500, `/search?q=!!!` error, stray `Â`, empty body of post 25) stay unfixed until scheduled; tests pin them.
- Old HRN audio (2010–2016) quality was unknown at the start of the run; `ciw backfill status` lists suspect transcripts.

## Risks

- A single backfill process: a CUDA error that wedges the driver fails every following episode. Check `ciw backfill status` or set `NTFY_TOPIC`.
- LLM spend: the cap is $25 a month, and every prompt-tuning pass re-reads the same transcripts, so tune on a few episodes first.
- Transcripts and clips are personal-use only; keep the web app off the public internet.
