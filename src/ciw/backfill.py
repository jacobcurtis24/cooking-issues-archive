"""The resumable backfill queue: download, transcribe and index every episode that still needs it.

``plan_backfill`` decides *what* (ordered episodes, estimates, disk check) without side effects;
``Backfill.run`` does it. State is the queue: an episode is "done" when its transcript is current
and indexed, so killing the run at any point and starting it again simply continues (``.part``
downloads resume, finished work is skipped). Nothing here reimplements downloading or
transcribing: it drives :func:`ciw.download.download_episode` and
:func:`ciw.transcribe.transcribe_episode` and adds ordering, failure isolation with cool-downs,
a prefetching download thread, graceful stop and observability.

Pipeline: one downloader thread (sequential, <= 2 requests/s via the shared ``HttpClient``)
prefetches at most ``backfill.prefetch`` episodes ahead of the single-threaded transcriber (the
main thread; one GPU). A slot semaphore enforces the bound: a slot is taken before a download
starts and given back when the transcriber *takes* that episode.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ciw import download as dl
from ciw import state
from ciw import transcribe as tx
from ciw.asr.base import Transcriber
from ciw.config import Config, guard_path, redact
from ciw.transcript import gap_stats

log = logging.getLogger("ciw.backfill")

SOURCES = ("acast", "hrn", "patreon")
MODES = ("all", "download-only", "transcribe-only")
DEFAULT_SPEED_X = 35.0  # realtime multiple of WhisperX large-v3 on the RTX 3080 (T5: 35.6x)
DEFAULT_BYTES_PER_S = 16000.0  # ~60 MB per hour of audio, the Acast/HRN average seen so far
DEFAULT_DOWNLOAD_MBPS = 15.0  # observed 15-37 MB/s
FALLBACK_FEED_DURATION_S = 3600.0
TRANSCRIPT_BYTES = 1_500_000  # transcript.json (~0.7 MB) + its share of the search index
CONSECUTIVE_FAILURES_ALERT = 3
ROLLING_WINDOW = 10
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED, EXIT_DISK = 0, 1, 2, 3, 4


class BackfillLocked(RuntimeError):
    """Another process holds the download or transcribe lock."""


# =========================================================================== planning


@dataclass
class Selection:
    sources: tuple[str, ...] = ("acast", "hrn")
    since: date | None = None
    until: date | None = None
    limit: int | None = None
    oldest_first: bool = False
    retry_failed: bool = False  # ignore cool-downs and the attempt limit
    stale: bool = False  # also redo transcripts made by another model/version/prompt
    mode: str = "all"  # all | download-only | transcribe-only


@dataclass
class Item:
    """One episode the run will work on (``need`` says what is missing)."""

    episode_id: str
    source: str
    title: str
    pub_date: str
    need: str  # download+transcribe | transcribe | download
    feed_duration_s: float | None
    audio_s: float  # real audio length if known, else the estimate
    audio_known: bool
    est_bytes: int  # still to download (0 when the file is already on disk)
    attempts: int = 0
    note: str = ""

    @property
    def needs_download(self) -> bool:
        return self.need in ("download+transcribe", "download")

    @property
    def needs_transcribe(self) -> bool:
        return self.need in ("download+transcribe", "transcribe")


@dataclass
class DiskCheck:
    path: Path
    free_bytes: int
    need_bytes: int  # estimated downloads
    floor_bytes: int  # download.min_free_gb
    data_free_bytes: int
    data_need_bytes: int

    @property
    def audio_ok(self) -> bool:
        return self.free_bytes >= self.need_bytes + self.floor_bytes

    @property
    def data_ok(self) -> bool:
        return self.data_free_bytes >= self.data_need_bytes + dl.GIB

    @property
    def ok(self) -> bool:
        return self.audio_ok and self.data_ok


@dataclass
class Plan:
    selection: Selection
    items: list[Item] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> episodes
    index_only: list[str] = field(default_factory=list)  # transcribed + current, not indexed
    speed_x: float = DEFAULT_SPEED_X
    speed_basis: str = "default"
    bytes_per_s: dict[str, float] = field(default_factory=dict)  # per source, "*" = fallback
    bytes_basis: str = "default"
    disk: DiskCheck | None = None
    candidates: int = 0  # episodes of the chosen sources/dates before filtering

    def count(self, need: str | None = None, source: str | None = None) -> int:
        return sum(
            1
            for i in self.items
            if (need is None or i.need == need) and (source is None or i.source == source)
        )

    @property
    def audio_s(self) -> float:
        return sum(i.audio_s for i in self.items if i.needs_transcribe)

    @property
    def download_bytes(self) -> int:
        return sum(i.est_bytes for i in self.items)

    @property
    def gpu_s(self) -> float:
        return self.audio_s / self.speed_x if self.speed_x > 0 else 0.0

    @property
    def download_s(self) -> float:
        return self.download_bytes / (DEFAULT_DOWNLOAD_MBPS * 1e6)

    @property
    def wall_s(self) -> float:
        """Downloads overlap the GPU, so the slower of the two."""
        return max(self.gpu_s, self.download_s)

    def sources_in_plan(self) -> list[str]:
        return [s for s in self.selection.sources if self.count(source=s)]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def measured_speed(conn: sqlite3.Connection) -> tuple[float, str]:
    """Realtime multiple measured from transcribed episodes (audio seconds / ASR seconds)."""
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(audio_duration_s) a, SUM(asr_runtime_s) r FROM episodes "
        "WHERE asr_runtime_s > 0 AND audio_duration_s > 0 AND transcribed_at IS NOT NULL"
    ).fetchone()
    if row["n"] and row["r"]:
        return row["a"] / row["r"], f"measured: {row['n']} transcribed episodes"
    return DEFAULT_SPEED_X, f"default {DEFAULT_SPEED_X:g}x (nothing transcribed yet)"


def measured_bytes_per_s(conn: sqlite3.Connection) -> tuple[dict[str, float], str]:
    """Bytes per second of audio from downloaded episodes, per source and overall (``*``)."""
    rows = conn.execute(
        "SELECT source, COUNT(*) n, SUM(audio_bytes) b, SUM(audio_duration_s) d FROM episodes "
        "WHERE audio_bytes > 0 AND audio_duration_s > 0 GROUP BY source"
    ).fetchall()
    out: dict[str, float] = {}
    total_b = total_d = n = 0.0
    for r in rows:
        out[r["source"]] = r["b"] / r["d"]
        total_b, total_d, n = total_b + r["b"], total_d + r["d"], n + r["n"]
    if not n:
        out["*"] = DEFAULT_BYTES_PER_S
        return out, f"default {DEFAULT_BYTES_PER_S / 1000:.0f} kB/s (nothing downloaded yet)"
    out["*"] = total_b / total_d
    return out, f"measured: {int(n)} downloaded episodes"


def real_over_feed_ratio(conn: sqlite3.Connection) -> float:
    """How much longer real audio is than the feed claims (mean over downloaded episodes)."""
    row = conn.execute(
        "SELECT SUM(audio_duration_s) a, SUM(duration_s) f FROM episodes "
        "WHERE audio_duration_s > 0 AND duration_s > 0"
    ).fetchone()
    return row["a"] / row["f"] if row["a"] and row["f"] else 1.0


def indexed_ids(cfg: Config) -> set[str]:
    """Episodes whose transcript is in the search index (empty if there is no index)."""
    from ciw import search_index as si

    if not si.index_exists(cfg.search_db):
        return set()
    try:
        with si.open_readonly(cfg.search_db) as conn:
            return {
                r["episode_id"]
                for r in conn.execute("SELECT episode_id FROM indexed WHERE has_transcript = 1")
            }
    except Exception:  # an unreadable index only means "re-index", never a failed plan
        return set()


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    import shutil

    return shutil.disk_usage(probe).free


def plan_backfill(
    cfg: Config,
    conn: sqlite3.Connection,
    selection: Selection,
    transcriber: Transcriber | None,
    *,
    now: datetime | None = None,
) -> Plan:
    """Decide the ordered work list. Touches nothing: no network, no writes, no model load.

    Per episode, in order: wrong source/dates are not candidates; a current, indexed transcript
    is ``done`` (a current one that is not indexed is only re-indexed); a cool-down in the future
    or an exhausted attempt budget parks the episode (``retry_failed`` overrides); the rest
    needs a download, a transcription or both. ``limit`` counts episodes that need work.
    """
    now = now or datetime.now(UTC)
    bf = cfg.backfill
    plan = Plan(selection)
    plan.speed_x, plan.speed_basis = measured_speed(conn)
    plan.bytes_per_s, plan.bytes_basis = measured_bytes_per_s(conn)
    ratio = real_over_feed_ratio(conn)
    indexed = indexed_ids(cfg)
    skipped = {
        "done": 0,
        "cooldown": 0,
        "out-of-attempts": 0,
        "stale": 0,
        "has-audio": 0,
        "no-audio": 0,
        "no-meta": 0,
    }

    rows: list[sqlite3.Row] = []
    seen: set[str] = set()
    everything = state.list_episodes(conn)
    for source in selection.sources:
        chosen = [r for r in everything if dl.is_source(r, source) and r["episode_id"] not in seen]
        chosen.sort(
            key=lambda r: (r["pub_date"], r["episode_id"]), reverse=not selection.oldest_first
        )
        seen.update(r["episode_id"] for r in chosen)
        rows.extend(chosen)
    for row in rows:
        day = row["pub_date"][:10]
        if (selection.since and day < str(selection.since)) or (
            selection.until and day > str(selection.until)
        ):
            continue
        plan.candidates += 1
        eid = row["episode_id"]
        dplan, meta = dl.plan_episode(cfg, row)
        t_action = "transcribe"
        t_detail = ""
        if transcriber is not None:
            tplan = tx.plan_for_output(
                tx.transcript_path(cfg, eid), transcriber, force=False, stale=selection.stale
            )
            t_action, t_detail = tplan.action, tplan.detail
        transcript_current = (
            t_action == "skip" and row["stage"] in state.STAGES[state.STAGES.index("transcribed") :]
        )
        if transcript_current:
            skipped["done"] += 1
            if eid not in indexed:
                plan.index_only.append(eid)
            continue
        if t_action == "stale":
            skipped["stale"] += 1
            continue
        if not selection.retry_failed:
            until = _parse_iso(row["next_retry_at"])
            if row["attempts"] >= bf.max_attempts:
                skipped["out-of-attempts"] += 1
                continue
            if until is not None and until > now:
                skipped["cooldown"] += 1
                continue
        if dplan.action == "blocked":
            skipped["no-meta"] += 1
            continue
        has_audio = dplan.action == "skip"
        factor = ratio if not row["audio_duration_s"] else 1.0
        feed = row["duration_s"]
        known = bool(row["audio_duration_s"])
        audio_s = row["audio_duration_s"] or (feed or FALLBACK_FEED_DURATION_S) * factor
        if selection.mode == "download-only":
            if has_audio:
                skipped["has-audio"] += 1
                continue
            need = "download"
        elif selection.mode == "transcribe-only":
            if not has_audio:
                skipped["no-audio"] += 1
                continue
            need = "transcribe"
        else:
            need = "transcribe" if has_audio else "download+transcribe"
        bps = plan.bytes_per_s.get(row["source"], plan.bytes_per_s["*"])
        est_bytes = 0 if has_audio or dplan.action == "adopt" else int(audio_s * bps)
        plan.items.append(
            Item(
                eid,
                row["source"],
                row["title"],
                row["pub_date"],
                need,
                feed,
                float(audio_s),
                known,
                est_bytes,
                attempts=row["attempts"] or 0,
                note=t_detail if t_action == "retranscribe" else dplan.detail,
            )
        )
    if selection.limit is not None:
        plan.items = plan.items[: selection.limit]
    plan.skipped = {k: v for k, v in skipped.items() if v}
    est_data = sum(1 for i in plan.items if i.needs_transcribe) * TRANSCRIPT_BYTES
    plan.disk = DiskCheck(
        cfg.audio_dir,
        _free_bytes(cfg.audio_dir),
        plan.download_bytes,
        int(cfg.download.min_free_gb * dl.GIB),
        _free_bytes(cfg.data_dir),
        est_data,
    )
    return plan


# ----------------------------------------------------------------------- formatting


def fmt_hms(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def fmt_gib(n: float) -> str:
    return f"{n / dl.GIB:.1f} GiB"


def format_plan(plan: Plan, *, show_first: int = 8) -> str:
    sel = plan.selection
    lines = []
    order = "oldest first" if sel.oldest_first else "newest first"
    lines.append(
        f"Backfill plan: sources {', '.join(sel.sources)} ({order}), mode {sel.mode}"
        + (f", since {sel.since}" if sel.since else "")
        + (f", until {sel.until}" if sel.until else "")
        + (f", limit {sel.limit}" if sel.limit is not None else "")
    )
    both = plan.count("download+transcribe")
    tonly = plan.count("transcribe")
    donly = plan.count("download")
    lines.append(
        f"  episodes needing work: {len(plan.items)} of {plan.candidates} considered "
        f"(download+transcribe {both}, transcribe only {tonly}"
        + (f", download only {donly}" if donly else "")
        + ")"
    )
    for source in plan.selection.sources:
        n = plan.count(source=source)
        if n:
            extra = f", download only {plan.count('download', source)}" if donly else ""
            lines.append(
                f"    {source}: {n}  "
                f"(download+transcribe {plan.count('download+transcribe', source)}"
                f", transcribe only {plan.count('transcribe', source)}{extra})"
            )
    if plan.skipped or plan.index_only:
        parts = [f"{k} {v}" for k, v in plan.skipped.items()]
        if plan.index_only:
            parts.append(f"{len(plan.index_only)} done but not indexed (will be indexed)")
        lines.append("  skipped: " + ", ".join(parts))
    unknown = sum(1 for i in plan.items if i.needs_transcribe and not i.audio_known)
    lines.append(
        f"  audio: {plan.audio_s / 3600:.1f} h to transcribe"
        + (
            f" (real length known for {len(plan.items) - unknown}, feed length estimated for "
            f"{unknown})"
            if unknown
            else ""
        )
    )
    kbps = (
        ", ".join(f"{s} {b / 1000:.1f} kB/s" for s, b in plan.bytes_per_s.items() if s != "*")
        or f"{plan.bytes_per_s.get('*', 0) / 1000:.1f} kB/s"
    )
    lines.append(
        f"  download: {fmt_gb(plan.download_bytes)} ESTIMATE = audio length x observed bytes/s "
        f"({kbps}; {plan.bytes_basis}); files already on disk count 0"
    )
    lines.append(
        f"  GPU: {plan.gpu_s / 3600:.1f} h ESTIMATE at {plan.speed_x:.1f}x realtime "
        f"({plan.speed_basis})"
    )
    lines.append(
        f"  wall time: about {fmt_hms(plan.wall_s)} (downloads overlap the GPU; download alone "
        f"{fmt_hms(plan.download_s)} at ~{DEFAULT_DOWNLOAD_MBPS:g} MB/s)"
    )
    d = plan.disk
    if d:
        lines.append(
            f"  disk: {fmt_gib(d.free_bytes)} free on {d.path} (needs {fmt_gib(d.need_bytes)} + "
            f"{fmt_gib(d.floor_bytes)} floor): {'ok' if d.audio_ok else 'NOT ENOUGH'}; "
            f"{fmt_gib(d.data_free_bytes)} free where data/ lives (needs ~"
            f"{d.data_need_bytes / 1e6:.0f} MB): {'ok' if d.data_ok else 'NOT ENOUGH'}"
        )
    if plan.items and show_first:
        lines.append("  first in order:")
        for i in plan.items[:show_first]:
            lines.append(
                f"    {i.episode_id[:26]:<26} {i.pub_date[:10]} {i.source:<5} "
                f"{fmt_hms(i.audio_s):>8}  {i.need:<19} {i.title[:40]}"
                + (f"  [attempt {i.attempts}]" if i.attempts else "")
            )
        if len(plan.items) > show_first:
            lines.append(f"    ... and {len(plan.items) - show_first} more")
    return "\n".join(lines)


# =========================================================================== event log


class EventLog:
    """``backfill-YYYYMMDD.jsonl`` in ``log_dir``: one JSON object per line, a new file each UTC
    day, only the newest ``keep`` files are retained, and a day's file stops growing at
    ``max_bytes`` (one final ``log-capped`` event, then silence) so logs cannot fill ``/``."""

    def __init__(self, directory: Path, *, keep: int = 14, max_bytes: int = 20_000_000):
        guard_path(directory, "backfill log_dir")
        self.dir = directory
        self.keep = keep
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self._capped: set[str] = set()
        self._pruned_for: str | None = None

    def path_for(self, day: str) -> Path:
        return self.dir / f"backfill-{day}.jsonl"

    def _prune(self) -> None:
        files = sorted(self.dir.glob("backfill-*.jsonl"))
        for old in files[: max(0, len(files) - self.keep)]:
            try:
                old.unlink()
            except OSError:
                pass

    def write(self, event: str, **fields: Any) -> None:
        stamp = datetime.now(UTC)
        day = stamp.strftime("%Y%m%d")
        record = {"ts": stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "event": event}
        record.update({k: v for k, v in fields.items() if v is not None})
        with self.lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                if self._pruned_for != day:
                    self._prune()
                    self._pruned_for = day
                path = self.path_for(day)
                if day in self._capped:
                    return
                size = path.stat().st_size if path.exists() else 0
                if size >= self.max_bytes:
                    record = {
                        "ts": record["ts"],
                        "event": "log-capped",
                        "max_bytes": self.max_bytes,
                    }
                    self._capped.add(day)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                if (
                    self._pruned_for == day
                    and len(list(self.dir.glob("backfill-*.jsonl"))) > self.keep
                ):
                    self._prune()
            except OSError as exc:  # a full disk must not kill the run
                log.warning("could not write the event log: %s", exc)


# =========================================================================== execution


@dataclass
class RunSummary:
    done: int = 0  # transcribed this run
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    attempted: int = 0
    wall_s: float = 0.0
    audio_s: float = 0.0  # transcribed this run
    work_s: float = 0.0  # time spent inside transcription
    suspects: list[str] = field(default_factory=list)
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # id, stage, message
    stopped: str | None = None  # why the run ended early
    fatal: str | None = None
    waiting: int = 0  # downloaded but not transcribed (left for the next run)
    max_ahead: int = 0

    @property
    def speed_x(self) -> float | None:
        return self.audio_s / self.work_s if self.work_s > 0 else None

    @property
    def exit_code(self) -> int:
        if self.fatal:
            return EXIT_DISK
        return EXIT_FAILED if self.failed else EXIT_OK

    def line(self) -> str:
        speed = f", {self.speed_x:.1f}x realtime" if self.speed_x else ""
        return (
            f"done {self.done}, downloaded {self.downloaded}, failed {self.failed}, skipped "
            f"{self.skipped} in {fmt_hms(self.wall_s)}; {self.audio_s / 3600:.2f} h of audio"
            f"{speed}; suspect {len(self.suspects)}"
        )


Echo = Callable[[str], None]
DownloadFn = Callable[[Config, str, dl.HttpClient], dl.DownloadResult]
_END = object()


def _read_lock_pid(path: Path) -> str:
    try:
        return path.read_text().strip() or "?"
    except OSError:
        return "?"


class Backfill:
    """Run a :class:`Plan`. ``download_fn`` / ``transcriber`` are injectable for tests."""

    def __init__(
        self,
        cfg: Config,
        plan: Plan,
        *,
        transcriber: Transcriber | None = None,
        download_fn: DownloadFn | None = None,
        echo: Echo = print,
        max_hours: float | None = None,
        install_signals: bool = True,
        abort_fn: Callable[[int], None] = os._exit,
        notify: Callable[..., bool] | None = None,
        started_at: float | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.plan = plan
        self.mode = plan.selection.mode
        self.transcriber = transcriber
        self.download_fn = download_fn or self._default_download
        self.echo = echo
        self.max_hours = max_hours
        self.install_signals = install_signals
        self.abort_fn = abort_fn
        self.notify = notify
        self.clock = clock
        self.started_at = started_at if started_at is not None else clock()
        bf = cfg.backfill
        self.events = EventLog(
            bf.log_dir, keep=bf.log_keep_files, max_bytes=int(bf.log_max_mb * 1_000_000)
        )
        self.summary = RunSummary()
        self._signals = 0
        self._signal_name: str | None = None
        self._fatal = threading.Event()
        self._halt = threading.Event()  # the main loop is done: the downloader must stop too
        self._stop_logged: str | None = None
        self._slots = threading.Semaphore(bf.prefetch)
        self._queue: queue.Queue[Any] = queue.Queue()
        self._count_lock = threading.Lock()
        self._dl_started = 0
        self._taken = 0
        self._client: dl.HttpClient | None = None
        self._streak = 0
        self._alerted = False
        self._recent: deque[tuple[float, float]] = deque(maxlen=ROLLING_WINDOW)
        self._done_ids: set[str] = set()
        self._stop_file_seen = False
        self._index = {i.episode_id: n for n, i in enumerate(plan.items, 1)}

    # ----------------------------------------------------------------- stop control

    def handle_signal(self, signum: int, _frame: Any = None) -> None:
        """First SIGINT/SIGTERM: finish the current unit and exit. Second: abort right now."""
        name = signal.Signals(signum).name
        self._signals += 1
        if self._signals == 1:
            self._signal_name = name
            self.echo(
                f"{name} received: finishing the current episode, then stopping "
                "(send it again to abort immediately)"
            )
            self.events.write("signal", signal=name, action="stop-after-current")
            return
        self.events.write("abort", signal=name)
        self.echo(f"{name} again: aborting now (a .part download resumes on the next run)")
        self.abort_fn(130)

    def stop_reason(self) -> str | None:
        if self._signal_name:
            return f"signal {self._signal_name}"
        if self.cfg.backfill.stop_file.exists():
            self._stop_file_seen = True
            return f"stop file {self.cfg.backfill.stop_file.name}"
        if self.max_hours is not None and self.clock() - self.started_at >= self.max_hours * 3600:
            return f"--max-hours {self.max_hours:g} reached"
        if self._halt.is_set():
            return "finished"
        return None

    def _note_stop(self, reason: str) -> None:
        if self._stop_logged != reason:
            self._stop_logged = reason
            self.echo(f"stopping: {reason}")
            self.events.write("stop", reason=reason)

    # ----------------------------------------------------------------------- locks

    def _locks(self, stack: ExitStack) -> None:
        try:
            if self.mode != "download-only":
                stack.enter_context(tx.transcribe_lock(self.cfg))
            if self.mode != "transcribe-only":
                stack.enter_context(dl.download_lock(self.cfg))
        except (tx.TranscribeLocked, dl.DownloadLocked) as exc:
            which = "transcribe" if isinstance(exc, tx.TranscribeLocked) else "download"
            path = self.cfg.data_dir / f"{which}.lock"
            raise BackfillLocked(
                f"cannot start: the {which} lock is held (pid {_read_lock_pid(path)}). "
                "Another `ciw backfill run`, `ciw download` or `ciw transcribe` is running; "
                f"wait for it or stop it first (lock file: {path})."
            ) from None

    # --------------------------------------------------------------------- download

    def _default_download(self, cfg: Config, episode_id: str, client: dl.HttpClient):
        with state.connect(cfg.state_db) as conn:
            return dl.download_episode(cfg, conn, episode_id, client=client)

    def _download_loop(self) -> None:
        try:
            for item in self.plan.items:
                while not self._slots.acquire(timeout=0.2):
                    if self.stop_reason() or self._fatal.is_set():
                        return
                if self.stop_reason() or self._fatal.is_set():
                    self._slots.release()
                    return
                with self._count_lock:
                    self._dl_started += 1
                    self.summary.max_ahead = max(
                        self.summary.max_ahead, self._dl_started - self._taken
                    )
                result = None
                if item.needs_download:
                    self._ev("download-start", item)
                    self._line("download", item)
                    t0 = time.monotonic()
                    try:
                        if self._client is None:
                            self._client = dl.make_client(self.cfg)
                        result = self.download_fn(self.cfg, item.episode_id, self._client)
                    except Exception as exc:
                        result = dl.DownloadResult(
                            item.episode_id,
                            "failed",
                            redact(
                                f"{type(exc).__name__}: {exc}", self.cfg.secrets.secret_values()
                            ),
                        )
                    result.elapsed_s = result.elapsed_s or time.monotonic() - t0
                self._queue.put((item, result))
                if result is not None and result.fatal:
                    self._fatal.set()
                    return
        finally:
            self._queue.put(_END)

    # ---------------------------------------------------------------------- logging

    def _ev(self, event: str, item: Item | None = None, **fields: Any) -> None:
        if item is not None:
            fields.update(
                episode_id=item.episode_id,
                title=item.title,
                source=item.source,
                n=self._index.get(item.episode_id),
                total=len(self.plan.items),
            )
        self.events.write(event, **fields)

    def _eta(self, after: Item | None) -> tuple[float | None, float]:
        """(ETA seconds from the rolling per-episode rate, GPU hours left at the plan speed)."""
        remaining = [i for i in self.plan.items if i.episode_id not in self._done_ids]
        rest = sum(
            i.audio_s for i in remaining if i.needs_transcribe or self.mode == "download-only"
        )
        gpu_h = sum(i.audio_s for i in remaining if i.needs_transcribe) / self.plan.speed_x / 3600
        if self._recent:
            audio = sum(a for a, _ in self._recent)
            took = sum(t for _, t in self._recent)
            return (rest / (audio / took) if audio and took else None), gpu_h
        return rest / self.plan.speed_x, gpu_h

    def _line(self, stage: str, item: Item, extra: str = "") -> None:
        n = self._index.get(item.episode_id, 0)
        total = len(self.plan.items)
        eta, gpu_h = self._eta(item)
        elapsed = self.clock() - self.started_at
        clock = datetime.now().strftime("%H:%M:%S")
        self.echo(
            f"[{n:>{len(str(total))}}/{total}] {clock} {stage:<10} {item.episode_id[:26]:<26} "
            f"{item.title[:44]!r} | elapsed {fmt_hms(elapsed)} | ETA {fmt_hms(eta)} | "
            f"GPU left {gpu_h:.1f} h" + (f" | {extra}" if extra else "")
        )

    # --------------------------------------------------------------------- failures

    def _backoff(self, attempts: int) -> timedelta:
        steps = self.cfg.backfill.retry_backoff_minutes
        return timedelta(minutes=steps[min(max(attempts, 1), len(steps)) - 1] if steps else 0)

    def _fail(self, item: Item, stage: str, message: str, *, penalize: bool = True) -> None:
        message = redact(message, self.cfg.secrets.secret_values())[:500]
        attempts = item.attempts
        retry_at = None
        if penalize:
            holder: list[str | None] = [None]

            def backoff(n: int) -> str:
                holder[0] = (datetime.now(UTC) + self._backoff(n)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return holder[0]

            with state.connect(self.cfg.state_db) as conn:
                attempts = state.record_failure(
                    conn,
                    item.episode_id,
                    stage=stage,
                    error=f"{stage}: {message}"[:500],
                    backoff=backoff,
                )
            retry_at = holder[0]
        parked = penalize and attempts >= self.cfg.backfill.max_attempts
        self.summary.failed += 1
        self.summary.failures.append((item.episode_id, stage, message))
        self._done_ids.add(item.episode_id)
        self._ev(
            f"{stage}-failed", item, error=message, attempts=attempts, next_retry_at=retry_at,
            parked=parked or None,
        )  # fmt: skip
        self._line(
            f"{stage}-FAIL",
            item,
            f"{message[:120]}"
            + (f" | attempt {attempts}/{self.cfg.backfill.max_attempts}" if penalize else "")
            + (" | parked" if parked else ""),
        )
        self._streak += 1
        if self._streak >= CONSECUTIVE_FAILURES_ALERT and not self._alerted:
            self._alerted = True
            self._send(
                "Backfill: repeated failures",
                f"{self._streak} episodes in a row failed. Last: {item.episode_id} ({stage}): "
                f"{message[:150]}. The run continues; check `ciw backfill status`.",
                priority="high",
                tags=["warning"],
            )

    def _send(self, title: str, body: str, **kw: Any) -> None:
        sender = self.notify
        if sender is None:
            from ciw.notify import send_ntfy

            sender = send_ntfy
        try:
            sent = sender(self.cfg, title, body, **kw)
        except Exception as exc:  # never let a notification break the run
            sent = False
            log.warning("notification failed: %s", type(exc).__name__)
        self.events.write("ntfy", title=title, sent=bool(sent))

    # -------------------------------------------------------------------- transcribe

    def _transcribe_row(self, item: Item, correction: tx.CorrectionSettings | None) -> tx.Result:
        """Transcribe one episode. The row is read on a short-lived connection and the long GPU
        work runs on a fresh one that has read nothing, so the write at the end can never hit a
        stale snapshot (the downloader thread commits meanwhile). If it still does, the
        transcript is already on disk and the retry only repairs the state row."""
        assert self.transcriber is not None
        with state.connect(self.cfg.state_db) as conn:
            row = state.get_episode(conn, item.episode_id)
        if row is None:
            raise RuntimeError("episode vanished from state")
        last: sqlite3.OperationalError | None = None
        for _ in range(3):
            try:
                with state.connect(self.cfg.state_db) as conn:
                    return tx.transcribe_episode(
                        self.cfg,
                        conn,
                        row,
                        self.transcriber,
                        stale=self.plan.selection.stale,
                        correction=correction,
                    )
            except sqlite3.OperationalError as exc:
                last = exc
                log.warning("state write failed (%s); retrying", exc)
                time.sleep(0.5)
                with state.connect(self.cfg.state_db) as conn:
                    row = state.get_episode(conn, item.episode_id) or row
        assert last is not None
        raise last

    def _transcribe(self, item: Item, correction: tx.CorrectionSettings | None) -> None:
        assert self.transcriber is not None
        self._ev("transcribe-start", item, audio_s=round(item.audio_s, 1))
        self._line("transcribe", item, f"audio {fmt_hms(item.audio_s)}")
        t0 = time.monotonic()
        try:
            res = self._transcribe_row(item, correction)
        except Exception as exc:
            self._fail(item, "transcribe", f"{type(exc).__name__}: {exc}")
            return
        took = time.monotonic() - t0
        if res.outcome == "transcribed":
            self._streak = 0
            audio = (res.transcript.audio_duration_s if res.transcript else None) or item.audio_s
            self.summary.done += 1
            self.summary.audio_s += audio
            self.summary.work_s += took
            self._recent.append((audio, took))
            self._done_ids.add(item.episode_id)
            stats = gap_stats(
                res.transcript,
                self._audio_s_in_state(item.episode_id),
                self.cfg.backfill.suspect_gap_pct,
            )
            if stats.suspect:
                self.summary.suspects.append(item.episode_id)
            words = len(res.transcript.words) if res.transcript else 0
            self._ev(
                "transcribe-done", item, elapsed_s=round(took, 1), audio_s=round(audio, 1),
                x_realtime=round(audio / took, 1) if took else None, words=words,
                gap_s=stats.gap_s, gap_count=stats.count, suspect=stats.suspect or None,
                corrections=len(res.transcript.corrections) if res.transcript else None,
                runtime_s=res.transcript.runtime_s if res.transcript else None,
            )  # fmt: skip
            self._line(
                "done",
                item,
                f"{fmt_hms(took)} ({audio / took:.1f}x), {words} words"
                + (f", SUSPECT: {stats.count} gaps = {stats.gap_s:.0f} s" if stats.suspect else ""),
            )
            from ciw import search_index as si

            si.index_best_effort(self.cfg, [item.episode_id])
        elif res.outcome in ("skipped", "stale"):
            self.summary.skipped += 1
            self._done_ids.add(item.episode_id)
            self._ev("skipped", item, reason=res.message)
            self._line("skip", item, res.message[:100])
        else:  # failed | blocked
            self._fail(item, "transcribe", res.message or res.outcome)

    def _audio_s_in_state(self, episode_id: str) -> float | None:
        with state.connect(self.cfg.state_db) as conn:
            row = state.get_episode(conn, episode_id)
        return row["audio_duration_s"] if row else None

    # -------------------------------------------------------------------------- run

    def run(self) -> RunSummary:
        """Execute the plan. Raises :class:`BackfillLocked` if another run holds a lock."""
        with ExitStack() as stack:
            self._locks(stack)
            self._clear_stale_stop()
            previous = self._install_signals()
            try:
                self._run_locked()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
                if self._client is not None:
                    self._client.close()
        return self.summary

    def _clear_stale_stop(self) -> None:
        stop = self.cfg.backfill.stop_file
        guard_path(stop, "backfill stop_file")
        try:
            if stop.exists() and stop.stat().st_mtime < self.started_at:
                stop.unlink()
                self.echo(
                    f"warning: removed a stale stop file ({stop}) left from before this run started"
                )
        except OSError:
            pass

    def _install_signals(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        if self.install_signals and threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, self.handle_signal)
        return previous

    def _run_locked(self) -> None:
        cfg, plan, summary = self.cfg, self.plan, self.summary
        correction = None
        if self.mode != "download-only":
            if self.transcriber is None:
                raise RuntimeError("a transcriber is required unless mode is download-only")
            correction = tx.load_correction(cfg)
        self.events.write(
            "run-start", pid=os.getpid(), mode=self.mode, episodes=len(plan.items),
            sources=list(plan.selection.sources), prefetch=cfg.backfill.prefetch,
            max_hours=self.max_hours, est_gpu_h=round(plan.gpu_s / 3600, 2),
            est_download_gb=round(plan.download_bytes / 1e9, 2),
        )  # fmt: skip
        self.echo(
            f"backfill: {len(plan.items)} episodes ({self.mode}), prefetch {cfg.backfill.prefetch}"
            + (f", max {self.max_hours:g} h" if self.max_hours else "")
            + f"; log {self.events.path_for(datetime.now(UTC).strftime('%Y%m%d'))}"
        )
        if plan.index_only and self.mode != "download-only":
            from ciw import search_index as si

            si.index_best_effort(cfg, plan.index_only, echo=self.echo)
        thread = threading.Thread(target=self._download_loop, name="backfill-download", daemon=True)
        thread.start()
        try:
            while True:
                try:
                    got = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if (reason := self.stop_reason()) and not thread.is_alive():
                        self._note_stop(reason)
                        break
                    continue
                if got is _END:
                    break
                item, result = got
                with self._count_lock:
                    self._taken += 1
                self._slots.release()
                if result is not None:
                    self._handle_download(item, result)
                if self.summary.fatal:  # e.g. the disk guard: stop cleanly, nothing was lost
                    self._note_stop("fatal error")
                    break
                if reason := self.stop_reason():
                    self._note_stop(reason)
                    break
                if self.mode == "download-only":
                    self._done_ids.add(item.episode_id)
                    continue
                if result is not None and result.outcome == "failed":
                    continue
                summary.attempted += 1
                self._transcribe(item, correction)
                if reason := self.stop_reason():
                    self._note_stop(reason)
                    break
        finally:
            self._finish(thread)

    def _handle_download(self, item: Item, result: dl.DownloadResult) -> None:
        if result.outcome == "failed":
            if result.fatal:
                self.summary.fatal = result.message
                self._ev("fatal", item, error=redact(result.message))
                self.echo(
                    f"FATAL: {result.message}\nstopping the run; nothing was lost, "
                    "free some space and start it again"
                )
                self._done_ids.add(item.episode_id)
                return
            self._fail(item, "download", result.message)
            return
        if self.mode == "download-only":
            self._streak = 0
        self.summary.downloaded += result.outcome in ("downloaded", "adopted")
        self._ev(
            "download-done", item, outcome=result.outcome, bytes=result.size,
            fetched=result.fetched, elapsed_s=round(result.elapsed_s, 1),
            audio_s=round(result.duration_s, 1) if result.duration_s else None,
            mb_s=round(result.speed / 1e6, 1) if result.speed else None,
        )  # fmt: skip
        if result.duration_s:
            item.audio_s, item.audio_known = result.duration_s, True
        if self.mode == "download-only":
            self.summary.done += 1
            self._recent.append((item.audio_s, max(result.elapsed_s, 0.01)))
        self._line(
            "downloaded",
            item,
            f"{result.outcome}, {result.size / 1e6:.1f} MB"
            + (f" at {result.speed / 1e6:.1f} MB/s" if result.speed else ""),
        )

    def _finish(self, thread: threading.Thread) -> None:
        summary = self.summary
        self._halt.set()  # the downloader starts nothing new once the main loop is over
        thread.join()
        summary.wall_s = self.clock() - self.started_at
        while True:
            try:
                got = self._queue.get_nowait()
            except queue.Empty:
                break
            if got is not _END and got[1] is not None and got[1].outcome != "failed":
                summary.waiting += 1
        summary.stopped = self._stop_logged
        if self._stop_file_seen:
            try:
                self.cfg.backfill.stop_file.unlink(missing_ok=True)
            except OSError:
                pass
        self._ev_end()

    def _ev_end(self) -> None:
        s = self.summary
        self.events.write(
            "run-end", done=s.done, downloaded=s.downloaded, failed=s.failed, skipped=s.skipped,
            wall_s=round(s.wall_s, 1), audio_h=round(s.audio_s / 3600, 3),
            x_realtime=round(s.speed_x, 1) if s.speed_x else None, suspects=s.suspects,
            stopped=s.stopped, fatal=redact(s.fatal) if s.fatal else None, waiting=s.waiting,
            max_ahead=s.max_ahead, exit_code=s.exit_code,
        )  # fmt: skip

    def final_report(self) -> str:
        s = self.summary
        lines = ["", "backfill summary", f"  {s.line()}"]
        if s.stopped:
            lines.append(f"  stopped early: {s.stopped}")
        if s.waiting:
            lines.append(f"  {s.waiting} downloaded episodes are waiting for the next run")
        if s.fatal:
            lines.append(f"  FATAL: {s.fatal}")
        if s.suspects:
            lines.append(
                "  suspect (silent gaps > "
                f"{self.cfg.backfill.suspect_gap_pct:g}% of the audio): " + ", ".join(s.suspects)
            )
        for eid, stage, msg in s.failures:
            lines.append(f"  FAILED {eid} ({stage}): {msg[:160]}")
        return "\n".join(lines)

    def notify_summary(self) -> None:
        s = self.summary
        title = (
            f"Backfill {'stopped: disk' if s.fatal else 'finished'}: "
            f"{s.done} done, {s.failed} failed"
        )
        body = (
            f"{s.line()}."
            + (f" Stopped early: {s.stopped}." if s.stopped else "")
            + (f" Suspect: {len(s.suspects)}." if s.suspects else "")
        )
        self._send(
            title,
            body,
            priority="high" if (s.failed or s.fatal) else "default",
            tags=["microphone"],
        )


def run_backfill(cfg: Config, plan: Plan, **kwargs: Any) -> tuple[RunSummary, Backfill]:
    """Run ``plan``, print the summary, send the one-line ntfy. Returns (summary, runner)."""
    runner = Backfill(cfg, plan, **kwargs)
    summary = runner.run()
    runner.echo(runner.final_report())
    runner.notify_summary()
    return summary, runner


# =========================================================================== status / admin


def dir_size(path: Path) -> int:
    total = 0
    for entry in os.scandir(path) if path.is_dir() else ():
        try:
            if entry.is_dir(follow_symlinks=False):
                total += dir_size(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def lock_holder(path: Path) -> str | None:
    """pid of a process holding ``path`` (a flock), or None if it is free/absent."""
    import fcntl

    if not path.exists():
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_lock_pid(path)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def retry_failed(conn: sqlite3.Connection, episode_ids: list[str] | None = None) -> int:
    """Clear cool-downs and attempt counts of failed episodes; returns how many were reset."""
    rows = conn.execute(
        "SELECT episode_id FROM episodes WHERE attempts > 0 OR next_retry_at IS NOT NULL"
    ).fetchall()
    ids = [r["episode_id"] for r in rows if episode_ids is None or r["episode_id"] in episode_ids]
    for eid in ids:
        state.clear_failure(conn, eid)
    return len(ids)


def _short(text: str | None, n: int = 110) -> str:
    return (text or "").replace("\n", " ")[:n]


def gather_status(
    cfg: Config, conn: sqlite3.Connection, transcriber: Transcriber | None
) -> dict[str, Any]:
    """Everything ``ciw backfill status`` prints, as plain data."""
    counts: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT source, stage, COUNT(*) n FROM episodes GROUP BY source, stage"):
        counts.setdefault(r["source"], {})[r["stage"]] = r["n"]
    failing = [
        dict(r)
        for r in conn.execute(
            "SELECT episode_id, source, title, stage, attempts, last_error_stage, next_retry_at, "
            "error FROM episodes WHERE (attempts > 0 OR next_retry_at IS NOT NULL) "
            "AND NOT (stage IN ('transcribed','segmented','scored') AND attempts = 0) "
            "ORDER BY next_retry_at, episode_id"
        )
    ]
    suspects = [
        dict(r)
        for r in conn.execute(
            "SELECT episode_id, source, title, asr_gap_count, asr_gap_s, audio_duration_s "
            "FROM episodes WHERE suspect = 1 ORDER BY pub_date DESC"
        )
    ]
    plan = plan_backfill(cfg, conn, Selection(sources=tuple(cfg.backfill.order)), transcriber)
    log_dir = cfg.backfill.log_dir
    logs = sorted(log_dir.glob("backfill-*.jsonl")) if log_dir.is_dir() else []
    audio_bytes = dir_size(cfg.audio_dir)
    import shutil

    def free(path: Path) -> int:
        return _free_bytes(path)

    return {
        "counts": counts,
        "failing": failing,
        "suspects": suspects,
        "plan": plan,
        "audio_bytes": audio_bytes,
        "data_bytes": dir_size(cfg.data_dir),
        "free_root": free(Path("/")),
        "free_ssd": free(Path("/mnt/ssd0")) if Path("/mnt/ssd0").exists() else None,
        "free_audio": free(cfg.audio_dir),
        "total_root": shutil.disk_usage("/").total,
        "transcribe_pid": lock_holder(cfg.data_dir / "transcribe.lock"),
        "download_pid": lock_holder(cfg.data_dir / "download.lock"),
        "stop_file": cfg.backfill.stop_file.exists(),
        "logs": logs,
    }


def format_status(cfg: Config, st: dict[str, Any]) -> str:
    from ciw.state import STAGES

    lines = []
    running = st["transcribe_pid"] or st["download_pid"]
    lines.append(
        "run: "
        + (
            f"ACTIVE (pid {st['transcribe_pid'] or st['download_pid']})"
            if running
            else "not running"
        )
        + (
            "; STOP file present (the run will exit after the current episode)"
            if st["stop_file"]
            else ""
        )
    )
    lines.append("episodes per source and stage:")
    header = f"  {'source':<8}" + "".join(f"{s:>12}" for s in STAGES[:3]) + f"{'total':>8}"
    lines.append(header)
    totals = dict.fromkeys(STAGES[:3], 0)
    for source, by_stage in sorted(st["counts"].items()):
        later = sum(n for s, n in by_stage.items() if s not in STAGES[:3])
        row = [by_stage.get(s, 0) + (later if s == "transcribed" else 0) for s in STAGES[:3]]
        for s, n in zip(STAGES[:3], row, strict=True):
            totals[s] += n
        lines.append(f"  {source:<8}" + "".join(f"{n:>12}" for n in row) + f"{sum(row):>8}")
    lines.append(
        f"  {'all':<8}"
        + "".join(f"{totals[s]:>12}" for s in STAGES[:3])
        + f"{sum(totals.values()):>8}"
    )
    plan: Plan = st["plan"]
    lines.append(
        f"remaining work: {len(plan.items)} episodes (download+transcribe "
        f"{plan.count('download+transcribe')}, transcribe only {plan.count('transcribe')}); "
        f"parked/cooling: "
        + (
            ", ".join(
                f"{k} {v}" for k, v in plan.skipped.items() if k in ("cooldown", "out-of-attempts")
            )
            or "0"
        )
    )
    lines.append(
        f"ETA: {plan.audio_s / 3600:.1f} audio h at {plan.speed_x:.1f}x ({plan.speed_basis}) = "
        f"{plan.gpu_s / 3600:.1f} GPU h; download still to fetch ~{fmt_gb(plan.download_bytes)}"
    )
    if st["failing"]:
        lines.append(f"failures ({len(st['failing'])}):")
        for f in st["failing"][:25]:
            lines.append(
                f"  {f['episode_id'][:26]:<26} {f['source']:<5} attempts {f['attempts']}/"
                f"{cfg.backfill.max_attempts} {f['last_error_stage'] or '-':<10} retry after "
                f"{f['next_retry_at'] or '-'}  {_short(f['error'])}"
            )
        if len(st["failing"]) > 25:
            lines.append(f"  ... and {len(st['failing']) - 25} more")
    else:
        lines.append("failures: none")
    if st["suspects"]:
        lines.append(
            f"suspect episodes ({len(st['suspects'])}): silent gaps over "
            f"{cfg.backfill.suspect_gap_pct:g}% of the audio"
        )
        for s in st["suspects"][:25]:
            lines.append(
                f"  {s['episode_id'][:26]:<26} {s['asr_gap_count']} gaps, "
                f"{s['asr_gap_s']:.0f} s of "
                f"{fmt_hms(s['audio_duration_s'])}  {s['title'][:40]}"
            )
    else:
        lines.append("suspect episodes: none")
    lines.append(
        f"disk: audio_dir {fmt_gib(st['audio_bytes'])} used ({cfg.audio_dir}), "
        f"{fmt_gib(st['free_audio'])} free there"
        + (f"; /mnt/ssd0 {fmt_gib(st['free_ssd'])} free" if st["free_ssd"] is not None else "")
        + f"; / {fmt_gib(st['free_root'])} free; data/ {st['data_bytes'] / 1e6:.0f} MB"
    )
    if st["logs"]:
        lines.append(f"logs: {len(st['logs'])} files, newest {st['logs'][-1]}")
    return "\n".join(lines)


def request_stop(cfg: Config) -> Path:
    stop = cfg.backfill.stop_file
    guard_path(stop, "backfill stop_file")
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(f"stop requested {datetime.now(UTC).isoformat()}\n")
    return stop


def iter_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Parse a backfill JSONL log (used by tests and tooling)."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)
