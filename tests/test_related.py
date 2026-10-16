# ruff: noqa: E501
"""Related blog posts (ciw.related) on a tiny fixture blog DB, the episode page box, and the
'Mentioned in episodes' box on blog post pages."""

import sqlite3

import pytest
from bs4 import BeautifulSoup
from webfake import add_episode, make_transcript, make_words

from ciw import related
from ciw import search_index as si
from ciw.web import create_app

POSTS = [
    # id, title, body, tags, type
    (
        1,
        "The Searzall Torch",
        "The searzall is a torch attachment. Searzall tips and searzall tricks.",
        "Searzall",
        "post",
    ),
    (
        2,
        "Carbonation Basics",
        "Carbonation with CO2 and a carbonator. Forced carbonation is quick.",
        "Carbonation",
        "post",
    ),
    (
        3,
        "Cooking Issues Radio",
        "Cooking Issues goes on the radio. Cooking Issues is great.",
        "",
        "post",
    ),
    (
        4,
        "Pizza Dough",
        "Cooking Issues pizza dough. Flour water yeast. A little searzall use.",
        "",
        "post",
    ),
    (
        5,
        "Rotary Evaporators",
        "A rotary evaporator or rotovap distills flavors. Rotary evaporation primer.",
        "",
        "primer",
    ),
    (
        6,
        "Unrelated",
        "Nothing to see. Wylie Dufresne once cooked. Cooking Issues forever.",
        "",
        "post",
    ),
    (7, "Another Cooking Issues Post", "Cooking Issues once more.", "", "post"),
    (8, "Cooking Issues Again", "Cooking Issues yet again.", "", "post"),
]
VOCAB = ["Cooking Issues", "Searzall", "carbonation", "rotary evaporator", "rotovap", "sous vide"]


@pytest.fixture
def blog(tmp_path):
    path = tmp_path / "blog.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE posts (id INTEGER PRIMARY KEY, url TEXT, title TEXT, date TEXT,
            author TEXT, categories TEXT, tags TEXT, body_html TEXT, body_text TEXT, post_type TEXT);
        CREATE VIRTUAL TABLE posts_fts USING fts5(title, body_text, categories, tags,
            content='posts', content_rowid='id');"""
    )
    for i, title, body, tags, kind in POSTS:
        conn.execute(
            "INSERT INTO posts VALUES (?, ?, ?, ?, '', '', ?, '', ?, ?)",
            (i, f"u{i}", title, f"2010-01-{i:02d}", tags, body, kind),
        )
    conn.execute("INSERT INTO posts_fts(rowid, title, body_text, categories, tags) "
                 "SELECT id, title, body_text, categories, tags FROM posts")  # fmt: skip
    conn.commit()
    conn.close()
    return path


def test_key_terms_weigh_frequency_and_rarity(blog):
    conn = related.connect_blog(blog)
    terms = related.key_terms(
        title="Torch talk",
        notes="We discuss the Searzall and carbonation and Cooking Issues.",
        transcript_text="searzall searzall searzall carbonation cooking issues",
        vocabulary=VOCAB,
        df=related.make_df(conn),
        n_posts=len(POSTS),
    )
    names = [t.term for t in terms]
    assert names[0] == "Searzall"  # more mentions, df 2
    assert "carbonation" in names
    assert "Cooking Issues" not in names  # in 6 of 8 posts: too common
    assert "rotovap" not in names  # not in the episode
    sear = terms[0]
    assert (sear.tf, sear.df, sear.source) == (3, 2, "vocabulary")
    assert all(a.score >= b.score for a, b in zip(terms, terms[1:], strict=False))


def test_terms_with_no_blog_match_are_dropped(blog):
    conn = related.connect_blog(blog)
    terms = related.key_terms(
        title="x", notes="sous vide all day, Searzall", transcript_text=None,
        vocabulary=VOCAB, df=related.make_df(conn), n_posts=len(POSTS), min_terms=0,
    )  # fmt: skip
    assert [t.term for t in terms] == ["Searzall"]  # "sous vide" matches no post


def test_capitalised_phrases_fill_in_when_the_vocabulary_is_thin(blog):
    conn = related.connect_blog(blog)
    terms = related.key_terms(
        title="Chat with Zorblax",
        notes="Wylie Dufresne is back. Talking Searzall.",
        transcript_text=None,
        vocabulary=VOCAB,
        df=related.make_df(conn),
        n_posts=len(POSTS),
    )
    by = {t.term: t.source for t in terms}
    assert by["Searzall"] == "vocabulary"
    assert by.get("Wylie Dufresne") == "capitalized"
    assert "Zorblax" not in by  # matches no post
    assert "Talking" not in by and "Chat" not in by


def test_max_terms_is_respected(blog):
    conn = related.connect_blog(blog)
    terms = related.key_terms(
        title="", notes="Searzall carbonation rotovap rotary evaporator", transcript_text=None,
        vocabulary=VOCAB, df=related.make_df(conn), n_posts=len(POSTS), max_terms=2,
    )  # fmt: skip
    assert len(terms) == 2


def test_related_posts_are_ranked_and_explain_themselves(blog):
    res = related.related_posts(
        blog,
        title="Torch talk",
        notes="Searzall and carbonation",
        transcript_text="searzall searzall carbonation co2",
        vocabulary=VOCAB,
    )
    ids = [p.id for p in res.posts]
    assert set(ids) == {1, 2, 4}  # the Searzall post, the carbonation post, the pizza mention
    assert ids[:2] in ([1, 2], [2, 1]) and ids[-1] == 4
    top = {p.id: p for p in res.posts}
    assert top[1].terms == ["Searzall"] and top[2].terms == ["carbonation"]
    assert "<mark>" in top[1].snippet.lower()
    assert [p.score for p in res.posts] == sorted(p.score for p in res.posts)
    assert len(related.related_posts(blog, title="", notes="Searzall carbonation",
                                     vocabulary=VOCAB, limit=1).posts) == 1  # fmt: skip


def test_prefix_matching_finds_plurals_and_snippets_are_escaped(blog):
    conn = sqlite3.connect(blog)
    conn.execute(
        "UPDATE posts SET body_text = body_text || ' <script>x</script> searzalls' WHERE id=4"
    )
    conn.execute("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    res = related.related_posts(blog, title="", notes="Searzall", vocabulary=VOCAB)
    post4 = next(p for p in res.posts if p.id == 4)
    assert "<script>" not in post4.snippet


def test_nothing_to_go_on_gives_nothing(blog):
    assert related.related_posts(blog, title="", notes="", vocabulary=[]).posts == []
    assert (
        related.related_posts(blog, title="lowercase words only", notes="", vocabulary=VOCAB).posts
        == []
    )


def test_blog_db_is_opened_read_only(blog):
    conn = related.connect_blog(blog)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM posts")


def test_count_mentions_matches_the_quality_rules():
    toks = "the sous vide and sous-vide and searzalls plus rotary evaporator evaporators".split()
    from ciw.quality import mentions, tokens

    toks = tokens(" ".join(toks))
    idx = related.token_index(toks)
    for term in ["sous vide", "Searzall", "rotary evaporator", "sous-vide", "missing thing"]:
        assert related.count_mentions(term, toks, idx) == len(mentions(term, toks)), term


def test_show_boilerplate_flags_terms_in_many_episodes(tmp_cfg):
    for i in range(20):
        add_episode(
            tmp_cfg,
            f"e{i}",
            f"Ep {i}",
            notes="Cooking Issues Searzall" if i < 2 else "Cooking Issues",
        )
    si.update_index(tmp_cfg)
    with si.open_readonly(tmp_cfg.search_db) as ic:
        skip = related.show_boilerplate(ic)
        assert skip("Cooking Issues") and not skip("Searzall") and not skip("!!!")


def test_post_terms_and_episodes_mentioning(tmp_cfg):
    assert related.post_terms("The Searzall: Now On Kickstarter", "tag", "", VOCAB) == ["Searzall"]
    assert related.post_terms("Lunch", "", "", VOCAB) == []
    add_episode(tmp_cfg, "m1", "Torch show", notes="x", transcript="we love the searzall. " * 3)
    add_episode(tmp_cfg, "m2", "Other", notes="Searzall in the notes")
    add_episode(tmp_cfg, "m3", "None", notes="nothing here")
    si.update_index(tmp_cfg)
    with si.open_readonly(tmp_cfg.search_db) as ic:
        got = related.episodes_mentioning(ic, ["Searzall"])
    assert [(m.episode_id, m.mentions, m.in_notes) for m in got] == [
        ("m2", 0, True),
        ("m1", 1, False),
    ]  # a show-notes hit counts like five passages
    assert related.episodes_mentioning(None, []) == []


# --- web ------------------------------------------------------------------------------------


@pytest.fixture
def web(tmp_cfg, blog, tmp_path):
    from conftest import write_config

    from ciw.config import load_config

    vocab = tmp_path / "vocab.txt"
    vocab.write_text("\n".join(VOCAB) + "\n")
    write_config(tmp_path, f"blog_db: {blog}\nasr:\n  vocabulary_file: {vocab}\n")
    cfg = load_config(tmp_path, use_dotenv=False)
    add_episode(cfg, "ep1", "Torch talk", notes="Today: the Searzall and carbonation.",
                transcript=make_transcript(make_words("we use the searzall and carbonation here")))  # fmt: skip
    add_episode(cfg, "ep2", "Nothing", notes="lowercase only")
    si.update_index(cfg)
    app = create_app(cfg)
    app.config["TESTING"] = True
    return app.test_client()


def soup(r):
    return BeautifulSoup(r.get_data(as_text=True), "html.parser")


def test_episode_page_lists_related_posts_with_matched_terms(web):
    s = soup(web.get("/episode/ep1"))
    box = s.select_one("#related-posts")
    items = box.select("li")
    assert {li["data-post-id"] for li in items} == {"1", "2", "4"}
    first = items[0]
    assert first.select_one("a")["href"] == f"/post/{first['data-post-id']}"
    assert "matched:" in first.text and "<mark>" in str(first)
    assert "Key terms:" in box.text and len(items) <= 5


def test_episode_without_key_terms_says_so(web):
    s = soup(web.get("/episode/ep2"))
    assert "No related posts found" in s.select_one("#related-posts").text


def test_related_failure_does_not_break_the_page(web, monkeypatch):
    monkeypatch.setattr(related, "related_posts", lambda *a, **k: 1 / 0)
    r = web.get("/episode/ep1")
    assert r.status_code == 200 and soup(r).select_one("#related-posts") is None


def test_post_page_shows_episodes_that_mention_its_topic(web):
    s = soup(web.get("/post/1"))  # "The Searzall Torch" -> Searzall
    box = s.select_one("#episode-mentions")
    assert box is not None and box.select_one("a")["href"].startswith("/episode/ep1?q=Searzall")
    assert "Mentioned in episodes" in box.text and "show notes" in box.text


def test_post_page_without_matches_is_untouched(web):
    for pid in (3, 6, 7):
        t = web.get(f"/post/{pid}").get_data(as_text=True)
        assert "episode-mentions" not in t and "Mentioned in episodes" not in t
        assert "</article>\n\n<!-- Prev / Next -->\n<nav" in t


def test_post_page_without_an_index_is_untouched(blog_cfg):
    t = create_app(blog_cfg).test_client().get("/post/1").get_data(as_text=True)
    assert "episode-mentions" not in t and "</article>\n\n<!-- Prev / Next -->\n<nav" in t
