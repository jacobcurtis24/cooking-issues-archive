"""Route characterization tests via Flask's test client (real DB, read-only)."""

import html
import re

import pytest

SOME_POST_ID = 1  # "The Searzall: Now On Kickstarter" (newest post)
SOME_POST_TITLE = "The Searzall: Now On Kickstarter"
PRIMER_TITLES = [
    "Hydrocolloids Primer",
    "Liquid Nitrogen Primer",
    "Rotary Evaporation in the Kitchen",
    "Transglutaminase, aka Meat Glue",
    "Low-Temperature and Sous-Vide Primer",
    "Purdy Pictures: The Charts",
    "Part I. Introduction to Low-Temperature Cooking and Sous-Vide",
    "Part II. Low-Temperature Cooking Without a Vacuum",
]


def body(resp) -> str:
    return resp.get_data(as_text=True)


def browse_ids(text: str) -> list[int]:
    return [int(i) for i in re.findall(r'<h3><a href="/post/(\d+)">', text)]


# --- index ------------------------------------------------------------------


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200
    t = body(r)
    assert "Cooking Issues Archive" in t
    assert "216 posts by Dave Arnold" in t
    assert "Technical Primers" in t
    assert "Recent Posts" in t
    assert 'action="/search"' in t
    # 8 primer cards, 15 recent post cards
    assert t.count('class="primer-card"') == 8
    assert t.count('class="post-card"') == 15
    assert SOME_POST_TITLE in t


# --- browse -----------------------------------------------------------------


def test_browse_page_one(client):
    r = client.get("/browse")
    assert r.status_code == 200
    t = body(r)
    assert "All Posts" in t
    assert "216 posts" in t
    assert len(browse_ids(t)) == 24
    assert '<span class="current">1</span>' in t
    assert "Next ›" in t
    assert "‹ Prev" not in t


def test_browse_pagination_page_two_differs(client):
    p1 = browse_ids(body(client.get("/browse?page=1")))
    r2 = client.get("/browse?page=2")
    assert r2.status_code == 200
    t2 = body(r2)
    p2 = browse_ids(t2)
    assert len(p2) == 24
    assert not set(p1) & set(p2)
    assert '<span class="current">2</span>' in t2
    assert "‹ Prev" in t2


def test_browse_last_page_has_remainder(client):
    # 216 posts / 24 per page = exactly 9 pages
    r = client.get("/browse?page=9")
    assert r.status_code == 200
    assert len(browse_ids(body(r))) == 24
    assert "Next ›" not in body(r)


def test_browse_page_out_of_range_is_empty_but_200(client):
    r = client.get("/browse?page=999")
    assert r.status_code == 200
    assert browse_ids(body(r)) == []


def test_browse_page_zero_clamps_to_one(client):
    assert browse_ids(body(client.get("/browse?page=0"))) == browse_ids(
        body(client.get("/browse?page=1"))
    )


def test_browse_tag_filter(client):
    r = client.get("/browse?tag=sous%20vide")
    assert r.status_code == 200
    t = body(r)
    assert 'Posts tagged "sous vide"' in t
    assert "Clear filter" in t
    ids = browse_ids(t)
    assert 0 < len(ids) < 24
    assert re.search(r"<p>6 posts — <a href=\"/browse\">Clear filter</a></p>", t)
    assert len(ids) == 6


def test_browse_tag_with_no_matches(client):
    r = client.get("/browse?tag=zzz-no-such-tag")
    assert r.status_code == 200
    assert browse_ids(body(r)) == []
    assert "0 posts" in body(r)


def test_browse_non_integer_page_is_a_500_known_bug(flask_app):
    """KNOWN BUG (pinned, not fixed): int(request.args['page']) is unguarded."""
    flask_app.config["TESTING"] = False
    r = flask_app.test_client().get("/browse?page=abc")
    assert r.status_code == 500


# --- post -------------------------------------------------------------------


def test_post_found(client):
    r = client.get(f"/post/{SOME_POST_ID}")
    assert r.status_code == 200
    t = body(r)
    assert SOME_POST_TITLE in t
    assert "View original" in t
    assert 'class="post-nav"' in t


def test_post_newest_has_no_next_but_has_prev(client):
    t = body(client.get(f"/post/{SOME_POST_ID}"))
    assert "/post/2" in t  # prev (older) post


def test_primer_post_found(client):
    r = client.get("/post/217")
    assert r.status_code == 200
    assert "Hydrocolloids Primer" in body(r)


def test_post_missing_is_404(client):
    assert client.get("/post/999999").status_code == 404
    assert client.get("/post/0").status_code == 404


def test_post_non_integer_id_is_404(client):
    assert client.get("/post/not-a-number").status_code == 404


# --- search -----------------------------------------------------------------


def test_search_hit(client):
    r = client.get("/search?q=carbonation")
    assert r.status_code == 200
    t = body(r)
    assert "9 results for" in t
    assert "<mark>" in t  # snippet highlighting
    assert 'class="result-item' in t


def test_search_prefix_and_multiword(client):
    # safe_fts turns each word into a quoted prefix term: "sous"* "vide"*
    t = body(client.get("/search?q=sous+vide"))
    assert "results for" in t
    assert t.count('class="result-item') > 1


def test_search_no_results(client):
    r = client.get("/search?q=zzzqxyzzy")
    assert r.status_code == 200
    t = body(r)
    assert "No results for" in t
    assert 'class="result-item' not in t


def test_search_empty_query_shows_hint(client):
    for url in ("/search", "/search?q=", "/search?q=%20%20"):
        r = client.get(url)
        assert r.status_code == 200
        t = body(r)
        assert "Enter a search term above" in t
        assert 'class="result-count"' not in t


def test_search_punctuation_only_shows_error_not_500(client):
    """safe_fts('!!!') == '' -> FTS5 syntax error, caught and shown inline."""
    r = client.get("/search?q=!!!")
    assert r.status_code == 200
    assert "Search error" in html.unescape(body(r))


# --- primers ----------------------------------------------------------------


def test_primers_listing(client):
    r = client.get("/primers")
    assert r.status_code == 200
    t = body(r)
    assert "Technical Primers" in t
    assert t.count('class="primer-full-card"') == 8
    for title in PRIMER_TITLES:
        assert title in t


@pytest.mark.parametrize("path", ["/", "/browse", "/primers", "/search"])
def test_pages_extend_base_layout(client, path):
    t = body(client.get(path))
    assert t.lstrip().lower().startswith("<!doctype html>")
    assert 'class="site-nav"' in t
