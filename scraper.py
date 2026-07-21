#!/usr/bin/env python3
"""
Web scraper for the Keepit knowledge base (keepit.com, lp.keepit.com).

Design goals (learned from the previous version's mistakes):
  * Each URL is fetched exactly ONCE. Content, internal links and PDF links are
    all extracted from that single response.
  * robots.txt is actually respected (Disallow rules + crawl-delay), and the
    User-Agent identifies the bot honestly.
  * Output is deterministic and change-aware: a page keeps its original
    `scraped_at` until its content genuinely changes, so unchanged runs produce
    byte-identical files and therefore no git diff / no redundant commit.

Outputs:
  data/scraped_content.json   list of {url, title, content, scraped_at}
  data/manifest.json          {url: content_hash} lightweight state
  data/pdfs/<slug>.txt         extracted PDF text (one file per PDF)
"""

import io
import os
import re
import sys
import json
import time
import hashlib
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
import pdfplumber

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DOMAINS = ["keepit.com", "lp.keepit.com"]
START_URLS = [f"https://{d}/" for d in DOMAINS]

# Honest, identifiable User-Agent. Edit the contact URL to point at your repo.
USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "KeepitKBBot/1.0 (+https://github.com/keke-keepit/keepit-kb; content ingestion)",
)

DEFAULT_DELAY = float(os.environ.get("SCRAPER_DELAY", "0.5"))  # seconds/request
TIMEOUT = int(os.environ.get("SCRAPER_TIMEOUT", "15"))
# Safety cap so an unbounded crawl can't run forever. 0 = unlimited.
MAX_PAGES = int(os.environ.get("SCRAPER_MAX_PAGES", "0"))

DATA_DIR = Path("data")
CONTENT_FILE = DATA_DIR / "scraped_content.json"
MANIFEST_FILE = DATA_DIR / "manifest.json"
PDF_DIR = DATA_DIR / "pdfs"
LOG_FILE = DATA_DIR / "refresh_log.json"   # one record appended per run

LOG_KEEP = 200        # keep only the most recent N run records
LOG_URL_CAP = 25      # max changed URLs listed per record

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".json", ".xml", ".zip", ".gz", ".tar",
    ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".eot",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scraper")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def content_hash(title: str, content: str) -> str:
    """Hash of the material we care about. Title changes count as changes."""
    return hashlib.sha256(f"{title}\n{content}".encode("utf-8")).hexdigest()


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def in_scope(url: str) -> bool:
    return any(domain_of(url) == d or domain_of(url).endswith("." + d) for d in DOMAINS)


def normalize(url: str) -> str:
    """Drop fragments and collapse a trailing slash so /x and /x/ don't dupe."""
    url, _ = urldefrag(url)
    if url.endswith("/") and urlparse(url).path != "/":
        url = url[:-1]
    return url


def is_page_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not path.endswith(SKIP_EXTENSIONS) and not path.endswith(".pdf")


def slug_for(url: str) -> str:
    """Human-readable, collision-resistant filename for a PDF URL."""
    name = Path(urlparse(url).path).stem or "document"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "document"
    short = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{name}-{short}.txt"


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #

class Robots:
    """One RobotFileParser per domain, fetched with our own session/UA."""

    def __init__(self, session: requests.Session):
        self.session = session
        self.parsers: Dict[str, Optional[RobotFileParser]] = {}

    def _parser(self, url: str) -> Optional[RobotFileParser]:
        dom = domain_of(url)
        if dom in self.parsers:
            return self.parsers[dom]
        rp = RobotFileParser()
        robots_url = f"https://{urlparse(url).netloc}/robots.txt"
        try:
            resp = self.session.get(robots_url, timeout=TIMEOUT)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # no robots.txt -> allow all
        except requests.RequestException as e:
            log.warning("Could not read %s: %s (allowing all)", robots_url, e)
            rp = None
        self.parsers[dom] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._parser(url)
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def delay(self, url: str) -> float:
        rp = self._parser(url)
        if rp is None:
            return DEFAULT_DELAY
        cd = rp.crawl_delay(USER_AGENT)
        return max(DEFAULT_DELAY, float(cd)) if cd else DEFAULT_DELAY


# --------------------------------------------------------------------------- #
# Sitemaps
# --------------------------------------------------------------------------- #

def sitemap_seeds(session: requests.Session, robots: Robots) -> Set[str]:
    """Collect page URLs advertised in sitemaps (recursing into sitemap indexes)."""
    seeds: Set[str] = set()
    to_read: deque = deque()
    seen: Set[str] = set()

    for base in START_URLS:
        rp = robots._parser(base)
        if rp is not None:
            for sm in rp.site_maps() or []:
                to_read.append(sm)
        for guess in ("/sitemap.xml", "/sitemap_index.xml"):
            to_read.append(urljoin(base, guess))

    while to_read:
        sm_url = to_read.popleft()
        if sm_url in seen:
            continue
        seen.add(sm_url)
        try:
            resp = session.get(sm_url, timeout=TIMEOUT)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.content, "xml")
            # Nested sitemap index
            for loc in soup.select("sitemap > loc"):
                to_read.append(loc.text.strip())
            # Actual URLs
            for loc in soup.select("url > loc"):
                u = normalize(loc.text.strip())
                if in_scope(u) and is_page_url(u):
                    seeds.add(u)
            time.sleep(DEFAULT_DELAY)
        except requests.RequestException as e:
            log.warning("Sitemap error %s: %s", sm_url, e)

    log.info("Sitemaps contributed %d seed URLs", len(seeds))
    return seeds


# --------------------------------------------------------------------------- #
# Page parsing (single fetch -> content + links + pdf links)
# --------------------------------------------------------------------------- #

def parse_html(html: str, url: str) -> Tuple[Optional[dict], Set[str], Set[str]]:
    """Return (page_dict or None, internal_page_links, pdf_links)."""
    soup = BeautifulSoup(html, "html.parser")

    links: Set[str] = set()
    pdfs: Set[str] = set()
    for a in soup.find_all("a", href=True):
        target = normalize(urljoin(url, a["href"].strip()))
        if not in_scope(target):
            continue
        if urlparse(target).path.lower().endswith(".pdf"):
            pdfs.add(target)
        elif is_page_url(target):
            links.add(target)

    # Extract readable content from a copy with noise removed.
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()

    title = soup.title.get_text(strip=True) if soup.title else url
    main = None
    for sel in ("main", "article", '[role="main"]', ".content", ".page-content"):
        main = soup.select_one(sel)
        if main:
            break
    root = main or soup.body or soup
    text = "\n".join(
        line for line in
        (ln.strip() for ln in root.get_text("\n").splitlines())
        if line
    )

    page = None
    if len(text) >= 50:
        page = {"url": url, "title": title, "content": text}
    else:
        log.debug("Skipping thin page %s (%d chars)", url, len(text))
    return page, links, pdfs


def crawl(session: requests.Session, robots: Robots,
          seeds: Set[str]) -> Tuple[List[dict], Set[str]]:
    """BFS crawl. Each URL fetched once. Returns (pages, pdf_urls)."""
    queue: deque = deque(sorted(seeds) or START_URLS)
    for s in START_URLS:
        queue.append(normalize(s))
    visited: Set[str] = set()
    pages: List[dict] = []
    pdf_urls: Set[str] = set()

    while queue:
        url = normalize(queue.popleft())
        if url in visited:
            continue
        visited.add(url)

        if not robots.allowed(url):
            log.debug("robots.txt disallows %s", url)
            continue

        try:
            resp = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning("Fetch failed %s: %s", url, e)
            continue

        time.sleep(robots.delay(url))

        if resp.status_code != 200:
            continue
        if "text/html" not in resp.headers.get("Content-Type", ""):
            continue

        page, links, pdfs = parse_html(resp.text, url)
        if page:
            pages.append(page)
        pdf_urls.update(pdfs)
        for link in links:
            if link not in visited:
                queue.append(link)

        if len(pages) % 25 == 0:
            log.info("Crawled %d pages, %d queued, %d PDFs seen",
                     len(pages), len(queue), len(pdf_urls))

        if MAX_PAGES and len(pages) >= MAX_PAGES:
            log.warning("Hit MAX_PAGES=%d, stopping crawl", MAX_PAGES)
            break

    log.info("Crawl finished: %d pages, %d PDFs", len(pages), len(pdf_urls))
    return pages, pdf_urls


# --------------------------------------------------------------------------- #
# PDFs (stable output: only rewritten when the extracted text changes)
# --------------------------------------------------------------------------- #

def process_pdfs(session: requests.Session, robots: Robots,
                 pdf_urls: Set[str]) -> int:
    changed = 0
    for url in sorted(pdf_urls):
        if not robots.allowed(url):
            continue
        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            time.sleep(robots.delay(url))
            parts = []
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                for i, pg in enumerate(pdf.pages, 1):
                    txt = pg.extract_text()
                    if txt:
                        parts.append(f"--- Page {i} ---\n{txt}")
        except Exception as e:  # pdfplumber raises various types
            log.warning("PDF failed %s: %s", url, e)
            continue

        if not parts:
            continue
        # No timestamp in the body -> stable across runs.
        body = f"Source: {url}\n\n" + "\n\n".join(parts) + "\n"
        path = PDF_DIR / slug_for(url)
        if path.exists() and path.read_text(encoding="utf-8") == body:
            continue
        path.write_text(body, encoding="utf-8")
        changed += 1
        log.info("Wrote PDF text: %s", path.name)
    log.info("PDFs updated: %d", changed)
    return changed


# --------------------------------------------------------------------------- #
# Change-aware reconciliation
# --------------------------------------------------------------------------- #

def load_previous() -> Dict[str, dict]:
    if not CONTENT_FILE.exists():
        return {}
    try:
        with open(CONTENT_FILE, encoding="utf-8") as f:
            return {e["url"]: e for e in json.load(f)}
    except (json.JSONDecodeError, KeyError, OSError) as e:
        log.warning("Could not read previous content (%s); treating as empty", e)
        return {}


def reconcile(pages: List[dict], previous: Dict[str, dict]
              ) -> Tuple[List[dict], Dict[str, str], dict]:
    """Preserve scraped_at for unchanged pages; only stamp changed/new ones.

    The returned report lists the URLs that were new/changed/deleted (not just
    counts) so the run log can say *what* changed.
    """
    now = datetime.now(timezone.utc).isoformat()
    final: List[dict] = []
    manifest: Dict[str, str] = {}
    report = {"new": [], "changed": [], "deleted": [], "unchanged": 0}

    for page in pages:
        url = page["url"]
        h = content_hash(page["title"], page["content"])
        manifest[url] = h
        prev = previous.get(url)
        if prev and content_hash(prev.get("title", ""), prev.get("content", "")) == h:
            final.append(prev)            # keep old entry (and its scraped_at)
            report["unchanged"] += 1
        else:
            final.append({**page, "scraped_at": now})
            (report["changed"] if prev else report["new"]).append(url)

    report["deleted"] = sorted(set(previous) - {p["url"] for p in pages})
    report["new"].sort()
    report["changed"].sort()
    final.sort(key=lambda e: e["url"])
    manifest = dict(sorted(manifest.items()))
    return final, manifest, report


def write_json(path: Path, obj) -> None:
    # Deterministic: sorted upstream, fixed separators, trailing newline.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def append_run_log(started_at: datetime, success: bool, duration: float,
                   result: dict, error: Optional[str]) -> None:
    """Append one record to data/refresh_log.json (kept to the last LOG_KEEP).

    Written on every run, success or failure, so the file always answers
    'when did we last refresh, what changed, and did it work?'. Because it
    changes each run it also serves as the commit that keeps the scheduled
    workflow from being auto-disabled after 60 days of inactivity.
    """
    DATA_DIR.mkdir(exist_ok=True)
    entries: list = []
    if LOG_FILE.exists():
        try:
            loaded = json.loads(LOG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                entries = loaded
        except (json.JSONDecodeError, OSError):
            entries = []

    changed = result.get("changed_urls", [])
    shown = changed[:LOG_URL_CAP]
    if len(changed) > LOG_URL_CAP:
        shown.append(f"... (+{len(changed) - LOG_URL_CAP} more)")

    entries.append({
        "run_at": started_at.isoformat(),
        "success": success,
        "duration_seconds": round(duration, 1),
        "pages_total": result.get("pages_total", 0),
        "pdfs_updated": result.get("pdfs_updated", 0),
        "changes": result.get("changes",
                              {"new": 0, "changed": 0, "unchanged": 0, "deleted": 0}),
        "changed_urls": shown,
        "error": error,
    })
    write_json(LOG_FILE, entries[-LOG_KEEP:])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    started_at = datetime.now(timezone.utc)
    clock = time.monotonic()
    result: dict = {"pages_total": 0, "pdfs_updated": 0, "changed_urls": [],
                    "changes": {"new": 0, "changed": 0, "unchanged": 0, "deleted": 0}}
    error: Optional[str] = None
    success = False

    try:
        DATA_DIR.mkdir(exist_ok=True)
        PDF_DIR.mkdir(exist_ok=True)

        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        robots = Robots(session)

        seeds = sitemap_seeds(session, robots)
        pages, pdf_urls = crawl(session, robots, seeds)

        previous = load_previous()
        final, manifest, report = reconcile(pages, previous)
        pdf_changes = process_pdfs(session, robots, pdf_urls)

        write_json(CONTENT_FILE, final)
        write_json(MANIFEST_FILE, manifest)

        counts = {
            "new": len(report["new"]), "changed": len(report["changed"]),
            "unchanged": report["unchanged"], "deleted": len(report["deleted"]),
        }
        result["pages_total"] = len(final)
        result["pdfs_updated"] = pdf_changes
        result["changes"] = counts
        result["changed_urls"] = (
            [f"[new] {u}" for u in report["new"]]
            + [f"[changed] {u}" for u in report["changed"]]
            + [f"[deleted] {u}" for u in report["deleted"]]
        )

        log.info("Summary: %(new)d new, %(changed)d changed, "
                 "%(unchanged)d unchanged, %(deleted)d deleted", counts)
        log.info("PDF files changed: %d", pdf_changes)
        log.info("Pages total: %d | PDFs on disk: %d",
                 len(final), len(list(PDF_DIR.glob('*.txt'))))
        success = True
    except Exception as e:
        log.error("Fatal error: %s", e, exc_info=True)
        error = f"{type(e).__name__}: {e}"
    finally:
        # Always record the run, even if the crawl blew up mid-way.
        try:
            append_run_log(started_at, success, time.monotonic() - clock,
                           result, error)
        except Exception as e:  # logging must never mask the real outcome
            log.error("Could not write run log: %s", e)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
