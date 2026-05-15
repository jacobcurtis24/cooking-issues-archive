#!/usr/bin/env python3
"""
Scrape cookingissues.com into a local SQLite database with full-text search.

Usage:
  python scrape.py            # scrape everything (resumable)
  python scrape.py --search "sous vide chicken"
  python scrape.py --search "agar" --limit 5
"""

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cookingissues.com"
DB_PATH = Path("cooking_issues.db")
DELAY = 1.5  # seconds between requests — be polite
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-archiver/1.0; +https://github.com/personal)"
}

PRIMER_URLS = [
    "https://cookingissues.com/primers/hydrocolloids-primer/",
    "https://cookingissues.com/primers/liquid-nitrogen-primer/",
    "https://cookingissues.com/primers/rotovap/",
    "https://cookingissues.com/primers/transglutaminase-aka-meat-glue/",
    "https://cookingissues.com/primers/sous-vide/",
    "https://cookingissues.com/primers/sous-vide/purdy-pictures-the-charts/",
    "https://cookingissues.com/primers/sous-vide/part-i-introduction-to-low-temperature-cooking-and-sous-vide/",
    "https://cookingissues.com/primers/sous-vide/part-ii-low-temperature-cooking-without-a-vacuum/",
]


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id          INTEGER PRIMARY KEY,
            url         TEXT UNIQUE NOT NULL,
            title       TEXT,
            date        TEXT,
            author      TEXT,
            categories  TEXT,
            tags        TEXT,
            body_html   TEXT,
            body_text   TEXT,
            post_type   TEXT DEFAULT 'post',  -- 'post' or 'primer'
            scraped_at  TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
            title,
            body_text,
            categories,
            tags,
            content='posts',
            content_rowid='id'
        );

        CREATE TABLE IF NOT EXISTS scraped_index_pages (
            url TEXT PRIMARY KEY
        );
    """)
    conn.commit()


def index_page_done(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM scraped_index_pages WHERE url = ?", (url,)
    ).fetchone() is not None


def mark_index_page_done(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("INSERT OR IGNORE INTO scraped_index_pages (url) VALUES (?)", (url,))
    conn.commit()


def post_exists(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM posts WHERE url = ?", (url,)
    ).fetchone() is not None


def insert_post(conn: sqlite3.Connection, post: dict) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO posts
            (url, title, date, author, categories, tags, body_html, body_text, post_type, scraped_at)
        VALUES
            (:url, :title, :date, :author, :categories, :tags, :body_html, :body_text, :post_type, :scraped_at)
    """, post)
    # Rebuild FTS index for this row
    row = conn.execute("SELECT id FROM posts WHERE url = ?", (post["url"],)).fetchone()
    if row:
        conn.execute("INSERT OR REPLACE INTO posts_fts(rowid, title, body_text, categories, tags) VALUES (?, ?, ?, ?, ?)",
                     (row[0], post["title"], post["body_text"], post["categories"], post["tags"]))
    conn.commit()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code == 404:
            print(f"  404: {url}")
            return None
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        print(f"  ERROR fetching {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def extract_post_links(soup: BeautifulSoup) -> list[str]:
    """Pull all individual post URLs from an archive/index page."""
    links = []
    # WordPress typically marks post links with class 'entry-title' or inside <article>
    for a in soup.select("article h1 a, article h2 a, .entry-title a, h1.entry-title a"):
        href = a.get("href", "")
        if href and urlparse(href).netloc in ("cookingissues.com", "www.cookingissues.com"):
            links.append(href)
    return list(dict.fromkeys(links))  # dedupe while preserving order


def clean_text(html_content) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = html_content.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_post(soup: BeautifulSoup, url: str, post_type: str = "post") -> dict:
    # Title
    title_el = (
        soup.select_one("h1.entry-title")
        or soup.select_one(".entry-title")
        or soup.select_one("h1")
    )
    title = title_el.get_text(strip=True) if title_el else ""

    # Date
    date = ""
    time_el = soup.select_one("time.entry-date, time[datetime]")
    if time_el:
        date = time_el.get("datetime", time_el.get_text(strip=True))
    else:
        # Fall back to URL date pattern
        m = re.search(r"/(\d{4}/\d{2}/\d{2})/", url)
        if m:
            date = m.group(1).replace("/", "-")

    # Author — prefer the <a rel="author"> inside .byline to avoid "by" prefix
    author = ""
    author_el = soup.select_one(".byline [rel='author'], .author a, [rel='author']")
    if not author_el:
        author_el = soup.select_one(".author, .byline")
    if author_el:
        author = author_el.get_text(strip=True)

    # Categories & tags
    categories = ", ".join(
        a.get_text(strip=True)
        for a in soup.select(".cat-links a, .entry-categories a")
    )
    tags = ", ".join(
        a.get_text(strip=True)
        for a in soup.select(".tags-links a, .entry-tags a")
    )

    # Body — prefer .entry-content, fall back to .post-content / article
    body_el = (
        soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
    )
    body_html = str(body_el) if body_el else ""
    body_text = clean_text(body_el) if body_el else ""

    return {
        "url": url,
        "title": title,
        "date": date,
        "author": author,
        "categories": categories,
        "tags": tags,
        "body_html": body_html,
        "body_text": body_text,
        "post_type": post_type,
        "scraped_at": datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def scrape_post(conn: sqlite3.Connection, url: str, post_type: str = "post") -> bool:
    """Fetch and store a single post. Returns True if newly scraped."""
    if post_exists(conn, url):
        return False
    print(f"  Scraping: {url}")
    soup = fetch(url)
    if soup is None:
        return False
    post = parse_post(soup, url, post_type)
    insert_post(conn, post)
    time.sleep(DELAY)
    return True


def scrape_all_posts(conn: sqlite3.Connection) -> None:
    """Walk all 44 archive pages, scraping every post."""
    page = 1
    while True:
        if page == 1:
            index_url = BASE_URL + "/"
        else:
            index_url = f"{BASE_URL}/page/{page}/"

        if index_page_done(conn, index_url):
            print(f"[page {page}] already done, skipping")
            page += 1
            # Stop after we pass the known max (44), with a cushion
            if page > 50:
                break
            continue

        print(f"[page {page}] fetching index: {index_url}")
        soup = fetch(index_url)
        if soup is None:
            print(f"  Could not fetch page {page}, stopping index crawl.")
            break

        links = extract_post_links(soup)
        if not links:
            print(f"  No post links found on page {page} — reached the end.")
            break

        print(f"  Found {len(links)} posts")
        for link in links:
            scrape_post(conn, link, post_type="post")

        mark_index_page_done(conn, index_url)
        time.sleep(DELAY)
        page += 1


def scrape_primers(conn: sqlite3.Connection) -> None:
    """Scrape the known primer pages."""
    print("\n--- Scraping primers ---")
    for url in PRIMER_URLS:
        scrape_post(conn, url, post_type="primer")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(conn: sqlite3.Connection, query: str, limit: int = 10) -> None:
    rows = conn.execute("""
        SELECT p.title, p.url, p.date, p.categories, p.tags,
               snippet(posts_fts, 1, '[', ']', '...', 32) AS excerpt
        FROM posts_fts
        JOIN posts p ON posts_fts.rowid = p.id
        WHERE posts_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (query, limit)).fetchall()

    if not rows:
        print("No results found.")
        return

    for i, (title, url, date, cats, tags, excerpt) in enumerate(rows, 1):
        print(f"\n{'='*70}")
        print(f"{i}. {title}")
        print(f"   {url}")
        if date:
            print(f"   Date: {date}")
        if cats:
            print(f"   Categories: {cats}")
        if tags:
            print(f"   Tags: {tags}")
        print(f"\n   {excerpt}")

    print(f"\n{'='*70}")
    print(f"{len(rows)} result(s) for '{query}'")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    primers = conn.execute("SELECT COUNT(*) FROM posts WHERE post_type='primer'").fetchone()[0]
    pages_done = conn.execute("SELECT COUNT(*) FROM scraped_index_pages").fetchone()[0]
    print(f"Database: {DB_PATH}")
    print(f"  Total posts scraped : {total} ({primers} primers)")
    print(f"  Index pages done    : {pages_done}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Cooking Issues scraper & search")
    parser.add_argument("--search", "-s", metavar="QUERY", help="Full-text search query")
    parser.add_argument("--limit", "-n", type=int, default=10, help="Max search results (default 10)")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.stats:
        print_stats(conn)
        conn.close()
        return

    if args.search:
        search(conn, args.search, args.limit)
        conn.close()
        return

    # Default: scrape
    print(f"Starting scrape — saving to {DB_PATH}")
    print("(Re-run at any time; already-scraped posts are skipped)\n")
    scrape_all_posts(conn)
    scrape_primers(conn)
    print("\nDone.")
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
