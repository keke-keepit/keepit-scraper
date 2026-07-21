# Keepit content scraper

Crawls `keepit.com` and `lp.keepit.com`, extracts clean page text and linked
PDF text, and writes change-aware output for a Claude Skill / RAG knowledge base.

## Files

| File | Purpose |
|------|---------|
| `scraper.py` | The scraper (single-fetch crawl, PDF extraction, change detection) |
| `requirements.txt` | Python dependencies |
| `.github/workflows/scrape.yml` | Runs the scraper on a schedule and commits results |

## Output

```
data/
├── scraped_content.json   # [{url, title, content, scraped_at}, ...]
├── manifest.json          # {url: content_hash} — lightweight state
├── refresh_log.json       # one record per run (see below)
└── pdfs/<name>-<hash>.txt # extracted text, one file per PDF
```

`scraped_content.json` matches the requested schema:

```json
[
  { "url": "https://...", "title": "Page Title",
    "content": "Clean text ...", "scraped_at": "2025-01-01T00:00:00+00:00" }
]
```

## How change detection works

A page's `scraped_at` is only updated when its title or content actually
changes (compared by SHA-256). Unchanged pages keep their previous entry
verbatim, so a run with no real changes produces **byte-identical** files and
therefore no git diff and no commit. PDF `.txt` files are only rewritten when
the extracted text changes.

## Run log

Every run appends one record to `data/refresh_log.json` (most recent
`LOG_KEEP = 200` kept), written on success *and* failure:

```json
{
  "run_at": "2026-07-21T00:00:00+00:00",
  "success": true,
  "duration_seconds": 12.3,
  "pages_total": 128,
  "pdfs_updated": 1,
  "changes": { "new": 2, "changed": 5, "unchanged": 121, "deleted": 0 },
  "changed_urls": ["[new] https://...", "[changed] https://...", "... (+N more)"],
  "error": null
}
```

Because this file changes on every run, the weekly workflow always has
something to commit — which is also what keeps the scheduled trigger from being
auto-disabled after 60 days of repository inactivity. The content files
themselves stay byte-stable when nothing changed, so a no-change run commits
only this log, not the whole knowledge base.

## Run locally

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python scraper.py
```

Tunable via environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `SCRAPER_USER_AGENT` | `KeepitKBBot/1.0 (+.../your-repo; ...)` | **Edit this** to point at your repo/contact |
| `SCRAPER_DELAY` | `0.5` | Seconds between requests (a larger robots.txt `crawl-delay` wins) |
| `SCRAPER_TIMEOUT` | `15` | Per-request timeout (s) |
| `SCRAPER_MAX_PAGES` | `0` | Safety cap on pages crawled; `0` = unlimited |

## robots.txt

The crawler reads each domain's `robots.txt`, obeys `Disallow` rules and
`crawl-delay`, and uses sitemaps listed there as crawl seeds. Set an honest
`SCRAPER_USER_AGENT` before running so site owners can identify the bot.

## GitHub Actions

The workflow runs on manual dispatch, weekly (`Sun 00:00 UTC`), and on pushes
that modify the scraper itself. It installs deps, runs the scraper, and commits
`data/` only if something changed. On failure it opens a labelled issue with the
tail of the run log. Requires the default `GITHUB_TOKEN` (no secrets needed);
the job grants itself `contents: write` and `issues: write`.

## Use in a Skill

```python
import json
docs = json.load(open("data/scraped_content.json"))
for d in docs:
    index(d["url"], d["title"], d["content"])   # your embedding/RAG step
```

## Known limitations

- JavaScript-rendered content isn't executed (no headless browser); pages that
  build their body client-side may extract little text.
- PDFs behind auth or generated via JS redirects aren't followed.
- Content extraction uses common selectors (`main`, `article`, …) with a body
  fallback; unusual layouts may include some boilerplate.
