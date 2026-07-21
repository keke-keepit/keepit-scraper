#!/usr/bin/env python3
"""
Keepit Knowledge Base Builder
=============================
Builds and incrementally refreshes a local, citation-ready knowledge base
from the Keepit websites, driven entirely by their published sitemaps.

It replaces the older four-script pipeline (wget mirror + download_pdfs +
keepit_scraper + merge_pages + orchestrator). No wget, no merge step.

WHAT IT DOES
------------
1. Reads every sitemap (www.keepit.com + lp.keepit.com), following sitemap
   indexes and gz-compressed sitemaps automatically.
2. Filters out non-English, cookie/privacy/legal/careers, and preview URLs.
3. Compares each URL against a manifest and only re-fetches pages whose
   <lastmod> changed (or whose content hash changed when no lastmod exists).
4. Extracts clean article text from each page (trafilatura).
5. Finds PDF links on fetched pages, downloads new ones, and extracts their
   FULL text (pymupdf) into a searchable .txt with a Source URL header.
6. Deletes files for pages that have dropped out of the sitemap (latest-only).
7. Writes manifest.json (state) and changelog.txt (human-readable run log).

Each output file starts with a "Source URL:" header so answers can cite it.

USAGE
-----
    pip install requests trafilatura pymupdf
    python build_kb.py                 # incremental refresh into ./kb
    python build_kb.py --out /path/kb  # choose output location
    python build_kb.py --full          # ignore manifest, rebuild everything
    python build_kb.py --limit 25      # cap fetches (testing)
    python build_kb.py -v              # verbose

OUTPUT (inside --out, default ./kb)
-----------------------------------
    pages/<slug>.txt      one clean text file per web page
    pdfs/<slug>.txt       full extracted text of each PDF
    manifest.json         url -> lastmod, hash, file, last_seen
    changelog.txt         log of added / updated / removed per run
"""

import argparse
import gzip
import hashlib
import json
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote
import xml.etree.ElementTree as ET

# ── dependency checks ─────────────────────────────────────────────────────────

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")
try:
    import trafilatura
except ImportError:
    sys.exit("Missing dependency. Run:  pip install trafilatura")
try:
    import fitz  # pymupdf
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pymupdf")


# ── configuration ─────────────────────────────────────────────────────────────

SITEMAPS = [
    "https://www.keepit.com/sitemap.xml",
    "https://lp.keepit.com/sitemap.xml",
]

# Non-English language sections to skip (path starts with /xx/)
LANGUAGE_CODES = ["de", "fr", "nl", "da", "sv", "nb", "no", "fi",
                  "es", "it", "pt", "pl", "ja", "zh", "ko"]

# URL path fragments to skip everywhere
SKIP_PATTERNS = [
    r"/cookie", r"/privacy", r"/legal", r"/gdpr",
    r"/careers", r"/jobs",
    r"/cdn-cgi", r"/_hcms/", r"/hs/manage-preferences",
    r"/hs/preferences-center", r"/sample-",
]

USER_AGENT = (
    "KeepitKB/2.0 (+internal knowledge-base builder; "
    "Mozilla/5.0 compatible)"
)

MIN_WAIT, MAX_WAIT = 1.0, 2.5   # polite delay between network requests
REQUEST_TIMEOUT = 30
MAX_RETRIES = 2

PDF_LINK_RE = re.compile(r'https?://[^\s\'"<>]+?\.pdf', re.IGNORECASE)
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


# ── small helpers ───────────────────────────────────────────────────────────--

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def polite_sleep() -> None:
    time.sleep(random.uniform(MIN_WAIT, MAX_WAIT))


def make_session() -> "requests.Session":
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch(session, url: str, verbose: bool = False):
    """GET a URL with simple retries. Returns requests.Response or None."""
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r
            if verbose:
                print(f"      HTTP {r.status_code} for {url}")
        except Exception as e:
            if verbose:
                print(f"      error ({attempt}) {url}: {e}")
        if attempt <= MAX_RETRIES:
            time.sleep(1.5 * attempt)
    return None


def should_skip(url: str) -> bool:
    path = urlparse(url).path
    for code in LANGUAGE_CODES:
        if re.match(rf"^/{code}(/|$)", path, re.IGNORECASE):
            return True
    return any(re.search(p, url, re.IGNORECASE) for p in SKIP_PATTERNS)


def slugify(url: str, ext: str) -> str:
    parsed = urlparse(url)
    base = (parsed.netloc + parsed.path).replace("www.", "")
    safe = re.sub(r"[^\w\-]", "_", base)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return (safe or "home") + ext


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


# ── sitemap parsing ─────────────────────────────────────────────────────────--

def parse_sitemap_bytes(raw: bytes):
    """Yield (loc, lastmod) from a urlset, or (loc, None) marked as index."""
    # transparently gunzip if needed
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return
    tag = root.tag.lower()
    if tag.endswith("sitemapindex"):
        for sm in root.findall(f"{SITEMAP_NS}sitemap"):
            loc = sm.findtext(f"{SITEMAP_NS}loc")
            if loc:
                yield ("INDEX", loc.strip())
    else:  # urlset
        for u in root.findall(f"{SITEMAP_NS}url"):
            loc = u.findtext(f"{SITEMAP_NS}loc")
            if not loc:
                continue
            lastmod = u.findtext(f"{SITEMAP_NS}lastmod")
            yield (loc.strip(), (lastmod or "").strip())


def collect_urls(session, roots, verbose=False) -> dict:
    """Walk all sitemaps (following indexes). Returns {url: lastmod}."""
    seen_sitemaps = set()
    queue = list(roots)
    urls = {}
    while queue:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        print(f"  sitemap: {sm_url}")
        r = fetch(session, sm_url, verbose)
        if not r:
            print(f"    (could not fetch)")
            continue
        for a, b in parse_sitemap_bytes(r.content):
            if a == "INDEX":
                queue.append(b)
            else:
                if not should_skip(a):
                    urls[a] = b  # lastmod (may be "")
        polite_sleep()
    print(f"  {len(urls)} candidate page URLs after filtering")
    return urls


# ── extraction ────────────────────────────────────────────────────────────---

def extract_page_text(html: str) -> str:
    return trafilatura.extract(
        html,
        include_links=False,
        include_images=False,
        include_tables=True,
    ) or ""


def page_file_body(url: str, lastmod: str, text: str) -> str:
    return (
        f"Source URL: {url}\n"
        f"Content type: Web page\n"
        f"Last modified: {lastmod or 'unknown'}\n"
        f"Retrieved: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"\n{'=' * 60}\n\n"
        f"{text.strip()}\n"
    )


def extract_pdf_text(pdf_bytes: bytes) -> tuple:
    """Return (full_text, num_pages, title)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    parts = [doc[i].get_text("text").strip() for i in range(doc.page_count)]
    title = (doc.metadata or {}).get("title", "").strip()
    pages = doc.page_count
    doc.close()
    return "\n\n".join(p for p in parts if p), pages, title


def pdf_file_body(url, title, pages, text) -> str:
    return (
        f"Source URL: {url}\n"
        f"Content type: PDF document\n"
        f"Title: {title or 'unknown'}\n"
        f"Pages: {pages}\n"
        f"Retrieved: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"\n{'=' * 60}\n\n"
        f"{text.strip()}\n"
    )


def clean_pdf_url(raw: str) -> str:
    m = PDF_LINK_RE.search(raw.replace("&amp;", "&"))
    return m.group(0) if m else ""


# ── grouped output for SharePoint upload ──────────────────────────────────────

def group_for(url: str) -> str:
    """Classify a page URL into one topic group by its path."""
    p = urlparse(url).path.lower()
    if p.startswith("/services") or "/backup" in p or "/mcp" in p:
        return "services"
    if p.startswith(("/help", "/docs", "/support", "/kb")):
        return "help"
    if p.startswith("/blog"):
        return "blog"
    if p.startswith("/customers") or "case-stud" in p:
        return "customers"
    if p.startswith("/partners"):
        return "partners"
    if p.startswith(("/press", "/news")):
        return "press"
    if p.startswith("/resources") or "whitepaper" in p or "webinar" in p or "ebook" in p:
        return "resources"
    if p.startswith("/security") or "compliance" in p or "trust" in p or "certification" in p:
        return "security"
    return "company"


def write_grouped_upload(out: Path, entries: dict) -> tuple:
    """Regenerate out/upload/ : a few grouped .txt files (per-page Source URL
    headers preserved inside) that the owner copies into SharePoint Keepit-KB.
    Kept to ~10 files so the skill's connector-mirror is a handful of reads."""
    upload = out / "upload"
    if upload.exists():
        shutil.rmtree(upload)
    upload.mkdir(parents=True, exist_ok=True)

    groups: dict = {}
    docs = []
    for url, ent in entries.items():
        f = out / ent.get("file", "")
        if not f.exists():
            continue
        if ent.get("type") == "page":
            groups.setdefault(group_for(url), []).append((url, f))
        elif ent.get("type") == "pdf":
            docs.append((url, f))

    def bundle(title, count_label, count, items):
        parts = [f"KEEPIT KNOWLEDGE BASE — {title}",
                 f"Generated: {datetime.now().strftime('%Y-%m-%d')}",
                 f"{count_label}: {count}", "=" * 60, ""]
        for _url, f in sorted(items):
            parts.append(f.read_text(encoding="utf-8", errors="ignore").strip())
            parts.append("\n" + "-" * 60 + "\n")
        return "\n".join(parts)

    written = 0
    for g, items in sorted(groups.items()):
        (upload / f"keepit_{g}.txt").write_text(
            bundle(g.upper(), "Pages", len(items), items), encoding="utf-8")
        written += 1
    if docs:
        (upload / "keepit_documents.txt").write_text(
            bundle("DOCUMENTS (PDFs)", "Documents", len(docs), docs), encoding="utf-8")
        written += 1
    return upload, written


# ── main build ────────────────────────────────────────────────────────────---

def main() -> int:
    ap = argparse.ArgumentParser(description="Keepit Knowledge Base Builder")
    ap.add_argument("--out", default="~/KeepitKB/kb",
                    help="output directory (default ~/KeepitKB/kb)")
    ap.add_argument("--full", action="store_true", help="rebuild everything, ignore manifest")
    ap.add_argument("--limit", type=int, default=0, help="max pages to fetch (0 = no cap)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    out = Path(args.out).expanduser().resolve()
    pages_dir = out / "pages"
    pdfs_dir = out / "pdfs"
    manifest_path = out / "manifest.json"
    changelog_path = out / "changelog.txt"
    pages_dir.mkdir(parents=True, exist_ok=True)
    pdfs_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  Keepit Knowledge Base Builder")
    print("=" * 60)
    print(f"  Output: {out}")

    manifest = {"entries": {}}
    if manifest_path.exists() and not args.full:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            print("  (manifest unreadable — starting fresh)")
    entries = manifest.get("entries", {})

    session = make_session()

    print("\nReading sitemaps...")
    current_urls = collect_urls(session, SITEMAPS, args.verbose)

    added = updated = unchanged = failed = 0
    pdf_added = 0
    seen_pdf_urls = set()

    print("\nProcessing pages...")
    page_items = sorted(current_urls.items())
    if args.limit:
        page_items = page_items[: args.limit]

    for i, (url, lastmod) in enumerate(page_items, 1):
        prev = entries.get(url)
        # Skip untouched pages: same lastmod and file still present
        if prev and prev.get("type") == "page" and lastmod and prev.get("lastmod") == lastmod \
           and (out / prev.get("file", "")).exists():
            prev["last_seen"] = now_iso()
            unchanged += 1
            continue

        r = fetch(session, url, args.verbose)
        polite_sleep()
        if not r:
            failed += 1
            continue

        text = extract_page_text(r.text)
        if len(text.strip()) < 100:
            if args.verbose:
                print(f"    thin/empty, skipped: {url}")
            continue

        h = content_hash(text)
        fname = f"pages/{slugify(url, '.txt')}"
        is_new = prev is None
        if prev and prev.get("hash") == h and (out / fname).exists():
            unchanged += 1
        else:
            (out / fname).write_text(page_file_body(url, lastmod, text), encoding="utf-8")
            added += 1 if is_new else 0
            updated += 0 if is_new else 1
            if args.verbose:
                print(f"    {'NEW ' if is_new else 'UPD '}{url}")

        entries[url] = {
            "type": "page", "lastmod": lastmod, "hash": h,
            "file": fname, "last_seen": now_iso(),
        }

        # discover PDF links on this page
        for raw in PDF_LINK_RE.findall(r.text):
            pdf_url = clean_pdf_url(raw)
            if pdf_url and pdf_url not in seen_pdf_urls:
                seen_pdf_urls.add(pdf_url)

    # ── PDFs ────────────────────────────────────────────────────────────────
    print(f"\nProcessing PDFs ({len(seen_pdf_urls)} unique links discovered)...")
    for pdf_url in sorted(seen_pdf_urls):
        prev = entries.get(pdf_url)
        fname = f"pdfs/{slugify(pdf_url, '.txt')}"
        if prev and prev.get("type") == "pdf" and (out / fname).exists() and not args.full:
            prev["last_seen"] = now_iso()
            continue
        r = fetch(session, pdf_url, args.verbose)
        polite_sleep()
        if not r or "pdf" not in r.headers.get("content-type", "").lower() \
           and not pdf_url.lower().endswith(".pdf"):
            failed += 1
            continue
        try:
            text, pages, title = extract_pdf_text(r.content)
        except Exception as e:
            if args.verbose:
                print(f"    could not parse PDF {pdf_url}: {e}")
            failed += 1
            continue
        if len(text.strip()) < 50:
            continue
        (out / fname).write_text(pdf_file_body(pdf_url, title, pages, text), encoding="utf-8")
        entries[pdf_url] = {
            "type": "pdf", "hash": content_hash(text),
            "file": fname, "last_seen": now_iso(),
        }
        pdf_added += 1
        if args.verbose:
            print(f"    PDF {title or pdf_url} ({pages}p)")

    # ── prune pages no longer in the sitemap (latest-only policy) ────────────
    removed = 0
    if not args.limit:  # never prune during a capped test run
        live = set(current_urls.keys())
        for url in list(entries.keys()):
            ent = entries[url]
            if ent.get("type") == "page" and url not in live:
                f = out / ent.get("file", "")
                if f.exists():
                    f.unlink()
                del entries[url]
                removed += 1

    # ── save state ───────────────────────────────────────────────────────────
    manifest = {"generated": now_iso(), "entries": entries}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    # ── grouped files for SharePoint upload ───────────────────────────────────
    upload_dir, upload_files = write_grouped_upload(out, entries)

    page_count = sum(1 for e in entries.values() if e.get("type") == "page")
    pdf_count = sum(1 for e in entries.values() if e.get("type") == "pdf")

    with open(changelog_path, "a", encoding="utf-8") as log:
        log.write(f"\n{'=' * 60}\n{now_iso()}"
                  f"{'  [--full]' if args.full else ''}"
                  f"{'  [--limit %d]' % args.limit if args.limit else ''}\n")
        log.write(f"  added {added}  updated {updated}  unchanged {unchanged}  "
                  f"removed {removed}  pdfs+ {pdf_added}  failed {failed}\n")
        log.write(f"  totals: {page_count} pages, {pdf_count} pdfs\n")

    print("\n" + "=" * 60)
    print("  DONE")
    print(f"  added {added} | updated {updated} | unchanged {unchanged} | "
          f"removed {removed} | pdfs+ {pdf_added} | failed {failed}")
    print(f"  KB now holds {page_count} pages + {pdf_count} pdfs")
    print(f"  cache : {out}")
    print(f"  UPLOAD: {upload_dir}  ({upload_files} grouped files)")
    print(f"  --> copy the contents of the UPLOAD folder into")
    print(f"      SharePoint: KeKeSite / Shared Documents / Keepit-KB")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
