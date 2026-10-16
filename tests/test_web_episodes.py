"""Episodes blueprint: list page, filters, pagination, nav (synthetic episodes in tmp dirs)."""

import re

import pytest
from bs4 import BeautifulSoup
from webfake import add_episode

from ciw import search_index as si
from ciw.web import create_app


def page(resp) -> BeautifulSoup:
    return BeautifulSoup(resp.get_data(as_text=True), "html.parser")


def card_ids(resp) -> list[str]:
    return [c["data-episode-id"] for c in page(resp).select("article.episode-card")]


@pytest.fixture
def cfg(blog_cfg):
    """tmp config (only real path: the read-only blog DB) holding six synthetic episodes."""
    add_episode(blog_cfg, "t1", "Biscuits and Bouillon", "2026-03-01", source="acast",
                notes="Dave and Nastassia talk about biscuits.", transcript="we make biscuits now",
                audio=True, audio_duration_s=3700.0, duration_s=3600.0)  # fmt: skip
    add_episode(blog_cfg, "a2", "Audio Only Episode", "2025-12-20", source="acast",
                notes="Just downloaded.", audio=True)  # fmt: skip
    add_episode(blog_cfg, "h1", "Heritage Radio Show", "2025-06-01", source="hrn",
                notes="Rotary evaporator <b>news</b>.", duration_s=2400.0)  # fmt: skip
    add_episode(blog_cfg, "h2", "Another HRN Show", "2024-02-01", source="hrn", notes="Old stuff.")
    add_episode(
        blog_cfg, "p1", "Patreon Only Bonus", "2025-07-01", source="patreon", notes="Bonus!"
    )
    add_episode(blog_cfg, "n1", "No Meta File", "2023-01-01", source="acast", meta=False)
    return blog_cfg


@pytest.fixture
def web(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def test_list_shows_every_episode_newest_first(web):
    r = web.get("/episodes")
    assert r.status_code == 200
    assert card_ids(r) == ["t1", "a2", "p1", "h1", "h2", "n1"]
    assert "6 episodes" in r.get_data(as_text=True)


def test_card_contents(web):
    soup = page(web.get("/episodes"))
    cards = {c["data-episode-id"]: c for c in soup.select("article.episode-card")}
    t1 = cards["t1"]
    assert t1.select_one("h3 a")["href"] == "/episode/t1"
    assert t1.select_one("h3 a").text == "Biscuits and Bouillon"
    text = t1.get_text(" ", strip=True)
    assert "Acast" in text and "1 March 2026" in text
    assert "1 h 02 min" in text  # audio_duration_s (3700) wins over the feed's 3600
    assert t1.select_one(".stage-transcribed").text == "Transcribed"
    assert "biscuits" in t1.select_one("p").text
    assert cards["a2"].select_one(".stage-audio").text == "Audio only"
    assert cards["h1"].select_one(".stage-notes").text == "Show notes only"
    assert "40 min" in cards["h1"].get_text()  # feed duration when there is no audio
    assert "HRN" in cards["h1"].get_text() and "Patreon" in cards["p1"].get_text()
    assert "<b>" not in str(cards["h1"])  # excerpts are plain text
    assert cards["n1"].select_one("p") is None  # no meta.json: no excerpt, still listed


@pytest.mark.parametrize(
    "query, expected",
    [
        ("source=hrn", ["h1", "h2"]),
        ("source=patreon", ["p1"]),
        ("source=nonsense", ["t1", "a2", "p1", "h1", "h2", "n1"]),
        ("year=2025", ["a2", "p1", "h1"]),
        ("year=1999", []),
        ("year=abc", ["t1", "a2", "p1", "h1", "h2", "n1"]),
        ("status=transcribed", ["t1"]),
        ("status=downloaded", ["t1", "a2"]),
        ("status=all", ["t1", "a2", "p1", "h1", "h2", "n1"]),
        ("status=bogus", ["t1", "a2", "p1", "h1", "h2", "n1"]),
        ("source=hrn&year=2025", ["h1"]),
        ("source=acast&status=downloaded&year=2026", ["t1"]),
    ],
)
def test_filters(web, query, expected):
    r = web.get(f"/episodes?{query}")
    assert r.status_code == 200
    assert card_ids(r) == expected


def test_empty_result_message_and_clear_link(web):
    t = web.get("/episodes?year=1999").get_data(as_text=True)
    assert "No episodes match these filters" in t and "Clear filters" in t


def test_title_search_without_an_index_titles_only_with_a_notice(web):
    r = web.get("/episodes?q=biscuits")
    assert card_ids(r) == ["t1"]
    assert "ciw index" in r.get_data(as_text=True)
    assert card_ids(web.get("/episodes?q=EVAPORATOR")) == []  # notes need the index


def test_search_with_index_covers_show_notes_and_title(web, cfg):
    si.update_index(cfg)
    assert card_ids(web.get("/episodes?q=evaporator")) == ["h1"]
    assert card_ids(web.get("/episodes?q=nastassia")) == ["t1"]
    assert card_ids(web.get("/episodes?q=Bonus")) == ["p1"]  # title (and notes)
    assert card_ids(web.get("/episodes?q=bonus&source=hrn")) == []
    assert "ciw index" not in web.get("/episodes?q=bonus").get_data(as_text=True)


@pytest.mark.parametrize("q", ["!!!", "%", "_", '"', "'; DROP TABLE episodes;--", "a" * 500])
def test_awkward_queries_do_not_crash(web, cfg, q):
    for indexed in (False, True):
        if indexed:
            si.update_index(cfg)
        assert web.get("/episodes", query_string={"q": q}).status_code == 200


def test_percent_and_underscore_are_literals_in_title_search(web):
    assert card_ids(web.get("/episodes?q=%25")) == []
    assert card_ids(web.get("/episodes?q=_")) == []


@pytest.fixture
def many(blog_cfg):
    for i in range(55):
        add_episode(
            blog_cfg, f"e{i:02d}", f"Episode {i:02d}", f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        )
    app = create_app(blog_cfg)
    return app.test_client()


def test_pagination_24_per_page(many):
    ids = []
    for n in (1, 2, 3):
        r = many.get(f"/episodes?page={n}")
        got = card_ids(r)
        assert len(got) == (24, 24, 7)[n - 1]
        ids += got
    assert len(set(ids)) == 55 and ids == sorted(ids, reverse=True)
    t = many.get("/episodes").get_data(as_text=True)
    assert '<span class="current">1</span>' in t and "Next ›" in t and "‹ Prev" not in t
    assert "55 episodes" in t


def test_pagination_links_keep_the_filters(many):
    soup = page(many.get("/episodes?year=2026&q=episode&status=all"))
    hrefs = [a["href"] for a in soup.select(".pagination a")]
    assert hrefs and all("year=2026" in h and "q=episode" in h for h in hrefs)
    assert any("page=2" in h for h in hrefs)


@pytest.mark.parametrize("value", ["abc", "", "-5", "0", "1.5", "99999999999999999999", "٣"])
def test_bad_page_values_fall_back_instead_of_500(many, value):
    r = many.get("/episodes", query_string={"page": value})
    assert r.status_code == 200
    assert len(card_ids(r)) in (7, 24)


def test_page_past_the_end_shows_the_last_page(many):
    assert len(card_ids(many.get("/episodes?page=50"))) == 7


def test_no_state_db_is_a_helpful_empty_page(blog_cfg):
    app = create_app(blog_cfg)
    r = app.test_client().get("/episodes")
    assert r.status_code == 200 and "ciw poll" in r.get_data(as_text=True)


def test_nav_has_episodes_link_and_marks_it_active(web):
    soup = page(web.get("/episodes"))
    active = [a.text for a in soup.select("nav.site-nav a.active")]
    assert active == ["Episodes"]
    for path in ("/", "/browse", "/primers", "/search?q=agar"):
        links = page(web.get(path)).select("nav.site-nav a")
        assert [a.text for a in links] == ["Home", "Primers", "All Posts", "Episodes"]
        assert not any(a.text == "Episodes" and a.has_attr("class") for a in links)


def test_static_assets_are_served(web):
    r = web.get("/static/episodes.css")
    assert r.status_code == 200 and "text/css" in r.content_type
    assert re.search(r"\.utt\b", r.get_data(as_text=True))
