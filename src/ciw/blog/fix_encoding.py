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
import sqlite3
from pathlib import Path

from ciw.blog.encoding import fix_mojibake
from ciw.config import project_root

DB_PATH = project_root() / "cooking_issues.db"
FIELDS = ["title", "author", "categories", "tags", "body_text", "body_html"]


def run(db_path: Path | None = None, dry_run: bool = False) -> None:
    """Repair mojibake in ``db_path`` (default: the repo blog DB); ``dry_run`` writes nothing."""
    conn = sqlite3.connect(db_path or DB_PATH)
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
        for i, (a, b) in enumerate(zip(orig_text, fixed_text, strict=False)):
            if a != b:
                start = max(0, i - 20)
                end = min(len(orig_text), i + 40)
                print(f"  Post {row['id']:3d}  [{sample_field}]")
                print(f"    before: {repr(orig_text[start:end])}")
                end2 = min(len(fixed_text), i + 40)
                print(f"    after:  {repr(fixed_text[start:end2])}")
                break

        if not dry_run:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [row["id"]]
            conn.execute(f"UPDATE posts SET {set_clause} WHERE id = ?", values)

    if not dry_run and changed_posts > 0:
        print("\nRebuilding FTS index…")
        conn.execute("INSERT INTO posts_fts(posts_fts) VALUES('rebuild')")
        conn.commit()

    conn.close()

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done.")
    print(f"  Posts with changes : {changed_posts}")
    print(f"  Field fixes total  : {total_field_fixes}")
    if dry_run:
        print("  Run without --dry-run to apply changes.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix cp1252 mojibake in cooking_issues.db")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing to DB"
    )
    args = parser.parse_args()
    run(DB_PATH, args.dry_run)


if __name__ == "__main__":
    main()
