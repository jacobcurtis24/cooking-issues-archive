"""A fake Anthropic client for tests: no network, no key, records every request.

Install with :func:`install`. Under pytest ``ciw.llm.make_client`` refuses to build the real
client, so a test that forgets to install a fake fails instead of spending money.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from ciw import llm

QUOTE = "chill the liquid to thirty four degrees"
TRANSCRIPT_TEXT = (
    "so today we are talking about home carbonation rigs and how a cold keg holds the gas "
    "dave says always chill the liquid to thirty four degrees before carbonating "
    "then sous vide brisket for thirty six hours changes the texture a lot"
)


def default_json(model: str = "claude-sonnet-5-5") -> dict[str, Any]:
    return {
        "summary": "Carbonation and brisket.",
        "tags": ["carbonation", "sous vide"],
        "key_quotes": [{"timestamp": "00:05", "quote": QUOTE}],
    }


def message(
    text: str,
    *,
    model: str = "claude-sonnet-5-5",
    stop_reason: str = "end_turn",
    input_tokens: int = 20_000,
    output_tokens: int = 3_000,
    cache_read: int = 0,
    cache_write: int = 0,
    request_id: str = "req_fake",
    stop_details: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        _request_id=request_id,
    )


Responder = Callable[[dict[str, Any]], Any]  # request -> message | Exception


class _Stream:
    def __init__(self, outcome: Any):
        self.outcome = outcome

    def __enter__(self) -> _Stream:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get_final_message(self) -> Any:
        return self.outcome


class FakeClient:
    def __init__(self, respond: Responder | None = None, *, delay: float = 0.0):
        self.respond = respond or (
            lambda req: message(json.dumps(default_json(req["model"])), model=req["model"])
        )
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self.max_inflight = 0
        self._inflight = 0
        self._lock = threading.Lock()
        self.messages = SimpleNamespace(stream=self._stream, count_tokens=self._count)

    def _stream(self, **req: Any) -> _Stream:
        with self._lock:
            self.calls.append(req)
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.delay:
                time.sleep(self.delay)
            return _Stream(self.respond(req))
        finally:
            with self._lock:
                self._inflight -= 1

    def _count(self, **req: Any) -> SimpleNamespace:
        self.count_calls.append(req)
        return SimpleNamespace(input_tokens=12_345)


def install(monkeypatch, client: FakeClient) -> FakeClient:
    """Make ``ciw.llm.make_client`` return ``client`` for the rest of the test."""
    monkeypatch.setattr(llm, "_client_factory", lambda cfg: client)
    return client
