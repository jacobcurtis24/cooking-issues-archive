"""Feed lookup, fetching, parsing and identity (no network: httpx.MockTransport + fixtures)."""

import logging
from datetime import UTC, datetime

import httpx
import pytest
from fakeweb import ACAST_URL, HRN_URL, PATREON_TOKEN, PATREON_URL, FakeWeb, fixture

from ciw.feeds import (
    FeedAuthError,
    FeedEmptyError,
    FeedFetchError,
    FeedLookupError,
    HttpClient,
    episode_id_for,
    fetch_feed,
    html_to_text,
    normalize_title,
    parse_duration,
    parse_feed,
    resolve_feed_url,
)

# ------------------------------------------------------------------ Apple lookup


def test_resolve_feed_url_reads_first_result():
    web = FakeWeb()
    with web.client() as client:
        assert resolve_feed_url(client, 1572356745) == ACAST_URL
        assert resolve_feed_url(client, 380317598) == HRN_URL
    req = web.requests[0]
    assert req.url.params["id"] == "1572356745" and req.url.params["entity"] == "podcast"
    assert "ciw" in req.headers["user-agent"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text='{"resultCount":0,"results":[]}'),
        httpx.Response(200, text="not json"),
        httpx.Response(200, text='{"results":[{"collectionName":"x"}]}'),
        httpx.Response(200, text='{"results":[{"feedUrl":"ftp://nope"}]}'),
        httpx.Response(404),
    ],
)
def test_resolve_feed_url_errors_are_clear(response):
    client = HttpClient(
        httpx.MockTransport(lambda r: response), min_interval=0, backoff=0, sleep=lambda s: None
    )
    with pytest.raises(FeedLookupError, match="apple-lookup 7"):
        resolve_feed_url(client, 7)


def test_resolve_feed_url_network_failure_is_a_lookup_error():
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    client = HttpClient(
        httpx.MockTransport(boom), min_interval=0, backoff=0, retries=1, sleep=lambda s: None
    )
    with pytest.raises(FeedLookupError):
        resolve_feed_url(client, 7)


# --------------------------------------------------------------------- fetching


def test_fetch_is_conditional_and_handles_304():
    web = FakeWeb()
    with web.client() as client:
        first = fetch_feed(client, ACAST_URL, label="acast")
        assert first.status == 200 and first.body and first.etag
        again = fetch_feed(client, ACAST_URL, label="acast", etag=first.etag)
    assert again.not_modified and again.body is None
    assert web.requests[-1].headers["if-none-match"] == first.etag


def test_fetch_retries_5xx_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, content=b"<rss/>")

    sleeps = []
    client = HttpClient(
        httpx.MockTransport(handler), min_interval=0, backoff=1.0, sleep=sleeps.append
    )
    assert fetch_feed(client, "https://x.test/feed", label="x").status == 200
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff


def test_fetch_gives_up_after_retries_on_timeouts():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = HttpClient(
        httpx.MockTransport(handler), min_interval=0, backoff=0, retries=2, sleep=lambda s: None
    )
    with pytest.raises(FeedFetchError, match="giving up after 3 attempts"):
        fetch_feed(client, "https://x.test/feed", label="x")


@pytest.mark.parametrize("status", [401, 403])
def test_fetch_auth_errors_are_typed(status):
    web = FakeWeb()
    web.status[PATREON_URL] = status
    with web.client() as client, pytest.raises(FeedAuthError) as err:
        fetch_feed(client, PATREON_URL, label="patreon")
    assert err.value.status == status
    assert PATREON_TOKEN not in str(err.value)
    assert len(web.requests) == 1  # auth errors are not retried


def test_fetch_other_4xx_is_a_fetch_error_not_auth():
    web = FakeWeb()
    web.status[ACAST_URL] = 404
    with web.client() as client, pytest.raises(FeedFetchError) as err:
        fetch_feed(client, ACAST_URL, label="acast")
    assert not isinstance(err.value, FeedAuthError)


def test_requests_are_throttled(monkeypatch):
    waits = []
    web = FakeWeb()
    client = HttpClient(httpx.MockTransport(web), min_interval=0.5, sleep=waits.append)
    fetch_feed(client, ACAST_URL, label="a")
    fetch_feed(client, HRN_URL, label="b")
    assert waits and 0 < waits[-1] <= 0.5


# ----------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1:01:25", 3685.0),
        ("01:04:48", 3888.0),
        ("59:54", 3594.0),
        ("3685", 3685.0),
        ("3685.5", 3685.5),
        ("0:00:30", 30.0),
        (" 12:00 ", 720.0),
        ("", None),
        (None, None),
        ("abc", None),
        ("1:2:3:4", None),
        ("-5", None),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_acast_fixture_parses_to_audio_items():
    parsed = parse_feed(fixture("acast"), "acast")
    assert len(parsed.items) == 8 and parsed.skipped_non_audio == 0
    biscuits = next(i for i in parsed.items if i.guid == "69715223a6c658f1834fb3d6")
    assert biscuits.title == "Biscuits, Bouillon and Beyond"
    assert biscuits.pub_date == datetime(2026, 1, 24, 8, 0, tzinfo=UTC)
    assert biscuits.duration_s == 3685
    assert biscuits.enclosure_type == "audio/mpeg"
    assert biscuits.enclosure_url.endswith("/e/69715223a6c658f1834fb3d6/media.mp3")
    # unreliable hint: the feed says 147403780, the real file is 58960972 bytes
    assert biscuits.enclosure_length_hint == 147403780
    assert biscuits.link.startswith("https://shows.acast.com/")
    assert biscuits.title_norm == "biscuits bouillon and beyond"
    assert "carbonation" in biscuits.description.lower()
    assert "<" not in biscuits.description and "Hosted on Acast" not in biscuits.description
    assert "<" in biscuits.description_html


def test_hrn_fixture_parses_including_odd_guids():
    parsed = parse_feed(fixture("hrn"), "hrn")
    assert len(parsed.items) == 8
    guids = {i.guid for i in parsed.items}
    assert any(g.startswith("http://s3.amazonaws.com/") for g in guids)  # legacy guids are URLs
    assert any(len(g) == 36 for g in guids)  # ART19 UUIDs
    first = parsed.items[0]
    assert first.title == "Boris Lopez's Combat-Style Cuisine"
    assert first.pub_date == datetime(2021, 4, 30, 0, 38, 40, tzinfo=UTC)  # "-0000" offset
    assert first.duration_s == 4373
    assert all(i.enclosure_type == "audio/mpeg" for i in parsed.items)
    ep1 = parsed.items[-1]
    assert ep1.episode_number == 1 or ep1.episode_number is None or ep1.episode_number >= 0
    assert "Privacy Policy" not in first.description


def test_patreon_fixture_filters_non_audio_and_counts_skips():
    parsed = parse_feed(fixture("patreon"), "patreon")
    assert parsed.total_entries == 8
    assert parsed.skipped_non_audio == 2  # an image enclosure and a post without enclosure
    assert len(parsed.items) == 6
    assert {i.enclosure_type for i in parsed.items} == {"audio/mpeg", "audio/x-m4a"}
    assert all(i.enclosure_type.startswith("audio/") for i in parsed.items)


def test_skipped_non_audio_is_logged(caplog):
    with caplog.at_level(logging.INFO):
        parse_feed(fixture("patreon"), "patreon")
    assert "skipped 2 non-audio" in caplog.text


def test_item_without_guid_or_date_is_skipped_not_fatal():
    xml = b"""<rss version="2.0"><channel><title>t</title>
    <item><title>no date</title><guid>g1</guid>
      <enclosure url="http://a/b.mp3" type="audio/mpeg"/></item>
    <item><title>ok</title><guid>g2</guid><pubDate>Mon, 05 Oct 2026 15:00:00 GMT</pubDate>
      <enclosure url="http://a/c.mp3" type="audio/mpeg"/></item></channel></rss>"""
    parsed = parse_feed(xml, "acast")
    assert [i.guid for i in parsed.items] == ["g2"] and parsed.skipped_invalid == 1


@pytest.mark.parametrize(
    "body",
    [
        b"<rss version='2.0'><channel><title>x</title></channel></rss>",
        b"<html><body>Please log in</body></html>",
        b"",
        b"<rss version='2.0'><channel><item><title>p</title><guid>1</guid>"
        b"<enclosure url='http://a/b.jpg' type='image/jpeg'/></item></channel></rss>",
    ],
)
def test_feed_with_zero_audio_items_raises_empty(body):
    with pytest.raises(FeedEmptyError):
        parse_feed(body, "patreon")
    assert parse_feed(body, "patreon", require_items=False).items == []


def test_html_to_text_keeps_structure():
    html = "<p>Topics:</p><ul><li>One</li><li>Two &amp; three</li></ul>line<br>break&nbsp;x"
    assert html_to_text(html) == "Topics:\n\n- One\n- Two & three\nline\nbreak x"
    assert html_to_text("") == ""


@pytest.mark.parametrize(
    ("title", "norm"),
    [
        ("Biscuits, Bouillon and Beyond", "biscuits bouillon and beyond"),
        ("Cooking Issues: Biscuits, Bouillon & Beyond [Patreon]", "biscuits bouillon and beyond"),
        (
            "Dryads – Spirits of the Trees with Brian Höfling",
            "dryads spirits of the trees with brian hofling",
        ),
        ("Episode 303: Any Issues", "any issues"),
        ("Ep. 12 - Peppers!", "peppers"),
        ("NTT: The Outdoor Kitchen (Ad-Free)", "ntt the outdoor kitchen"),
        ("  Café   CRÈME  ", "cafe creme"),
        ("", ""),
    ],
)
def test_normalize_title(title, norm):
    assert normalize_title(title) == norm


# ------------------------------------------------------------------- identity


def test_episode_id_rules():
    assert episode_id_for("acast", "69715223a6c658f1834fb3d6") == "69715223a6c658f1834fb3d6"
    assert episode_id_for("acast", "51368124") == "51368124"
    assert episode_id_for("acast", "../evil/guid").startswith("acast-")
    p = episode_id_for("patreon", "134000001")
    assert p.startswith("patreon-") and len(p) == len("patreon-") + 16
    assert episode_id_for("patreon", "134000001") == p  # deterministic
    assert episode_id_for("patreon", "134000002") != p
    uuid = "5e8e7c01-c77e-4bca-968a-0ca521d72708"
    assert episode_id_for("hrn", uuid) == f"hrn-{uuid}"
    legacy = episode_id_for(
        "hrn", "http://s3.amazonaws.com/hrn/audios/909/07_27_cooking_issues.mp3"
    )
    assert legacy.startswith("hrn-") and len(legacy) == 4 + 16
    with pytest.raises(ValueError):
        episode_id_for("spotify", "x")  # type: ignore[arg-type]


def test_episode_ids_are_filesystem_safe_for_every_fixture_item():
    for name in ("acast", "hrn", "patreon"):
        for item in parse_feed(fixture(name), name).items:
            eid = episode_id_for(name, item.guid)
            assert eid and "/" not in eid and "\\" not in eid and not eid.startswith(".")
            assert len(eid) <= 64 and all(c.isalnum() or c in "-_" for c in eid)


# ------------------------------------------------------------------- redaction


def test_patreon_token_never_reaches_logs_or_errors(caplog):
    web = FakeWeb()
    web.status[PATREON_URL] = 500
    with caplog.at_level(logging.DEBUG), web.client(secrets=[PATREON_URL, PATREON_TOKEN]) as c:
        with pytest.raises(FeedFetchError) as err:
            fetch_feed(c, PATREON_URL, label="patreon")
    assert PATREON_TOKEN not in caplog.text and PATREON_TOKEN not in str(err.value)
    assert "retry" in caplog.text  # the retry warnings were emitted, and are clean


def test_httpx_request_logging_is_silenced():
    assert logging.getLogger("httpx").level >= logging.WARNING
