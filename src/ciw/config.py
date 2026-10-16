"""Configuration: ``config.yaml`` (non-secret) plus ``.env`` (secrets).

Paths in ``config.yaml`` may be relative; they are resolved against the project root so the
tools work from any working directory. The project root is ``$CIW_ROOT`` if set, else the
nearest ancestor of this package containing ``pyproject.toml``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

ROOT_ENV_VAR = "CIW_ROOT"
CONFIG_FILENAME = "config.yaml"
REDACTED = "***"

# Environment variables holding secrets; never log their values.
SECRET_ENV_VARS = ("PATREON_FEED_URL", "HF_TOKEN", "ANTHROPIC_API_KEY", "NTFY_TOPIC")


# --------------------------------------------------------------------------- root


def project_root() -> Path:
    """Locate the project root: ``$CIW_ROOT`` or the nearest ancestor with pyproject.toml."""
    env = os.environ.get(ROOT_ENV_VAR)
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(
        f"Cannot locate the project root (no pyproject.toml above {__file__}); set {ROOT_ENV_VAR}."
    )


# ------------------------------------------------------------------- test-safety guard

# Under pytest, anything that would touch the real runtime data must fail loudly. Twice (T4, T5)
# an old stub test ran a real `ciw download`/`ciw transcribe` against the real config and caused
# real downloads and GPU work. pytest sets PYTEST_CURRENT_TEST in the test process and in any
# subprocess it starts, so the guard also covers `python -c` tests. A test that really must read
# the real paths opts in with the ``allow_real_paths`` fixture (or ``@pytest.mark.real_paths``),
# which sets ALLOW_REAL_PATHS_ENV. The blog database at the repo root is read-only and allowed.
PYTEST_ENV_VAR = "PYTEST_CURRENT_TEST"
ALLOW_REAL_PATHS_ENV = "CIW_TESTS_ALLOW_REAL_PATHS"
GUARDED_PATHS = ("data_dir", "state_db", "audio_dir", "clips_dir")
REAL_AUDIO_ROOT = Path("/mnt/ssd0/ciw")


class UnsafeTestPathError(RuntimeError):
    """A test tried to resolve a runtime path to the real repo ``data/`` or ``/mnt/ssd0/ciw``."""


def code_root() -> Path:
    """The checkout this package lives in (ignores ``$CIW_ROOT``)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(f"Cannot locate the project root (no pyproject.toml above {__file__}).")


def real_runtime_roots() -> tuple[Path, ...]:
    return (code_root() / "data", REAL_AUDIO_ROOT)


def _under(path: Path, root: Path) -> bool:
    return Path(os.path.abspath(path)).resolve().is_relative_to(root.resolve())


def guard_path(path: Path | str, what: str = "path") -> None:
    """Raise :class:`UnsafeTestPathError` if, under pytest, ``path`` is in the real runtime data."""
    if not os.environ.get(PYTEST_ENV_VAR) or os.environ.get(ALLOW_REAL_PATHS_ENV):
        return
    for root in real_runtime_roots():
        if _under(Path(path), root):
            raise UnsafeTestPathError(
                f"refusing to use {what} {path} under pytest: it is inside the real runtime "
                f"data ({root}). Use the tmp_cfg fixture (or a tmp config.yaml with tmp "
                f"audio_dir/clips_dir); a test that really needs the real paths must use the "
                f"allow_real_paths fixture."
            )


def guard_config(cfg: Config) -> None:
    for name in GUARDED_PATHS:
        guard_path(getattr(cfg, name), f"config {name}")


# ------------------------------------------------------------------------- models


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppleIds(_Section):
    acast: int = 1572356745
    hrn: int = 380317598


class AsrConfig(_Section):
    backend: str = "whisperx"  # registry key, see ciw.asr
    model: str = "large-v3"
    batch_size: int = Field(8, ge=1)  # first batch size tried; halved on CUDA OOM
    min_batch_size: int = Field(2, ge=1)  # smallest batch tried before changing compute_type
    compute_type: str = "float16"
    fallback_compute_type: str | None = "int8_float16"  # tried (batch 8 -> 2 again) after OOM
    device: str = "cuda"
    vad: str = "pyannote"  # WhisperX VAD method: pyannote or silero (VAD is always on)
    language: str = "en"
    diarize: bool = True  # needs HF_TOKEN; without it the run proceeds without speakers
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    # Model caches (HF_HOME, and TORCH_HOME=<hf_home>/torch) live on the big disk, not ~/.cache.
    # A real HF_HOME / TORCH_HOME in the environment wins over this.
    hf_home: Path = Path("/mnt/ssd0/ciw/hf_cache")
    # Optional text file, one jargon term per line (# comments): the vocabulary. It always drives
    # the correction pass; it is ALSO Whisper's initial_prompt only when `vocabulary_prompt` is
    # true. Off by default: in the T6 experiment the prompt fixed jargon but made large-v3 drop
    # 7-18% of the speech (whole 30 s windows), see docs/progress.md.
    vocabulary_file: Path | None = None
    vocabulary_prompt: bool = False
    initial_prompt_max_chars: int = Field(600, ge=0)
    # Post-ASR correction against the vocabulary (ciw.correct). `aliases_file` holds explicit
    # `heard => correct` pairs (the highest-confidence rule); `correct_min_confidence` is the
    # bar for fuzzy (non-alias) fixes.
    correct: bool = True
    aliases_file: Path | None = None
    correct_min_confidence: float = Field(0.86, ge=0, le=1)


class LlmConfig(_Section):
    model: str = "claude-sonnet-5-5"
    monthly_budget_usd: float = Field(25.0, ge=0)


class NotifyConfig(_Section):
    server: str = "https://ntfy.sh"
    # The topic itself is a secret: it is read from the environment (NTFY_TOPIC), not YAML.
    topic_env: str = "NTFY_TOPIC"
    threshold: int = Field(7, ge=0, le=10)
    clip_threshold: int = Field(9, ge=0, le=10)


class DownloadConfig(_Section):
    # Refuse to start a download when the audio filesystem has less free space than this (GiB).
    min_free_gb: float = Field(20.0, ge=0)
    # ffprobe duration must be within max(duration_tolerance_s, duration_tolerance_pct of the
    # feed's duration). Acast's dynamic ad insertion makes real files up to ~5% longer.
    duration_tolerance_s: float = Field(120.0, ge=0)
    duration_tolerance_pct: float = Field(0.05, ge=0, le=1)


class WebConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(5000, ge=1, le=65535)
    debug: bool = False


class Secrets(BaseModel):
    """Secret values from the environment / ``.env``. Reprs are masked by SecretStr."""

    patreon_feed_url: SecretStr | None = None
    hf_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    ntfy_topic: SecretStr | None = None

    def secret_values(self) -> list[str]:
        return [v.get_secret_value() for v in self.__dict__.values() if v is not None]


class Config(_Section):
    root: Path = Field(default_factory=project_root)
    blog_db: Path = Path("cooking_issues.db")
    data_dir: Path = Path("data")
    audio_dir: Path = Path("/mnt/ssd0/ciw/audio")
    clips_dir: Path = Path("/mnt/ssd0/ciw/clips")
    state_db: Path = Path("data/state.sqlite")
    apple_ids: AppleIds = AppleIds()
    poll_interval_hours: float = Field(6, gt=0)
    asr: AsrConfig = AsrConfig()
    llm: LlmConfig = LlmConfig()
    notify: NotifyConfig = NotifyConfig()
    download: DownloadConfig = DownloadConfig()
    web: WebConfig = WebConfig()
    secrets: Secrets = Field(default_factory=Secrets, repr=False)

    @property
    def episodes_dir(self) -> Path:
        return self.data_dir / "episodes"

    @property
    def ntfy_topic(self) -> str | None:
        """The ntfy topic, read from the env var named by ``notify.topic_env``."""
        if self.notify.topic_env == "NTFY_TOPIC" and self.secrets.ntfy_topic:
            return self.secrets.ntfy_topic.get_secret_value()
        return os.environ.get(self.notify.topic_env) or None

    def resolve_paths(self) -> Config:
        """Return a copy with every relative path made absolute against ``root``."""
        updates = {
            name: self.root / getattr(self, name)
            for name in ("blog_db", "data_dir", "audio_dir", "clips_dir", "state_db")
            if not getattr(self, name).is_absolute()
        }
        cfg = self.model_copy(update=updates)
        asr_updates = {
            name: self.root / getattr(self.asr, name)
            for name in ("hf_home", "vocabulary_file", "aliases_file")
            if getattr(self.asr, name) is not None and not getattr(self.asr, name).is_absolute()
        }
        if asr_updates:
            cfg = cfg.model_copy(update={"asr": self.asr.model_copy(update=asr_updates)})
        return cfg


# ------------------------------------------------------------------------ loading


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines (``#`` comments, optional ``export``, optional quotes)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Load ``path`` into ``environ`` (default ``os.environ``) without overriding real env vars.

    Returns the keys that were set. A missing file is not an error.
    """
    env = os.environ if environ is None else environ
    if not path.is_file():
        return {}
    added = {}
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        if value and not env.get(key):
            env[key] = value
            added[key] = value
    return added


def load_config(root: Path | None = None, *, use_dotenv: bool = True) -> Config:
    """Load ``<root>/config.yaml`` (defaults if absent) and ``<root>/.env`` secrets."""
    root = (root or project_root()).resolve()
    path = root / CONFIG_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping")
    if use_dotenv:
        load_dotenv(root / ".env")
    secrets = Secrets(**{k.lower(): os.environ.get(k) or None for k in SECRET_ENV_VARS})
    cfg = Config(root=root, secrets=secrets, **data).resolve_paths()
    guard_config(cfg)
    return cfg


# ---------------------------------------------------------------------- redaction

_SECRET_PARAM = re.compile(
    r"(?i)\b([\w-]*(?:token|key|secret|password|auth|sig)[\w-]*)=([^&\s\"']+)"
)


def redact(text: object, secrets: Iterable[str] | Mapping[str, str] | None = None) -> str:
    """Return ``text`` with secret values masked, safe for logs.

    Masks (1) any literal secret passed in ``secrets``, (2) current values of the known secret
    environment variables, and (3) the value of ``token=``/``key=``/``auth=``-style URL or
    query parameters.
    """
    s = str(text)
    literals: list[str] = []
    if isinstance(secrets, Mapping):
        literals.extend(secrets.values())
    elif secrets is not None:
        literals.extend(secrets)
    literals.extend(os.environ.get(k, "") for k in SECRET_ENV_VARS)
    for lit in sorted({x for x in literals if x and len(x) >= 4}, key=len, reverse=True):
        s = s.replace(lit, REDACTED)
    return _SECRET_PARAM.sub(lambda m: f"{m.group(1)}={REDACTED}", s)
