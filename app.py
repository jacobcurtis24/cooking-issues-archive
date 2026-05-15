#!/usr/bin/env python3
"""Cooking Issues local viewer.  Run: python3 app.py → http://localhost:5000"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, render_template, request

DB_PATH = Path("cooking_issues.db")
app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def safe_fts(q: str) -> str:
    words = re.findall(r"\w+", q)
    return " ".join(f'"{w}"*' for w in words)


@app.template_filter("fmt_date")
def fmt_date(s: str) -> str:
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s[:19])
        return dt.strftime("%-d %B %Y")
    except Exception:
        return s[:10]


@app.template_filter("short_excerpt")
def short_excerpt(text: str, n: int = 220) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


@app.route("/")
def index():
    db = get_db()
    primers = db.execute(
        "SELECT id, title, body_text FROM posts WHERE post_type='primer' ORDER BY title"
    ).fetchall()
    recent = db.execute(
        "SELECT id, title, date, tags, body_text "
        "FROM posts WHERE post_type='post' ORDER BY date DESC LIMIT 15"
    ).fetchall()
    total = db.execute("SELECT COUNT(*) FROM posts WHERE post_type='post'").fetchone()[0]
    db.close()
    return render_template("index.html", primers=primers, recent=recent, total=total)


@app.route("/post/<int:post_id>")
def post(post_id: int):
    db = get_db()
    p = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not p:
        abort(404)
    prev_p = db.execute(
        "SELECT id, title FROM posts WHERE post_type=? AND date < ? ORDER BY date DESC LIMIT 1",
        (p["post_type"], p["date"]),
    ).fetchone()
    next_p = db.execute(
        "SELECT id, title FROM posts WHERE post_type=? AND date > ? ORDER BY date ASC LIMIT 1",
        (p["post_type"], p["date"]),
    ).fetchone()
    db.close()
    return render_template("post.html", post=p, prev_post=prev_p, next_post=next_p)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results, count, error = [], 0, None
    if q:
        db = get_db()
        try:
            rows = db.execute(
                """SELECT p.id, p.title, p.date, p.tags, p.post_type,
                          snippet(posts_fts, 1, '<mark>', '</mark>', '…', 52) AS excerpt
                   FROM posts_fts
                   JOIN posts p ON posts_fts.rowid = p.id
                   WHERE posts_fts MATCH ?
                   ORDER BY rank
                   LIMIT 60""",
                (safe_fts(q),),
            ).fetchall()
            results, count = rows, len(rows)
        except Exception as e:
            error = str(e)
        db.close()
    return render_template("search.html", q=q, results=results, count=count, error=error)


@app.route("/primers")
def primers():
    db = get_db()
    rows = db.execute(
        "SELECT id, title, body_text FROM posts WHERE post_type='primer' ORDER BY title"
    ).fetchall()
    db.close()
    return render_template("primers.html", primers=rows)


@app.route("/browse")
def browse():
    page = max(1, int(request.args.get("page", 1)))
    tag = request.args.get("tag", "").strip()
    per_page = 24
    offset = (page - 1) * per_page

    db = get_db()
    if tag:
        total = db.execute(
            "SELECT COUNT(*) FROM posts WHERE post_type='post' AND tags LIKE ?", (f"%{tag}%",)
        ).fetchone()[0]
        posts = db.execute(
            "SELECT id, title, date, tags, body_text FROM posts "
            "WHERE post_type='post' AND tags LIKE ? ORDER BY date DESC LIMIT ? OFFSET ?",
            (f"%{tag}%", per_page, offset),
        ).fetchall()
    else:
        total = db.execute("SELECT COUNT(*) FROM posts WHERE post_type='post'").fetchone()[0]
        posts = db.execute(
            "SELECT id, title, date, tags, body_text FROM posts "
            "WHERE post_type='post' ORDER BY date DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()

    all_tags: set[str] = set()
    for row in db.execute("SELECT tags FROM posts WHERE post_type='post' AND tags != ''"):
        for t in row["tags"].split(","):
            t = t.strip()
            if t and t != "Uncategorized":
                all_tags.add(t)
    db.close()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "browse.html",
        posts=posts, page=page, total_pages=total_pages, total=total,
        tag=tag, all_tags=sorted(all_tags),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
