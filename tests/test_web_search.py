"""Unified /search: blog posts (unchanged) plus transcripts and show notes of episodes."""

import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup
from webfake import add_episode, make_transcript, make_words

from ciw import search_index as si
from ciw.web import create_app, episodes

NEEDLE = "zymomonas"


@pytest.fixture
def cfg(blog_cfg):
    words = make_words(
        "so today we talk about carbonation in the kitchen. later a bacterium called "
        "Zymomonas shows up in the fermentation segment and then more carbonation talk",
        gaps={9: 3.0},  # a pause: two utterances
    )
    add_episode(blog_cfg, "e1", "Fizz Episode", "2026-02-01", notes="Notes mention carbonation.",
                transcript=make_transcript(words))  # fmt: skip
    add_episode(blog_cfg, "e2", "Rotovap Show", "2026-01-01", notes="About the rotovap only.",
                transcript="a plain transcript without the keyword")  # fmt: skip
    add_episode(blog_cfg, "e3", "Notes Only Episode", "2025-12-01", notes="Zymomonas in the notes.")
    si.update_index(blog_cfg)
    return blog_cfg


@pytest.fixture
def web(cfg):
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def soup(resp) -> BeautifulSoup:
    return BeautifulSoup(resp.get_data(as_text=True), "html.parser")


def groups(s):
    return {g["data-episode-id"]: g for g in s.select(".ep-hit")}


def test_phrase_from_a_transcript_is_found_and_links_to_its_timestamp(web):
    r = web.get(f"/search?q={NEEDLE}")
    assert r.status_code == 200
    s = soup(r)
    g = groups(s)
    assert set(g) == {"e1", "e3"}
    link = g["e1"].select_one("li a.ts")
    url = urlparse(link["href"])
    assert url.path == "/episode/e1" and parse_qs(url.query) == {"q": [NEEDLE]}
    start = int(url.fragment.removeprefix("t="))
    assert start > 0 and link.text == f"[{start // 60:02d}:{start % 60:02d}]"
    assert "<mark>Zymomonas</mark>" in str(g["e1"].select_one(".hit-snippet"))
    # following the link: the page highlights the term and the anchor is a real utterance
    page = soup(web.get(link["href"].split("#")[0]))
    assert [m.text for m in page.select("mark.hit")] == ["Zymomonas"]
    assert any(abs(float(u["data-start"]) - start) < 1 for u in page.select(".utt"))


def test_hits_are_grouped_per_episode_with_at_most_three_snippets(blog_cfg):
    text = " ".join(f"carbonation number {i} stop. " + "filler " * 20 for i in range(6))
    words = make_words(text, gaps={})
    # make every sentence its own utterance with a pause between
    gaps = {i: 2.0 for i, w in enumerate(words) if w.w == "stop."}
    words = make_words(text, gaps={k + 1: v for k, v in gaps.items()})
    add_episode(blog_cfg, "many", "Many Hits", transcript=make_transcript(words))
    si.update_index(blog_cfg)
    s = soup(create_app(blog_cfg).test_client().get("/search?q=carbonation&scope=episodes"))
    (g,) = groups(s).values()
    assert len(g.select("li")) == 3
    assert "more passage" in g.select_one(".more").text
    times = [int(a["href"].split("#t=")[1]) for a in g.select("li a.ts")]
    assert times == sorted(times)  # shown in time order


def test_show_notes_and_title_hits_for_episodes_without_transcript(web):
    s = soup(web.get("/search?q=rotovap"))
    g = groups(s)
    assert set(g) == {"e2"} and "<mark>rotovap</mark>" in str(g["e2"].select_one(".notes-snip"))
    s = soup(web.get("/search?q=Fizz"))
    assert "<mark>Fizz</mark>" in str(groups(s)["e1"].select_one("h3"))
    only_notes = groups(soup(web.get(f"/search?q={NEEDLE}")))["e3"]
    assert (
        only_notes.select("li") == [] and "Notes Only Episode" in only_notes.select_one("h3").text
    )


def test_blog_only_hits_leave_the_episode_section_empty(web):
    s = soup(web.get("/search?q=sous+vide"))
    assert len(s.select(".result-item")) > 1
    assert s.select_one("#episode-results") is not None and not s.select(".ep-hit")
    assert (
        "No episode transcripts or show notes mention"
        in s.select_one(".result-count:last-of-type").text
    )


def test_blog_and_episode_hits_together(web):
    s = soup(web.get("/search?q=carbonation"))
    assert len(s.select(".result-item")) == 9  # the blog result, exactly as before
    assert set(groups(s)) == {"e1"}
    assert [h.text for h in s.select(".search-section-title")] == ["Blog posts", "In episodes"]
    # order on the page: blog results first, then episodes
    html = str(s)
    assert html.index("result-list") < html.index('id="episode-results"')


def test_no_hits_anywhere(web):
    s = soup(web.get("/search?q=zzzqxyzzy"))
    t = s.get_text(" ", strip=True)
    assert "No results for" in t and "No episode transcripts or show notes mention" in t
    assert not s.select(".result-item") and not s.select(".ep-hit")


@pytest.mark.parametrize("q", ["!!!", "???", "   !  ", '"""', "()", "***"])
def test_punctuation_only_gets_a_friendly_message_in_the_episode_section(web, q):
    r = web.get("/search", query_string={"q": q})
    assert r.status_code == 200
    s = soup(r)
    if q.strip():
        assert "Use at least one letter or number" in s.select_one("#episode-notice").text
        text = s.select_one("#episode-notice").text.lower()
        assert "fts5" not in text and "syntax" not in text
        assert not s.select(".ep-hit")


@pytest.mark.parametrize(
    "q", ["a OR b", "NEAR(x y)", "col:thing", "x*", "^start", '"unbalanced', "it's", "naïve café",
          "-negated", "AND", "NOT", "\\", "a" * 400, "%00", "\x00x"],
)  # fmt: skip
def test_odd_queries_never_surface_fts_errors_from_the_episode_code(web, q):
    r = web.get("/search", query_string={"q": q})
    assert r.status_code == 200
    ep_part = r.get_data(as_text=True).split('id="episode-results"')[-1]
    assert "fts5" not in ep_part.lower() and "syntax error" not in ep_part.lower()


def test_internal_sqlite_errors_become_a_friendly_message(web, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("fts5: syntax error near ')'")

    monkeypatch.setattr(si, "search_utterances", boom)
    s = soup(web.get(f"/search?q={NEEDLE}"))
    note = s.select_one("#episode-notice").text
    assert "unavailable" in note and "fts5" not in note and "syntax" not in note


def test_missing_index_shows_a_one_line_notice(cfg, web):
    cfg.search_db.unlink()
    r = web.get("/search?q=carbonation")
    assert r.status_code == 200
    s = soup(r)
    assert "ciw index" in s.select_one("#episode-notice").text
    assert len(s.select(".result-item")) == 9  # blog results are unaffected
    assert web.get("/search?q=carbonation&scope=blog").status_code == 200


def test_corrupt_index_is_treated_like_a_missing_one(cfg, web):
    cfg.search_db.write_bytes(b"this is not a database" * 100)
    r = web.get("/search?q=carbonation")
    assert r.status_code == 200 and "ciw index" in soup(r).select_one("#episode-notice").text


@pytest.mark.parametrize(
    "scope, blog, eps",
    [("all", True, True), ("blog", True, False), ("episodes", False, True), ("bogus", True, True),
     ("", True, True)],
)  # fmt: skip
def test_scope_selects_the_sections(web, scope, blog, eps):
    s = soup(web.get("/search", query_string={"q": "carbonation", "scope": scope}))
    assert bool(s.select(".result-item")) is blog
    assert bool(s.select(".ep-hit")) is eps
    assert (s.select_one("#episode-results") is not None) is eps


def test_scope_tabs_show_counts_and_keep_the_query(web):
    s = soup(web.get("/search?q=carbonation&scope=blog"))
    tabs = {a.text.split(" (")[0]: a for a in s.select("#scope-tabs a")}
    assert set(tabs) == {"All", "Blog posts", "Episodes"}
    assert "(9)" in tabs["Blog posts"].text and "(1)" in tabs["Episodes"].text
    assert "active" in tabs["Blog posts"]["class"] and "active" not in tabs["All"].get("class", [])
    assert parse_qs(urlparse(tabs["Episodes"]["href"]).query) == {
        "q": ["carbonation"],
        "scope": ["episodes"],
    }
    assert s.select_one('form.search-form-lg input[name="scope"]')["value"] == "blog"


def test_empty_query_page_is_unchanged(web):
    for url in ("/search", "/search?q=", "/search?q=%20&scope=episodes"):
        s = soup(web.get(url))
        assert "Enter a search term above" in s.get_text()
        assert not s.select("#scope-tabs") and not s.select("#episode-results")
        assert not s.select('input[name="scope"]')


def test_blog_results_markup_is_the_same_with_and_without_the_episode_section(web, client):
    """The blog block of /search is byte-identical to what the plain blog app renders."""

    def blog_block(html):
        m = re.search(
            r'<div class="result-list">.*?</div>\s*(?=\s*<h2 [^>]*id="episode-results")',
            html,
            re.S,
        )
        return m.group(0) if m else None

    new = web.get("/search?q=carbonation").get_data(as_text=True)
    old = client.get("/search?q=carbonation").get_data(as_text=True)  # same code, no index
    assert blog_block(new) is not None
    assert blog_block(new) == blog_block(old)


def test_hostile_transcript_text_is_escaped_in_snippets(blog_cfg):
    ws = make_words("try <script>alert(1)</script> and <img src=x onerror=alert(2)> carbonation")
    add_episode(blog_cfg, "x", "X <b>title</b>", transcript=make_transcript(ws),
                notes="<script>alert(3)</script> carbonation")  # fmt: skip
    si.update_index(blog_cfg)
    t = create_app(blog_cfg).test_client().get("/search?q=carbonation&scope=episodes")
    html = t.get_data(as_text=True)
    assert "<script>alert" not in html and "<img src=x" not in html and "<b>title" not in html
    assert "&lt;script&gt;" in html


def test_stale_index_entries_are_skipped(cfg, web):
    from ciw import state

    with state.connect(cfg.state_db) as conn:
        state.delete_episode(conn, "e1")
    assert set(groups(soup(web.get(f"/search?q={NEEDLE}")))) == {"e3"}


def test_search_episodes_returns_counts(cfg):
    app = create_app(cfg)
    with app.test_request_context("/"):
        res = episodes.search_episodes("carbonation")
        both = episodes.search_episodes("zymomonas")
    assert (res.status, res.total_hits, res.total_episodes, res.shown) == ("ok", 2, 1, 1)
    assert [g.episode_id for g in res.groups] == ["e1"] and res.groups[0].n_hits == 2
    assert both.total_hits == 1 and both.total_episodes == 2 and both.shown == 2
