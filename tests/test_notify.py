"""ntfy sender and feed-health alerts (mocked transports, no network)."""

import logging

import httpx
import pytest
from fakeweb import PATREON_TOKEN, PATREON_URL, FakeWeb
from typer.testing import CliRunner

from ciw import ingest, notify, state
from ciw.cli import app
from ciw.config import Secrets, load_config

runner = CliRunner()
TOPIC = "ciw-test-topic-abc123"


class Ntfy:
    """Collects POSTs; ``status`` / ``fail`` control the answer."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.fail = False

    def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return httpx.Response(self.status, json={"id": "x"})


@pytest.fixture
def ntfy(monkeypatch):
    fake = Ntfy()
    monkeypatch.setattr(
        notify, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake))
    )
    return fake


@pytest.fixture
def cfg_topic(tmp_cfg, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    return tmp_cfg.model_copy(update={"secrets": Secrets(ntfy_topic=TOPIC)})


# ------------------------------------------------------------------------ send_ntfy


def test_send_posts_plain_text_with_headers(cfg_topic, ntfy):
    ok = notify.send_ntfy(
        cfg_topic,
        "Title here",
        "body text",
        priority="high",
        tags=("warning", "x"),
        click="https://example.com/ep",
        attach="https://example.com/clip.mp3",
    )
    assert ok is True
    (req,) = ntfy.requests
    assert req.method == "POST" and str(req.url) == f"https://ntfy.sh/{TOPIC}"
    assert req.content == b"body text"
    assert req.headers["title"] == "Title here" and req.headers["priority"] == "high"
    assert req.headers["tags"] == "warning,x"
    assert req.headers["click"] == "https://example.com/ep"
    assert req.headers["attach"] == "https://example.com/clip.mp3"


def test_defaults_and_non_ascii_title(cfg_topic, ntfy):
    assert notify.send_ntfy(cfg_topic, "Café – news", "ünï")
    (req,) = ntfy.requests
    assert req.headers["priority"] == "default" and "tags" not in req.headers
    assert req.headers["title"].isascii()
    assert req.content.decode("utf-8") == "ünï"


def test_no_topic_logs_and_returns_false_without_network(tmp_cfg, ntfy, caplog):
    with caplog.at_level(logging.INFO):
        assert notify.send_ntfy(tmp_cfg, "Feed broke", "details") is False
    assert ntfy.requests == []
    assert "NTFY_TOPIC unset" in caplog.text and "Feed broke" in caplog.text


def test_network_failure_returns_false_and_never_leaks_topic(cfg_topic, ntfy, caplog):
    ntfy.fail = True
    with caplog.at_level(logging.DEBUG):
        assert notify.send_ntfy(cfg_topic, "t", "m") is False
    assert TOPIC not in caplog.text


def test_http_error_status_returns_false(cfg_topic, ntfy):
    ntfy.status = 500
    assert notify.send_ntfy(cfg_topic, "t", "m") is False


def test_message_and_title_are_redacted(cfg_topic, ntfy):
    notify.send_ntfy(
        cfg_topic, f"see https://x/y?token={PATREON_TOKEN}", f"url https://x/y?auth={PATREON_TOKEN}"
    )
    (req,) = ntfy.requests
    assert PATREON_TOKEN not in req.content.decode() and PATREON_TOKEN not in req.headers["title"]
    assert "***" in req.content.decode()


# ------------------------------------------------------------------ feed health alerts


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(ingest, "make_client", lambda cfg: fake.client(cfg.secrets.secret_values()))
    return fake


@pytest.fixture
def env(with_patreon, web, ntfy, monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    return web, ntfy


def poll():
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 0, result.output
    return result


def titles(ntfy):
    return [r.headers["title"] for r in ntfy.requests]


def test_auth_failure_alerts_once_and_names_the_fallback(env):
    web, ntfy = env
    web.status[PATREON_URL] = 403
    first = poll()
    assert len(ntfy.requests) == 1
    req = ntfy.requests[0]
    assert str(req.url) == f"https://ntfy.sh/{TOPIC}"
    assert req.headers["title"] == "Patreon feed rejected (HTTP 403)"
    assert req.headers["priority"] == "high"
    body = req.content.decode()
    assert "falls back to the public Acast and HRN feeds" in body
    assert "ntfy: patreon: auth alert sent" in first.output
    for text in (body, first.output, req.headers["title"]):
        assert PATREON_TOKEN not in text and TOPIC not in text
    poll()  # same status again (6 hours later): silent
    poll()
    assert len(ntfy.requests) == 1
    with state.connect(load_config().state_db) as conn:
        assert state.get_feed(conn, "patreon")["alert_status"] == "auth"


def test_status_change_alerts_again_and_recovery_sends_one_note(env):
    web, ntfy = env
    web.status[PATREON_URL] = 401
    poll()
    web.status[PATREON_URL] = 403  # still auth: not a change
    poll()
    assert titles(ntfy) == ["Patreon feed rejected (HTTP 401)"]
    del web.status[PATREON_URL]
    web.bodies[PATREON_URL] = (
        b"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title></channel></rss>"
    )
    poll()  # empty feed: a different bad status, so a new alert
    assert titles(ntfy)[-1] == "Patreon feed has no audio items"
    poll()
    assert len(ntfy.requests) == 2
    from fakeweb import fixture

    web.bodies[PATREON_URL] = fixture("patreon")
    poll()
    assert titles(ntfy)[-1] == "Patreon feed recovered"
    assert "healthy again" in ntfy.requests[-1].content.decode()
    assert ntfy.requests[-1].headers["priority"] == "default"
    poll()
    poll()
    assert len(ntfy.requests) == 3  # no further notes while healthy
    web.status[PATREON_URL] = 403  # and a new failure alerts again
    poll()
    assert len(ntfy.requests) == 4


def test_transient_errors_are_not_alerted_and_do_not_reset(env):
    web, ntfy = env
    web.status[PATREON_URL] = 403
    poll()
    web.status[PATREON_URL] = 500  # network-ish trouble: no alert, no 'recovered'
    poll()
    web.status[PATREON_URL] = 403  # still the same incident: still silent
    poll()
    assert len(ntfy.requests) == 1


def test_failed_push_is_retried_on_the_next_poll(env):
    web, ntfy = env
    web.status[PATREON_URL] = 403
    ntfy.fail = True
    poll()
    assert len(ntfy.requests) == 1
    ntfy.fail = False
    poll()
    assert len(ntfy.requests) == 2  # delivered this time
    poll()
    assert len(ntfy.requests) == 2


def test_without_a_topic_the_alert_is_only_logged(with_patreon, web, ntfy, monkeypatch, caplog):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    web.status[PATREON_URL] = 403
    with caplog.at_level(logging.WARNING):
        poll()
    assert ntfy.requests == []
    assert "alert not sent: Patreon feed rejected" in caplog.text
    assert PATREON_TOKEN not in caplog.text


def test_acast_empty_feed_alerts_with_its_own_text(env):
    web, ntfy = env
    from fakeweb import ACAST_URL

    web.bodies[ACAST_URL] = (
        b"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title></channel></rss>"
    )
    poll()
    assert "Acast feed has no audio items" in titles(ntfy)
    assert "Patreon" not in ntfy.requests[0].content.decode()
