# Cooking Issues Archive

A local archive of [cookingissues.com](https://cookingissues.com) — 216 blog posts and 8 technical primers by Dave Arnold, stored in a searchable SQLite database with a web viewer.

## Requirements

- Python 3.9+
- `pip install requests beautifulsoup4 lxml flask`

## Quick start

The database is already populated. Just start the viewer:

```bash
python3 app.py
```

Then open **http://localhost:5000**.

---

## Web viewer

```bash
python3 app.py
```

| Page | URL | Description |
|---|---|---|
| Home | `/` | Search bar, primers, 15 recent posts |
| Primers | `/primers` | All 8 technical reference guides |
| Browse | `/browse` | Full archive, 24 per page, filterable by tag |
| Post | `/post/<id>` | Full post with prev/next navigation |
| Search | `/search?q=…` | Full-text search with highlighted excerpts |

## CLI search

Search without starting the server:

```bash
python3 scrape.py --search "agar clarification"
python3 scrape.py --search "sous vide chicken" --limit 20
python3 scrape.py --stats
```

---

## Re-scraping

The scraper is resumable — already-scraped posts are skipped based on URL. Safe to re-run at any time:

```bash
python3 scrape.py
```

To scrape from scratch, delete `cooking_issues.db` first.

## Verifying the database

Fetches a sample of posts from the live site and compares them to the local database:

```bash
python3 compare.py
```

## Fixing encoding (if needed)

If the database develops encoding artifacts (`â€™` instead of `'`), run:

```bash
python3 fix_encoding.py          # apply fixes
python3 fix_encoding.py --dry-run  # preview without changing anything
```

This is applied automatically during scraping, so it's only needed if you're patching an existing database.

---

## Files

```
cooking_issues/
├── scrape.py          # scraper + CLI search
├── app.py             # Flask web viewer
├── fix_encoding.py    # standalone encoding fix
├── compare.py         # verify DB against live site
├── cooking_issues.db  # SQLite database (224 posts)
├── README.md          # this file
├── summary.md         # technical decisions and architecture
└── templates/         # Jinja2 HTML templates
```
