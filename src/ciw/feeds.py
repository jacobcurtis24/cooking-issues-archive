"""Podcast feeds: Apple lookup, polite HTTP fetching, parsing into ``FeedItem``.

Three feeds feed the pipeline: the public Acast feed (show notes, fallback audio), the HRN
archive feed (2010-2021, hosted on ART19 these days) and the private Patreon feed (URL with an
auth token, from ``PATREON_FEED_URL``). Matching Acast <-> Patreon lives in ``ciw.matching``.

Logging rule: a feed URL is only ever logged through ``ciw.config.redact``; the Patreon URL is
never logged at all (it is referred to as "patreon").
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import feedparser
import httpx
from bs4 import BeautifulSoup, NavigableString

from ciw.config import redact
from ciw.models import FeedItem, Source

log = logging.getLogger(__name__)

# httpx logs every request URL at INFO; that would leak the Patreon token.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

USER_AGENT = "ciw/0.1 (personal podcast archive tool)"
TIMEOUT_S = 30.0
MIN_REQUEST_INTERVAL_S = 0.5  # <= 2 requests/second
MAX_RETRIES = 3
BACKOFF_S = 1.0
APPLE_LOOKUP = "https://itunes.apple.com/lookup"

# --------------------------------------------------------------------------- errors


class FeedError(Exception):
    """Base class for feed problems. Messages never contain an unredacted URL."""


class FeedLookupError(FeedError):
    """The Apple lookup returned nothing usable."""


class FeedFetchError(FeedError):
    """Network trouble or an unexpected HTTP status (after retries)."""


class FeedAuthError(FeedFetchError):
    """HTTP 401/403: the (Patreon) feed token is wrong, expired or the tier lapsed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class FeedEmptyError(FeedError):
    """The feed parsed, but contains zero usable audio items."""


# ------------------------------------------------------------------------ HTTP client


class HttpClient:
    """A small polite wrapper around ``httpx.Client``.

    Enforces a minimum interval between requests, a 30 s timeout, a User-Agent, and retries
    with exponential backoff for timeouts, transport errors, 429 and 5xx. ``transport`` lets
    tests inject ``httpx.MockTransport``.
    """

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        min_interval: float = MIN_REQUEST_INTERVAL_S,
        timeout: float | httpx.Timeout = TIMEOUT_S,
        retries: int = MAX_RETRIES,
        backoff: float = BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
        secrets: Iterable[str] = (),
    ):
        self.http = httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self.min_interval = min_interval
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self._last = 0.0
        self.secrets = list(secrets)

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    def safe(self, text: object) -> str:
        return redact(text, self.secrets)

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    def throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, *, label: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """GET with retries. ``label`` names the request in logs and errors (no raw URL)."""
        last = "no attempt"
        for attempt in range(self.retries + 1):
            if attempt:
                delay = self.backoff * (2 ** (attempt - 1))
                log.warning(
                    "%s: retry %d/%d in %.1fs (%s)", label, attempt, self.retries, delay, last
                )
                self._sleep(delay)
            self.throttle()
            try:
                resp = self.http.get(url, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = f"{type(exc).__name__}: {self.safe(exc)}"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    self._sleep(min(int(retry_after), 60))
                continue
            return resp
        raise FeedFetchError(f"{label}: giving up after {self.retries + 1} attempts ({last})")


# ------------------------------------------------------------------- Apple lookup


def resolve_feed_url(client: HttpClient, apple_id: int) -> str:
    """Resolve an Apple Podcasts id to its RSS feed URL (``results[0].feedUrl``)."""
    label = f"apple-lookup {apple_id}"
    try:
        resp = client.get(f"{APPLE_LOOKUP}?id={apple_id}&entity=podcast", label=label)
    except FeedFetchError as exc:
        raise FeedLookupError(str(exc)) from exc
    if resp.status_code != 200:
        raise FeedLookupError(f"{label}: HTTP {resp.status_code}")
    try:
        data = json.loads(resp.text)
        feed_url = data["results"][0]["feedUrl"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise FeedLookupError(f"{label}: no feedUrl in the lookup response") from exc
    if not isinstance(feed_url, str) or not feed_url.startswith(("http://", "https://")):
        raise FeedLookupError(f"{label}: unusable feedUrl in the lookup response")
    return feed_url


# ----------------------------------------------------------------------- fetching


@dataclass
class FetchResult:
    status: int
    body: bytes | None  # None for 304
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def fetch_feed(
    client: HttpClient,
    url: str,
    *,
    label: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """Conditional GET of a feed. Raises FeedAuthError (401/403) or FeedFetchError."""
    headers = {"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    resp = client.get(url, label=label, headers=headers)
    status = resp.status_code
    if status in (401, 403):
        raise FeedAuthError(f"{label}: HTTP {status} (not authorised)", status)
    if status == 304:
        return FetchResult(304, None, etag, last_modified)
    if status != 200:
        raise FeedFetchError(f"{label}: unexpected HTTP {status}")
    return FetchResult(
        200,
        resp.content,
        resp.headers.get("etag"),
        resp.headers.get("last-modified"),
    )


# ------------------------------------------------------------------------ parsing


def parse_duration(value: object) -> float | None:
    """Parse ``itunes:duration``: ``H:MM:SS``, ``MM:SS`` or plain seconds. None if unusable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


_BLOCK_TAGS = ("p", "div", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr")
_BOILERPLATE = re.compile(
    r"^(hosted on acast\b|see privacy policy at https?://art19\.com|see .*acast\.com/privacy)",
    re.IGNORECASE,
)


def html_to_text(html: str) -> str:
    """HTML to plain text: block tags and <br> become newlines, list items get ``- ``."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for li in soup.find_all("li"):
        li.insert(0, NavigableString("- "))
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before(NavigableString("\n"))
        if tag.name != "li":  # items stack tightly; the enclosing ul/ol closes the block
            tag.insert_after(NavigableString("\n"))
    text = soup.get_text().replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if not _BOILERPLATE.match(ln)]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


_PREFIXES = [
    re.compile(r"^cooking issues( with dave arnold)?\s*[:\-–—|]\s*"),
    re.compile(r"^(episode|ep\.?|#)\s*\d+\s*[:\-–—.|]\s*"),
    re.compile(
        r"^[\[(]?\s*(patreon|patrons? only)( exclusive| early access)?\s*[\])]?\s*[:\-–—]\s*"
    ),
]
_SUFFIXES = [
    re.compile(
        r"\s*[(\[]\s*(patreon|ad[- ]?free|early access|full( episode)?|premium)\s*[)\]]\s*$"
    ),
    re.compile(r"\s*[\-–—|:]\s*(cooking issues( with dave arnold)?|patreon)\s*$"),
]


def normalize_title(title: str) -> str:
    """Casefold, strip diacritics/punctuation, drop common prefixes/suffixes, single-space."""
    s = unicodedata.normalize("NFKD", title or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold().strip()
    s = s.replace("&", " and ")
    for _ in range(2):  # a suffix can expose a prefix and vice versa
        for pat in _PREFIXES:
            s = pat.sub("", s)
        for pat in _SUFFIXES:
            s = pat.sub("", s)
    s = re.sub(r"[^\w]+|_", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class ParsedFeed:
    source: Source
    items: list[FeedItem] = field(default_factory=list)
    total_entries: int = 0
    skipped_non_audio: int = 0
    skipped_invalid: int = 0  # audio items without a guid or a publish date


def _audio_enclosure(entry: feedparser.FeedParserDict) -> dict | None:
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).strip().casefold().startswith("audio/") and enc.get("href"):
            return enc
    return None


def _to_int(value: object) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def parse_feed(body: bytes | str, source: Source, *, require_items: bool = True) -> ParsedFeed:
    """Parse feed XML into ``FeedItem`` s (audio/* enclosures only).

    Raises ``FeedEmptyError`` if nothing usable remains (a login page, an emptied feed, a feed of
    only non-audio posts) unless ``require_items`` is false.
    """
    parsed = feedparser.parse(body)
    out = ParsedFeed(source=source, total_entries=len(parsed.entries))
    for entry in parsed.entries:
        enc = _audio_enclosure(entry)
        if enc is None:
            out.skipped_non_audio += 1
            continue
        guid = (entry.get("id") or "").strip()
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not guid or not stamp:
            out.skipped_invalid += 1
            continue
        title = (entry.get("title") or "").strip()
        candidates = [c.get("value", "") for c in entry.get("content", []) or []]
        candidates.append(entry.get("summary", "") or "")
        html = max(candidates, key=len).strip()
        out.items.append(
            FeedItem(
                source=source,
                guid=guid,
                title=title,
                title_norm=normalize_title(title),
                description=html_to_text(html),
                description_html=html,
                pub_date=datetime.fromtimestamp(calendar.timegm(stamp), UTC),
                duration_s=parse_duration(entry.get("itunes_duration")),
                enclosure_url=enc["href"],
                enclosure_type=str(enc.get("type", "")).strip(),
                enclosure_length_hint=_to_int(enc.get("length")),
                link=entry.get("link") or None,
                episode_number=_to_int(entry.get("itunes_episode")),
            )
        )
    if out.skipped_non_audio:
        log.info("%s: skipped %d non-audio item(s)", source, out.skipped_non_audio)
    if out.skipped_invalid:
        log.warning("%s: skipped %d item(s) without guid or date", source, out.skipped_invalid)
    if require_items and not out.items:
        raise FeedEmptyError(
            f"{source}: feed parsed to zero audio items "
            f"({out.total_entries} entries, {out.skipped_non_audio} non-audio)"
        )
    return out


# --------------------------------------------------------------------- identity

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX24 = re.compile(r"^[0-9a-f]{24}$")


def _sha1_16(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def episode_id_for(source: Source, guid: str) -> str:
    """Stable, filesystem-safe episode_id for an item that identifies by ``guid``.

    * acast:   the guid itself (24 hex), else ``acast-<sha1[:16]>`` if it is not path-safe
    * patreon: ``patreon-<sha1(guid)[:16]>`` (Patreon guids can be URLs)
    * hrn:     ``hrn-<guid>`` when the guid is a plain token (the ART19 UUIDs), else
               ``hrn-<sha1(guid)[:16]>`` (the legacy http://... guids)
    """
    if source == "acast":
        if _HEX24.match(guid) or _SAFE_ID.match(guid):
            return guid
        return f"acast-{_sha1_16(guid)}"
    if source == "patreon":
        return f"patreon-{_sha1_16(guid)}"
    if source == "hrn":
        if _SAFE_ID.match(guid):
            return f"hrn-{guid}"
        return f"hrn-{_sha1_16(guid)}"
    raise ValueError(f"unknown source {source!r}")
