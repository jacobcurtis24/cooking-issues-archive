"""Acast <-> Patreon matching rules, tolerance edges and ambiguity."""

from datetime import UTC, datetime, timedelta

import pytest
from fakeweb import fixture

from ciw import matching
from ciw.feeds import normalize_title, parse_feed
from ciw.matching import match_feeds
from ciw.models import FeedItem

BASE = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def item(source, guid, title, *, days=0.0, dur=3600.0, seconds=0):
    return FeedItem(
        source=source,
        guid=guid,
        title=title,
        title_norm=normalize_title(title),
        pub_date=BASE + timedelta(days=days, seconds=seconds),
        duration_s=dur,
        enclosure_url=f"https://x/{guid}.mp3",
        enclosure_type="audio/mpeg",
    )


def acast(guid="a1", title="Carbonation Q&A", **kw):
    return item("acast", guid, title, **kw)


def patreon(guid="p1", title="Carbonation Q&A", **kw):
    return item("patreon", guid, title, **kw)


def test_simple_match_with_title_variants():
    r = match_feeds(
        [acast(title="Biscuits, Bouillon and Beyond")],
        [patreon(title="Cooking Issues: Biscuits, Bouillon & Beyond [Patreon]")],
    )
    assert [(a.guid, p.guid) for a, p in r.pairs] == [("a1", "p1")]
    assert not r.ambiguous and not r.acast_only and not r.patreon_only


@pytest.mark.parametrize(
    ("offset", "matches"),
    [
        # offset = Patreon publish time minus Acast publish time
        (timedelta(days=3), True),  # Patreon 3 days AFTER Acast: exactly at the window edge
        (timedelta(days=3, seconds=1), False),
        (timedelta(0), True),
        (timedelta(days=-3), True),  # Patreon early access (typical)
        (timedelta(days=-14), True),  # ... even two weeks early (seen in the real feeds)
        (timedelta(days=-21), True),  # exactly at the lead edge
        (timedelta(days=-21, seconds=-1), False),
    ],
)
def test_date_window_edges(offset, matches):
    r = match_feeds([acast()], [patreon(days=offset.days, seconds=offset.seconds)])
    assert bool(r.pairs) is matches


def test_patreon_early_access_by_days_matches():
    r = match_feeds([acast(days=5)], [patreon(days=0)])
    assert len(r.pairs) == 1 and not r.patreon_only and not r.acast_only


def test_recurring_titles_are_separated_by_near_identical_durations():
    """Weekly look-alike titles inside the lead window must not turn into ambiguity when the
    durations tell them apart (the real feeds agree within a few seconds)."""
    a = [
        acast("a1", "No Tangent Tuesday", days=7, dur=3601),
        acast("a2", "No Tangent Tuesday", days=14, dur=3302),
    ]
    p = [
        patreon("p1", "No Tangent Tuesday", days=4, dur=3600),
        patreon("p2", "No Tangent Tuesday", days=11, dur=3300),
    ]
    r = match_feeds(a, p)
    assert sorted((x.guid, y.guid) for x, y in r.pairs) == [("a1", "p1"), ("a2", "p2")]
    assert not r.ambiguous


def test_same_title_and_same_duration_stays_ambiguous():
    a = [acast("a1", "No Tangent Tuesday", days=7), acast("a2", "No Tangent Tuesday", days=14)]
    p = [patreon("p1", "No Tangent Tuesday", days=4), patreon("p2", "No Tangent Tuesday", days=11)]
    r = match_feeds(a, p)
    assert not r.pairs and len(r.ambiguous) == 1


def test_unknown_durations_never_break_a_tie():
    a = [acast("a1", "Weekly", days=7, dur=None), acast("a2", "Weekly", days=14, dur=None)]
    p = [patreon("p1", "Weekly", days=4, dur=None), patreon("p2", "Weekly", days=11, dur=None)]
    assert not match_feeds(a, p).pairs


@pytest.mark.parametrize(
    ("a_dur", "p_dur", "matches"),
    [
        (3600, 3600 + 360, True),  # 360 s is under 10% of the longer one
        (3600, 3600 + 400, True),  # 400/4000 = 10% exactly
        (3600, 3600 + 410, False),  # just over 10%
        (300, 300 + 120, True),  # short episode: 120 s absolute tolerance
        (300, 300 + 121, False),
        (None, 3600, True),  # unknown duration never vetoes
        (3600, None, True),
    ],
)
def test_duration_tolerance_edges(a_dur, p_dur, matches):
    r = match_feeds([acast(dur=a_dur)], [patreon(dur=p_dur)])
    assert bool(r.pairs) is matches


def test_duration_constants_are_configurable(monkeypatch):
    a, p = acast(dur=3600), patreon(dur=3600 + 1000)
    assert not match_feeds([a], [p]).pairs
    monkeypatch.setattr(matching, "DURATION_TOLERANCE_PCT", 0.5)
    assert match_feeds([a], [p]).pairs
    monkeypatch.setattr(matching, "DATE_WINDOW_DAYS", 0)
    assert not match_feeds([a], [patreon(dur=3600, days=1)]).pairs


def test_different_titles_do_not_match():
    assert not match_feeds([acast(title="Sous vide eggs")], [patreon(title="Pizza ovens")]).pairs


def test_similar_titles_with_different_numbers_do_not_match():
    r = match_feeds([acast(title="Q and A part 1")], [patreon(title="Q and A part 2")])
    assert not r.pairs


def test_near_identical_titles_match_by_similarity():
    r = match_feeds(
        [acast(title="Dave answers your questions about ovens")],
        [patreon(title="Dave answers your question about ovens")],
    )
    assert len(r.pairs) == 1


def test_two_candidates_are_reported_not_guessed():
    r = match_feeds(
        [acast("a1", "NTT: The Outdoor Kitchen")],
        [
            patreon("p1", "NTT: The Outdoor Kitchen"),
            patreon("p2", "NTT: The Outdoor Kitchen (Ad-Free)"),
        ],
    )
    assert not r.pairs
    assert len(r.ambiguous) == 1
    amb = r.ambiguous[0]
    assert [i.guid for i in amb.acast] == ["a1"] and {i.guid for i in amb.patreon} == {"p1", "p2"}
    assert "a1" in amb.describe()
    # unmatched items stay available as standalone episodes
    assert [i.guid for i in r.acast_only] == ["a1"]
    assert {i.guid for i in r.patreon_only} == {"p1", "p2"}


def test_two_acast_items_competing_for_one_patreon_post_are_ambiguous():
    r = match_feeds([acast("a1"), acast("a2", days=1)], [patreon("p1")])
    assert not r.pairs and len(r.ambiguous) == 1


def test_unambiguous_neighbours_still_match():
    a = [acast("a1", "Eggs", days=0), acast("a2", "Ovens", days=7)]
    p = [patreon("p1", "Ovens", days=7), patreon("p2", "Eggs", days=0)]
    r = match_feeds(a, p)
    assert sorted((x.guid, y.guid) for x, y in r.pairs) == [("a1", "p2"), ("a2", "p1")]


def test_locked_pairs_stay_paired_even_if_rules_disagree():
    a, p = acast(title="One"), patreon(title="Totally different", days=30)
    assert not match_feeds([a], [p]).pairs
    r = match_feeds([a], [p], locked=[("a1", "p1")])
    assert len(r.pairs) == 1
    # a lock whose Patreon post vanished is ignored
    assert not match_feeds([a], [], locked=[("a1", "gone")]).pairs


def test_synthetic_fixture_end_to_end():
    ac = parse_feed(fixture("acast"), "acast").items
    pa = parse_feed(fixture("patreon"), "patreon").items
    r = match_feeds(ac, pa)
    pairs = {a.guid: p.guid for a, p in r.pairs}
    assert pairs == {
        "69715223a6c658f1834fb3d6": "134000001",  # title/date/duration all differ slightly
        "6acfbad2253c13a8980d51bd": "134000002",  # +3 days exactly, accents, en dash
    }
    assert len(r.ambiguous) == 1
    assert {i.guid for i in r.ambiguous[0].patreon} == {"134000006", "134000007"}
    assert {i.guid for i in r.patreon_only} == {"134000003", "134000006", "134000007", "134000008"}
    assert "6acfbb58e65da00825c0f2e7" in {i.guid for i in r.acast_only}
