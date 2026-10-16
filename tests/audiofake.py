"""Helpers for downloader tests: tiny real mp3s (made with ffmpeg) and an in-memory audio host."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx

from ciw.feeds import HttpClient

START_URL = "https://audio.example/ep/{id}.mp3?auth=SECRETSIGNATURE"
CDN_HOST = "cdn.example"


def ffmpeg_exe() -> str | None:
    beside = Path(sys.executable).parent / "ffmpeg"
    return str(beside) if beside.is_file() else shutil.which("ffmpeg")


def make_mp3(path: Path, seconds: int = 3, freq: int = 440) -> bytes:
    exe = ffmpeg_exe()
    subprocess.run(
        [exe, "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}"]
        + ["-c:a", "libmp3lame", "-b:a", "64k", "-y", str(path)],
        check=True,
    )
    return path.read_bytes()


class _BrokenStream(httpx.SyncByteStream):
    """Yields ``head`` then raises a read error (a connection reset mid-body)."""

    def __init__(self, head: bytes):
        self.head = head

    def __iter__(self) -> Iterator[bytes]:
        yield self.head
        raise httpx.ReadError("connection reset by peer")


class _Chunked(httpx.SyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    def __iter__(self) -> Iterator[bytes]:
        for i in range(0, len(self.body), 4096):
            yield self.body[i : i + 4096]


class AudioWeb:
    """``audio.example`` redirects to ``cdn.example`` (like Acast's signed URLs) which serves
    ``body`` with Range support. Knobs let tests misbehave."""

    def __init__(self, body: bytes):
        self.body = body
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.ignore_range = False
        self.content_type = "audio/mpeg"
        self.etag: str | None = None
        self.statuses: list[int | tuple[int, dict]] = []  # forced answers, consumed first
        self.declared_length: int | None = None  # lie about Content-Length (full 200s only)
        self.chunked = False  # no Content-Length at all
        self.break_after: int | None = None  # first body response dies after this many bytes
        self.range_total_lie: int | None = None  # claim a different total in Content-Range

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "audio.example":
            target = f"https://{CDN_HOST}{request.url.path}?sig=EXPIRING"
            return httpx.Response(302, headers={"location": target})
        assert request.url.host == CDN_HOST, request.url
        if self.statuses:
            forced = self.statuses.pop(0)
            if isinstance(forced, tuple):
                return httpx.Response(forced[0], headers=forced[1])
            return httpx.Response(forced)
        body = self.body
        total = len(body)
        hdrs = {"content-type": self.content_type}
        if self.etag:
            hdrs["etag"] = self.etag
        if request.method == "HEAD":
            return httpx.Response(200, headers=hdrs)
        rng = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if rng and self.ignore_range:
            rng = None
        if rng and if_range and if_range != self.etag:
            rng = None
        if rng:
            spec = rng.removeprefix("bytes=")
            a, _, b = spec.partition("-")
            start = int(a)
            end = int(b) if b else total - 1
            if start >= total:
                return httpx.Response(416, headers={"content-range": f"bytes */{total}"})
            end = min(end, total - 1)
            chunk = body[start : end + 1]
            claimed = self.range_total_lie or total
            hdrs["content-range"] = f"bytes {start}-{end}/{claimed}"
            if self.break_after is not None:
                head, self.break_after = chunk[: self.break_after], None
                hdrs["content-length"] = str(len(chunk))
                return httpx.Response(206, headers=hdrs, stream=_BrokenStream(head))
            hdrs["content-length"] = str(len(chunk))
            return httpx.Response(206, headers=hdrs, stream=_Chunked(chunk))
        if self.break_after is not None:
            head, self.break_after = body[: self.break_after], None
            hdrs["content-length"] = str(total)
            return httpx.Response(200, headers=hdrs, stream=_BrokenStream(head))
        if self.chunked:
            return httpx.Response(200, headers=hdrs, stream=_Chunked(body))
        hdrs["content-length"] = str(
            self.declared_length if self.declared_length is not None else total
        )
        return httpx.Response(200, headers=hdrs, stream=_Chunked(body))

    def body_requests(self) -> list[httpx.Request]:
        """Requests that could have fetched audio bytes (anything but the 0-0 size probes)."""
        return [
            r
            for r in self.requests
            if r.url.host == CDN_HOST
            and r.method == "GET"
            and r.headers.get("range") != "bytes=0-0"
        ]

    def client(self, secrets=()) -> HttpClient:
        return HttpClient(
            httpx.MockTransport(self),
            min_interval=0,
            sleep=self.sleeps.append,
            secrets=secrets,
        )
