"""Downloader: streaming, resume, verification, adoption, policies. No network, tiny real mp3s."""

import logging
import os
from collections import namedtuple
from datetime import UTC, date, datetime

import httpx
import pytest
from audiofake import START_URL, AudioWeb, ffmpeg_exe, make_mp3

from ciw import download, ingest, state
from ciw.config import DownloadConfig
from ciw.models import EpisodeMeta

pytestmark = pytest.mark.skipif(ffmpeg_exe() is None, reason="ffmpeg is not installed")

EID = "a" * 24
_usage = namedtuple("usage", "total used free")


@pytest.fixture(scope="module")
def mp3(tmp_path_factory):
    return make_mp3(tmp_path_factory.mktemp("mp3") / "tone.mp3", seconds=3, freq=440)


@pytest.fixture(scope="module")
def mp3_b(tmp_path_factory):
    return make_mp3(tmp_path_factory.mktemp("mp3b") / "tone.mp3", seconds=3, freq=880)


@pytest.fixture
def cfg(tmp_cfg, tmp_path):
    return tmp_cfg.model_copy(
        update={"audio_dir": tmp_path / "audio", "download": DownloadConfig(min_free_gb=0)}
    )


def add_episode(
    cfg, eid=EID, *, duration=3.0, source="acast", audio_source="acast", pub=(2026, 1, 24), **kw
):
    meta = EpisodeMeta(
        guid=eid,
        guids={source: eid},
        source=source,
        audio_source=audio_source,
        title=f"Episode {eid[:4]}",
        pub_date=datetime(*pub, 8, tzinfo=UTC),
        duration_s=duration,
        enclosure_url=kw.pop("url", START_URL.format(id=eid)),
        enclosure_type=kw.pop("type", "audio/mpeg"),
        enclosure_length_hint=kw.pop("hint", 999999),
    )
    meta.save(ingest.meta_path(cfg, eid))
    with state.connect(cfg.state_db) as conn:
        state.upsert_episode(conn, meta)
    return meta


def run(cfg, web, eid=EID, **kw):
    with state.connect(cfg.state_db) as conn:
        with web.client(secrets=cfg.secrets.secret_values()) as client:
            res = download.download_episode(cfg, conn, eid, client=client, **kw)
        return res, state.get_episode(conn, eid)


# ------------------------------------------------------------------------- happy path


def test_full_download_verifies_and_records_state(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    res, row = run(cfg, web)
    final = cfg.audio_dir / f"{EID}.mp3"
    assert res.outcome == "downloaded", res.message
    assert final.read_bytes() == mp3
    assert not list(cfg.audio_dir.glob("*.part*"))
    assert row["stage"] == "downloaded" and row["error"] is None
    assert row["audio_path"] == str(final) and row["audio_bytes"] == len(mp3)
    assert row["audio_duration_s"] == pytest.approx(3.0, abs=0.1)
    assert row["audio_downloaded_from"] == "acast" and row["downloaded_at"]
    import hashlib

    assert row["audio_sha256"] == hashlib.sha256(mp3).hexdigest()
    # started from the stored URL, followed the redirect, sent a User-Agent, no Range on a fresh GET
    assert web.requests[0].url.host == "audio.example"
    assert all("ciw" in r.headers["user-agent"] for r in web.requests)
    assert "range" not in web.body_requests()[0].headers
    assert res.fetched == len(mp3) and res.size == len(mp3)


def test_signed_redirect_target_is_never_persisted_or_logged(cfg, mp3, caplog):
    add_episode(cfg)
    with caplog.at_level(logging.DEBUG):
        run(cfg, AudioWeb(mp3))
    with state.connect(cfg.state_db) as conn:
        dump = " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM episodes"))
    for secret in ("EXPIRING", "SECRETSIGNATURE"):
        assert secret not in dump and secret not in caplog.text


@pytest.mark.parametrize(
    ("etype", "url", "ext"),
    [
        ("audio/mpeg", "https://x/y", "mp3"),
        ("audio/x-m4a", "https://x/y", "m4a"),
        ("audio/mp4", "https://x/y", "m4a"),
        ("", "https://x/y/file.ogg?x=1", "ogg"),
        ("", "https://x/y/file", "mp3"),
        ("application/weird", "https://x/y/file.txt", "mp3"),
    ],
)
def test_extension_from_enclosure_type_then_url(cfg, etype, url, ext):
    meta = add_episode(cfg, type=etype, url=url)
    assert download.audio_extension(meta) == ext


def test_m4a_enclosure_type_names_the_file_m4a(cfg, mp3):
    add_episode(cfg, type="audio/x-m4a")
    res, _ = run(cfg, AudioWeb(mp3))  # (mp3 data in the m4a name is fine for ffprobe)
    assert res.path.name == f"{EID}.m4a" and res.outcome == "downloaded"


def test_unknown_length_skips_size_check_but_still_probes(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.chunked = True
    res, row = run(cfg, web)
    assert res.outcome == "downloaded" and "no size" in res.message
    assert row["stage"] == "downloaded"


# ----------------------------------------------------------------------------- resume


def test_resumes_from_truncated_part_with_range(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    part = cfg.audio_dir / f"{EID}.mp3.part"
    part.write_bytes(mp3[:9000])
    web = AudioWeb(mp3)
    res, _ = run(cfg, web)
    assert res.outcome == "downloaded"
    assert web.body_requests()[0].headers["range"] == "bytes=9000-"
    assert (cfg.audio_dir / f"{EID}.mp3").read_bytes() == mp3
    assert res.fetched == len(mp3) - 9000 and not part.exists()


def test_connection_reset_midway_resumes_without_losing_bytes(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.break_after = 5000
    res, _ = run(cfg, web)
    assert res.outcome == "downloaded", res.message
    assert (cfg.audio_dir / f"{EID}.mp3").read_bytes() == mp3
    ranges = [r.headers.get("range") for r in web.body_requests()]
    assert ranges == [None, "bytes=5000-"]
    assert web.sleeps == [download.BACKOFF_S]  # one backoff, then progress


def test_server_ignoring_range_restarts_clean(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    (cfg.audio_dir / f"{EID}.mp3.part").write_bytes(b"garbage-from-another-version" * 100)
    web = AudioWeb(mp3)
    web.ignore_range = True
    res, _ = run(cfg, web)
    assert res.outcome == "downloaded"
    assert (cfg.audio_dir / f"{EID}.mp3").read_bytes() == mp3  # no splice of old + new


def test_if_range_validator_mismatch_restarts_clean(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.etag = '"v2"'
    cfg.audio_dir.mkdir(parents=True)
    (cfg.audio_dir / f"{EID}.mp3.part").write_bytes(mp3[:4000])
    download._write_validators(
        cfg.audio_dir / f"{EID}.mp3.part", {"total": len(mp3), "etag": '"v1"'}
    )
    res, _ = run(cfg, web)
    assert web.body_requests()[0].headers["if-range"] == '"v1"'
    assert res.outcome == "downloaded"
    assert (cfg.audio_dir / f"{EID}.mp3").read_bytes() == mp3


def test_changed_total_on_resume_restarts_clean(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    part = cfg.audio_dir / f"{EID}.mp3.part"
    part.write_bytes(mp3[:4000])
    download._write_validators(part, {"total": len(mp3) + 77})  # stitched differently before
    res, _ = run(cfg, AudioWeb(mp3))
    assert res.outcome == "downloaded"
    assert (cfg.audio_dir / f"{EID}.mp3").read_bytes() == mp3


def test_416_when_part_is_already_complete_verifies_it(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    (cfg.audio_dir / f"{EID}.mp3.part").write_bytes(mp3)
    web = AudioWeb(mp3)
    res, _ = run(cfg, web)
    assert res.outcome == "downloaded" and res.fetched == 0
    assert web.body_requests()[0].headers["range"] == f"bytes={len(mp3)}-"
    assert (cfg.audio_dir / f"{EID}.mp3").read_bytes() == mp3


def test_416_when_part_is_oversized_restarts(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    (cfg.audio_dir / f"{EID}.mp3.part").write_bytes(mp3 + b"extra-junk" * 50)
    res, _ = run(cfg, AudioWeb(mp3))
    assert res.outcome == "downloaded"
    assert (cfg.audio_dir / f"{EID}.mp3").read_bytes() == mp3


# ------------------------------------------------------------------------- failures


def failed_cleanly(cfg, res, row, fragment):
    assert res.outcome == "failed" and fragment in res.message
    assert row["stage"] == "discovered" and fragment in row["error"]
    assert not (cfg.audio_dir / f"{EID}.mp3").exists()


def test_size_mismatch_deletes_part_and_records_error(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.declared_length = len(mp3) - 100  # server claims fewer bytes than it sends
    res, row = run(cfg, web)
    failed_cleanly(cfg, res, row, "declared")
    assert not list(cfg.audio_dir.glob("*.part*"))


def test_stuck_server_that_always_cuts_short_gives_up_and_keeps_part(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3[:3000])
    web.ignore_range = True  # every request is a fresh 200 that ends early
    web.declared_length = len(mp3)
    res, row = run(cfg, web)
    assert res.outcome == "failed" and "giving up" in res.message
    assert row["stage"] == "discovered" and row["error"]
    assert (cfg.audio_dir / f"{EID}.mp3.part").exists()  # transient: kept for a later resume
    assert not (cfg.audio_dir / f"{EID}.mp3").exists()


def test_non_audio_content_type_is_a_failure(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(b"<html>nope</html>")
    web.content_type = "text/html; charset=utf-8"
    res, row = run(cfg, web)
    failed_cleanly(cfg, res, row, "content-type")
    assert len(web.body_requests()) == 1  # no retries for a permanent problem


def test_octet_stream_is_accepted(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.content_type = "application/octet-stream"
    assert run(cfg, web)[0].outcome == "downloaded"


def test_ffprobe_duration_mismatch_fails_and_removes_part(cfg, mp3):
    add_episode(cfg, duration=900.0)  # the feed says 15 min, the file is 3 s
    res, row = run(cfg, AudioWeb(mp3))
    failed_cleanly(cfg, res, row, "duration")
    assert not list(cfg.audio_dir.glob("*.part*"))


def test_undecodable_bytes_fail_ffprobe(cfg):
    add_episode(cfg)
    res, row = run(cfg, AudioWeb(b"this is not audio at all" * 2000))
    failed_cleanly(cfg, res, row, "ffprobe")


def test_unknown_feed_duration_skips_the_comparison(cfg, mp3):
    add_episode(cfg, duration=None)
    assert run(cfg, AudioWeb(mp3))[0].outcome == "downloaded"


def test_duration_tolerance_is_max_of_120s_and_5_percent():
    download.check_duration(3700.0, 3600.0)  # 100 s < 180 s (5%)
    download.check_duration(3779.0, 3600.0)
    with pytest.raises(download.DownloadError):
        download.check_duration(3790.0, 3600.0)
    download.check_duration(1115.0, 1000.0)  # 115 s < 120 s floor
    with pytest.raises(download.DownloadError):
        download.check_duration(1125.0, 1000.0)


def test_404_is_not_retried(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.statuses = [404]
    res, row = run(cfg, web)
    failed_cleanly(cfg, res, row, "HTTP 404")
    assert web.sleeps == []


def test_retry_with_exponential_backoff_on_503_then_success(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.statuses = [503, 502]
    res, _ = run(cfg, web)
    assert res.outcome == "downloaded"
    assert web.sleeps == [2.0, 4.0]


def test_retry_after_header_is_honoured(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.statuses = [(429, {"retry-after": "7"}), (503, {"retry-after": "100000"})]
    res, _ = run(cfg, web)
    assert res.outcome == "downloaded"
    assert web.sleeps == [7.0, download.MAX_RETRY_AFTER_S]


def test_gives_up_after_max_retries_and_records_error(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    web.statuses = [503] * 10
    res, row = run(cfg, web)
    assert res.outcome == "failed" and "giving up" in res.message
    assert web.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert row["stage"] == "discovered" and "giving up" in row["error"]


def test_timeouts_and_resets_are_retried(cfg, mp3):
    add_episode(cfg)
    web = AudioWeb(mp3)
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return web(request)

    client = download.HttpClient(
        httpx.MockTransport(flaky), min_interval=0, sleep=web.sleeps.append
    )
    with state.connect(cfg.state_db) as conn:
        res = download.download_episode(cfg, conn, EID, client=client)
    assert res.outcome == "downloaded" and web.sleeps == [2.0]


def test_failure_does_not_leak_url_secrets(cfg, caplog):
    add_episode(cfg)
    web = AudioWeb(b"")

    def boom(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

    client = download.HttpClient(httpx.MockTransport(boom), min_interval=0, sleep=lambda s: None)
    with caplog.at_level(logging.DEBUG), state.connect(cfg.state_db) as conn:
        res = download.download_episode(cfg, conn, EID, client=client)
        error = state.get_episode(conn, EID)["error"]
    assert res.outcome == "failed"
    for text in (res.message, error, caplog.text):
        assert "SECRETSIGNATURE" not in text
    assert web.requests == []


def test_missing_meta_and_unknown_episode_fail_softly(cfg, mp3):
    res, _ = run(cfg, AudioWeb(mp3), eid="nope")
    assert res.outcome == "failed" and "unknown" in res.message
    add_episode(cfg)
    ingest.meta_path(cfg, EID).unlink()
    res, row = run(cfg, AudioWeb(mp3))
    assert res.outcome == "failed" and "meta.json" in res.message and row["stage"] == "discovered"


# ------------------------------------------------------------ idempotency / adoption


def test_second_run_makes_no_network_requests(cfg, mp3):
    add_episode(cfg)
    run(cfg, AudioWeb(mp3))
    web2 = AudioWeb(mp3)
    res, _ = run(cfg, web2)
    assert res.outcome == "skipped" and web2.requests == []


def test_adopts_existing_correct_file_without_downloading(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    final = cfg.audio_dir / f"{EID}.mp3"
    final.write_bytes(mp3)
    before = final.stat()
    web = AudioWeb(mp3)
    res, row = run(cfg, web)
    assert res.outcome == "adopted", res.message
    assert web.body_requests() == [] and len(web.requests) >= 1  # only the 0-0 size probe
    assert all(r.headers.get("range") == "bytes=0-0" for r in web.requests if r.method == "GET")
    after = final.stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_size) == (
        after.st_ino,
        after.st_mtime_ns,
        after.st_size,
    )
    assert row["stage"] == "downloaded" and row["audio_bytes"] == len(mp3)
    assert row["audio_path"] == str(final) and row["audio_sha256"]


def test_adoption_refuses_a_file_whose_size_differs_from_the_server(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    final = cfg.audio_dir / f"{EID}.mp3"
    final.write_bytes(mp3[:-500])  # truncated copy
    res, row = run(cfg, AudioWeb(mp3))
    assert res.outcome == "failed" and "not adopted" in res.message
    assert row["stage"] == "discovered"
    assert final.read_bytes() == mp3[:-500]  # never deleted or touched


def test_adoption_refuses_a_file_with_wrong_duration(cfg, mp3):
    add_episode(cfg, duration=1200.0)
    cfg.audio_dir.mkdir(parents=True)
    (cfg.audio_dir / f"{EID}.mp3").write_bytes(mp3)
    res, row = run(cfg, AudioWeb(mp3))
    assert res.outcome == "failed" and "duration" in res.message and row["stage"] == "discovered"


def test_other_files_in_audio_dir_are_never_touched(cfg, mp3):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    excerpt = cfg.audio_dir / "test_excerpt_60s.mp3"
    excerpt.write_bytes(b"precious")
    run(cfg, AudioWeb(mp3))
    assert excerpt.read_bytes() == b"precious"


def test_file_missing_but_state_downloaded_is_fetched_again(cfg, mp3):
    add_episode(cfg)
    run(cfg, AudioWeb(mp3))
    (cfg.audio_dir / f"{EID}.mp3").unlink()
    res, row = run(cfg, AudioWeb(mp3))
    assert res.outcome == "downloaded" and row["stage"] == "downloaded"


# ------------------------------------------------------- source switch / redownload


def test_source_switch_does_not_redownload_by_default(cfg, mp3, caplog):
    add_episode(cfg)
    run(cfg, AudioWeb(mp3))
    add_episode(cfg, audio_source="patreon")  # a Patreon copy appeared later
    web2 = AudioWeb(mp3)
    with caplog.at_level(logging.INFO):
        res, row = run(cfg, web2)
    assert res.outcome == "skipped" and web2.requests == []
    assert "keeping it" in caplog.text and "--redownload" in caplog.text
    assert row["audio_source"] == "patreon" and row["audio_downloaded_from"] == "acast"


def test_redownload_replaces_atomically_and_updates_source(cfg, mp3, mp3_b):
    add_episode(cfg)
    run(cfg, AudioWeb(mp3))
    add_episode(cfg, audio_source="patreon")
    final = cfg.audio_dir / f"{EID}.mp3"
    web = AudioWeb(mp3_b)
    seen = {}

    real_replace = os.replace

    def spy(src, dst):
        seen["old_intact_at_replace"] = final.read_bytes() == mp3
        seen["part_exists"] = os.path.exists(src)
        return real_replace(src, dst)

    import unittest.mock as mock

    with mock.patch("ciw.download.os.replace", spy):
        res, row = run(cfg, web, force=True)
    assert res.outcome == "downloaded"
    assert seen == {"old_intact_at_replace": True, "part_exists": True}
    assert final.read_bytes() == mp3_b
    assert row["audio_downloaded_from"] == "patreon" and row["stage"] == "downloaded"
    import hashlib

    assert row["audio_sha256"] == hashlib.sha256(mp3_b).hexdigest()
    assert not list(cfg.audio_dir.glob("*.part*"))


def test_failed_redownload_keeps_the_old_file_and_stage(cfg, mp3):
    add_episode(cfg)
    run(cfg, AudioWeb(mp3))
    final = cfg.audio_dir / f"{EID}.mp3"
    web = AudioWeb(b"<html/>")
    web.content_type = "text/html"
    res, row = run(cfg, web, force=True)
    assert res.outcome == "failed"
    assert final.read_bytes() == mp3
    assert row["stage"] == "downloaded" and "content-type" in row["error"]
    assert row["audio_sha256"]


def test_redownload_of_an_untracked_existing_file_replaces_it(cfg, mp3, mp3_b):
    add_episode(cfg)
    cfg.audio_dir.mkdir(parents=True)
    final = cfg.audio_dir / f"{EID}.mp3"
    final.write_bytes(mp3)
    res, _ = run(cfg, AudioWeb(mp3_b), force=True)
    assert res.outcome == "downloaded" and final.read_bytes() == mp3_b


# -------------------------------------------------------------------------- verify


def test_verify_checks_local_file_and_backfills_sha(cfg, mp3):
    add_episode(cfg)
    run(cfg, AudioWeb(mp3))
    with state.connect(cfg.state_db) as conn:
        conn.execute("UPDATE episodes SET audio_sha256 = NULL")
    web = AudioWeb(mp3)
    res, row = run(cfg, web, verify=True)
    assert res.outcome == "skipped" and "verified ok" in res.message
    assert row["audio_sha256"]
    assert web.body_requests() == []


def test_verify_detects_a_corrupted_file(cfg, mp3):
    add_episode(cfg)
    run(cfg, AudioWeb(mp3))
    final = cfg.audio_dir / f"{EID}.mp3"
    data = bytearray(mp3)
    data[100] ^= 0xFF
    final.write_bytes(bytes(data))
    res, row = run(cfg, AudioWeb(mp3), verify=True)
    assert res.outcome == "failed" and "sha256" in res.message
    assert row["stage"] == "downloaded" and row["error"]


# ------------------------------------------------------------- disk guard and lock


def test_disk_guard_min_free_gb_blocks_before_any_request(cfg, mp3):
    big = cfg.model_copy(update={"download": DownloadConfig(min_free_gb=1e9)})
    add_episode(big)
    web = AudioWeb(mp3)
    res, row = run(big, web)
    assert res.outcome == "failed" and res.fatal and "min_free_gb" in res.message
    assert web.requests == [] and row["stage"] == "discovered"


def test_disk_guard_needs_twice_the_expected_size(cfg, mp3, monkeypatch):
    add_episode(cfg, hint=600)
    free = 1000
    monkeypatch.setattr(download.shutil, "disk_usage", lambda p: _usage(10_000, 9000, free))
    web = AudioWeb(mp3)
    res, _ = run(cfg, web)
    assert res.outcome == "failed" and "2x" in res.message and web.requests == []


def test_disk_guard_rechecks_with_the_real_size_from_the_response(cfg, mp3, monkeypatch):
    add_episode(cfg, hint=10)  # the hint is wrong (tiny); the response says better
    monkeypatch.setattr(
        download.shutil,
        "disk_usage",
        lambda p: _usage(10**6, 0, len(mp3) + 5),
    )
    res, row = run(cfg, AudioWeb(mp3))
    assert res.outcome == "failed" and "2x" in res.message
    assert not (cfg.audio_dir / f"{EID}.mp3").exists() and row["stage"] == "discovered"


def test_lock_prevents_a_second_downloader(cfg):
    with download.download_lock(cfg):
        with pytest.raises(download.DownloadLocked):
            with download.download_lock(cfg):
                pass
    with download.download_lock(cfg):  # released afterwards
        pass


def test_batch_holds_the_lock_and_isolates_failures(cfg, mp3):
    add_episode(cfg, "a" * 24, pub=(2026, 1, 3))
    add_episode(cfg, "b" * 24, pub=(2026, 1, 2), duration=999.0)  # will fail the duration check
    add_episode(cfg, "c" * 24, pub=(2026, 1, 1))
    web = AudioWeb(mp3)
    with state.connect(cfg.state_db) as conn:
        rows = state.list_episodes(conn)
        seen = []
        with web.client() as client:
            summary = download.download_batch(
                cfg, conn, rows, client=client, on_result=lambda r, res: seen.append(res.outcome)
            )
    assert seen == ["downloaded", "failed", "downloaded"]
    assert (summary.count("downloaded"), summary.failed) == (2, 1)


def test_batch_stops_on_a_fatal_disk_guard(cfg, mp3):
    big = cfg.model_copy(update={"download": DownloadConfig(min_free_gb=1e9)})
    add_episode(big, "a" * 24)
    add_episode(big, "b" * 24, pub=(2026, 1, 1))
    web = AudioWeb(mp3)
    with state.connect(big.state_db) as conn:
        with web.client() as client:
            summary = download.download_batch(big, conn, state.list_episodes(conn), client=client)
    assert len(summary.results) == 1 and summary.failed == 1 and web.requests == []


def test_progress_callback_reports_bytes_and_speed(cfg, mp3, monkeypatch):
    add_episode(cfg)
    monkeypatch.setattr(download, "PROGRESS_INTERVAL_S", 0.0)
    calls = []
    run(cfg, AudioWeb(mp3), progress=lambda *a: calls.append(a))
    assert calls and calls[-1][1] <= len(mp3) and calls[-1][2] == len(mp3)
    assert all(c[0] == EID and c[3] >= 0 for c in calls)


# ------------------------------------------------------------------------ selection


@pytest.fixture
def many(cfg, mp3):
    add_episode(cfg, "a" * 24, pub=(2026, 3, 1))
    add_episode(cfg, "b" * 24, pub=(2026, 2, 1), source="acast", audio_source="patreon")
    add_episode(cfg, "c" * 24, pub=(2026, 1, 1))
    add_episode(cfg, "hrn-1", pub=(2015, 5, 1), source="hrn", audio_source="hrn")
    add_episode(cfg, "hrn-2", pub=(2014, 5, 1), source="hrn", audio_source="hrn")
    run(cfg, AudioWeb(mp3), eid="a" * 24)  # the newest is already downloaded
    return cfg


def ids(rows):
    return [r["episode_id"] for r in rows]


def test_latest_picks_the_n_newest_so_a_rerun_does_nothing_new(many):
    with state.connect(many.state_db) as conn:
        rows, unknown = download.select_episodes(conn, latest=2)
        assert ids(rows) == ["a" * 24, "b" * 24] and unknown == []  # a is downloaded: skipped later
        rows, _ = download.select_episodes(conn, latest=1, source="hrn")
        assert ids(rows) == ["hrn-1"]
        rows, _ = download.select_episodes(conn, latest=5, source="patreon")
        assert ids(rows) == ["b" * 24]  # matched episode whose audio comes from Patreon


def test_selectors_combine(many):
    with state.connect(many.state_db) as conn:
        rows, _ = download.select_episodes(conn, since=date(2026, 1, 15))
        assert ids(rows) == ["b" * 24]
        rows, _ = download.select_episodes(conn, all_=True)
        assert len(rows) == 4  # everything except the downloaded one
        rows, _ = download.select_episodes(conn, all_=True, limit=2)
        assert ids(rows) == ["b" * 24, "c" * 24]
        rows, unknown = download.select_episodes(conn, episode_ids=("a" * 24, "zzz"))
        assert ids(rows) == ["a" * 24] and unknown == ["zzz"]  # explicit ids may be downloaded
        rows, _ = download.select_episodes(conn, latest=1, since=date(2026, 1, 15))
        assert ids(rows) == ["a" * 24]
        rows, _ = download.select_episodes(conn, all_=True, include_done=True)
        assert len(rows) == 5


def test_duration_tolerance_comes_from_config(cfg, mp3):
    add_episode(cfg, duration=900.0)
    assert run(cfg, AudioWeb(mp3))[0].outcome == "failed"
    loose = cfg.model_copy(
        update={"download": DownloadConfig(min_free_gb=0, duration_tolerance_s=1000)}
    )
    assert run(loose, AudioWeb(mp3))[0].outcome == "downloaded"
