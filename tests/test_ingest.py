"""poll / diff / check-audio against the fake web (no network)."""

import logging
import stat

import httpx
import pytest
from fakeweb import ACAST_URL, HRN_URL, PATREON_TOKEN, PATREON_URL, FakeWeb

from ciw import ingest, state
from ciw.models import EpisodeMeta

BISCUITS = "69715223a6c658f1834fb3d6"
DRYADS = "6acfbad2253c13a8980d51bd"


def run_poll(cfg, web, sources=ingest.SOURCES):
    with web.client(secrets=cfg.secrets.secret_values()) as client:
        return ingest.poll(cfg, client, sources)


def load_meta(cfg, episode_id):
    return EpisodeMeta.load(ingest.meta_path(cfg, episode_id))


def snapshot(cfg):
    """(mtime_ns of every meta.json, episodes table dump) - to prove a poll changed nothing."""
    mtimes = {p.parent.name: p.stat().st_mtime_ns for p in cfg.episodes_dir.glob("*/meta.json")}
    with state.connect(cfg.state_db) as conn:
        rows = [tuple(r) for r in conn.execute("SELECT * FROM episodes ORDER BY episode_id")]
    return mtimes, rows


# -------------------------------------------------------------- public feeds only


def test_first_poll_stores_acast_and_hrn_and_skips_patreon(tmp_cfg, caplog):
    web = FakeWeb()
    with caplog.at_level(logging.INFO):
        rep = run_poll(tmp_cfg, web)
    assert rep.feeds["acast"].new == 8 and rep.feeds["hrn"].new == 8
    assert rep.feeds["patreon"].status == "skipped"
    assert "no PATREON_FEED_URL" in caplog.text
    assert not rep.all_failed
    assert len(list(tmp_cfg.episodes_dir.glob("*/meta.json"))) == 16

    meta = load_meta(tmp_cfg, BISCUITS)
    assert meta.guid == BISCUITS and meta.source == "acast" and meta.audio_source == "acast"
    assert meta.guids == {"acast": BISCUITS}
    assert meta.duration_s == 3685
    assert meta.enclosure_length_hint == 147403780
    with state.connect(tmp_cfg.state_db) as conn:
        row = state.get_episode(conn, BISCUITS)
        assert row["stage"] == "discovered" and row["duration_s"] == 3685
        assert row["pub_date"] == "2026-01-24T08:00:00Z"
        feed = state.get_feed(conn, "acast")
        assert feed["apple_id"] == 1572356745 and feed["feed_url"] == ACAST_URL
        assert feed["etag"] and feed["item_count"] == 8 and feed["last_status"] == "200"
        assert (
            state.get_feed(conn, "patreon") is None
            or state.get_feed(conn, "patreon")["feed_url"] is None
        )


def test_hrn_ids_and_folder_names(tmp_cfg):
    run_poll(tmp_cfg, FakeWeb(), ("hrn",))
    names = {p.name for p in tmp_cfg.episodes_dir.iterdir()}
    assert "hrn-5e8e7c01-c77e-4bca-968a-0ca521d72708" in names
    legacy = [n for n in names if n.startswith("hrn-") and len(n) == 20]
    assert len(legacy) == 4  # the four http://s3.amazonaws.com/... guids are hashed
    assert all(ch.isalnum() or ch == "-" for n in names for ch in n)


def test_second_poll_is_a_no_op(tmp_cfg):
    web = FakeWeb()
    run_poll(tmp_cfg, web)
    before = snapshot(tmp_cfg)
    web.requests.clear()
    rep = run_poll(tmp_cfg, web)
    assert all(r.new == 0 and r.updated == 0 for r in rep.feeds.values())
    assert rep.feeds["acast"].unchanged == 8 and rep.feeds["hrn"].unchanged == 8
    assert rep.feeds["acast"].status == "not-modified"  # conditional GET answered 304
    assert snapshot(tmp_cfg) == before  # same mtimes, same rows incl. updated_at


def test_second_poll_without_validators_still_does_not_rewrite(tmp_cfg):
    """Even a full 200 re-download must not touch meta.json when content is identical."""
    web = FakeWeb()
    run_poll(tmp_cfg, web)
    before = snapshot(tmp_cfg)
    with state.connect(tmp_cfg.state_db) as conn:
        conn.execute("UPDATE feeds SET etag = NULL, last_modified = NULL")
    rep = run_poll(tmp_cfg, web)
    assert rep.feeds["acast"].status == "ok"
    assert rep.feeds["acast"].updated == 0 and rep.feeds["acast"].unchanged == 8
    assert snapshot(tmp_cfg) == before


def test_changed_feed_content_updates_only_that_episode(tmp_cfg):
    web = FakeWeb()
    run_poll(tmp_cfg, web)
    before = snapshot(tmp_cfg)
    web.bodies[ACAST_URL] = web.bodies[ACAST_URL].replace(
        b"<title>Biscuits, Bouillon and Beyond</title>",
        b"<title>Biscuits, Bouillon and Beyond (revised)</title>",
    )
    rep = run_poll(tmp_cfg, web)
    assert rep.feeds["acast"].updated == 1 and rep.feeds["acast"].unchanged == 7
    assert load_meta(tmp_cfg, BISCUITS).title.endswith("(revised)")
    after = snapshot(tmp_cfg)
    changed = {k for k in after[0] if after[0][k] != before[0][k]}
    assert changed == {BISCUITS}


def test_apple_lookup_failure_falls_back_to_last_known_url(tmp_cfg, caplog):
    web = FakeWeb()
    run_poll(tmp_cfg, web)
    web.lookup.clear()  # Apple now returns no result
    with caplog.at_level(logging.WARNING):
        rep = run_poll(tmp_cfg, web)
    assert rep.feeds["acast"].status in ("ok", "not-modified")
    assert "using last known feed URL" in caplog.text


def test_apple_lookup_failure_on_first_poll_is_an_error(tmp_cfg):
    web = FakeWeb()
    web.lookup.clear()
    rep = run_poll(tmp_cfg, web, ("acast",))
    assert rep.feeds["acast"].status == "error"
    assert "apple-lookup" in rep.feeds["acast"].error
    assert rep.all_failed


def test_partial_failure_is_not_total_failure(tmp_cfg):
    web = FakeWeb()
    web.status[ACAST_URL] = 500
    rep = run_poll(tmp_cfg, web)
    assert rep.feeds["acast"].status == "error"
    assert rep.feeds["hrn"].new == 8
    assert not rep.all_failed
    web.status[HRN_URL] = 500
    assert run_poll(tmp_cfg, web).all_failed


# ----------------------------------------------------------------------- Patreon


def test_patreon_merge_matching_and_identity(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    rep = run_poll(cfg, web)
    assert rep.matched == 2 and len(rep.ambiguous) == 1
    assert rep.feeds["patreon"].skipped_non_audio == 2
    # 8 acast-identity + 4 patreon-only + 8 hrn
    assert (
        rep.feeds["acast"].new == 8 and rep.feeds["patreon"].new == 4 and rep.feeds["hrn"].new == 8
    )

    merged = load_meta(cfg, BISCUITS)  # same id as the Acast guid
    assert merged.source == "acast" and merged.audio_source == "patreon"
    assert merged.guids == {"acast": BISCUITS, "patreon": "134000001"}
    assert merged.enclosure_url.startswith("https://www.patreon.com/file?h=134000001")  # audio
    assert merged.title == "Biscuits, Bouillon and Beyond"  # show notes/title from Acast
    assert "carbonation" in merged.description.lower()
    assert merged.duration_s == 3940 and merged.items["acast"].duration_s == 3685
    assert merged.enclosure_length_hint == 61000000
    assert set(merged.items) == {"acast", "patreon"}

    only = [p.name for p in cfg.episodes_dir.glob("patreon-*")]
    assert len(only) == 4
    bonus = next(m for m in (load_meta(cfg, n) for n in only) if "Bonus" in m.title)
    assert bonus.source == "patreon" and bonus.audio_source == "patreon"
    assert bonus.enclosure_type == "audio/x-m4a" and bonus.duration_s == 25 * 60 + 10

    with state.connect(cfg.state_db) as conn:
        assert state.get_episode(conn, DRYADS)["audio_source"] == "patreon"
        assert state.guid_owner(conn, "patreon", "134000002") == DRYADS


def test_second_poll_with_patreon_is_a_no_op(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    run_poll(cfg, web)
    before = snapshot(cfg)
    rep = run_poll(cfg, web)
    assert all(r.new == 0 and r.updated == 0 for r in rep.feeds.values())
    assert rep.matched == 2
    assert snapshot(cfg) == before


def test_patreon_appearing_later_upgrades_the_same_episode(tmp_cfg, with_patreon):
    web = FakeWeb()
    run_poll(tmp_cfg, web)  # public feeds only
    assert load_meta(tmp_cfg, BISCUITS).audio_source == "acast"
    rep = run_poll(with_patreon, web)
    assert rep.feeds["acast"].new == 0 and rep.feeds["acast"].updated == 2  # the two matches
    assert load_meta(with_patreon, BISCUITS).audio_source == "patreon"
    assert len(list(with_patreon.episodes_dir.glob(f"{BISCUITS}*"))) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_patreon_auth_failure_is_recorded_and_public_feeds_continue(with_patreon, status):
    cfg, web = with_patreon, FakeWeb()
    web.status[PATREON_URL] = status
    rep = run_poll(cfg, web)
    assert rep.feeds["patreon"].status == "auth" and rep.feeds["patreon"].failed
    assert rep.feeds["acast"].new == 8 and rep.feeds["hrn"].new == 8
    assert not rep.all_failed
    with state.connect(cfg.state_db) as conn:
        assert state.get_feed(conn, "patreon")["last_status"] == f"auth_error:{status}"
    assert PATREON_TOKEN not in (rep.feeds["patreon"].error or "")


def test_patreon_empty_feed_is_recorded(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    web.bodies[PATREON_URL] = b"<rss version='2.0'><channel><title>empty</title></channel></rss>"
    rep = run_poll(cfg, web)
    assert rep.feeds["patreon"].status == "empty"
    with state.connect(cfg.state_db) as conn:
        assert state.get_feed(conn, "patreon")["last_status"] == "empty"
    assert rep.feeds["acast"].new == 8


def test_lost_patreon_access_does_not_downgrade_matched_episodes(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    run_poll(cfg, web)
    before = snapshot(cfg)
    web.status[PATREON_URL] = 403
    rep = run_poll(cfg, web)
    assert rep.feeds["patreon"].status == "auth"
    assert rep.feeds["acast"].updated == 0
    assert load_meta(cfg, BISCUITS).audio_source == "patreon"
    assert snapshot(cfg) == before


def test_patreon_only_episode_keeps_its_id_when_acast_twin_appears(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    run_poll(
        cfg, web, ("patreon",)
    )  # Acast not fetched yet and no cache: everything is Patreon-only
    first = {p.name for p in cfg.episodes_dir.iterdir()}
    assert all(n.startswith("patreon-") for n in first) and len(first) == 6
    rep = run_poll(cfg, web, ("acast",))  # Patreon comes from cache for matching
    assert rep.matched == 2
    names = {p.name for p in cfg.episodes_dir.iterdir()}
    assert BISCUITS not in names  # id is stable: it stays patreon-<hash>
    merged = next(
        m
        for m in (load_meta(cfg, n) for n in names if n.startswith("patreon-"))
        if m.guids.get("acast") == BISCUITS
    )
    assert merged.source == "acast" and merged.audio_source == "patreon"
    assert len(names) == 6 + 6  # 8 acast items, 2 of them merged into existing: 6 new


def test_duplicate_created_while_blind_is_merged_later(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    run_poll(cfg, web, ("acast",))
    ingest.cache_path(cfg, "acast").unlink()  # simulate a poll that cannot see the Acast feed
    run_poll(cfg, web, ("patreon",))
    with state.connect(cfg.state_db) as conn:
        dupes = state.find_episode_ids(conn, [("patreon", "134000001")])
    assert len(dupes) == 1 and dupes[0].startswith("patreon-")
    rep = run_poll(cfg, web)  # now both feeds are visible
    assert rep.absorbed == 2  # Biscuits and Dryads twins
    assert not list(cfg.episodes_dir.glob(f"{dupes[0]}*"))
    assert load_meta(cfg, BISCUITS).guids["patreon"] == "134000001"


def test_progressed_duplicate_is_left_alone_and_reported(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    run_poll(cfg, web, ("acast",))
    ingest.cache_path(cfg, "acast").unlink()
    run_poll(cfg, web, ("patreon",))
    with state.connect(cfg.state_db) as conn:
        dupe = state.find_episode_ids(conn, [("patreon", "134000001")])[0]
        conn.execute("UPDATE episodes SET stage = 'downloaded' WHERE episode_id = ?", (dupe,))
    rep = run_poll(cfg, web)
    assert rep.conflicts >= 1
    assert (ingest.episode_dir(cfg, dupe) / "meta.json").is_file()


# ---------------------------------------------------------------------- secrets


def test_patreon_token_never_leaks(with_patreon, caplog):
    cfg, web = with_patreon, FakeWeb()
    with caplog.at_level(logging.DEBUG):
        run_poll(cfg, web)
        web.status[PATREON_URL] = 500
        run_poll(cfg, web)
        web.status[PATREON_URL] = 403
        run_poll(cfg, web)
    assert PATREON_TOKEN not in caplog.text
    assert PATREON_URL not in caplog.text
    assert PATREON_TOKEN.encode() not in cfg.state_db.read_bytes()
    with state.connect(cfg.state_db) as conn:
        assert state.get_feed(conn, "patreon")["feed_url"] is None
    cache = ingest.cache_path(cfg, "patreon")
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


# -------------------------------------------------------------------------- diff


def test_diff_reports_pairs_and_patreon_only(with_patreon):
    cfg, web = with_patreon, FakeWeb()
    with web.client() as client:
        rep = ingest.diff_feeds(cfg, client)
    assert rep.patreon_configured and rep.counts == {"acast": 8, "hrn": 8, "patreon": 6}
    assert [p.acast.guid for p in rep.pairs] == [BISCUITS, DRYADS]
    biscuits = rep.pairs[0]
    assert biscuits.delta_s == 3940 - 3685
    assert biscuits.delta_pct == pytest.approx(100 * 255 / 3685)
    assert rep.pairs[1].delta_s == 0
    assert rep.median_delta_s == pytest.approx(127.5)
    assert {i.guid for i in rep.patreon_only} == {
        "134000003",
        "134000006",
        "134000007",
        "134000008",
    }
    in_range = {i.title for i in rep.acast_only_in_range}
    assert "NTT: Rules are Meant to Be Broken" in in_range
    assert "No Tangent Tuesday: Full Boat" not in in_range  # older than the Patreon feed
    assert len(rep.ambiguous) == 1 and rep.skipped_non_audio == 2
    assert "longer" in rep.hint()
    with state.connect(cfg.state_db) as conn:  # diff does not write episodes
        assert state.list_episodes(conn) == []


def test_diff_without_patreon_still_reports_feed_counts(tmp_cfg):
    web = FakeWeb()
    with web.client() as client:
        rep = ingest.diff_feeds(tmp_cfg, client)
    assert not rep.patreon_configured and rep.pairs == []
    assert rep.counts == {"acast": 8, "hrn": 8}
    assert rep.ranges["acast"] == ("2021-05-17", "2026-10-14")
    assert "no ad-free hint" in rep.hint()


# ------------------------------------------------------------------- check-audio


def test_pick_sample_buckets():
    rows = [{"i": i} for i in range(20)]
    picked = ingest.pick_sample(rows, 3)
    assert [b for b, _ in picked] == ["newest"] * 3 + ["middle"] * 3 + ["oldest"] * 3
    assert [r["i"] for _, r in picked] == [0, 1, 2, 9, 10, 11, 17, 18, 19]
    assert len(ingest.pick_sample(rows[:4], 3)) == 4  # overlapping buckets de-duplicate
    assert ingest.pick_sample([], 3) == []


def test_check_audio_uses_head_and_never_reads_a_body(tmp_cfg):
    web = FakeWeb()
    run_poll(tmp_cfg, web, ("hrn",))
    web.requests.clear()
    with web.client() as client:
        rows = ingest.check_audio(tmp_cfg, client, "hrn", sample=1)
    assert [b for b, *_ in rows] == ["newest", "middle", "oldest"]
    res = rows[0][3]
    assert res.ok and res.method == "HEAD" and res.content_type == "audio/mpeg"
    assert res.content_length == 1234 and res.final_host == "rss.art19.com"
    assert [r.method for r in web.requests] == ["HEAD"] * 3


def test_check_audio_falls_back_to_one_byte_range_get(tmp_cfg):
    web = FakeWeb()
    run_poll(tmp_cfg, web, ("hrn",))
    web.requests.clear()
    web.head_status = 405
    with web.client() as client:
        rows = ingest.check_audio(tmp_cfg, client, "hrn", sample=1)
    res = rows[0][3]
    assert res.ok and res.method == "GET Range" and res.status == 206
    assert res.content_length == 987654  # total from Content-Range, not the 1 byte
    gets = [r for r in web.requests if r.method == "GET"]
    assert len(gets) == 3 and all(r.headers["range"] == "bytes=0-0" for r in gets)


def test_check_audio_distrusts_a_text_plain_head_stub(tmp_cfg):
    web = FakeWeb()
    run_poll(tmp_cfg, web, ("acast",))
    web.requests.clear()
    web.head_stub = True
    with web.client() as client:
        rows = ingest.check_audio(tmp_cfg, client, "acast", sample=1)
    res = rows[0][3]
    assert res.method == "GET Range" and res.ok and res.content_type == "audio/mpeg"
    assert [r.method for r in web.requests[:2]] == ["HEAD", "GET"]


def test_probe_result_requires_audio_content_type():
    assert not ingest.ProbeResult("HEAD", status=200, content_type="text/plain").ok
    assert not ingest.ProbeResult("HEAD", status=200, content_type=None).ok
    assert not ingest.ProbeResult("HEAD", status=404, content_type="audio/mpeg").ok
    assert ingest.ProbeResult("HEAD", status=206, content_type="audio/mpeg").ok
    assert ingest.ProbeResult("HEAD", status=200, content_type="application/octet-stream").ok


def test_check_audio_reports_dead_links(tmp_cfg):
    web = FakeWeb()
    run_poll(tmp_cfg, web, ("hrn",))
    web.audio_status = 404
    web.head_status = None

    def dead(request):
        return httpx.Response(404)

    with ingest.HttpClient(httpx.MockTransport(dead), min_interval=0) as client:
        rows = ingest.check_audio(tmp_cfg, client, "hrn", sample=1)
    assert rows and not any(r[3].ok for r in rows)
    assert rows[0][3].status == 404
