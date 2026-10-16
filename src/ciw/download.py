"""Resumable, verified audio downloads into ``cfg.audio_dir``.

Rules the code relies on (see docs/progress.md, T3/T4):

* The stored ``enclosure_url`` is the only URL ever persisted. Acast redirects it to signed,
  expiring URLs, so every (re)start begins at the enclosure URL and follows redirects; resolved
  URLs are never stored or logged. URLs only appear in logs through ``redact``.
* The ``length`` in the feed is a hint and is wrong by 1-5% (or much more). The real size comes
  from the response: a ``206`` ``Content-Range`` total, or ``Content-Length`` of a full ``200``.
* A file is only renamed to its final name after (a) its size equals the server-declared total
  and (b) ffprobe finds an audio stream whose duration is close to the feed's duration.
* Downloads are sequential (one process at a time, ``data/download.lock``) and throttled to at
  most 2 requests per second by the shared ``HttpClient``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ciw import state
from ciw.config import Config, redact
from ciw.feeds import USER_AGENT, HttpClient
from ciw.ingest import _audioish, meta_path, probe_audio
from ciw.models import EpisodeMeta

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 60.0
MAX_RETRIES = 5
BACKOFF_S = 2.0  # 2, 4, 8, 16, 32 s
MAX_RETRY_AFTER_S = 300.0
MAX_RESTARTS = 3  # clean restarts per episode (Range ignored / changed file / 416)
PROGRESS_INTERVAL_S = 0.5
DURATION_TOLERANCE_S = 120.0  # tolerance = max(this, DURATION_TOLERANCE_PCT of the feed duration)
DURATION_TOLERANCE_PCT = 0.05
GIB = 1024**3

Progress = Callable[[str, int, "int | None", float], None]  # id, bytes done, total, bytes/s


# --------------------------------------------------------------------------- errors


class DownloadError(Exception):
    """A failed episode. ``keep_part`` leaves a resumable ``.part`` on disk."""

    def __init__(self, message: str, *, keep_part: bool = False, fatal: bool = False):
        super().__init__(message)
        self.keep_part = keep_part
        self.fatal = fatal  # environmental (disk): stop the whole batch


class DiskGuardError(DownloadError):
    def __init__(self, message: str):
        super().__init__(message, keep_part=True, fatal=True)


class DownloadLocked(RuntimeError):
    """Another ``ciw download`` holds ``data/download.lock``."""


class _Retry(Exception):
    def __init__(self, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


# ---------------------------------------------------------------------------- result


@dataclass
class DownloadResult:
    episode_id: str
    outcome: str  # downloaded | adopted | skipped | failed
    message: str = ""
    path: Path | None = None
    size: int = 0  # size of the file on disk
    fetched: int = 0  # bytes transferred by this call
    duration_s: float | None = None
    elapsed_s: float = 0.0
    fatal: bool = False

    @property
    def speed(self) -> float:
        return self.fetched / self.elapsed_s if self.elapsed_s > 0 and self.fetched else 0.0


@dataclass
class BatchSummary:
    results: list[DownloadResult] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def failed(self) -> int:
        return self.count("failed")


# ------------------------------------------------------------------------- naming


_TYPE_EXT = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}
_URL_EXT = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"}


def audio_extension(meta: EpisodeMeta) -> str:
    """mp3 for audio/mpeg, m4a for audio/x-m4a|audio/mp4, else the URL's suffix, else mp3."""
    ctype = meta.enclosure_type.split(";")[0].strip().casefold()
    if ctype in _TYPE_EXT:
        return _TYPE_EXT[ctype]
    suffix = Path(urlparse(meta.enclosure_url).path).suffix.lstrip(".").casefold()
    return suffix if suffix in _URL_EXT else "mp3"


def final_path(cfg: Config, meta: EpisodeMeta) -> Path:
    return cfg.audio_dir / f"{meta.guid}.{audio_extension(meta)}"


def part_path(final: Path) -> Path:
    return final.with_name(final.name + ".part")


def _sidecar(part: Path) -> Path:
    return part.with_name(part.name + ".json")


# ----------------------------------------------------------------------------- lock


@contextmanager
def download_lock(cfg: Config) -> Iterator[None]:
    """Hold ``<data_dir>/download.lock`` (flock) so two ``ciw download`` cannot run at once."""
    path = cfg.data_dir / "download.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DownloadLocked(
                f"another `ciw download` is already running (lock: {path})"
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- ffprobe


class ProbeError(Exception):
    pass


@dataclass
class AudioInfo:
    duration_s: float
    codec: str | None


def ffprobe_exe() -> str:
    beside = Path(sys.executable).parent / "ffprobe"
    if beside.is_file():
        return str(beside)
    return shutil.which("ffprobe") or "ffprobe"


def probe_file(path: Path) -> AudioInfo:
    """ffprobe sanity check: an audio stream with a positive duration, or ``ProbeError``."""
    cmd = [
        ffprobe_exe(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("ffprobe timed out") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe rejected the file: {proc.stderr.strip()[:200] or 'no output'}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ProbeError("ffprobe output was not JSON") from exc
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise ProbeError("no audio stream found")
    duration = None
    for raw in (data.get("format", {}).get("duration"), audio[0].get("duration")):
        try:
            duration = float(raw)
            break
        except (TypeError, ValueError):
            continue
    if duration is None or duration <= 0:
        raise ProbeError("ffprobe found no usable duration")
    return AudioInfo(duration, audio[0].get("codec_name"))


def check_duration(
    actual: float,
    expected: float | None,
    tol_s: float = DURATION_TOLERANCE_S,
    tol_pct: float = DURATION_TOLERANCE_PCT,
) -> None:
    """Raise ``DownloadError`` if ``actual`` is further than max(120 s, 5%) from the feed's."""
    if not expected or expected <= 0:
        return
    tol = max(tol_s, tol_pct * expected)
    if abs(actual - expected) > tol:
        raise DownloadError(
            f"duration {actual:.0f}s differs from the feed's {expected:.0f}s "
            f"by more than {tol:.0f}s"
        )


def _tolerance(cfg: Config) -> tuple[float, float]:
    return cfg.download.duration_tolerance_s, cfg.download.duration_tolerance_pct


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------ disk guard


def check_disk(cfg: Config, need_bytes: int | None) -> None:
    """Refuse when free space is below ``download.min_free_gb`` or below 2x ``need_bytes``."""
    cfg.audio_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cfg.audio_dir).free
    floor = cfg.download.min_free_gb * GIB
    if free < floor:
        raise DiskGuardError(
            f"only {free / GIB:.1f} GiB free on the audio disk, below download.min_free_gb "
            f"({cfg.download.min_free_gb:g})"
        )
    if need_bytes and free < 2 * need_bytes:
        raise DiskGuardError(
            f"only {free / GIB:.1f} GiB free; need 2x the expected {need_bytes / GIB:.2f} GiB"
        )


# --------------------------------------------------------------------- HTTP plumbing


def make_client(cfg: Config) -> HttpClient:
    """The HTTP client used by the CLI (tests monkeypatch this to inject a MockTransport)."""
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=30.0, pool=30.0)
    return HttpClient(timeout=timeout, secrets=cfg.secrets.secret_values())


def _parse_content_range(value: str) -> tuple[int | None, int | None, int | None]:
    """``bytes a-b/total`` -> (a, b, total); ``bytes */total`` -> (None, None, total)."""
    value = value.strip()
    if not value.lower().startswith("bytes"):
        return None, None, None
    spec = value[5:].strip()
    rng, _, total_s = spec.partition("/")
    total = int(total_s) if total_s.isdigit() else None
    if rng.strip() == "*" or "-" not in rng:
        return None, None, total
    a, _, b = rng.partition("-")
    if a.strip().isdigit() and b.strip().isdigit():
        return int(a), int(b), total
    return None, None, total


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after", "").strip()
    return min(float(raw), MAX_RETRY_AFTER_S) if raw.isdigit() else None


def _read_validators(part: Path) -> dict[str, object]:
    try:
        data = json.loads(_sidecar(part).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_validators(part: Path, data: dict[str, object]) -> None:
    _sidecar(part).write_text(json.dumps(data))


def _discard_part(part: Path) -> None:
    for p in (part, _sidecar(part)):
        p.unlink(missing_ok=True)


@dataclass
class _Fetch:
    """Mutable state of one episode's transfer (shared across retries and restarts)."""

    episode_id: str
    part: Path
    fetched: int = 0
    total: int | None = None
    restarts: int = 0
    best_size: int = 0  # largest .part seen so far (progress = growing past it)
    started: float = field(default_factory=time.monotonic)
    last_report: float = 0.0


def _stream(
    cfg: Config,
    client: HttpClient,
    url: str,
    fx: _Fetch,
    progress: Progress | None,
) -> bool:
    """One GET (resuming from the .part). Returns True when the transfer is complete, False when
    the part was thrown away and the caller should start over. Raises ``_Retry`` for transient
    trouble and ``DownloadError`` for permanent failures."""
    part = fx.part
    start = part.stat().st_size if part.exists() else 0
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
        "Accept": "audio/*, application/octet-stream;q=0.9, */*;q=0.1",
    }
    saved = _read_validators(part) if start else {}
    if start:
        headers["Range"] = f"bytes={start}-"
        validator = saved.get("etag") or saved.get("last_modified")
        if validator:
            headers["If-Range"] = str(validator)
    client.throttle()
    try:
        with client.http.stream("GET", url, headers=headers) as resp:
            status = resp.status_code
            if status == 416:
                _, _, total = _parse_content_range(resp.headers.get("content-range", ""))
                if start and total is not None and total == start:
                    fx.total = total
                    return True  # the .part is already complete
                if start:
                    log.info("%s: 416 for an oversized/stale .part; restarting", fx.episode_id)
                    _discard_part(part)
                    return False
                raise DownloadError("server answered 416 to a fresh request")
            if status == 429 or status >= 500:
                raise _Retry(f"HTTP {status}", _retry_after(resp))
            if status not in (200, 206):
                raise DownloadError(f"HTTP {status} (not retried)")
            ctype = resp.headers.get("content-type", "")
            if not _audioish(ctype):
                raise DownloadError(
                    f"unexpected content-type {ctype.split(';')[0].strip() or '(none)'!r} "
                    "(probably an HTML error page)",
                    keep_part=True,
                )
            append = False
            if status == 206:
                a, _, total = _parse_content_range(resp.headers.get("content-range", ""))
                if a != start or (saved.get("total") and total and saved["total"] != total):
                    log.info("%s: resume point or file changed; restarting", fx.episode_id)
                    _discard_part(part)
                    return False
                append = start > 0
                fx.total = total
                if total is None and resp.headers.get("content-length", "").isdigit():
                    fx.total = start + int(resp.headers["content-length"])
            else:  # 200: the full body, whatever Range we asked for
                if start:
                    log.info("%s: server ignored Range (HTTP 200); restarting clean", fx.episode_id)
                    start = 0
                cl = resp.headers.get("content-length", "")
                fx.total = int(cl) if cl.isdigit() else None
            if fx.total:
                check_disk(cfg, max(fx.total - (start if append else 0), 0))
            _write_validators(
                part,
                {
                    "total": fx.total,
                    "etag": resp.headers.get("etag"),
                    "last_modified": resp.headers.get("last-modified"),
                },
            )
            written = start if append else 0
            with open(part, "ab" if append else "wb") as fh:
                try:
                    for chunk in resp.iter_raw():
                        fh.write(chunk)
                        written += len(chunk)
                        fx.fetched += len(chunk)
                        now = time.monotonic()
                        if progress and now - fx.last_report >= PROGRESS_INTERVAL_S:
                            fx.last_report = now
                            progress(
                                fx.episode_id,
                                written,
                                fx.total,
                                fx.fetched / max(now - fx.started, 1e-6),
                            )
                finally:
                    fh.flush()
                    os.fsync(fh.fileno())
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _Retry(f"{type(exc).__name__}: {client.safe(exc)}") from exc
    except httpx.HTTPError as exc:  # e.g. too many redirects
        raise DownloadError(f"{type(exc).__name__}: {client.safe(exc)}") from exc
    size = part.stat().st_size
    if fx.total is not None and size < fx.total:
        raise _Retry(f"connection ended early ({size} of {fx.total} bytes)")
    return True


def fetch_to_part(
    cfg: Config,
    client: HttpClient,
    url: str,
    episode_id: str,
    part: Path,
    progress: Progress | None = None,
) -> tuple[int, int | None]:
    """Download ``url`` into ``part`` (resuming, retrying). Returns ``(bytes fetched, total)``."""
    fx = _Fetch(episode_id, part)
    failures = 0  # consecutive failed requests that made no progress
    while True:
        try:
            done = _stream(cfg, client, url, fx, progress)
        except _Retry as exc:
            size = part.stat().st_size if part.exists() else 0
            if size > fx.best_size:  # got further than ever before: not a stuck server
                fx.best_size = size
                failures = 0
            failures += 1
            if failures > MAX_RETRIES:
                raise DownloadError(
                    f"giving up after {MAX_RETRIES} retries ({exc.reason})", keep_part=True
                ) from None
            delay = (
                exc.retry_after if exc.retry_after is not None else BACKOFF_S * 2 ** (failures - 1)
            )
            log.warning(
                "%s: %s; retry %d/%d in %.0fs",
                episode_id,
                exc.reason,
                failures,
                MAX_RETRIES,
                delay,
            )
            client.sleep(delay)
            continue
        if not done:
            fx.restarts += 1
            if fx.restarts > MAX_RESTARTS:
                raise DownloadError("the server kept changing the file; giving up")
            continue
        size = part.stat().st_size
        if fx.total is not None and size > fx.total:
            raise DownloadError(f"received {size} bytes but the server declared {fx.total}")
        return fx.fetched, fx.total


# --------------------------------------------------------------------------- episodes


def load_meta(cfg: Config, episode_id: str) -> EpisodeMeta:
    path = meta_path(cfg, episode_id)
    try:
        return EpisodeMeta.load(path)
    except (OSError, ValueError) as exc:
        raise DownloadError(f"meta.json missing or unreadable (run `ciw poll`): {exc}") from exc


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _record(
    conn: sqlite3.Connection, meta: EpisodeMeta, final: Path, info: AudioInfo, *, sha: str | None
) -> int:
    size = final.stat().st_size
    state.mark_downloaded(
        conn,
        meta.guid,
        audio_path=str(final),
        audio_bytes=size,
        audio_duration_s=info.duration_s,
        audio_sha256=sha,
        audio_downloaded_from=meta.audio_source,
    )
    state.checkpoint(conn)
    return size


def _is_downloaded(row: sqlite3.Row) -> bool:
    return row["stage"] == "downloaded" and bool(row["audio_path"])


def _adopt(
    cfg: Config, conn: sqlite3.Connection, client: HttpClient, meta: EpisodeMeta, final: Path
) -> DownloadResult:
    """Accept a file already on disk if ffprobe and a Range probe agree with it. Read-only."""
    size = final.stat().st_size
    try:
        info = probe_file(final)
        check_duration(info.duration_s, meta.duration_s, *_tolerance(cfg))
    except (ProbeError, DownloadError) as exc:
        raise DownloadError(f"existing file {final.name} cannot be adopted: {exc}") from None
    remote = probe_audio(client, meta.enclosure_url, range_only=True)
    if not remote.ok:
        reason = remote.error or f"HTTP {remote.status} {remote.content_type}"
        raise DownloadError(f"could not confirm {final.name} with the server ({reason})")
    if remote.content_length is not None and remote.content_length != size:
        raise DownloadError(
            f"existing file {final.name} is {size} bytes but the server says "
            f"{remote.content_length}; not adopted (use --redownload to replace it)"
        )
    sha = sha256_file(final)
    _record(conn, meta, final, info, sha=sha)
    log.info(
        "%s: adopted %s (%d bytes, matches the server's size from a Range probe; "
        "no audio body was fetched)",
        meta.guid,
        final.name,
        size,
    )
    return DownloadResult(
        meta.guid,
        "adopted",
        f"adopted existing file ({size} bytes, {info.duration_s:.0f}s, size matches the server)",
        final,
        size,
        0,
        info.duration_s,
    )


def _verify_local(
    cfg: Config,
    conn: sqlite3.Connection,
    client: HttpClient,
    meta: EpisodeMeta,
    row: sqlite3.Row,
    path: Path,
) -> DownloadResult:
    """``--verify``: re-check a downloaded file (size, hash if known, ffprobe, remote size)."""
    size = path.stat().st_size
    if row["audio_bytes"] is not None and size != row["audio_bytes"]:
        raise DownloadError(f"{path.name} is {size} bytes, state says {row['audio_bytes']}")
    sha = sha256_file(path)
    if row["audio_sha256"] and row["audio_sha256"] != sha:
        raise DownloadError(f"{path.name} sha256 differs from the recorded one")
    try:
        info = probe_file(path)
        check_duration(info.duration_s, meta.duration_s, *_tolerance(cfg))
    except ProbeError as exc:
        raise DownloadError(f"{path.name}: {exc}") from None
    note = ""
    remote = probe_audio(client, meta.enclosure_url, range_only=True)
    if remote.ok and remote.content_length not in (None, size):
        note = f"; note: the server now reports {remote.content_length} bytes"
    if not row["audio_sha256"]:
        conn.execute("UPDATE episodes SET audio_sha256=? WHERE episode_id=?", (sha, meta.guid))
        state.checkpoint(conn)
    return DownloadResult(
        meta.guid, "skipped", f"verified ok ({size} bytes){note}", path, size, 0, info.duration_s
    )


def download_episode(
    cfg: Config,
    conn: sqlite3.Connection,
    episode: sqlite3.Row | str,
    *,
    force: bool = False,
    verify: bool = False,
    progress: Progress | None = None,
    client: HttpClient | None = None,
) -> DownloadResult:
    """Download (or adopt, or skip) one episode. Never raises for an episode-level failure: the
    error is recorded in state and returned as ``outcome='failed'``.

    ``conn`` is the state connection; ``episode`` a row from ``episodes`` or an episode_id.
    """
    episode_id = episode if isinstance(episode, str) else episode["episode_id"]
    row = state.get_episode(conn, episode_id)
    started = time.monotonic()
    if row is None:
        return DownloadResult(episode_id, "failed", "unknown episode (not in state)")
    own_client = client is None
    client = client or make_client(cfg)
    final: Path | None = None
    part: Path | None = None
    keep_part = False
    try:
        meta = load_meta(cfg, episode_id)
        final = final_path(cfg, meta)
        part = part_path(final)
        res = _run(cfg, conn, client, row, meta, final, part, force, verify, progress)
    except DownloadError as exc:
        keep_part = exc.keep_part
        msg = redact(str(exc), cfg.secrets.secret_values())
        log.warning("%s: %s", episode_id, msg)
        if part is not None and not keep_part:
            _discard_part(part)
        # A failed re-download must not demote an episode that already has a good file.
        state.set_error(conn, episode_id, msg)
        state.checkpoint(conn)
        res = DownloadResult(episode_id, "failed", msg, fatal=exc.fatal)
    finally:
        if own_client:
            client.close()
    res.elapsed_s = time.monotonic() - started
    return res


def _run(
    cfg: Config,
    conn: sqlite3.Connection,
    client: HttpClient,
    row: sqlite3.Row,
    meta: EpisodeMeta,
    final: Path,
    part: Path,
    force: bool,
    verify: bool,
    progress: Progress | None,
) -> DownloadResult:
    episode_id = meta.guid
    recorded = Path(row["audio_path"]) if row["audio_path"] else None

    if _is_downloaded(row) and not force:
        assert recorded is not None
        if recorded.is_file():
            if row["audio_source"] != row["audio_downloaded_from"] and row["audio_downloaded_from"]:
                log.info(
                    "%s: audio source is now %s but the stored copy came from %s; keeping it "
                    "(use --redownload to replace it)",
                    episode_id,
                    row["audio_source"],
                    row["audio_downloaded_from"],
                )
            if verify:
                return _verify_local(cfg, conn, client, meta, row, recorded)
            return DownloadResult(
                episode_id, "skipped", "already downloaded", recorded, recorded.stat().st_size
            )
        log.warning(
            "%s: state says downloaded but %s is missing; fetching again", episode_id, recorded
        )
        state.set_error(conn, episode_id, "audio file missing on disk", stage="discovered")
        state.checkpoint(conn)

    if final.is_file() and not force:
        return _adopt(cfg, conn, client, meta, final)

    # ---- download
    check_disk(cfg, meta.enclosure_length_hint)
    log.info("%s: downloading (%s copy)", episode_id, meta.audio_source)
    fetched, total = fetch_to_part(cfg, client, meta.enclosure_url, episode_id, part, progress)
    size = part.stat().st_size
    if total is not None and size != total:
        raise DownloadError(f"size mismatch: got {size} bytes, server declared {total}")
    try:
        info = probe_file(part)
    except ProbeError as exc:
        raise DownloadError(f"downloaded file failed ffprobe: {exc}") from None
    check_duration(info.duration_s, meta.duration_s, *_tolerance(cfg))
    sha = sha256_file(part)
    _fsync_file(part)
    os.replace(part, final)  # atomic; for --redownload this swaps the verified file in
    _fsync_dir(final.parent)
    _sidecar(part).unlink(missing_ok=True)
    if recorded and recorded != final and recorded.parent == final.parent and recorded.is_file():
        recorded.unlink()  # a replaced copy under another extension
    _record(conn, meta, final, info, sha=sha)
    note = "" if total is not None else " (server gave no size; ffprobe check only)"
    return DownloadResult(
        episode_id,
        "downloaded",
        f"{size} bytes, {info.duration_s:.0f}s{note}",
        final,
        size,
        fetched,
        info.duration_s,
    )


# -------------------------------------------------------------------------- selection


def is_source(row: sqlite3.Row, source: str) -> bool:
    """acast/hrn by episode identity; patreon by where the audio comes from."""
    if source == "patreon":
        return row["audio_source"] == "patreon" or row["source"] == "patreon"
    return row["source"] == source


def select_episodes(
    conn: sqlite3.Connection,
    *,
    latest: int | None = None,
    episode_ids: tuple[str, ...] = (),
    source: str | None = None,
    since: date | None = None,
    all_: bool = False,
    limit: int | None = None,
    include_done: bool = False,
) -> tuple[list[sqlite3.Row], list[str]]:
    """Choose episodes. Returns ``(rows newest first, unknown episode ids)``.

    ``--episode`` ids are taken as given (downloaded ones are then skipped or verified); the
    ``--all`` / ``--since`` only pick episodes that are not downloaded yet unless ``include_done``;
    ``latest`` picks the N newest whatever their stage.
    """
    rows = state.list_episodes(conn)
    filtered = [
        r
        for r in rows
        if (not source or is_source(r, source)) and (not since or r["pub_date"][:10] >= str(since))
    ]
    pending = [r for r in filtered if include_done or not _is_downloaded(r)]
    chosen: dict[str, sqlite3.Row] = {}
    if all_ or (since and latest is None and not episode_ids):
        chosen.update((r["episode_id"], r) for r in pending)
    if latest is not None:
        # The N newest episodes; the ones already downloaded are skipped when processed, so a
        # second identical run does nothing (and never creeps on to older episodes).
        chosen.update((r["episode_id"], r) for r in filtered[:latest])
    by_id = {r["episode_id"]: r for r in rows}
    unknown = []
    for eid in episode_ids:
        if eid in by_id:
            chosen[eid] = by_id[eid]
        else:
            unknown.append(eid)
    ordered = sorted(chosen.values(), key=lambda r: (r["pub_date"], r["episode_id"]), reverse=True)
    if limit is not None:
        ordered = ordered[:limit]
    return ordered, unknown


@dataclass
class Plan:
    action: str  # download | adopt | skip | redownload | verify | blocked
    detail: str = ""


def plan_episode(
    cfg: Config, row: sqlite3.Row, *, force: bool = False, verify: bool = False
) -> tuple[Plan, EpisodeMeta | None]:
    """What ``download_episode`` would do, without touching the network or the disk."""
    try:
        meta = load_meta(cfg, row["episode_id"])
    except DownloadError as exc:
        return Plan("blocked", str(exc)), None
    final = final_path(cfg, meta)
    if _is_downloaded(row) and not force and Path(row["audio_path"]).is_file():
        return (Plan("verify", "verify local file") if verify else Plan("skip", "downloaded")), meta
    if final.is_file() and not force:
        return Plan("adopt", f"{final.name} exists on disk"), meta
    if force and (_is_downloaded(row) or final.is_file()):
        return Plan("redownload", f"replace {final.name}"), meta
    return Plan("download", ""), meta


# ----------------------------------------------------------------------------- batch


def download_batch(
    cfg: Config,
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    force: bool = False,
    verify: bool = False,
    progress: Progress | None = None,
    client: HttpClient | None = None,
    on_result: Callable[[sqlite3.Row, DownloadResult], None] | None = None,
) -> BatchSummary:
    """Process ``rows`` one after another under the download lock. A failure never stops the
    batch, except a fatal one (disk guard), which would hit every following episode too."""
    summary = BatchSummary()
    own_client = client is None
    client = client or make_client(cfg)
    try:
        with download_lock(cfg):
            for row in rows:
                res = download_episode(
                    cfg, conn, row, force=force, verify=verify, progress=progress, client=client
                )
                summary.results.append(res)
                if on_result:
                    on_result(row, res)
                if res.fatal:
                    break
    finally:
        if own_client:
            client.close()
    return summary
