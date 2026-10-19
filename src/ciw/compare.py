"""The model comparison harness (T9 step 0): one prompt, many models/efforts, many episodes.

The prompt, schema, run matrix and sample episodes live in one user-edited file
(``llm.prompt_file``, default ``prompts/compare.yaml``). Results are one JSON file per
(episode, config) under ``<compare_dir>/<run_id>/<episode_id>/<config_id>.json``; ``run_id`` is
derived from the prompt hash, so editing the prompt starts a fresh run while the matrix and the
sample can change without losing results. A result that exists is never re-requested (only
``error`` results are retried), so a run resumes and stage 2 reuses stage 1.

Every API call also lands in the ``llm_usage`` ledger of ``state.sqlite``, which backs the monthly
budget cap. Checks on the outputs (:func:`run_checks`) never call the API.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ciw import llm, state
from ciw.config import Config, redact
from ciw.ingest import episode_dir
from ciw.models import EpisodeMeta, Transcript
from ciw.transcribe import transcript_path
from ciw.transcript import format_timestamp

PLACEHOLDERS = ("title", "pub_date", "duration", "show_notes", "transcript")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
PLACEHOLDER_MARK = "TODO"  # a prompt that still contains this is the unedited template
CHARS_PER_TOKEN = 3.6  # offline estimate of the rendered prompt (timestamps/labels included)
INPUT_MARGIN = 1.15  # the offline estimate may undershoot; the worst-case cost uses this margin
# Assumed output tokens (thinking included) per effort: (typical low, typical high). Replaced by
# the observed mean per config as soon as results exist (see :func:`estimate`).
OUTPUT_TOKEN_RANGE: dict[str, tuple[int, int]] = {
    "low": (1500, 5000),
    "medium": (2500, 9000),
    "high": (4000, 14000),
    "xhigh": (6000, 22000),
    "max": (8000, 32000),
}
RETRY_STATUSES = ("error",)  # results with these statuses are re-requested on the next run
RATE_LIMIT_STREAK = 3  # consecutive rate-limit failures that stop a run


# ------------------------------------------------------------------------ prompt file


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ConfigSpec(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    model: str
    effort: str

    @field_validator("model")
    @classmethod
    def _known_model(cls, v: str) -> str:
        if v not in llm.PRICES:
            raise ValueError(f"unknown model {v!r}; known: {', '.join(llm.PRICES)}")
        return v

    @field_validator("effort")
    @classmethod
    def _known_effort(cls, v: str) -> str:
        if v not in llm.EFFORTS:
            raise ValueError(f"effort must be one of {llm.EFFORTS}")
        return v


class CheckFields(_Strict):
    """Names of JSON keys (anywhere in the output) that the automatic checks look at."""

    quote_fields: list[str] = []  # strings that should be verbatim from the transcript
    timestamp_fields: list[str] = []  # mm:ss / h:mm:ss / seconds, inside the episode
    list_fields: list[str] = []  # lists of strings (tags, topics): agreement with the reference


class ComparePrompt(_Strict):
    system: str
    user: str
    schema_: dict[str, Any] | None = Field(None, alias="schema")
    max_tokens: int = Field(llm.DEFAULT_MAX_TOKENS, ge=1024, le=128_000)
    checks: CheckFields = CheckFields()
    matrix: list[ConfigSpec]
    reference: str
    sample: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> ComparePrompt:
        ids = [c.id for c in self.matrix]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("matrix needs at least one config, with unique ids")
        if self.reference not in ids:
            raise ValueError(f"reference {self.reference!r} is not a matrix id")
        if not self.sample or len(set(self.sample)) != len(self.sample):
            raise ValueError("sample needs at least one episode id, without repeats")
        return self

    def prompt_hash(self) -> str:
        blob = json.dumps(
            {"system": self.system, "user": self.user, "schema": self.schema_},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def run_id(self) -> str:
        return "run-" + self.prompt_hash()[:10]

    def is_placeholder(self) -> bool:
        return PLACEHOLDER_MARK in self.system or PLACEHOLDER_MARK in self.user

    def lint(self) -> list[str]:
        """Problems that must be fixed before a run (empty = ready)."""
        out: list[str] = []
        if self.is_placeholder():
            out.append(f"the prompt still contains {PLACEHOLDER_MARK!r}: it has not been written")
        for name, text in (("system", self.system), ("user", self.user)):
            for found in sorted(set(_PLACEHOLDER_RE.findall(text)) - set(PLACEHOLDERS)):
                out.append(f"{name}: unknown placeholder {{{{{found}}}}} (known: {PLACEHOLDERS})")
        if "transcript" not in _PLACEHOLDER_RE.findall(self.user):
            out.append("user: the template never uses {{transcript}}")
        if self.schema_ is None:
            out.append("schema: missing (JSON output was chosen; add a JSON Schema)")
        else:
            out.extend(lint_schema(self.schema_))
        return out


def lint_schema(schema: dict[str, Any]) -> list[str]:
    """JSON Schema validity plus the structured-outputs rule that objects are closed."""
    import jsonschema

    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        return [f"schema: not a valid JSON Schema: {exc.message}"]
    problems: list[str] = []
    if schema.get("type") != "object":
        problems.append("schema: the root must be an object (type: object)")

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                if node.get("additionalProperties") is not False:
                    problems.append(f"schema: {path or '<root>'} needs additionalProperties: false")
                missing = set(node.get("properties", {})) - set(node.get("required", []))
                if missing:
                    problems.append(
                        f"schema: {path or '<root>'} has properties not listed in required: "
                        f"{sorted(missing)}"
                    )
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(schema, "")
    return problems


def load_prompt(path: Path) -> ComparePrompt:
    """Read and structurally validate the prompt file (``lint()`` is separate: it may still
    contain the unedited placeholder)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ValueError(f"prompt file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return ComparePrompt.model_validate(data)


def render_template(template: str, values: dict[str, str]) -> str:
    """Replace ``{{name}}`` with ``values[name]`` (plain replacement: JSON braces are safe)."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            raise ValueError(f"unknown placeholder {{{{{name}}}}}")
        return values[name]

    return _PLACEHOLDER_RE.sub(sub, template)


# ------------------------------------------------------------------------ episode input


@dataclass
class EpisodeInput:
    episode_id: str
    title: str
    pub_date: str
    duration_s: float
    show_notes: str
    transcript: str  # rendered utterance lines (what the model sees)
    n_words: int
    plain_text: str = ""  # the words only, for the quote check

    def values(self) -> dict[str, str]:
        return {
            "title": self.title,
            "pub_date": self.pub_date,
            "duration": format_timestamp(self.duration_s),
            "show_notes": self.show_notes or "(none)",
            "transcript": self.transcript,
        }

    def prompt(self, p: ComparePrompt) -> tuple[str, str]:
        v = self.values()
        return render_template(p.system, v), render_template(p.user, v)


def load_episode(cfg: Config, episode_id: str) -> EpisodeInput:
    tp = transcript_path(cfg, episode_id)
    if not tp.is_file():
        raise ValueError(f"episode {episode_id}: no transcript at {tp}")
    meta = EpisodeMeta.load(episode_dir(cfg, episode_id) / "meta.json")
    tr = Transcript.load(tp)
    return EpisodeInput(
        episode_id=episode_id,
        title=meta.title,
        pub_date=meta.pub_date.date().isoformat(),
        duration_s=tr.duration_s,
        show_notes=meta.description.strip(),
        transcript=tr.render(),
        n_words=len(tr.words),
        plain_text=tr.text(),
    )


# ------------------------------------------------------------------------ jobs / plan


@dataclass(frozen=True)
class Job:
    episode_id: str
    spec: ConfigSpec

    @property
    def key(self) -> tuple[str, str]:
        return (self.episode_id, self.spec.id)


def run_dir(cfg: Config, run_id: str) -> Path:
    return cfg.llm.compare_dir / run_id


def result_path(cfg: Config, run_id: str, episode_id: str, config_id: str) -> Path:
    return run_dir(cfg, run_id) / episode_id / f"{config_id}.json"


def load_result(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def select_jobs(
    p: ComparePrompt, *, episodes: int | None = None, only_configs: list[str] | None = None
) -> list[Job]:
    """The (episode, config) grid, episodes in sample order (``episodes`` = the first N)."""
    eps = p.sample[:episodes] if episodes is not None else p.sample
    specs = [c for c in p.matrix if only_configs is None or c.id in only_configs]
    return [Job(e, s) for e in eps for s in specs]


def pending_jobs(
    cfg: Config, p: ComparePrompt, jobs: list[Job], *, retry_bad: bool = False
) -> list[Job]:
    """Jobs without a usable result. ``retry_bad`` also redoes truncated/refused/invalid ones."""
    out = []
    for job in jobs:
        existing = load_result(result_path(cfg, p.run_id, *job.key))
        if existing is None:
            out.append(job)
        elif existing.get("status") in RETRY_STATUSES or (
            retry_bad and existing.get("status") != "ok"
        ):
            out.append(job)
    return out


@dataclass
class Estimate:
    job: Job
    input_tokens: int
    output_lo: int
    output_hi: int
    worst_usd: float  # input + max_tokens of output: the hard bound used by the budget gate
    basis: str  # "offline" | "count_tokens"

    @property
    def usd_lo(self) -> float:
        return llm.cost_usd(self.job.spec.model, self.input_tokens, self.output_lo)

    @property
    def usd_hi(self) -> float:
        return llm.cost_usd(self.job.spec.model, self.input_tokens, self.output_hi)


def observed_output_tokens(cfg: Config, p: ComparePrompt) -> dict[str, float]:
    """Mean output tokens per config id over the finished results of this run."""
    sums: dict[str, list[int]] = {}
    base = run_dir(cfg, p.run_id)
    for path in sorted(base.glob("*/*.json")) if base.is_dir() else []:
        r = load_result(path)
        if r and r.get("status") in ("ok", "invalid", "truncated"):
            sums.setdefault(path.stem, []).append(int(r["result"]["output_tokens"]))
    return {k: sum(v) / len(v) for k, v in sums.items()}


def estimate(
    cfg: Config,
    p: ComparePrompt,
    jobs: list[Job],
    *,
    inputs: dict[str, EpisodeInput] | None = None,
    token_counts: dict[tuple[str, str], int] | None = None,
) -> list[Estimate]:
    """Per-job token and dollar estimates. Output tokens use the observed mean for the config
    when this run has results (+-30%), otherwise the assumed per-effort range."""
    inputs = inputs if inputs is not None else {}
    observed = observed_output_tokens(cfg, p)
    out: list[Estimate] = []
    for job in jobs:
        ep = inputs.get(job.episode_id) or inputs.setdefault(
            job.episode_id, load_episode(cfg, job.episode_id)
        )
        exact = (token_counts or {}).get(job.key)
        if exact is not None:
            n_in, basis = exact, "count_tokens"
        else:
            system, user = ep.prompt(p)
            n_in, basis = int((len(system) + len(user)) / CHARS_PER_TOKEN), "offline"
        if job.spec.id in observed:
            mean = observed[job.spec.id]
            lo, hi = int(mean * 0.7), int(mean * 1.3)
        else:
            lo, hi = OUTPUT_TOKEN_RANGE[job.spec.effort]
        margin = 1.0 if basis == "count_tokens" else INPUT_MARGIN
        worst = llm.cost_usd(job.spec.model, int(n_in * margin), p.max_tokens)
        out.append(Estimate(job, n_in, lo, hi, worst, basis))
    return out


# ------------------------------------------------------------------------------- run


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(res: llm.LlmResult) -> str:
    if res.stop_reason == "refusal":
        return "refusal"
    if res.stop_reason == "max_tokens":
        return "truncated"
    if res.parse_error is not None:
        return "invalid"
    return "ok" if res.ok else "invalid"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_run_meta(cfg: Config, p: ComparePrompt) -> None:
    """``run.json``: the exact prompt behind this run id (provenance; written once)."""
    path = run_dir(cfg, p.run_id) / "run.json"
    if path.exists():
        return
    _write_json(
        path,
        {
            "run_id": p.run_id,
            "prompt_hash": p.prompt_hash(),
            "created_at": _now(),
            "sdk_version": llm.sdk_version(),
            "prices_verified": llm.PRICES_VERIFIED,
            "system": p.system,
            "user": p.user,
            "schema": p.schema_,
            "max_tokens": p.max_tokens,
        },
    )


def _record_ledger(cfg: Config, **row: Any) -> None:
    """One short transaction per call (a crash keeps every finished call on the books)."""
    with state.connect(cfg.state_db) as conn:
        state.record_llm_usage(conn, purpose="compare", **row)


@dataclass
class RunSummary:
    run_id: str
    done: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    spent_usd: float = 0.0
    stopped: str | None = None  # why the run ended early, if it did
    errors: list[str] = field(default_factory=list)


def execute_job(
    cfg: Config,
    p: ComparePrompt,
    client: Any,
    job: Job,
    ep: EpisodeInput,
) -> tuple[str, float, str | None]:
    """One API call; writes the result file and the ledger row. Returns (status, usd, error).

    An :class:`llm.LlmError` becomes an ``error`` result (retried by the next run) except that a
    fatal one (bad key, no permission) is re-raised so the whole run stops.
    """
    system, user = ep.prompt(p)
    base = {
        "config_id": job.spec.id,
        "episode_id": job.episode_id,
        "model_requested": job.spec.model,
        "effort": job.spec.effort,
        "prompt_hash": p.prompt_hash(),
        "run_id": p.run_id,
        "sdk_version": llm.sdk_version(),
        "created_at": _now(),
    }
    path = result_path(cfg, p.run_id, *job.key)
    try:
        res = llm.call(
            client,
            model=job.spec.model,
            system=system,
            user=user,
            effort=job.spec.effort,
            schema=p.schema_,
            max_tokens=p.max_tokens,
        )
    except llm.LlmError as exc:
        _write_json(path, {**base, "status": "error", "error": redact(exc), "kind": exc.kind})
        _record_ledger(
            cfg,
            run_id=p.run_id,
            episode_id=job.episode_id,
            model=job.spec.model,
            effort=job.spec.effort,
            status="error",
            cost_usd=0.0,
            request_id=exc.request_id,
        )
        if exc.fatal:
            raise
        return "error", 0.0, redact(exc)
    status = _status(res)
    _write_json(path, {**base, "status": status, "result": res.to_dict()})
    _record_ledger(
        cfg,
        run_id=p.run_id,
        episode_id=job.episode_id,
        model=res.model,
        effort=job.spec.effort,
        status=status,
        cost_usd=res.cost_usd,
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens,
        cache_read=res.cache_read,
        cache_write=res.cache_write,
        request_id=res.request_id,
    )
    return status, res.cost_usd, None


def run_jobs(
    cfg: Config,
    p: ComparePrompt,
    jobs: list[Job],
    *,
    client: Any,
    max_usd: float,
    progress: Callable[[str], None] = lambda _msg: None,
    inputs: dict[str, EpisodeInput] | None = None,
    stop: threading.Event | None = None,
) -> RunSummary:
    """Run ``jobs`` with up to ``llm.concurrency`` calls in flight.

    Before each submit the worst case (spent + every in-flight call's worst case + this call's
    worst case, output at ``max_tokens``) must fit under ``max_usd`` and under the month's
    remaining budget, so neither cap can be passed whatever the model does. Results and ledger
    rows are written by the workers, so an interrupt loses nothing that finished.
    """
    summary = RunSummary(run_id=p.run_id)
    if not jobs:
        return summary
    write_run_meta(cfg, p)
    inputs = inputs if inputs is not None else {}
    ests = {e.job.key: e for e in estimate(cfg, p, jobs, inputs=inputs)}
    queue = list(jobs)
    inflight: dict[Future[tuple[str, float, str | None]], Job] = {}
    reserved: dict[Future[tuple[str, float, str | None]], float] = {}
    rate_streak = 0
    total = len(jobs)
    stop = stop or threading.Event()

    def month_headroom_ok(extra: float) -> bool:
        with state.connect(cfg.state_db) as conn:
            try:
                state.check_budget(conn, cfg.llm.monthly_budget_usd, extra)
            except state.BudgetExceeded as exc:
                summary.stopped = str(exc)
                return False
        return True

    with ThreadPoolExecutor(max_workers=cfg.llm.concurrency) as pool:
        try:
            while queue or inflight:
                while queue and len(inflight) < cfg.llm.concurrency and not summary.stopped:
                    if stop.is_set():
                        summary.stopped = "stopped"
                        break
                    job = queue[0]
                    worst = ests[job.key].worst_usd
                    committed = summary.spent_usd + sum(reserved.values())
                    if committed + worst > max_usd + 1e-9:
                        if not inflight:
                            summary.stopped = (
                                f"run budget ${max_usd:.2f}: ${summary.spent_usd:.2f} spent, "
                                f"next call could cost up to ${worst:.2f}"
                            )
                        break  # wait for in-flight calls to settle, then re-check
                    if not month_headroom_ok(sum(reserved.values()) + worst):
                        break
                    queue.pop(0)
                    ep = inputs.get(job.episode_id) or inputs.setdefault(
                        job.episode_id, load_episode(cfg, job.episode_id)
                    )
                    fut = pool.submit(execute_job, cfg, p, client, job, ep)
                    inflight[fut] = job
                    reserved[fut] = worst
                if not inflight:
                    break
                finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    job = inflight.pop(fut)
                    reserved.pop(fut, None)
                    try:
                        status, usd, err = fut.result()
                    except llm.LlmError as exc:
                        summary.stopped = f"fatal API error: {redact(exc)}"
                        summary.errors.append(f"{job.episode_id} {job.spec.id}: {redact(exc)}")
                        continue
                    summary.done += 1
                    summary.spent_usd += usd
                    summary.by_status[status] = summary.by_status.get(status, 0) + 1
                    if err:
                        summary.errors.append(f"{job.episode_id} {job.spec.id}: {err}")
                    rate_streak = rate_streak + 1 if (err and "rate limited" in err) else 0
                    if rate_streak >= RATE_LIMIT_STREAK:
                        summary.stopped = f"{rate_streak} rate-limit failures in a row"
                    progress(
                        f"[{summary.done}/{total}] {job.episode_id} {job.spec.id}: {status}"
                        f" ${usd:.3f} (run ${summary.spent_usd:.2f})"
                    )
                if summary.stopped:
                    queue.clear()
        except KeyboardInterrupt:
            summary.stopped = "interrupted"
            pool.shutdown(wait=True, cancel_futures=True)
    return summary


# ------------------------------------------------------------------------------ checks

_WORD_RE = re.compile(r"[a-z0-9]+")
_TS_RE = re.compile(r"(?<![\d:])(\d{1,3}):(\d{2})(?::(\d{2}))?(?![\d:])")


def normalize_words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("’", "'").replace("'", ""))


def _trigrams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def find_values(node: Any, names: set[str]) -> list[Any]:
    """Every value stored under a key in ``names``, anywhere in the JSON tree, in document order.
    A list value contributes its items."""
    out: list[Any] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            for k, v in n.items():
                if k in names:
                    out.extend(v if isinstance(v, list) else [v])
                else:
                    walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


def parse_timestamps(value: Any) -> list[float] | None:
    """Seconds for every timestamp in ``value`` (number, ``mm:ss``, ``h:mm:ss``, ``a-b``);
    None when nothing parses."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str):
        found = []
        for a, b, c in _TS_RE.findall(value):
            found.append(int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b))
        if found:
            return [float(x) for x in found]
        try:
            return [float(value)]
        except ValueError:
            return None
    return None


def run_checks(
    prompt: ComparePrompt,
    data: Any,
    *,
    transcript_text: str,
    duration_s: float,
    reference: Any = None,
) -> dict[str, Any]:
    """Automatic checks of one parsed output (see the module docstring; no API calls)."""
    checks: dict[str, Any] = {}
    if prompt.schema_ is not None:
        import jsonschema

        errs = sorted(
            jsonschema.Draft202012Validator(prompt.schema_).iter_errors(data),
            key=lambda e: list(e.path),
        )
        checks["schema_ok"] = not errs
        checks["schema_errors"] = [e.message[:160] for e in errs[:3]]
    cf = prompt.checks
    if cf.quote_fields:
        t_words = normalize_words(transcript_text)
        t_joined = " " + " ".join(t_words) + " "
        t_tri = _trigrams(t_words)
        exact = fuzzy = missing = 0
        missing_examples: list[str] = []
        for q in find_values(data, set(cf.quote_fields)):
            if not isinstance(q, str) or not q.strip():
                continue
            qw = normalize_words(q)
            if not qw:
                continue
            if " " + " ".join(qw) + " " in t_joined:
                exact += 1
                continue
            tri = _trigrams(qw)
            if tri and len(tri & t_tri) / len(tri) >= 0.8:
                fuzzy += 1
            else:
                missing += 1
                if len(missing_examples) < 3:
                    missing_examples.append(q[:100])
        checks["quotes"] = {
            "total": exact + fuzzy + missing,
            "exact": exact,
            "fuzzy": fuzzy,
            "missing": missing,
            "missing_examples": missing_examples,
        }
    if cf.timestamp_fields:
        stamps: list[float] = []
        unparseable = 0
        for v in find_values(data, set(cf.timestamp_fields)):
            parsed = parse_timestamps(v)
            if parsed is None:
                unparseable += 1
            else:
                stamps.extend(parsed)
        slack = 5.0
        checks["timestamps"] = {
            "total": len(stamps) + unparseable,
            "in_range": sum(0 <= s <= duration_s + slack for s in stamps),
            "out_of_range": sum(not (0 <= s <= duration_s + slack) for s in stamps),
            "unparseable": unparseable,
            "out_of_order": sum(b < a for a, b in zip(stamps, stamps[1:], strict=False)),
        }
    if cf.list_fields:
        lists = {f: _list_set(data, f) for f in cf.list_fields}
        checks["lists"] = {f: sorted(s) for f, s in lists.items()}
        if reference is not None:
            checks["agreement"] = {
                f: jaccard(lists[f], _list_set(reference, f)) for f in cf.list_fields
            }
    return checks


def _list_set(data: Any, field_name: str) -> set[str]:
    return {
        " ".join(normalize_words(v))
        for v in find_values(data, {field_name})
        if isinstance(v, str) and normalize_words(v)
    }


def jaccard(a: set[str], b: set[str]) -> float | None:
    """|a & b| / |a | b|; None when both are empty (nothing to agree on)."""
    if not a and not b:
        return None
    return len(a & b) / len(a | b)
