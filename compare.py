#!/usr/bin/env python3
"""
Compare existing cooking_issues.db against a fresh scrape of sampled posts.

Samples ~20 posts spread across the archive (oldest, newest, middle, posts
known to have had encoding issues), re-fetches them live using the updated
scrape.py logic, and reports any differences in title, tags, body_text length,
and encoding quality.
"""

import sqlite3
import sys
import time
from pathlib import Path

sys.argv = ["scrape.py"]  # prevent argparse from firing on import
import scrape

EXISTING_DB = Path("cooking_issues.db")
TEMP_DB     = Path("cooking_issues_compare.db")
DELAY       = 0.5  # faster than production — this is a test run

scrape.DELAY = DELAY

def sample_urls(conn: sqlite3.Connection, n: int = 20) -> list[tuple[int, str, str]]:
    """Return (id, url, post_type) spread evenly across the archive."""
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    step  = max(1, total // n)
    rows  = conn.execute(
        "SELECT id, url, post_type FROM posts ORDER BY id"
    ).fetchall()
    sampled = rows[::step][:n]
    # Also include a few posts known to have had encoding issues
    known_bad = conn.execute(
        "SELECT id, url, post_type FROM posts WHERE id IN (2,3,4,7,8,12,18,66)"
    ).fetchall()
    seen = {r[0] for r in sampled}
    for r in known_bad:
        if r[0] not in seen:
            sampled.append(r)
            seen.add(r[0])
    return sampled


def scrape_sample(urls: list[tuple[int, str, str]]) -> sqlite3.Connection:
    """Fetch each URL fresh and store in a temp DB."""
    TEMP_DB.unlink(missing_ok=True)
    conn = sqlite3.connect(TEMP_DB)
    scrape.init_db(conn)

    for _id, url, post_type in urls:
        print(f"  Fetching: {url}")
        soup = scrape.fetch(url)
        if soup is None:
            print(f"    WARN: could not fetch")
            continue
        post = scrape.parse_post(soup, url, post_type)
        scrape.insert_post(conn, post)
        time.sleep(DELAY)

    return conn


def compare(existing: sqlite3.Connection, fresh: sqlite3.Connection) -> int:
    """Diff each freshly scraped post against the existing DB. Returns issue count."""
    existing.row_factory = sqlite3.Row
    fresh.row_factory    = sqlite3.Row

    fresh_rows = fresh.execute("SELECT * FROM posts ORDER BY url").fetchall()
    issues = 0

    for fr in fresh_rows:
        ex = existing.execute(
            "SELECT * FROM posts WHERE url = ?", (fr["url"],)
        ).fetchone()
        if ex is None:
            print(f"\n  NEW (not in existing DB): {fr['url']}")
            issues += 1
            continue

        diffs = []
        for field in ("title", "tags", "categories", "author"):
            ev, fv = (ex[field] or "").strip(), (fr[field] or "").strip()
            if ev != fv:
                diffs.append((field, ev, fv))

        # Body text: compare length and check for residual mojibake
        ex_len, fr_len = len(ex["body_text"] or ""), len(fr["body_text"] or "")
        len_diff = abs(ex_len - fr_len)
        if len_diff > 50:          # allow minor whitespace variation
            diffs.append(("body_text_len", str(ex_len), str(fr_len)))

        # Mojibake check: flag if either version still contains â€
        ex_mojo = "â€" in (ex["body_text"] or "")
        fr_mojo = "â€" in (fr["body_text"] or "")
        if ex_mojo:
            diffs.append(("encoding", "EXISTING has mojibake", ""))
        if fr_mojo:
            diffs.append(("encoding", "", "FRESH has mojibake"))

        if diffs:
            issues += len(diffs)
            print(f"\n  Post {ex['id']:3d}: {ex['title'][:55]}")
            for field, ev, fv in diffs:
                print(f"    [{field}]")
                if ev: print(f"      existing: {repr(ev[:120])}")
                if fv: print(f"      fresh:    {repr(fv[:120])}")
        else:
            print(f"  Post {ex['id']:3d}: OK  {ex['title'][:55]}")

    return issues


def main() -> None:
    existing = sqlite3.connect(EXISTING_DB)

    print(f"Sampling posts from {EXISTING_DB}…")
    sample = sample_urls(existing)
    print(f"Selected {len(sample)} posts to re-scrape\n")

    print("Re-scraping from live site…")
    fresh = scrape_sample(sample)

    print(f"\n{'─'*60}")
    print("Comparison results:")
    print(f"{'─'*60}")
    issues = compare(existing, fresh)
    print(f"\n{'─'*60}")

    fresh.close()
    existing.close()
    TEMP_DB.unlink(missing_ok=True)

    if issues == 0:
        print("All clear — no differences found.")
    else:
        print(f"{issues} difference(s) found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
