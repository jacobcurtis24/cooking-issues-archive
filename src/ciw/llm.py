"""The Claude client: one streamed Messages call, its usage, and what it cost.

``anthropic`` is imported inside the functions, so ``ciw --help`` and the web app stay light.
Rules for the models used here (claude-api skill, 2026-10): thinking is adaptive and cannot be
switched off on Sonnet 5.5 / Opus 5.5; effort goes in ``output_config``; ``temperature``,
``top_p``, a forced ``tool_choice``, ``budget_tokens`` and an assistant prefill are all rejected,
so none of them is ever sent. JSON comes from ``output_config.format`` (structured outputs). The
server-side ``fallbacks`` parameter is deliberately NOT used: a fallback would silently change the
model that answered, which a model comparison must never do.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ciw.config import PYTEST_ENV_VAR, Config, redact

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MAX_TOKENS = 32000

# ---------------------------------------------------------------------------- pricing

PRICES_VERIFIED = "2026-10-06"  # from the claude-api skill's model table; check before relying
CACHE_READ_FACTOR = 0.1  # of the input price
CACHE_WRITE_FACTOR = 1.25  # 5-minute cache, of the input price
BATCH_FACTOR = 0.5


@dataclass(frozen=True)
class Price:
    """USD per million tokens. ``long_*`` apply when the prompt exceeds ``long_above`` tokens."""

    input: float
    output: float
    long_above: int | None = None
    long_input: float | None = None
    long_output: float | None = None


PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50, 100_000, 0.50, 2.50),
    "claude-sonnet-5-5": Price(2.00, 10.00),
    "claude-opus-5-5": Price(4.00, 20.00),
}


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    *,
    batch: bool = False,
) -> float:
    """Estimated cost of one call from its ``usage`` (thinking tokens are in ``output_tokens``).

    ``input_tokens`` is the uncached part. An unknown model raises ``KeyError`` rather than being
    priced at zero, so a typo cannot slip past the budget cap.
    """
    p = PRICES[model]
    prompt = input_tokens + cache_read + cache_write
    rin, rout = p.input, p.output
    if p.long_above is not None and prompt > p.long_above:
        rin, rout = p.long_input or rin, p.long_output or rout
    usd = (
        input_tokens * rin
        + cache_read * rin * CACHE_READ_FACTOR
        + cache_write * rin * CACHE_WRITE_FACTOR
        + output_tokens * rout
    ) / 1_000_000
    return usd * (BATCH_FACTOR if batch else 1.0)


# ----------------------------------------------------------------------------- client


class LlmError(RuntimeError):
    """An API failure with a secret-free message. ``fatal`` errors stop a whole run."""

    def __init__(
        self, kind: str, message: str, *, status: int | None = None, request_id: str | None = None
    ):
        super().__init__(message)
        self.kind = (
            kind  # auth | permission | rate_limit | bad_request | server | connection | config
        )
        self.status = status
        self.request_id = request_id

    @property
    def fatal(self) -> bool:
        return self.kind in ("auth", "permission", "config")


# Tests install a fake here; under pytest the real client can never be built.
_client_factory: Callable[[Config], Any] | None = None


def set_client_factory(factory: Callable[[Config], Any] | None) -> None:
    global _client_factory
    _client_factory = factory


def make_client(cfg: Config) -> Any:
    """An ``anthropic.Anthropic`` client from ``ANTHROPIC_API_KEY`` (never logged)."""
    if _client_factory is not None:
        return _client_factory(cfg)
    if os.environ.get(PYTEST_ENV_VAR):
        raise RuntimeError("tests must install a fake LLM client (ciw.llm.set_client_factory)")
    key = cfg.secrets.anthropic_api_key
    if key is None:
        raise LlmError("config", "ANTHROPIC_API_KEY is not set (put it in .env)")
    import anthropic

    return anthropic.Anthropic(api_key=key.get_secret_value(), max_retries=cfg.llm.max_retries)


def sdk_version() -> str:
    try:
        import anthropic

        return str(anthropic.__version__)
    except Exception:  # noqa: BLE001 - provenance only
        return "unknown"


# ------------------------------------------------------------------------------ calls


@dataclass
class LlmResult:
    """One finished call. ``data`` is the parsed JSON when a schema was given and it parsed."""

    model: str  # the model that answered (response.model)
    effort: str | None
    text: str
    data: Any = None
    parse_error: str | None = None
    stop_reason: str | None = None
    stop_details: dict[str, Any] | None = None
    input_tokens: int = 0  # uncached
    output_tokens: int = 0  # includes thinking
    cache_read: int = 0
    cache_write: int = 0
    request_id: str | None = None
    latency_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return cost_usd(
            self.model, self.input_tokens, self.output_tokens, self.cache_read, self.cache_write
        )

    @property
    def ok(self) -> bool:
        """A normal, complete answer (not truncated, not a refusal, parsed if JSON was asked)."""
        return self.stop_reason in ("end_turn", "stop_sequence") and self.parse_error is None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = self.cost_usd
        return d


def _to_error(exc: BaseException) -> LlmError:
    import anthropic

    rid = getattr(exc, "request_id", None)
    msg = redact(getattr(exc, "message", None) or exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return LlmError("auth", f"authentication failed: {msg}", status=401, request_id=rid)
    if isinstance(exc, anthropic.PermissionDeniedError):
        return LlmError("permission", f"permission denied: {msg}", status=403, request_id=rid)
    if isinstance(exc, anthropic.RateLimitError):
        return LlmError("rate_limit", f"rate limited: {msg}", status=429, request_id=rid)
    if isinstance(exc, anthropic.APIStatusError):
        kind = "server" if exc.status_code >= 500 else "bad_request"
        return LlmError(
            kind, f"HTTP {exc.status_code}: {msg}", status=exc.status_code, request_id=rid
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return LlmError("connection", f"connection error: {msg}")
    return LlmError("bad_request", f"{type(exc).__name__}: {msg}", request_id=rid)


def _api_errors() -> tuple[type[BaseException], ...]:
    import anthropic

    return (anthropic.APIError,)


def build_request(
    *,
    model: str,
    system: str,
    user: str,
    effort: str | None,
    schema: dict[str, Any] | None,
    max_tokens: int,
) -> dict[str, Any]:
    """The Messages request body (also what ``count_tokens`` and the tests look at)."""
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}, not {effort!r}")
    output_config: dict[str, Any] = {}
    if effort is not None:
        output_config["effort"] = effort
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "thinking": {"type": "adaptive"},
    }
    if output_config:
        req["output_config"] = output_config
    return req


def call(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    effort: str | None = None,
    schema: dict[str, Any] | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LlmResult:
    """One streamed Messages call. API failures raise :class:`LlmError`; a refusal or a
    truncated answer is a normal result (see ``stop_reason``)."""
    req = build_request(
        model=model, system=system, user=user, effort=effort, schema=schema, max_tokens=max_tokens
    )
    t0 = time.monotonic()
    try:
        with client.messages.stream(**req) as stream:
            msg = stream.get_final_message()
    except _api_errors() as exc:
        raise _to_error(exc) from None
    latency = time.monotonic() - t0
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    usage = msg.usage
    details = getattr(msg, "stop_details", None)
    if details is not None and not isinstance(details, dict):
        details = details.to_dict() if hasattr(details, "to_dict") else {"raw": str(details)}
    result = LlmResult(
        model=msg.model,
        effort=effort,
        text=text,
        stop_reason=msg.stop_reason,
        stop_details=details,
        input_tokens=int(usage.input_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
        cache_read=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        request_id=getattr(msg, "_request_id", None),
        latency_s=round(latency, 2),
    )
    if schema is not None and text.strip():
        try:
            result.data = json.loads(text)
        except json.JSONDecodeError as exc:
            result.parse_error = f"invalid JSON: {exc}"
    elif schema is not None:
        result.parse_error = "empty response"
    return result


def count_tokens(client: Any, *, model: str, system: str, user: str) -> int:
    """Exact input tokens for a prompt (the ``count_tokens`` endpoint; free of charge)."""
    try:
        resp = client.messages.count_tokens(
            model=model, system=system, messages=[{"role": "user", "content": user}]
        )
    except _api_errors() as exc:
        raise _to_error(exc) from None
    return int(resp.input_tokens)
