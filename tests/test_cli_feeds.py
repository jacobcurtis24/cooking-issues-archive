"""``ciw poll``, ``ciw feeds diff|check-audio`` and ``ciw episodes list`` via CliRunner."""

import pytest
from fakeweb import ACAST_URL, HRN_URL, PATREON_TOKEN, PATREON_URL, FakeWeb
from typer.testing import CliRunner

from ciw import ingest
from ciw.cli import app

runner = CliRunner()


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(ingest, "make_client", lambda cfg: fake.client(cfg.secrets.secret_values()))
    return fake


def row(output: str, name: str) -> list[str]:
    return next(ln.split() for ln in output.splitlines() if ln.startswith(name + " "))


def test_poll_prints_table_and_totals(tmp_cfg, web):
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 0, result.output
    assert row(result.output, "acast")[:6] == ["acast", "ok", "8", "8", "0", "0"]
    assert row(result.output, "hrn")[:6] == ["hrn", "ok", "8", "8", "0", "0"]
    assert row(result.output, "patreon")[1] == "skipped"
    assert row(result.output, "total")[1:3] == ["16", "16"]
    assert "note: patreon skipped (PATREON_FEED_URL is not set)" in result.output


def test_second_poll_reports_zero_new_and_zero_updated(tmp_cfg, web):
    runner.invoke(app, ["poll"])
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 0
    total = row(result.output, "total")
    assert total[1:5] == ["16", "0", "0", "16"]  # items, new, updated, unchanged


def test_poll_single_source(tmp_cfg, web):
    result = runner.invoke(app, ["poll", "--source", "hrn"])
    assert result.exit_code == 0
    assert row(result.output, "hrn")[3] == "8"
    assert row(result.output, "acast")[1] == "skipped"
    assert web.count(host="feeds.acast.com") == 0
    assert runner.invoke(app, ["poll", "--source", "bogus"]).exit_code != 0


def test_poll_exit_codes(tmp_cfg, web):
    web.status[ACAST_URL] = 500
    assert runner.invoke(app, ["poll"]).exit_code == 0  # partial success
    web.status[HRN_URL] = 500
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 1  # every feed failed
    assert "WARNING: acast feed: error" in result.output


@pytest.mark.parametrize("status", [401, 403])
def test_poll_warns_on_patreon_auth_and_continues(with_patreon, web, status):
    web.status[PATREON_URL] = status
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 0
    assert "WARNING: patreon feed: authorisation failed" in result.output
    assert row(result.output, "patreon")[1] == "auth"
    assert row(result.output, "acast")[3] == "8"
    assert PATREON_TOKEN not in result.output


def test_poll_with_patreon_reports_matches_and_ambiguity(with_patreon, web):
    result = runner.invoke(app, ["poll", "-v"])
    assert result.exit_code == 0, result.output
    assert "matched Acast<->Patreon: 2; ambiguous: 1" in result.output
    assert "AMBIGUOUS (left unmatched)" in result.output
    assert row(result.output, "patreon")[3] == "4"
    assert PATREON_TOKEN not in result.output and PATREON_URL not in result.output


def test_poll_warns_on_empty_patreon_feed(with_patreon, web):
    web.bodies[PATREON_URL] = b"<rss version='2.0'><channel/></rss>"
    result = runner.invoke(app, ["poll"])
    assert result.exit_code == 0
    assert "WARNING: patreon feed: no audio items" in result.output


def test_feeds_diff_with_patreon(with_patreon, web):
    result = runner.invoke(app, ["feeds", "diff"])
    out = result.output
    assert result.exit_code == 0, out
    assert "Matched pairs (2)" in out
    assert "+255" in out and "+6.9" in out  # biscuits: 3940 vs 3685 s
    assert "audio/mpeg/audio/mpeg (147403780/61000000)" in out
    assert "Patreon-only posts (4)" in out and "Patron Bonus" in out
    assert "Acast-only items inside the Patreon date range" in out
    assert "NTT: Rules are Meant to Be Broken" in out
    assert "Ambiguous (left unmatched) (1)" in out
    assert "median duration delta (patreon - acast): +128 s" in out
    assert "hint: Patreon is typically longer" in out
    assert PATREON_TOKEN not in out and "ENCLTOKEN987" not in out


def test_feeds_diff_without_patreon(tmp_cfg, web):
    result = runner.invoke(app, ["feeds", "diff"])
    assert result.exit_code == 0
    assert "Patreon feed not configured" in result.output
    assert "acast" in result.output and "2021-05-17 .. 2026-10-14" in result.output
    assert "Matched pairs" not in result.output


def test_episodes_list(tmp_cfg, web):
    assert "none" in runner.invoke(app, ["episodes", "list"]).output
    runner.invoke(app, ["poll"])
    result = runner.invoke(app, ["episodes", "list", "--source", "acast", "--limit", "3"])
    assert result.exit_code == 0
    lines = result.output.strip().splitlines()
    assert len(lines) == 4 and "NTT: The Outdoor Kitchen" in lines[0] and "1:01:09" in lines[0]
    assert "(3 shown; in state: acast 8, hrn 8)" in lines[-1]
    full = runner.invoke(app, ["episodes", "list", "--source", "acast"]).output
    assert "69715223a6c658f1834fb3d6" in full and "1:01:25" in full and "discovered" in full


def test_check_audio_command(tmp_cfg, web):
    assert runner.invoke(app, ["feeds", "check-audio"]).exit_code == 1  # nothing polled yet
    runner.invoke(app, ["poll"])
    result = runner.invoke(app, ["feeds", "check-audio", "--source", "hrn", "--sample", "2"])
    assert result.exit_code == 0, result.output
    assert "6/6 probes resolved to audio (hrn)" in result.output
    assert "rss.art19.com" in result.output and "audio/mpeg" in result.output
    assert runner.invoke(app, ["feeds", "check-audio", "--source", "nope"]).exit_code != 0
