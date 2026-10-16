"""Polling orchestration: fetch the feeds, merge Acast <-> Patreon, store episodes.

``poll`` is idempotent: the merged ``EpisodeMeta`` is compared with ``meta.json`` on disk and with
the state row, and nothing is rewritten when nothing changed. Raw feed bodies are cached under
``data/feeds/<name>.xml`` (mode 600) so a ``304 Not Modified`` (or a feed that was not requested
this run) can still take part in matching.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ciw import state
from ciw.config import Config
from ciw.feeds import (
    FeedAuthError,
    FeedEmptyError,
    FeedError,
    FeedLookupError,
    HttpClient,
    ParsedFeed,
    episode_id_for,
    fetch_feed,
    parse_feed,
    resolve_feed_url,
)
from ciw.matching import Ambiguity, MatchResult, match_feeds, read_pins
from ciw.models import EpisodeMeta, FeedItem, Source

log = logging.getLogger(__name__)

SOURCES: tuple[Source, ...] = ("acast", "hrn", "patreon")
_ORDER = {"acast": 0, "hrn": 1, "patreon": 2}


def make_client(cfg: Config) -> HttpClient:
    """The HTTP client used by the CLI (tests monkeypatch this to inject a MockTransport)."""
    return HttpClient(secrets=cfg.secrets.secret_values())


# ------------------------------------------------------------------------- paths


def episode_dir(cfg: Config, episode_id: str) -> Path:
    return cfg.episodes_dir / episode_id


def meta_path(cfg: Config, episode_id: str) -> Path:
    return episode_dir(cfg, episode_id) / "meta.json"


def cache_path(cfg: Config, name: str) -> Path:
    return cfg.data_dir / "feeds" / f"{name}.xml"


def _write_cache(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
    tmp.replace(path)


# ------------------------------------------------------------------------ reports


@dataclass
class FeedReport:
    name: str
    status: str = "pending"  # ok | not-modified | cached | skipped | auth | empty | error
    items: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_non_audio: int = 0
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in ("auth", "empty", "error")


@dataclass
class PollReport:
    feeds: dict[str, FeedReport] = field(default_factory=dict)
    ambiguous: list[Ambiguity] = field(default_factory=list)
    matched: int = 0
    absorbed: int = 0
    conflicts: int = 0

    @property
    def attempted(self) -> list[FeedReport]:
        return [r for r in self.feeds.values() if r.status not in ("skipped", "cached")]

    @property
    def all_failed(self) -> bool:
        att = self.attempted
        return bool(att) and all(r.failed for r in att)


# ----------------------------------------------------------------------- fetching


def _label(name: str, url: str, client: HttpClient) -> str:
    """How a feed is named in logs: the Patreon URL (it holds a token) is never shown."""
    return "patreon" if name == "patreon" else f"{name} {client.safe(url)}"


def _load_cached(cfg: Config, name: Source) -> ParsedFeed | None:
    path = cache_path(cfg, name)
    if not path.is_file():
        return None
    try:
        return parse_feed(path.read_bytes(), name)
    except FeedError as exc:
        log.warning("%s: cached feed unusable (%s)", name, exc)
        return None


def acquire(
    cfg: Config,
    conn: sqlite3.Connection,
    client: HttpClient,
    name: Source,
    report: FeedReport,
    *,
    record: bool = True,
) -> ParsedFeed | None:
    """Resolve, fetch and parse one feed; fill ``report``; None if it yielded nothing.

    ``record=False`` leaves the state tables and cache untouched (used by ``feeds diff``).
    """
    now = state.now_iso()
    apple_id = None if name == "patreon" else getattr(cfg.apple_ids, name)

    def note(**fields: object) -> None:
        if record:
            state.save_feed(conn, name, last_checked=now, **fields)

    if name == "patreon":
        secret = cfg.secrets.patreon_feed_url
        if secret is None:
            report.status = "skipped"
            report.error = "PATREON_FEED_URL is not set"
            log.info("patreon: no PATREON_FEED_URL configured, skipping the Patreon feed")
            return None
        url = secret.get_secret_value()
    else:
        saved = state.get_feed(conn, name)
        try:
            url = resolve_feed_url(client, apple_id)
        except FeedLookupError as exc:
            if saved is None or not saved["feed_url"]:
                report.status, report.error = "error", str(exc)
                note(apple_id=apple_id, last_status=f"error: {exc}")
                return None
            url = saved["feed_url"]
            log.warning("%s: Apple lookup failed (%s); using last known feed URL", name, exc)
        if record:
            state.save_feed(conn, name, apple_id=apple_id, feed_url=url)

    saved = state.get_feed(conn, name)
    cache = cache_path(cfg, name)
    etag = saved["etag"] if saved and cache.is_file() else None
    last_mod = saved["last_modified"] if saved and cache.is_file() else None
    label = _label(name, url, client)
    try:
        result = fetch_feed(client, url, label=label, etag=etag, last_modified=last_mod)
        if result.not_modified:
            parsed = _load_cached(cfg, name)
            if parsed is None:  # cache vanished or broke: refetch unconditionally
                result = fetch_feed(client, url, label=label)
        if not result.not_modified:
            assert result.body is not None
            parsed = parse_feed(result.body, name)
            if record:
                _write_cache(cache, result.body)
    except FeedAuthError as exc:
        report.status, report.error = "auth", str(exc)
        note(last_status=f"auth_error:{exc.status}")
        return None
    except FeedEmptyError as exc:
        report.status, report.error = "empty", str(exc)
        note(last_status="empty")
        return None
    except FeedError as exc:
        report.status, report.error = "error", str(exc)
        note(last_status=f"error: {exc}")
        return None

    assert parsed is not None
    report.status = "not-modified" if result.not_modified else "ok"
    report.items = len(parsed.items)
    report.skipped_non_audio = parsed.skipped_non_audio
    note(
        last_status=str(result.status),
        item_count=len(parsed.items),
        **(
            {}
            if result.not_modified
            else {"etag": result.etag, "last_modified": result.last_modified}
        ),
    )
    return parsed


# ------------------------------------------------------------------- meta building


def _read_meta(cfg: Config, episode_id: str) -> EpisodeMeta | None:
    path = meta_path(cfg, episode_id)
    if not path.is_file():
        return None
    try:
        return EpisodeMeta.load(path)
    except ValueError as exc:
        log.warning("%s: unreadable meta.json (%s); it will be rewritten", episode_id, exc)
        return None


def build_meta(
    episode_id: str, items: dict[str, FeedItem], existing: EpisodeMeta | None = None
) -> EpisodeMeta:
    """Merge per-feed items into one ``EpisodeMeta``.

    Identity, title and show notes come from Acast when present; the audio (url, type,
    duration) comes from Patreon when present, else the identity item.
    """
    ordered = dict(sorted(items.items(), key=lambda kv: _ORDER[kv[0]]))
    identity_src: Source = (
        "acast" if "acast" in ordered else "patreon" if "patreon" in ordered else "hrn"
    )
    audio_src: Source = "patreon" if "patreon" in ordered else identity_src
    notes, audio = ordered[identity_src], ordered[audio_src]
    return EpisodeMeta(
        guid=episode_id,
        guids={s: i.guid for s, i in ordered.items()},
        source=identity_src,
        audio_source=audio_src,
        title=notes.title,
        title_norm=notes.title_norm,
        description=notes.description,
        description_html=notes.description_html,
        pub_date=notes.pub_date,
        duration_s=audio.duration_s,
        enclosure_url=audio.enclosure_url,
        enclosure_type=audio.enclosure_type,
        enclosure_length_hint=audio.enclosure_length_hint,
        link=notes.link,
        episode_number=notes.episode_number,
        audio_file=existing.audio_file if existing else "audio.mp3",
        items=ordered,
    )


def _remove_absorbed(cfg: Config, conn: sqlite3.Connection, episode_id: str) -> None:
    state.delete_episode(conn, episode_id)
    folder = episode_dir(cfg, episode_id)
    if folder.is_dir() and {p.name for p in folder.iterdir()} <= {"meta.json"}:
        shutil.rmtree(folder)


def store_unit(
    cfg: Config, conn: sqlite3.Connection, unit: dict[str, FeedItem], report: PollReport
) -> tuple[str, EpisodeMeta] | None:
    """Create or update the episode for one unit (1-2 matched items, or a single item).

    The episode_id is stable once assigned: an existing row that owns any of the unit's guids
    keeps its id (so a Patreon-only episode that later gains its Acast twin is not renamed).
    Returns ``(outcome, meta)``, or None if the unit conflicts with an advanced duplicate.
    """
    guids = [(s, i.guid) for s, i in unit.items()]
    ids = state.find_episode_ids(conn, guids)
    first_src: Source = "acast" if "acast" in unit else "patreon" if "patreon" in unit else "hrn"
    if not ids:
        episode_id = episode_id_for(first_src, unit[first_src].guid)
    else:
        episode_id = unit["acast"].guid if "acast" in unit and unit["acast"].guid in ids else ids[0]
        for other in (i for i in ids if i != episode_id):
            row = state.get_episode(conn, other)
            if row is not None and row["stage"] != "discovered":
                log.warning(
                    "%s and %s are one episode, but %s has progressed; leaving both",
                    episode_id,
                    other,
                    other,
                )
                report.conflicts += 1
                return None
            log.info("merging duplicate episode %s into %s", other, episode_id)
            _remove_absorbed(cfg, conn, other)
            report.absorbed += 1

    existing = _read_meta(cfg, episode_id)
    items: dict[str, FeedItem] = dict(existing.items) if existing else {}
    items.update(unit)
    meta = build_meta(episode_id, items, existing)

    outcome = state.upsert_episode(conn, meta)
    on_disk = existing.model_dump(mode="json") if existing else None
    if on_disk != meta.model_dump(mode="json"):
        meta.save(meta_path(cfg, episode_id))
        if outcome == "unchanged":
            outcome = "updated"
    return outcome, meta


# -------------------------------------------------------------------------- poll


def poll(
    cfg: Config,
    client: HttpClient,
    sources: tuple[Source, ...] = SOURCES,
    *,
    conn: sqlite3.Connection | None = None,
) -> PollReport:
    """Poll the requested feeds and store what they contain."""
    if conn is None:
        with state.connect(cfg.state_db) as own:
            return poll(cfg, client, sources, conn=own)
    report = PollReport()
    parsed: dict[str, ParsedFeed] = {}
    for name in SOURCES:
        rep = report.feeds[name] = FeedReport(name)
        if name in sources:
            feed = acquire(cfg, conn, client, name, rep)
        else:
            rep.status = "skipped"
            feed = (
                _load_cached(cfg, name)
                if name != "patreon" or cfg.secrets.patreon_feed_url
                else None
            )
            if feed is not None:
                rep.status, rep.items = "cached", len(feed.items)
        if feed is not None:
            parsed[name] = feed

    acast = parsed["acast"].items if "acast" in parsed else []
    patreon = parsed["patreon"].items if "patreon" in parsed else []
    units: list[dict[str, FeedItem]] = []
    if "acast" in parsed or "patreon" in parsed:
        pins = read_pins(cfg.pins_file)
        match = match_feeds(acast, patreon, state.locked_pairs(conn) + pins)
        if "acast" in parsed and "patreon" in parsed:
            paired = {(a.guid, p.guid) for a, p in match.pairs}
            for pin in pins:
                if pin not in paired:
                    log.warning(
                        "pinned pair %s / %s was not applied (guid not in the feeds?)", *pin
                    )
        report.matched = len(match.pairs)
        report.ambiguous = match.ambiguous
        for amb in match.ambiguous:
            log.warning("ambiguous Acast/Patreon match, left unmatched: %s", amb.describe())
        units += [{"acast": a, "patreon": p} for a, p in match.pairs]
        units += [{"acast": a} for a in match.acast_only]
        units += [{"patreon": p} for p in match.patreon_only]
    if "hrn" in parsed:
        units += [{"hrn": i} for i in parsed["hrn"].items]

    for unit in units:
        stored = store_unit(cfg, conn, unit, report)
        if stored is None:
            continue
        outcome, meta = stored
        rep = report.feeds[meta.source]
        setattr(rep, outcome, getattr(rep, outcome) + 1)
    return report


# ---------------------------------------------------------------------------- diff


@dataclass
class PairDiff:
    acast: FeedItem
    patreon: FeedItem

    @property
    def delta_s(self) -> float | None:
        if self.acast.duration_s is None or self.patreon.duration_s is None:
            return None
        return self.patreon.duration_s - self.acast.duration_s

    @property
    def delta_pct(self) -> float | None:
        d = self.delta_s
        if d is None or not self.acast.duration_s:
            return None
        return 100.0 * d / self.acast.duration_s


@dataclass
class DiffReport:
    patreon_configured: bool
    patreon_error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    ranges: dict[str, tuple[str, str]] = field(default_factory=dict)
    pairs: list[PairDiff] = field(default_factory=list)
    patreon_only: list[FeedItem] = field(default_factory=list)
    acast_only_in_range: list[FeedItem] = field(default_factory=list)
    ambiguous: list[Ambiguity] = field(default_factory=list)
    skipped_non_audio: int = 0

    @property
    def median_delta_s(self) -> float | None:
        vals = [p.delta_s for p in self.pairs if p.delta_s is not None]
        return statistics.median(vals) if vals else None

    @property
    def median_delta_pct(self) -> float | None:
        vals = [p.delta_pct for p in self.pairs if p.delta_pct is not None]
        return statistics.median(vals) if vals else None

    def hint(self) -> str:
        pct = self.median_delta_pct
        if pct is None:
            return "no matched pairs with durations, so no ad-free hint"
        longer = sum(1 for p in self.pairs if (p.delta_s or 0) > 120)
        shorter = sum(1 for p in self.pairs if (p.delta_s or 0) < -120)
        kind = (
            "Patreon is typically longer (consistent with fuller or ad-free cuts)"
            if pct > 2
            else "Patreon is typically shorter (consistent with ads/segments trimmed)"
            if pct < -2
            else "Patreon durations are about the same as Acast"
        )
        return (
            f"{kind}: median {pct:+.1f}%; {longer} longer and {shorter} shorter by more than 2 min"
        )


def _range(items: list[FeedItem]) -> tuple[str, str] | None:
    if not items:
        return None
    dates = sorted(i.pub_date for i in items)
    return dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")


def diff_feeds(cfg: Config, client: HttpClient) -> DiffReport:
    """Fetch (conditionally, without touching state) and compare the feeds."""
    with state.connect(cfg.state_db) as conn:
        reports = {n: FeedReport(n) for n in SOURCES}
        feeds = {
            n: acquire(cfg, conn, client, n, reports[n], record=False)
            for n in ("acast", "hrn", "patreon")
        }
        locked = state.locked_pairs(conn) + read_pins(cfg.pins_file)
    out = DiffReport(
        patreon_configured=cfg.secrets.patreon_feed_url is not None,
        patreon_error=reports["patreon"].error if reports["patreon"].failed else None,
    )
    for name, feed in feeds.items():
        if feed is not None:
            out.counts[name] = len(feed.items)
            rng = _range(feed.items)
            if rng:
                out.ranges[name] = rng
            out.skipped_non_audio += feed.skipped_non_audio
    acast, patreon = feeds["acast"], feeds["patreon"]
    if acast is None or patreon is None:
        return out
    match: MatchResult = match_feeds(acast.items, patreon.items, locked)
    out.pairs = sorted((PairDiff(a, p) for a, p in match.pairs), key=lambda d: d.acast.pub_date)
    out.patreon_only = sorted(match.patreon_only, key=lambda i: i.pub_date)
    out.ambiguous = match.ambiguous
    lo, hi = min(i.pub_date for i in patreon.items), max(i.pub_date for i in patreon.items)
    out.acast_only_in_range = sorted(
        (i for i in match.acast_only if lo <= i.pub_date <= hi), key=lambda i: i.pub_date
    )
    return out


# --------------------------------------------------------------------- audio check


@dataclass
class ProbeResult:
    method: str
    status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    final_host: str | None = None
    redirects: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        """2xx and an audio-looking content type (some hosts answer HEAD with a text/plain stub)."""
        return self.status is not None and 200 <= self.status < 300 and _audioish(self.content_type)


def _audioish(content_type: str | None) -> bool:
    ct = (content_type or "").split(";")[0].strip().casefold()
    return ct.startswith("audio/") or ct in ("application/octet-stream", "binary/octet-stream")


def _length(resp: httpx.Response) -> int | None:
    cr = resp.headers.get("content-range", "")
    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
        return int(cr.rsplit("/", 1)[1])
    cl = resp.headers.get("content-length", "")
    return int(cl) if cl.isdigit() else None


def _result(method: str, resp: httpx.Response) -> ProbeResult:
    return ProbeResult(
        method=method,
        status=resp.status_code,
        content_type=resp.headers.get("content-type"),
        content_length=_length(resp),
        final_host=resp.url.host,
        redirects=len(resp.history),
    )


def probe_audio(client: HttpClient, url: str, *, range_only: bool = False) -> ProbeResult:
    """One HEAD request (redirects followed); on error/4xx/5xx fall back to a one-byte Range GET.
    Never reads a body. Only the final host is reported, never the URL."""
    if not range_only:
        client.throttle()
        try:
            resp = client.http.head(url)
            probe = _result("HEAD", resp)
            if probe.ok:
                return probe
        except httpx.HTTPError:
            pass
    client.throttle()
    try:
        with client.http.stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
            return _result("GET Range", resp)  # leaving the block closes it unread
    except httpx.HTTPError as exc:
        return ProbeResult(method="GET Range", error=f"{type(exc).__name__}: {client.safe(exc)}")


def pick_sample(rows: list[sqlite3.Row], n: int) -> list[tuple[str, sqlite3.Row]]:
    """``n`` newest, ``n`` oldest and ``n`` from the middle of ``rows`` (newest-first order)."""
    total = len(rows)
    chosen: dict[int, str] = {}
    for i in range(min(n, total)):
        chosen.setdefault(i, "newest")
    start = max(0, total // 2 - n // 2)
    for i in range(start, min(total, start + n)):
        chosen.setdefault(i, "middle")
    for i in range(max(0, total - n), total):
        chosen.setdefault(i, "oldest")
    return [(chosen[i], rows[i]) for i in sorted(chosen)]


def check_audio(
    cfg: Config, client: HttpClient, source: Source, sample: int = 3, *, range_only: bool = False
) -> list[tuple[str, str, str, ProbeResult]]:
    """Probe sampled enclosures of ``source``. Rows: (bucket, episode_id, title, result)."""
    with state.connect(cfg.state_db) as conn:
        rows = state.episodes_with_guid_source(conn, source)
    out = []
    for bucket, row in pick_sample(rows, sample):
        meta = _read_meta(cfg, row["episode_id"])
        item = meta.items.get(source) if meta else None
        if item is None:
            continue
        out.append(
            (
                bucket,
                row["episode_id"],
                row["title"],
                probe_audio(client, item.enclosure_url, range_only=range_only),
            )
        )
    return out
