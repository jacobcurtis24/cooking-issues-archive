"""Feed health alerts for ``ciw poll``: one ntfy message per status change, never per poll.

``feeds.alert_status`` remembers the unhealthy status an alert was last *sent* for (``auth`` or
``empty``). A feed that stays broken is silent on later polls; going back to healthy sends one
"recovered" note. Transient failures (``error``: network, 5xx) are neither alerted nor do they
reset anything. If the push could not be delivered (no topic, network down) nothing is recorded,
so the alert is attempted again on the next poll.
"""

from __future__ import annotations

import logging
import re
import sqlite3

from ciw import notify, state
from ciw.config import Config, redact
from ciw.ingest import FeedReport, PollReport

log = logging.getLogger(__name__)

HEALTHY = ("ok", "not-modified")
ALERTING = ("auth", "empty")

_FALLBACK = {
    "patreon": (
        "ciw falls back to the public Acast and HRN feeds, so polling continues, but "
        "Patreon-only posts and the Patreon (ad-free) audio are unavailable until it is fixed."
    ),
    "acast": "ciw keeps working from the other feeds and the last cached copy of this one.",
    "hrn": "ciw keeps working from the other feeds and the last cached copy of this one.",
}


def _alert_text(rep: FeedReport) -> tuple[str, str]:
    name = rep.name.capitalize()
    code = re.search(r"HTTP (\d{3})", rep.error or "")
    if rep.status == "auth":
        title = f"{name} feed rejected ({'HTTP ' + code.group(1) if code else 'not authorised'})"
        what = (
            "The feed answered 401/403 (not authorised): the token may have expired, been "
            "rotated, or the tier lapsed. Update PATREON_FEED_URL in .env."
            if rep.name == "patreon"
            else "The feed answered 401/403 (not authorised)."
        )
    else:
        title = f"{name} feed has no audio items"
        what = (
            "The feed was fetched but contains zero audio episodes (changed format, or an "
            "error page served as 200)."
        )
    return title, f"{what} {_FALLBACK.get(rep.name, '')}".strip()


def alert_feed_health(cfg: Config, conn: sqlite3.Connection, report: PollReport) -> list[str]:
    """Send alerts/recoveries for the feeds in ``report``; returns short descriptions of what
    was sent (for the CLI). Reads/writes ``feeds.alert_status`` in ``conn``."""
    sent: list[str] = []
    for rep in report.feeds.values():
        row = state.get_feed(conn, rep.name)
        last = row["alert_status"] if row else None
        if rep.status in ALERTING:
            if last == rep.status:
                continue
            title, body = _alert_text(rep)
            body = redact(body, cfg.secrets.secret_values())
            if notify.send_ntfy(
                cfg, title, body, priority="high", tags=("warning", f"feed-{rep.name}")
            ):
                state.save_feed(conn, rep.name, alert_status=rep.status)
                state.checkpoint(conn)
                sent.append(f"{rep.name}: {rep.status} alert")
            else:
                log.warning("%s feed is %s (alert not delivered)", rep.name, rep.status)
        elif rep.status in HEALTHY and last in ALERTING:
            title = f"{rep.name.capitalize()} feed recovered"
            body = (
                f"The {rep.name} feed is healthy again ({rep.items} audio items); it had been "
                f"failing with status '{last}'."
            )
            if notify.send_ntfy(cfg, title, body, tags=("white_check_mark", f"feed-{rep.name}")):
                state.save_feed(conn, rep.name, alert_status="ok")
                state.checkpoint(conn)
                sent.append(f"{rep.name}: recovered note")
    return sent
