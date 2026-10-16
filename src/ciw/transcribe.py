"""Transcription orchestration: choose episodes, skip what is current, run a backend, save.

The ASR backends live in :mod:`ciw.asr`; this module is backend-agnostic and never imports torch.
Per episode: ``data/episodes/<id>/transcript.json`` is written atomically, then the state row
moves to stage ``transcribed``. A failure is recorded in ``episodes.error`` and the batch goes on.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ciw import state
from ciw.asr.base import ProgressFn, Transcriber
from ciw.config import Config, redact
from ciw.ingest import episode_dir
from ciw.models import Transcript

logger = logging.getLogger("ciw.transcribe")


class TranscribeLocked(RuntimeError):
    pass


def transcript_path(cfg: Config, episode_id: str) -> Path:
    return episode_dir(cfg, episode_id) / "transcript.json"


@contextmanager
def transcribe_lock(cfg: Config) -> Iterator[None]:
    """Hold ``<data_dir>/transcribe.lock`` (flock): there is one GPU, so one run at a time."""
    path = cfg.data_dir / "transcribe.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TranscribeLocked(
                f"another `ciw transcribe` is already running (lock: {path})"
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# -------------------------------------------------------------------------- selection


def has_audio(row: sqlite3.Row) -> bool:
    return bool(row["audio_path"]) and row["stage"] != "discovered"


def select_episodes(
    conn: sqlite3.Connection,
    *,
    latest: int | None = None,
    episode_ids: Sequence[str] = (),
    all_downloaded: bool = False,
) -> tuple[list[sqlite3.Row], list[str]]:
    """Choose episodes to consider. Returns ``(rows newest first, unknown ids)``.

    ``latest N`` = the N newest *downloaded* episodes (ones that are already transcribed are
    skipped later, so a second identical run does nothing); ``all_downloaded`` = every downloaded
    episode; ``episode_ids`` are taken as given (unknown ids are returned separately; episodes
    without audio are kept so the plan can say why they are blocked).
    """
    rows = state.list_episodes(conn)
    downloaded = [r for r in rows if has_audio(r)]
    chosen: dict[str, sqlite3.Row] = {}
    if all_downloaded:
        chosen.update((r["episode_id"], r) for r in downloaded)
    if latest is not None:
        chosen.update((r["episode_id"], r) for r in downloaded[:latest])
    by_id = {r["episode_id"]: r for r in rows}
    unknown = []
    for eid in episode_ids:
        if eid in by_id:
            chosen[eid] = by_id[eid]
        else:
            unknown.append(eid)
    ordered = sorted(chosen.values(), key=lambda r: (r["pub_date"], r["episode_id"]), reverse=True)
    return ordered, unknown


# ------------------------------------------------------------------------------ plan


@dataclass
class Plan:
    action: str  # transcribe | retranscribe | skip | stale | blocked
    detail: str = ""


def _describe_stale(old: Transcript, tr: Transcriber) -> str:
    return (
        f"transcript made by {old.asr_backend} {old.asr_model} [{old.asr_version}], "
        f"current is {tr.name} {tr.model} [{tr.version}]"
    )


def plan_for_output(out: Path, tr: Transcriber, *, force: bool, stale: bool) -> Plan:
    """Decide what to do for one output file (shared by episode and ``--audio`` modes)."""
    if not out.is_file():
        return Plan("transcribe")
    try:
        existing = Transcript.load(out)
    except (ValidationError, ValueError, OSError) as exc:
        return Plan("transcribe", f"existing transcript unreadable ({type(exc).__name__}), redoing")
    if tr.matches(existing):
        if force:
            return Plan("retranscribe", "forced")
        note = "up to date"
        if not existing.diarization and existing.diarization_skipped:
            note += f"; no speakers ({existing.diarization_skipped})"
        return Plan("skip", note)
    if force or stale:
        return Plan("retranscribe", "stale: " + _describe_stale(existing, tr))
    return Plan("stale", _describe_stale(existing, tr) + "; re-run with --stale or --force")


def plan_episode(
    cfg: Config, row: sqlite3.Row, tr: Transcriber, *, force: bool = False, stale: bool = False
) -> Plan:
    audio = row["audio_path"]
    if not audio or not has_audio(row):
        return Plan("blocked", f"not downloaded (run: ciw download --episode {row['episode_id']})")
    if not Path(audio).is_file():
        return Plan("blocked", f"audio file is missing: {audio}")
    return plan_for_output(transcript_path(cfg, row["episode_id"]), tr, force=force, stale=stale)


# ----------------------------------------------------------------------------- running


@dataclass
class Result:
    episode_id: str
    outcome: str  # transcribed | skipped | stale | blocked | failed
    message: str = ""
    elapsed_s: float = 0.0
    transcript: Transcript | None = None


@dataclass
class Summary:
    results: list[Result] = field(default_factory=list)

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def failed(self) -> int:
        return self.count("failed") + self.count("blocked")


def _run_backend(tr: Transcriber, audio: Path, progress: ProgressFn | None) -> Transcript:
    """Run the backend and validate the result."""
    t0 = time.perf_counter()
    transcript = tr.transcribe(audio, progress=progress)
    if transcript.runtime_s is None:
        transcript.runtime_s = round(time.perf_counter() - t0, 2)
    if not transcript.words:
        raise ValueError("the transcript has no words (silent or undecodable audio?)")
    return transcript


def transcribe_file(
    audio: Path,
    out: Path,
    tr: Transcriber,
    *,
    force: bool = False,
    stale: bool = False,
    progress: ProgressFn | None = None,
) -> Result:
    """Transcribe an arbitrary file to ``out``; touches no pipeline state."""
    plan = plan_for_output(out, tr, force=force, stale=stale)
    name = audio.name
    if plan.action == "skip":
        return Result(name, "skipped", f"{plan.detail}; use --force to redo")
    if plan.action == "stale":
        return Result(name, "stale", plan.detail)
    t0 = time.perf_counter()
    try:
        transcript = _run_backend(tr, audio, progress)
        transcript.save(out)
    except Exception as exc:
        logger.debug("transcription failed", exc_info=True)
        return Result(
            name, "failed", redact(f"{type(exc).__name__}: {exc}"), time.perf_counter() - t0
        )
    return Result(name, "transcribed", str(out), time.perf_counter() - t0, transcript)


def transcribe_episode(
    cfg: Config,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    tr: Transcriber,
    *,
    force: bool = False,
    stale: bool = False,
    progress: ProgressFn | None = None,
) -> Result:
    eid = row["episode_id"]
    plan = plan_episode(cfg, row, tr, force=force, stale=stale)
    if plan.action == "blocked":
        return Result(eid, "blocked", plan.detail)
    if plan.action == "stale":
        return Result(eid, "stale", plan.detail)
    if plan.action == "skip":
        if row["stage"] == "downloaded":  # transcript exists but state never saw it: repair
            existing = Transcript.load(transcript_path(cfg, eid))
            state.mark_transcribed(
                conn,
                eid,
                asr_model=existing.asr_model,
                asr_version=existing.asr_version,
                asr_runtime_s=existing.runtime_s,
                diarization=existing.diarization,
            )
            state.checkpoint(conn)
            plan.detail += "; state repaired"
        return Result(eid, "skipped", plan.detail)

    t0 = time.perf_counter()
    try:
        transcript = _run_backend(tr, Path(row["audio_path"]), progress)
        transcript.save(transcript_path(cfg, eid))
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}")
        logger.debug("transcription of %s failed", eid, exc_info=True)
        state.set_error(conn, eid, f"transcribe: {message}"[:500])
        state.checkpoint(conn)
        return Result(eid, "failed", message, time.perf_counter() - t0)
    state.mark_transcribed(
        conn,
        eid,
        asr_model=transcript.asr_model,
        asr_version=transcript.asr_version,
        asr_runtime_s=transcript.runtime_s,
        diarization=transcript.diarization,
    )
    state.checkpoint(conn)
    return Result(eid, "transcribed", "", time.perf_counter() - t0, transcript)


def transcribe_batch(
    cfg: Config,
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    tr: Transcriber,
    *,
    force: bool = False,
    stale: bool = False,
    progress: Callable[[str, str, float | None], None] | None = None,
    on_result: Callable[[sqlite3.Row, Result], None] | None = None,
) -> Summary:
    """Transcribe ``rows`` one after another under the GPU lock; a failure never stops the batch."""
    summary = Summary()
    with transcribe_lock(cfg):
        for row in rows:
            eid = row["episode_id"]
            per_stage = (
                (lambda stage, frac, eid=eid: progress(eid, stage, frac)) if progress else None
            )
            result = transcribe_episode(
                cfg, conn, row, tr, force=force, stale=stale, progress=per_stage
            )
            summary.results.append(result)
            if on_result:
                on_result(row, result)
    return summary
