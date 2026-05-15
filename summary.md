# Cooking Issues Archive — Project Summary

## Goal

cookingissues.com is unreliable and hard to search. The goal was to scrape the entire site into a local SQLite database and build a clean web viewer that makes the content more discoverable than the original site.

---

## Phase 1: Site Investigation

Fetched and analyzed `cookingissues.com` before writing any code.

**Key findings:**
- WordPress blog, reverse-chronological post archive
- **44 pages** of posts, ~5 posts per page → ~220 posts total
- URL pattern: `cookingissues.com/YYYY/MM/DD/post-slug/` (standard WordPress date permalinks)
- Separate **Primers** section at `/primers/` — 8 long-form technical reference guides (hydrocolloids, liquid nitrogen, rotovap, transglutaminase, sous vide and sub-pages)
- Pagination stops at page 44; page 45 returns 404

**CSS selectors confirmed against live posts (not assumed):**
- Title: `h1.entry-title`
- Date: `time[datetime]` (ISO 8601 value in `datetime` attribute)
- Author: `[rel='author']` inside `.byline`
- Body: `.entry-content`
- Categories: `.cat-links a`
- Tags: `.tags-links a`

Most posts are tagged "Uncategorized" in categories. Tags are more useful — specific ingredient names, techniques, people (e.g. "agar", "searzall", "Jeffrey Steingarten").

---

## Phase 2: Scraper (`scrape.py`)

### Design decisions

**Resumable by design.** The scraper tracks which index pages have been fully processed (`scraped_index_pages` table) and skips posts that already exist (`posts` table, `url UNIQUE`). Safe to `Ctrl-C` and re-run.

**Two-pass architecture:**
1. Walk archive pages 1–44, extract post links, scrape each post
2. Scrape the known primer URLs directly (hardcoded list, since primers aren't linked from archive pages)

**Rate limiting:** 1.5 seconds between requests. Polite but not painfully slow (~8 min for a full run).

**Storage:** Single SQLite database (`cooking_issues.db`) with:
- `posts` table: url, title, date, author, categories, tags, body_html, body_text, post_type, scraped_at
- `posts_fts` virtual table (FTS5): indexes title + body_text + categories + tags with `content='posts'` for efficient full-text search
- `scraped_index_pages` table: tracks completed archive pages for resumability

**`body_html` vs `body_text`:** Both are stored. `body_html` is the raw `.entry-content` div (used for rendering in the viewer). `body_text` is extracted plain text (used for FTS indexing and card excerpts).

**FTS5 query escaping:** User queries are split into words and each word is wrapped in `"word"*` (prefix match) to support partial-word search and avoid FTS5 syntax errors from special characters.

### What was scraped

- **216 blog posts** (2009–2013)
- **8 primers** stored with `post_type='primer'`
- **44 index pages** marked as done
- Total: **224 documents**

---

## Phase 3: Web Viewer (`app.py` + templates)

### Tech stack

- **Flask** (lightweight Python web server, runs locally)
- **Jinja2** templates (bundled with Flask)
- **SQLite FTS5** for search (no external search engine needed)
- **No JS frameworks, no external CSS** — everything is inline in `base.html`; works offline

### Routes

| Route | Purpose |
|---|---|
| `GET /` | Homepage: hero search, primers grid, 15 recent posts |
| `GET /primers` | Primers landing page with descriptions |
| `GET /browse?page=N&tag=X` | Full chronological archive, 24/page, tag-filterable |
| `GET /post/<id>` | Single post reader with prev/next navigation |
| `GET /search?q=...` | Full-text search results with highlighted snippets |

### Design decisions

**Primers get visual distinction everywhere.** They're the most valuable reference content on the site and were effectively buried. Solution: dark forest green color scheme (`#1c2b1f`) for all primer cards, a "Reference Guide" badge, dedicated `/primers` page, and prominent placement on the homepage above the post grid.

**Homepage hierarchy:** Hero search → Primers → Recent posts. Discovery-first rather than chronological-first.

**Post reader uses Georgia serif** at 1.1rem / 1.85 line-height. The posts are long and technical; readable typography matters more than visual novelty. The rendered `body_html` (raw WordPress HTML) is styled via `.post-body > *` selectors, including WordPress-specific classes: `.alignright`, `.alignleft`, `.aligncenter`, `.wp-caption`, `.wp-caption-text`.

**Images load from the original site.** The scraper stores only text + HTML structure; images are referenced by their original `cookingissues.com` URLs. This means images require an internet connection but keeps the local DB small.

**Tag filtering on browse page.** Tags are more semantically useful than categories (which are almost all "Uncategorized"). Tags like "agar", "centrifuge", "ike-jime" are shown as filterable pills. The tag list excludes "Uncategorized" and is sorted alphabetically.

**Search uses FTS5 `snippet()`** to return 52-token excerpts with `<mark>` tags around matched terms. The Jinja template renders these with `| safe` since they come from our own controlled database. The accent color is `#b8460f` (burnt sienna) — chosen to evoke caramelization/heat.

**Prev/next navigation on posts** queries by `date` within the same `post_type`, so you don't accidentally navigate from a blog post into a primer.

### CSS architecture

Single `<style>` block in `base.html`. No external dependencies. Custom properties (`--accent`, `--primer-bg`, etc.) used throughout for consistency. Two width constraints:
- `--max-w: 1180px` — container for grids and browse lists
- `--read-w: 740px` — narrow column for post reading

Responsive breakpoint at 700px hides the nav (accessible via header search) and stacks the browse list vertically.

---

## File Layout

```
cooking_issues/
├── scrape.py          # scraper + CLI search tool
├── app.py             # Flask web server
├── cooking_issues.db  # SQLite database (224 posts)
├── summary.md         # this file
└── templates/
    ├── base.html      # shared layout + all CSS
    ├── index.html     # homepage
    ├── post.html      # single post reader
    ├── search.html    # search results
    ├── browse.html    # archive with pagination
    └── primers.html   # primers landing page
```

---

## Running

```bash
# Start the viewer
python3 app.py
# → http://localhost:5000

# Re-scrape (resumable, skips already-scraped posts)
python3 scrape.py

# CLI search (no server needed)
python3 scrape.py --search "agar clarification"
python3 scrape.py --stats
```

---

## Known Issues / Future Work

- **Encoding artifacts** in some `body_text` strings (`â€™` instead of `'`). This is a UTF-8/Latin-1 mojibake from the original site's HTTP response. `body_html` is unaffected, so the post reader looks correct; only excerpts/snippets in cards and search results may occasionally show garbled characters. Fix: re-scrape with explicit `response.encoding = 'utf-8'` override.

- **Images are not archived.** Posts with many images (photo diaries, centrifuge posts) won't display images offline. Future option: download images and rewrite `src` attributes.

- **No full-text search highlighting in browse/card excerpts.** Cards use the first N characters of `body_text`. A future improvement would be to store a manually curated excerpt or pull the first substantive paragraph.

- **Tag filter shows all tags simultaneously.** With ~100+ unique tags this gets long. Could be improved with a search-within-tags input or grouping by theme.

- **Comments not scraped.** WordPress comment threads are ignored. Some posts have active technical Q&A in comments that's worth preserving.

- **Site may have more posts.** The scraper stops at page 45 (404). If posts were added after the scrape, re-running `scrape.py` will pick them up (the FTS index is updated on insert).
