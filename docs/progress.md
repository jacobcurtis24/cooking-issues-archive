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
