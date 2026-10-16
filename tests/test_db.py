"""Sanity checks on the tracked cooking_issues.db (read-only)."""

import pytest


def test_expected_tables_exist(db):
    names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"posts", "posts_fts", "scraped_index_pages"} <= names


def test_post_counts(db):
    counts = dict(db.execute("SELECT post_type, COUNT(*) FROM posts GROUP BY post_type").fetchall())
    assert counts == {"post": 216, "primer": 8}
    assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 224


def test_post_date_range(db):
    lo, hi = db.execute("SELECT MIN(date), MAX(date) FROM posts WHERE post_type='post'").fetchone()
    assert lo.startswith("2009-04-30")
    assert hi.startswith("2013-11-29")


@pytest.mark.parametrize(
    "column", ["title", "body_text", "body_html", "tags", "categories", "author"]
)
def test_no_mojibake_marker(db, column):
    n = db.execute(f"SELECT COUNT(*) FROM posts WHERE {column} LIKE '%â€%'").fetchone()[0]
    assert n == 0


def test_fts_index_answers_queries(db):
    n = db.execute("SELECT COUNT(*) FROM posts_fts WHERE posts_fts MATCH 'carbonation'").fetchone()[
        0
    ]
    assert n == 9


def test_every_post_has_title_and_url(db):
    bad = db.execute(
        "SELECT COUNT(*) FROM posts WHERE COALESCE(title,'')='' OR COALESCE(url,'')=''"
    ).fetchone()[0]
    assert bad == 0


def test_only_known_post_has_empty_body(db):
    """Oddity pinned: post 25 (MoFAD fundraiser) has an empty body_text."""
    ids = [r[0] for r in db.execute("SELECT id FROM posts WHERE COALESCE(body_text,'')=''")]
    assert ids == [25]
