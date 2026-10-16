"""Minimal ntfy sender (T10 extends this with digests and clip pushes).

The topic is a secret (``NTFY_TOPIC``). Without one, alerts are only logged. Sending never
raises: a failed push must not break a poll or a download.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from ciw.config import Config, redact

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0
_PRIORITIES = {"min", "low", "default", "high", "urgent", "max", "1", "2", "3", "4", "5"}


def http_client() -> httpx.Client:
    """The client used for pushes (tests monkeypatch this to inject a MockTransport)."""
    return httpx.Client(timeout=TIMEOUT_S, headers={"User-Agent": "ciw/0.1"})


def _ascii(value: str) -> str:
    """Header values must be ASCII for httpx; keep them readable."""
    return value.encode("ascii", "replace").decode("ascii").replace("\n", " ").replace("\r", " ")


def send_ntfy(
    cfg: Config,
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: Iterable[str] = (),
    click: str | None = None,
    attach: str | None = None,
) -> bool:
    """POST a plain-text notification to ``<ntfy.server>/<topic>``. True only if it was sent.

    No topic: the alert is logged and False is returned. Network or HTTP errors are logged
    (with secrets redacted) and give False.
    """
    secrets = [*cfg.secrets.secret_values()]
    topic = cfg.ntfy_topic
    if topic:
        secrets.append(topic)
    title_s = redact(title, secrets)
    body = redact(message, secrets)
    if not topic:
        log.warning(
            "ntfy: no topic configured (NTFY_TOPIC unset); alert not sent: %s: %s", title_s, body
        )
        return False
    if priority not in _PRIORITIES:
        priority = "default"
    headers = {"Title": _ascii(title_s), "Priority": priority}
    tag_list = [t for t in tags if t]
    if tag_list:
        headers["Tags"] = ",".join(tag_list)
    if click:
        headers["Click"] = _ascii(click)
    if attach:
        headers["Attach"] = _ascii(attach)
    url = f"{cfg.notify.server.rstrip('/')}/{topic}"
    try:
        with http_client() as client:
            resp = client.post(url, content=body.encode("utf-8"), headers=headers)
        if resp.status_code >= 300:
            log.warning("ntfy: server answered HTTP %d; alert not delivered", resp.status_code)
            return False
    except httpx.HTTPError as exc:
        log.warning("ntfy: could not send (%s): %s", type(exc).__name__, redact(exc, secrets))
        return False
    log.info("ntfy: sent %r", title_s)
    return True
