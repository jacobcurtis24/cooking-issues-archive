#!/usr/bin/env python3
"""
Fix UTF-8 mojibake in cooking_issues.db.

Root cause: requests decoded the HTTP response as Windows-1252 (cp1252)
instead of UTF-8, because the server omitted a charset declaration. Each
UTF-8 byte sequence was misread as cp1252 characters, e.g.:
  UTF-8 bytes E2 80 99 → cp1252 chars â € ™ → stored as "â€™" instead of "'"

Fix: rebuild the original bytes from the cp1252 character values, then
re-decode as UTF-8. We process only non-ASCII runs (ASCII is identical in
both encodings), and use a complete byte→char reverse map that handles the
five undefined cp1252 bytes (0x81, 0x8D, 0x8F, 0x90, 0x9D) which Python
maps to their Latin-1 code-point equivalents on decode.

Fields fixed: title, author, categories, tags, body_text, body_html
"""

import argparse
import re
import sqlite3
from pathlib import Path

DB_PATH = Path("cooking_issues.db")
FIELDS = ["title", "author", "categories", "tags", "body_text", "body_html"]

# Build complete cp1252 reverse map: Unicode char → byte value.
# For the 5 undefined cp1252 bytes, Python's cp1252 codec yields the same
# code point as the byte value (Latin-1 fallback), so ord(char) == byte.
_CP1252_REVERSE: dict[str, int] = {}
for _b in range(256):
    try:
        _char = bytes([_b]).decode("cp1252")
    except UnicodeDecodeError:
        _char = chr(_b)          # undefined byte → Latin-1 equivalent char
    _CP1252_REVERSE[_char] = _b


def _fix_chunk(chunk: str) -> str:
    """Convert one run of non-ASCII mojibake chars back to UTF-8 text."""
    try:
        raw = bytes(_CP1252_REVERSE[c] for c in chunk)
        return raw.decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return chunk             # not mojibake — leave unchanged


def fix_mojibake(text: str) -> str:
    """Fix all mojibake runs in a string. Idempotent: already-correct text is unchanged."""
    if not text:
        return text
    return re.sub(r"[^\x00-\x7f]+", lambda m: _fix_chunk(m.group()), text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix cp1252 mojibake in cooking_issues.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing to DB")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, " + ", ".join(FIELDS) + " FROM posts ORDER BY id"
    ).fetchall()

    changed_posts = 0
    total_field_fixes = 0

    for row in rows:
        updates: dict[str, str] = {}
        for field in FIELDS:
            original = row[field]
            if not original:
                continue
            fixed = fix_mojibake(original)
            if fixed != original:
                updates[field] = fixed

        if not updates:
            continue

        changed_posts += 1
        total_field_fixes += len(updates)

        # Show a sample diff for each changed post
        sample_field = next(iter(updates))
        orig_text = row[sample_field]
        fixed_text = updates[sample_field]
        # Find first differing position and show surrounding context
        for i, (a, b) in enumerate(zip(orig_text, fixed_text)):
            if a != b:
                start = max(0, i - 20)
                end = min(len(orig_text), i + 40)
                print(f"  Post {row['id']:3d}  [{sample_field}]")
                print(f"    before: {repr(orig_text[start:end])}")
                end2 = min(len(fixed_text), i + 40)
                print(f"    after:  {repr(fixed_text[start:end2])}")
                break

        if not args.dry_run:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [row["id"]]
            conn.execute(f"UPDATE posts SET {set_clause} WHERE id = ?", values)

    if not args.dry_run and changed_posts > 0:
        print(f"\nRebuilding FTS index…")
        conn.execute("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")
        conn.commit()

    conn.close()

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Done.")
    print(f"  Posts with changes : {changed_posts}")
    print(f"  Field fixes total  : {total_field_fixes}")
    if args.dry_run:
        print("  Run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
