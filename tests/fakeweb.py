"""An in-memory web for feed tests: Apple lookup, three feeds, audio hosts (httpx.MockTransport)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from ciw.feeds import HttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
ACAST_URL = "https://feeds.acast.com/public/shows/60ad324ac26dcb0019ac8e4f"
HRN_URL = "https://rss.art19.com/cooking-issues"
PATREON_TOKEN = "SUPERSECRETPATREONTOKEN123"
PATREON_URL = f"https://www.patreon.com/rss/cookingissues?auth={PATREON_TOKEN}"


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.xml").read_bytes()


class FakeWeb:
    def __init__(self):
        self.bodies = {
            ACAST_URL: fixture("acast"),
            HRN_URL: fixture("hrn"),
            PATREON_URL: fixture("patreon"),
        }
        self.lookup = {
            "1572356745": ACAST_URL,
            "380317598": HRN_URL,
        }
        self.status: dict[str, int] = {}  # url -> forced status code
        self.requests: list[httpx.Request] = []
        self.audio_status = 200
        self.head_status: int | None = None  # force a HEAD status (then GET Range is used)

    def etag(self, url: str) -> str:
        return f'"etag-{hash(self.bodies[url]) & 0xFFFF:x}"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.url.host == "itunes.apple.com":
            ident = request.url.params.get("id")
            if ident not in self.lookup:
                return httpx.Response(200, text=json.dumps({"resultCount": 0, "results": []}))
            return httpx.Response(
                200,
                text=json.dumps({"resultCount": 1, "results": [{"feedUrl": self.lookup[ident]}]}),
            )
        if url in self.bodies:
            if url in self.status:
                return httpx.Response(self.status[url])
            etag = self.etag(url)
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304)
            return httpx.Response(200, content=self.bodies[url], headers={"etag": etag})
        # audio hosts
        if request.method == "HEAD":
            if self.head_status is not None:
                return httpx.Response(self.head_status)
            return httpx.Response(
                self.audio_status, headers={"content-type": "audio/mpeg", "content-length": "1234"}
            )
        if request.method == "GET" and request.headers.get("range") == "bytes=0-0":
            return httpx.Response(
                206,
                content=b"x",
                headers={"content-type": "audio/mpeg", "content-range": "bytes 0-0/987654"},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url.host}")

    def count(self, host: str | None = None, method: str | None = None) -> int:
        return sum(
            1
            for r in self.requests
            if (host is None or r.url.host == host) and (method is None or r.method == method)
        )

    def client(self, secrets=()) -> HttpClient:
        return HttpClient(
            httpx.MockTransport(self),
            min_interval=0,
            backoff=0,
            sleep=lambda _s: None,
            secrets=secrets,
        )
