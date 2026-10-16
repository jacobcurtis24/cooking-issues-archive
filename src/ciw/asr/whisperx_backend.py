"""WhisperX backend: faster-whisper large-v3 + wav2vec2 forced alignment + pyannote speakers.

Everything heavy (torch, whisperx, models) is imported inside :meth:`WhisperXTranscriber.transcribe`
so that ``ciw --help``, the web app and the unit tests stay fast and torch-free.

Pipeline, one model on the GPU at a time (each is loaded, run and freed before the next)::

    decode (ffmpeg) -> asr (faster-whisper + VAD) -> align (wav2vec2) -> diarize (pyannote)

Policies (also recorded in ``docs/progress.md``):

* **CUDA OOM back-off** for the ASR stage: ``batch_size`` (default 8) is halved down to
  ``min_batch_size`` (2); then the same ladder again with ``fallback_compute_type``
  (``int8_float16``). Every fallback is logged as a warning and the batch size / compute type
  actually used are stored in the transcript.
* **Word timestamps are never missing.** wav2vec2 cannot align tokens without letters (numbers,
  symbols). Such words, and whole segments the aligner skipped, get timestamps interpolated
  between their aligned neighbours (spread by character count); the count is stored in
  ``Transcript.notes``. Starts and ends are then forced to be non-decreasing.
* **Speakers are optional.** Without ``HF_TOKEN``, or when Hugging Face answers 401/403 for the
  gated pyannote model, transcription proceeds with ``speaker=None`` on every word,
  ``diarization=False`` and the reason in ``diarization_skipped``.
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import logging
import os
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from ciw.asr.base import (
    SKIP_DISABLED,
    SKIP_ERROR,
    SKIP_GATED,
    SKIP_NO_TOKEN,
    ProgressFn,
    Transcriber,
    diarization_hint,
)
from ciw.config import AsrConfig, redact
from ciw.models import StageStats, Transcript, Word

if TYPE_CHECKING:
    from ciw.config import Config

logger = logging.getLogger("ciw.asr.whisperx")
T = TypeVar("T")

SAMPLE_RATE = 16000
_MIB = 1024 * 1024

# ----------------------------------------------------------------------- environment


def interpreter_bin_dir() -> str:
    """Directory of the running interpreter (not symlink-resolved): the conda env's ``bin``."""
    return os.path.dirname(sys.executable)


def prepare_environment(asr: AsrConfig, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Make the process ready for WhisperX; returns what was set (for logging/tests).

    * The interpreter's ``bin`` goes first on ``PATH``: WhisperX shells out to ``ffmpeg`` and the
      py312 conda env's ffmpeg is not on the login PATH.
    * ``HF_HOME`` and ``TORCH_HOME`` (model caches) default to ``asr.hf_home`` on the big disk
      unless the environment already sets them. They are read when ``huggingface_hub`` is first
      imported, so call this before importing torch/whisperx.
    * The cuBLAS/cuDNN libraries need no ``LD_LIBRARY_PATH``: importing torch first (which
      ``transcribe`` does) loads the ``nvidia-*`` wheels' libraries, and CTranslate2 then finds
      them already resident. Tested cold on driver 580 / torch 2.8.0+cu128 / ctranslate2 4.8.3.
    """
    env = os.environ if environ is None else environ
    changed: dict[str, str] = {}
    bin_dir = interpreter_bin_dir()
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if not parts or parts[0] != bin_dir:
        env["PATH"] = os.pathsep.join([bin_dir, *[p for p in parts if p != bin_dir]])
        changed["PATH"] = env["PATH"]
    if "HF_HOME" not in env:
        env["HF_HOME"] = str(asr.hf_home)
        changed["HF_HOME"] = env["HF_HOME"]
    if "TORCH_HOME" not in env:
        env["TORCH_HOME"] = str(Path(env["HF_HOME"]) / "torch")
        changed["TORCH_HOME"] = env["TORCH_HOME"]
    return changed


def installed_versions() -> dict[str, str]:
    """Versions of the ASR stack from package metadata (imports nothing heavy)."""
    out = {}
    for pkg in ("whisperx", "faster-whisper", "ctranslate2"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = "not installed"
    return out


# --------------------------------------------------------------------------- OOM back-off


class OutOfMemoryExhausted(RuntimeError):
    """Every batch size / compute type in the back-off ladder ran out of GPU memory."""


_OOM_MARKERS = (
    "out of memory",
    "cublas_status_alloc_failed",
    "cudnn_status_alloc_failed",
    "failed to allocate",
)


def is_oom(exc: BaseException) -> bool:
    """True for torch's OutOfMemoryError and for CTranslate2's RuntimeError('CUDA failed with
    error out of memory')."""
    if type(exc).__name__ == "OutOfMemoryError":
        return True
    text = str(exc).lower()
    return isinstance(exc, RuntimeError | MemoryError) and any(m in text for m in _OOM_MARKERS)


def backoff_plan(
    batch_size: int, min_batch_size: int, compute_types: Sequence[str]
) -> list[tuple[str, int]]:
    """``[(compute_type, batch_size), ...]``: batch halved to ``min_batch_size`` per compute type.

    ``(8, 2, ["float16", "int8_float16"])`` ->
    float16 8, 4, 2 then int8_float16 8, 4, 2.
    """
    plan = []
    for compute_type in compute_types:
        batch = max(batch_size, min_batch_size)
        while True:
            plan.append((compute_type, batch))
            if batch <= min_batch_size:
                break
            batch = max(batch // 2, min_batch_size)
    return plan


def run_with_oom_backoff(
    attempt: Callable[[str, int], T],
    plan: Sequence[tuple[str, int]],
    *,
    cleanup: Callable[[bool], None] | None = None,
    notes: list[str] | None = None,
) -> tuple[T, str, int]:
    """Run ``attempt(compute_type, batch_size)`` down the ladder until one does not OOM.

    Returns ``(result, compute_type, batch_size)`` of the successful attempt. Non-OOM errors
    propagate. After an OOM ``cleanup(drop_model)`` is called (free GPU memory;
    ``drop_model`` is True when the next step needs a different compute type, so the loaded model
    must go), outside the ``except`` block so the failed attempt's tensors are really released.
    """
    for i, (compute_type, batch) in enumerate(plan):
        failure: BaseException | None = None
        try:
            return attempt(compute_type, batch), compute_type, batch
        except Exception as exc:
            if not is_oom(exc):
                raise
            failure = exc
        nxt = plan[i + 1] if i + 1 < len(plan) else None
        msg = f"CUDA out of memory (compute_type={compute_type}, batch_size={batch}); " + (
            f"retrying with compute_type={nxt[0]}, batch_size={nxt[1]}"
            if nxt
            else "no fallback left"
        )
        logger.warning(msg)
        if notes is not None:
            notes.append(msg)
        if cleanup is not None:
            cleanup(nxt is None or nxt[0] != compute_type)
        if nxt is None:
            tried = ", ".join(f"{c}/{b}" for c, b in plan)
            raise OutOfMemoryExhausted(
                f"GPU out of memory at every setting tried ({tried})"
            ) from failure
    raise OutOfMemoryExhausted("empty back-off plan")  # pragma: no cover


# ------------------------------------------------------------------------------ vocabulary


def load_vocabulary_prompt(path: Path | None, max_chars: int = 600) -> str | None:
    """Read a vocabulary file (one term per line, ``#`` comments) into an initial_prompt.

    Returns ``None`` when no file is configured or it holds no terms. The prompt is the terms
    joined by ", " and cut at a term boundary to ``max_chars`` (Whisper only attends to the last
    224 tokens of the prompt anyway). T6 owns how the file is built.
    """
    if path is None or not Path(path).is_file():
        return None
    terms = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        term = raw.split("#", 1)[0].strip()
        if term:
            terms.append(term)
    if not terms:
        return None
    prompt = ""
    for term in terms:
        piece = term if not prompt else f", {term}"
        if max_chars and len(prompt) + len(piece) > max_chars:
            break
        prompt += piece
    return prompt or None


def prompt_hash(prompt: str | None) -> str | None:
    return hashlib.sha256(prompt.encode()).hexdigest()[:12] if prompt else None


# -------------------------------------------------------------------------------- words


def words_from_segments(segments: Sequence[dict[str, Any]]) -> tuple[list[Word], int]:
    """Flatten WhisperX segments into ``Word`` objects with guaranteed numeric timestamps.

    Returns ``(words, n_interpolated)``. A word without both ``start`` and ``end`` (the aligner
    skips tokens without letters) gets a slice of the gap between its aligned neighbours, sized by
    character count. A segment with no words at all (alignment failed for it) is split on
    whitespace and spread over the segment's own start/end. Finally starts and ends are made
    non-decreasing, so ``words[i].start <= words[i+1].start`` and ``start <= end`` always hold.
    """
    out: list[Word] = []
    interpolated = 0
    cursor = 0.0  # end of the last word emitted
    for seg in segments:
        seg_speaker = seg.get("speaker")
        raw = seg.get("words") or [{"word": t} for t in str(seg.get("text", "")).split()]
        items: list[dict[str, Any]] = []
        for r in raw:
            token = str(r.get("word", "")).strip()
            if not token:
                continue
            start, end = r.get("start"), r.get("end")
            if start is None or end is None:
                start = end = None
            items.append(
                {"w": token, "start": start, "end": end, "speaker": r.get("speaker") or seg_speaker}
            )
        if not items:
            continue
        seg_start = seg.get("start")
        seg_end = seg.get("end")
        seg_start = cursor if seg_start is None else float(seg_start)
        i = 0
        while i < len(items):
            if items[i]["start"] is not None:
                i += 1
                continue
            j = i
            while j < len(items) and items[j]["start"] is None:
                j += 1
            lo = items[i - 1]["end"] if i > 0 else max(seg_start, cursor)
            if j < len(items):
                hi = items[j]["start"]
            else:
                hi = float(seg_end) if seg_end is not None else lo
            hi = max(hi, lo)
            weights = [max(len(items[k]["w"]), 1) for k in range(i, j)]
            total = float(sum(weights))
            t = lo
            for k, wgt in zip(range(i, j), weights, strict=True):
                nxt = t + (hi - lo) * wgt / total
                items[k]["start"], items[k]["end"] = t, nxt
                t = nxt
            interpolated += j - i
            i = j
        for it in items:
            start = max(float(it["start"]), out[-1].start if out else 0.0)
            end = max(float(it["end"]), start, out[-1].end if out else 0.0)
            out.append(
                Word(w=it["w"], start=round(start, 3), end=round(end, 3), speaker=it["speaker"])
            )
        cursor = out[-1].end
    return out, interpolated


# ---------------------------------------------------------------------------- GPU meter


class VramMeter:
    """Peak GPU memory of one stage.

    ``torch.cuda.max_memory_allocated`` only sees torch tensors, not CTranslate2's allocator, so
    this also polls the device-wide used memory (``cudaMemGetInfo``) from a thread; the reported
    peak is the highest value seen minus what was in use when the stage began (other processes,
    e.g. the desktop, are excluded as long as they stay constant).
    """

    def __init__(self, torch_mod: Any, interval: float = 0.2):
        self.torch = torch_mod
        self.interval = interval
        self.baseline = 0
        self.peak_used = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _used(self) -> int:
        free, total = self.torch.cuda.mem_get_info()
        return total - free

    def _poll(self) -> None:
        while not self._stop.wait(self.interval):
            with contextlib.suppress(Exception):
                self.peak_used = max(self.peak_used, self._used())

    def __enter__(self) -> VramMeter:
        self.torch.cuda.reset_peak_memory_stats()
        self.baseline = self.peak_used = self._used()
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with contextlib.suppress(Exception):
            self.peak_used = max(self.peak_used, self._used())

    @property
    def peak_vram_mb(self) -> float:
        return max(self.peak_used - self.baseline, 0) / _MIB

    @property
    def peak_torch_mb(self) -> float:
        return self.torch.cuda.max_memory_allocated() / _MIB


# ------------------------------------------------------------------------ diarization gate


def classify_hf_error(exc: BaseException) -> str | None:
    """``SKIP_GATED`` if ``exc`` looks like a Hugging Face 401/403/gated-repo refusal."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return SKIP_GATED
    text = f"{type(exc).__name__} {exc}".lower()
    markers = ("gatedrepo", "gated repo", "401", "403", "unauthorized", "forbidden", "restricted")
    if any(m in text for m in markers):
        return SKIP_GATED
    return None


def check_diarization_access(model: str, token: str) -> str | None:
    """Cheap pre-flight (downloads the model's small config.yaml) so a bad token or an
    unaccepted licence is found before an hour of ASR, not after. Returns ``None`` if the model
    is reachable, else a skip reason (``SKIP_GATED`` or ``SKIP_ERROR: ...``)."""
    try:
        from huggingface_hub import hf_hub_download

        hf_hub_download(model, "config.yaml", token=token)
    except Exception as exc:
        gated = classify_hf_error(exc)
        if gated:
            return gated
        return f"{SKIP_ERROR}: {redact(f'{type(exc).__name__}: {exc}', [token])[:200]}"
    return None


# ----------------------------------------------------------------------------- backend


class WhisperXTranscriber(Transcriber):
    name = "whisperx"

    def __init__(
        self,
        cfg: Config,
        *,
        model: str | None = None,
        diarize: bool | None = None,
        hf_token: str | None = None,
        check_access: Callable[[str, str], str | None] | None = None,
    ):
        update: dict[str, Any] = {}
        if model:
            update["model"] = model
        if diarize is not None:
            update["diarize"] = diarize
        self.asr = cfg.asr.model_copy(update=update)
        if hf_token is None and cfg.secrets.hf_token is not None:
            hf_token = cfg.secrets.hf_token.get_secret_value()
        self._token = hf_token or None
        self._check_access = check_access or check_diarization_access

    # -- identity (cheap: no imports of torch/whisperx)

    @property
    def model(self) -> str:
        return self.asr.model

    @property
    def version(self) -> str:
        return "; ".join(f"{k} {v}" for k, v in installed_versions().items())

    def _initial_prompt(self, override: str | None) -> str | None:
        if override is not None:
            return override or None
        return load_vocabulary_prompt(self.asr.vocabulary_file, self.asr.initial_prompt_max_chars)

    def _diarization_plan(self) -> str | None:
        """Why speakers will be skipped, or None if diarization will be attempted."""
        if not self.asr.diarize:
            return SKIP_DISABLED
        if not self._token:
            return SKIP_NO_TOKEN
        return self._check_access(self.asr.diarization_model, self._token)

    def describe(self) -> str:
        if not self.asr.diarize:
            speakers = "speakers off"
        elif not self._token:
            speakers = "speakers: skipped, HF_TOKEN not set"
        else:
            speakers = f"speakers: {self.asr.diarization_model}"
        return (
            f"whisperx {self.asr.model} {self.asr.compute_type}, batch {self.asr.batch_size}, "
            f"vad {self.asr.vad}, {speakers}"
        )

    # -- the work

    def transcribe(
        self,
        audio_path: Path,
        *,
        initial_prompt: str | None = None,
        progress: ProgressFn | None = None,
    ) -> Transcript:
        started = time.perf_counter()
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise FileNotFoundError(f"audio file not found: {audio_path}")
        prepare_environment(self.asr)
        warnings.filterwarnings("ignore", message=r"(?s).*torchcodec is not installed correctly.*")
        warnings.filterwarnings("ignore", category=UserWarning, module=r"pyannote\..*")

        import torch  # first: loads the CUDA libs that CTranslate2 needs
        import whisperx

        device = self.asr.device
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA is not available to torch (check `nvidia-smi` and that torch is a CUDA "
                "build); refusing to fall back to the CPU."
            )
        if device.startswith("cuda"):
            free, total = torch.cuda.mem_get_info()
            logger.info(
                "GPU %s: %.1f of %.1f GiB free",
                torch.cuda.get_device_name(0),
                free / 1024**3,
                total / 1024**3,
            )

        prompt = self._initial_prompt(initial_prompt)
        skip_reason = self._diarization_plan()
        if skip_reason and skip_reason != SKIP_DISABLED:
            logger.warning(diarization_hint(skip_reason, self.asr.diarization_model))
        notes: list[str] = []
        stages: list[StageStats] = []
        emit = progress or (lambda stage, frac: None)

        @contextlib.contextmanager
        def stage(name: str) -> Iterator[StageStats]:
            """Time a stage, meter its VRAM, append its StageStats, log one line."""
            emit(name, None)
            entry = StageStats(name=name, seconds=0.0)
            t0 = time.perf_counter()
            meter = VramMeter(torch) if device.startswith("cuda") else None
            if meter:
                meter.__enter__()
            try:
                yield entry
            finally:
                entry.seconds = round(time.perf_counter() - t0, 2)
                if meter:
                    meter.__exit__(None, None, None)
                    entry.peak_vram_mb = round(meter.peak_vram_mb, 1)
                    entry.peak_torch_mb = round(meter.peak_torch_mb, 1)
                stages.append(entry)
                logger.info(
                    "stage %s: %.1fs, peak VRAM +%s MiB (torch %s MiB)",
                    name, entry.seconds, entry.peak_vram_mb, entry.peak_torch_mb,
                )  # fmt: skip

        def free_gpu() -> None:
            gc.collect()
            if device.startswith("cuda"):
                torch.cuda.empty_cache()

        # decode ---------------------------------------------------------------------
        with stage("decode"):
            audio = whisperx.load_audio(str(audio_path))
        audio_duration = len(audio) / SAMPLE_RATE

        # asr ------------------------------------------------------------------------
        holder: dict[str, Any] = {"model": None, "compute_type": None}

        def load(compute_type: str) -> Any:
            return whisperx.load_model(
                self.asr.model,
                device,
                compute_type=compute_type,
                language=self.asr.language,
                vad_method=self.asr.vad,
                asr_options={"initial_prompt": prompt},
            )

        def attempt(compute_type: str, batch_size: int) -> dict[str, Any]:
            if holder["compute_type"] != compute_type:
                holder["model"] = None
                free_gpu()
                holder["model"] = load(compute_type)
                holder["compute_type"] = compute_type
            logger.info("ASR: %s, compute_type=%s, batch_size=%d", self.asr.model, compute_type,
                        batch_size)  # fmt: skip
            return holder["model"].transcribe(
                audio,
                batch_size=batch_size,
                language=self.asr.language,
                progress_callback=lambda pct: emit("asr", pct / 100),
            )

        def cleanup(drop_model: bool) -> None:
            if drop_model:
                holder["model"] = None
                holder["compute_type"] = None
            free_gpu()

        types = [self.asr.compute_type]
        if self.asr.fallback_compute_type and self.asr.fallback_compute_type != types[0]:
            types.append(self.asr.fallback_compute_type)
        plan = backoff_plan(self.asr.batch_size, self.asr.min_batch_size, types)
        with stage("asr"):
            try:
                result, used_compute, used_batch = run_with_oom_backoff(
                    attempt, plan, cleanup=cleanup, notes=notes
                )
            finally:
                holder["model"] = None
                free_gpu()
        segments = result["segments"]
        language = result.get("language") or self.asr.language
        logger.info("ASR done: %d segments (compute_type=%s, batch_size=%d)", len(segments),
                    used_compute, used_batch)  # fmt: skip

        # align ----------------------------------------------------------------------
        with stage("align"):
            align_model, align_meta = whisperx.load_align_model(language, device)
            try:
                aligned = whisperx.align(
                    segments,
                    align_model,
                    align_meta,
                    audio,
                    device,
                    return_char_alignments=False,
                    progress_callback=lambda pct: emit("align", pct / 100),
                )
            finally:
                del align_model
                free_gpu()

        # diarize --------------------------------------------------------------------
        diarized = False
        diar_stage = StageStats(name="diarize", seconds=0.0, skipped=skip_reason)
        if skip_reason is None:
            try:
                with stage("diarize"):
                    from whisperx.diarize import DiarizationPipeline

                    pipeline = DiarizationPipeline(
                        model_name=self.asr.diarization_model, token=self._token, device=device
                    )
                    try:
                        frame = pipeline(
                            audio, progress_callback=lambda p: emit("diarize", p / 100)
                        )
                        aligned = whisperx.assign_word_speakers(frame, aligned, fill_nearest=True)
                        diarized = True
                    finally:
                        del pipeline
                        free_gpu()
            except Exception as exc:
                detail = redact(f"{type(exc).__name__}: {exc}", [self._token or ""])[:200]
                gated = classify_hf_error(exc) is not None
                skip_reason = SKIP_GATED if gated else f"{SKIP_ERROR}: {detail}"
                logger.warning(
                    "diarization failed, keeping the transcript without speakers: %s", detail
                )
                if gated:
                    logger.warning(diarization_hint(SKIP_GATED, self.asr.diarization_model))
                free_gpu()
                stages[-1].skipped = skip_reason  # stage() recorded it before the exception
                for seg in aligned.get("segments", []):
                    seg.pop("speaker", None)
                    for w in seg.get("words", []):
                        w.pop("speaker", None)
        else:
            stages.append(diar_stage)
            emit("diarize", None)

        # words ----------------------------------------------------------------------
        words, interpolated = words_from_segments(aligned["segments"])
        if interpolated:
            notes.append(
                f"{interpolated} of {len(words)} words had no aligned timestamps and were "
                "interpolated between neighbours"
            )
        if not diarized:
            words = [w.model_copy(update={"speaker": None}) for w in words]
        speakers = {w.speaker for w in words if w.speaker}
        return Transcript(
            asr_model=self.asr.model,
            asr_version=self.version,
            asr_backend=self.name,
            language=language,
            device=device,
            compute_type=used_compute,
            batch_size=used_batch,
            vad=self.asr.vad,
            diarization=diarized,
            diarization_model=self.asr.diarization_model if diarized else None,
            diarization_skipped=None if diarized else skip_reason,
            num_speakers=len(speakers) if diarized else None,
            initial_prompt=prompt,
            vocab_hash=prompt_hash(prompt),
            audio_duration_s=round(audio_duration, 2),
            runtime_s=round(time.perf_counter() - started, 2),
            stages=stages,
            notes=notes,
            words=words,
        )


def make_transcriber(cfg: Config, **overrides: Any) -> WhisperXTranscriber:
    return WhisperXTranscriber(cfg, **overrides)
