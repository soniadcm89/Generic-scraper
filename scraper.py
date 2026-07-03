"""
scraper.py — the scraping pipeline, kept free of any Streamlit code so it can be
tested and reused on its own.

Pipeline overview
-----------------
1. discover_article_urls()  : find candidate article URLs on the target site using
                              newspaper4k's source builder, supplemented by a
                              requests + BeautifulSoup pass over the page's links.
2. RobotsChecker            : checks robots.txt once per domain and answers
                              "may I fetch this URL?" for each candidate.
3. fetch_article()          : download + parse one article with newspaper4k
                              (with a timeout and one retry).
4. article_matches()        : keyword + date-range filter.
5. run_scrape()             : ties it all together and reports progress through a
                              callback, returning rows of {title, url, date}.
"""

from __future__ import annotations

import concurrent.futures as cf
import gzip
import re
import threading
import time
import unicodedata
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import trafilatura
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from dateutil import parser as dateparser
from lxml import etree

import newspaper  # newspaper4k — only for the optional full-text export now

# A browser-like User-Agent: many news sites refuse requests from the default
# python-requests UA. We still honour robots.txt (checked for "*") regardless.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20  # seconds, for every HTTP request we make

# How many times to try downloading an article before giving up. Higher =
# fewer articles skipped due to transient timeouts (steadier match counts),
# at the cost of more time spent on genuinely dead URLs.
FETCH_ATTEMPTS = 4


def _browser_get(url: str, timeout: int = REQUEST_TIMEOUT):
    """GET a URL with the browser-impersonating client (curl_cffi) so Cloudflare
    treats us like a real browser. Used for every HTTP call — discovery AND
    article downloads — so the whole scraper works on protected sites."""
    return cffi_requests.get(url, impersonate="chrome", timeout=timeout)


class AdaptiveThrottle:
    """Thread-safe adaptive request pacing (AIMD, like TCP congestion control).

    Worker threads call wait() before each request, then report ok() or
    rate_limited(). A 429 widens the gap between request *starts* (multiplicative
    back-off); a run of successes narrows it again (additive recovery). So the
    request RATE self-adjusts to just under what a site tolerates — one "parallel
    downloads" setting then works on both fast and rate-limited sites without any
    per-site tuning. On a site that never 429s, the gap stays 0 (no slowdown)."""

    _FIRST_BACKOFF = 0.15       # gap after the first 429 (~6-7 requests/second)
    _BACKOFF_FACTOR = 1.7       # widen the gap by this on each further 429
    _MAX_INTERVAL = 1.0         # never slower than 1 request / second
    _RECOVER_STEP = 0.01        # gap shaved off on EVERY success (not streak-gated)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._interval = 0.0        # seconds between request starts (0 = full speed)
        self._next = 0.0            # monotonic time the next request may start
        self.hits = 0               # 429s absorbed, for reporting

    def wait(self) -> None:
        """Reserve the next paced slot and sleep until it's our turn."""
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
            delay = start - now
        if delay > 0:
            time.sleep(delay)

    def rate_limited(self) -> None:
        with self._lock:
            self.hits += 1
            self._interval = min(self._MAX_INTERVAL,
                                 self._interval * self._BACKOFF_FACTOR
                                 if self._interval else self._FIRST_BACKOFF)

    def ok(self) -> None:
        # Recover a little on EVERY success, so intermittent 429s can't ratchet
        # the gap up to the max and leave it stuck there (the crawl-slow bug).
        with self._lock:
            if self._interval > 0:
                self._interval = max(0.0, self._interval - self._RECOVER_STEP)


# --------------------------------------------------------------------------- #
# Configuration and result containers
# --------------------------------------------------------------------------- #

@dataclass
class ScrapeConfig:
    """Everything the UI collects from the user."""
    target_url: str
    keywords: list[str]                 # already split + stripped, may not be empty
    match_scope: str                    # "title", "body" or "title+body"
    start_date: date
    end_date: date
    include_undated: bool = False       # keep articles whose date can't be found?
    delay_seconds: float = 1.0          # pause between article requests
    max_articles: int = 0               # cap on articles fetched; <= 0 = no limit
    exclude_patterns: list[str] = field(default_factory=list)
    # URL substrings (lower-cased) that disqualify a candidate during discovery,
    # e.g. "corrections-and-clarifications". Empty = no extra filtering.
    use_sitemap: bool = True            # enumerate the whole site via its sitemap
    crawl_fallback: bool = True         # BFS-crawl the site when sitemaps are thin
    max_crawl_pages: int = 150          # cap on pages fetched during a crawl
    ignore_accents: bool = True         # match "genero" == "género" == "gênero"
    whole_word: bool = False            # match whole words only (no "generoso")
    follow_links: bool = False          # also queue article links found on each page
    workers: int = 16                   # parallel article downloads


@dataclass
class ScrapeResult:
    """What run_scrape() hands back to the UI."""
    rows: list[dict] = field(default_factory=list)      # {title, url, date}
    checked: int = 0                                    # articles actually fetched
    discovered: int = 0                                 # unique in-window candidates
    truncated: bool = False                             # more candidates than the cap
    failed: int = 0                                     # download failed after retries
    rate_limit_hits: int = 0                            # 429s absorbed by the throttle
    skipped_robots: int = 0                             # blocked by robots.txt
    skipped_undated: int = 0                            # dropped for missing date
    errors: list[str] = field(default_factory=list)     # human-readable messages


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #

class RobotsChecker:
    """Fetches robots.txt once per domain and caches the parsed rules.

    If robots.txt cannot be retrieved at all we assume crawling is allowed,
    which is the conventional interpretation (no rules published = no limits).
    """

    def __init__(self) -> None:
        self._parsers: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._lock = threading.Lock()           # cache is shared across workers

    def can_fetch(self, url: str) -> bool:
        netloc = urlparse(url).netloc
        with self._lock:
            if netloc not in self._parsers:
                self._parsers[netloc] = self._load(url)
            parser = self._parsers[netloc]
        if parser is None:                      # robots.txt unreachable
            return True
        return parser.can_fetch("*", url)

    @staticmethod
    def _load(url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parts = urlparse(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            resp = _browser_get(robots_url)
            if resp.status_code >= 400:
                return None                     # no robots.txt -> allow all
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(resp.text.splitlines())
            return parser
        except Exception:  # noqa: BLE001 — unreachable robots.txt -> allow all
            return None


# --------------------------------------------------------------------------- #
# Step 1 — discover candidate article URLs
# --------------------------------------------------------------------------- #

def _same_domain(url: str, base: str) -> bool:
    """True if url is on the same site as base ('www.' prefix ignored)."""
    a = urlparse(url).netloc.lower().removeprefix("www.")
    b = urlparse(base).netloc.lower().removeprefix("www.")
    return a == b and a != ""


def _is_blocked_path(url: str) -> bool:
    """True for pages that are clearly not text articles (videos, galleries,
    tag/category indexes, account pages, assets…). Applied to URLs from BOTH
    discovery methods — newspaper4k happily returns video pages, which have no
    body text and waste the article budget."""
    path = urlparse(url).path.lower()
    blocked = ("/tag/", "/tags/", "/topico/", "/topic/", "/category/",
               "/author/", "/search", "/login", "/signin", "/subscribe",
               "/newsletter", "/video/", "/videos/", "/audio/", "/podcast",
               "/gallery/", "/photos/", "/live/", "/contact", "/about",
               "/privacy", "/terms", "/rss", "/feed")
    if any(b in path for b in blocked):
        return True
    return path.endswith((".jpg", ".png", ".gif", ".pdf", ".xml", ".css", ".js"))


def _looks_like_article(url: str) -> bool:
    """Cheap heuristic: does this path look like an article page rather than a
    section index, tag page, login page, etc.?  Deliberately permissive —
    non-articles that slip through simply won't match keywords/dates later."""
    path = urlparse(url).path.lower()
    if not path or path == "/":
        return False
    if _is_blocked_path(url):
        return False
    # Date in the path (e.g. /2026/07/01/...) is a strong article signal.
    if re.search(r"/20\d{2}/", path):
        return True
    segments = [s for s in path.rstrip("/").split("/") if s]
    if not segments:
        return False
    last = segments[-1]
    if last.endswith((".html", ".htm", ".shtml")):
        return True

    def _is_slug(seg: str) -> bool:
        # A long hyphenated slug ("prime-minister-resigns-over-scandal"). Short
        # two-word slugs ("love-and-sex") are usually section indexes, so require
        # several hyphens or some length.
        hyphens = seg.count("-")
        return hyphens >= 3 or (hyphens >= 2 and len(seg) >= 25)

    # Many sites end an article URL with a numeric id (e.g.
    # /mundo/artigo/<slug>/18102236); the descriptive slug is then the segment
    # BEFORE the id, so test the last two segments, not just the last.
    if any(_is_slug(seg) for seg in segments[-2:]):
        return True
    # An explicit article marker (/artigo/, /noticia/) ending in a numeric id is
    # an article even when the slug is short (e.g. /opiniao/artigo/eva/12345).
    if last.isdigit() and any(m in path for m in ("/artigo/", "/noticia/")):
        return True
    return False


# --- date helper shared by sitemap parsing and article extraction ---------- #

def _parse_date_loose(text: Optional[str]) -> Optional[date]:
    """Parse a date-ish string (ISO, RFC822, sitemap lastmod…) into a plain
    date, or None. Rejects absurd years produced by over-lenient parsing."""
    if not text:
        return None
    try:
        dt = dateparser.parse(text, ignoretz=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt and 1990 <= dt.year <= datetime.now().year + 1:
        return dt.date() if isinstance(dt, datetime) else dt
    return None


# --- sitemap discovery ------------------------------------------------------ #
# A sitemap is the universal, polite way to enumerate a whole news site: almost
# every site publishes one (often declared in robots.txt) and it lists article
# URLs, usually with a <lastmod>/<news:publication_date>. We walk the sitemap
# tree, prune branches older than the requested window, and date pre-filter the
# URLs so we only download article bodies that could fall in range.

# lastmod is "last modified", which is >= the publish date, so we keep a small
# grace window past end_date to avoid dropping articles edited after publication.
_DATE_GRACE = timedelta(days=2)
_MAX_SITEMAP_FETCHES = 60           # ceiling on plain sitemap files fetched per run
_MONTH_RE = re.compile(r"(20\d{2})[-_/](\d{1,2})(?!\d)")     # a YYYY-MM in a name


def _fetch_bytes(url: str) -> bytes:
    """GET raw bytes, transparently decompressing .gz sitemaps."""
    resp = _browser_get(url)
    resp.raise_for_status()
    content = resp.content
    if url.lower().endswith(".gz") or content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content


def _localname(element) -> str:
    """XML tag name without its namespace prefix ('loc', 'sitemap', 'url'…)."""
    tag = element.tag
    if not isinstance(tag, str):        # comments / processing instructions
        return ""
    return etree.QName(tag).localname


def _child_text(element, name: str, deep: bool = False) -> Optional[str]:
    """Text of the first descendant (deep) or child whose localname == name."""
    iterator = element.iter() if deep else element
    for child in iterator:
        if child is not element and _localname(child) == name:
            return (child.text or "").strip() or None
    return None


def _parse_sitemap(content: bytes):
    """Parse a sitemap or sitemap-index. Returns (child_sitemaps, url_entries):
    child_sitemaps = list of (loc, lastmod_date); url_entries = list of
    (loc, best_date)."""
    children: list[tuple[str, Optional[date]]] = []
    entries: list[tuple[str, Optional[date]]] = []
    root = etree.fromstring(content)    # raises on malformed XML; caller guards
    for el in root:
        kind = _localname(el)
        if kind == "sitemap":                       # entry in a sitemap index
            loc = _child_text(el, "loc")
            if loc:
                children.append((loc, _parse_date_loose(_child_text(el, "lastmod"))))
        elif kind == "url":                          # entry in a urlset
            loc = _child_text(el, "loc")
            if not loc:
                continue
            # Prefer the news publication date; fall back to lastmod.
            pub = _child_text(el, "publication_date", deep=True)
            best = _parse_date_loose(pub) or _parse_date_loose(_child_text(el, "lastmod"))
            entries.append((loc, best))
    return children, entries


def _sitemap_seeds(target_url: str) -> list[str]:
    """Candidate sitemap URLs: those declared in robots.txt plus common paths."""
    parts = urlparse(target_url)
    base = f"{parts.scheme}://{parts.netloc}"
    seeds: list[str] = []
    try:                                # robots.txt often declares the sitemap
        text = _browser_get(f"{base}/robots.txt").text
        for line in text.splitlines():
            if line.lower().startswith("sitemap:"):
                seeds.append(line.split(":", 1)[1].strip())
    except Exception:  # noqa: BLE001
        pass
    for path in ("/sitemapindex.xml", "/sitemap_index.xml", "/sitemap.xml",
                 "/sitemap-index.xml", "/news-sitemap.xml"):
        seeds.append(base + path)
    # De-duplicate while preserving order.
    out, seen = [], set()
    for s in seeds:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _sitemap_in_window(loc: str, start_date: Optional[date],
                       end_date: Optional[date]) -> bool:
    """Best-effort test of whether a child sitemap could hold in-window URLs,
    based on a YYYY-MM (or bare YYYY) in its filename — e.g.
    'sitemap-2026-06.xml'. Returns True whenever the name carries no usable
    date, so we never skip a sitemap we can't reason about.

    We deliberately do NOT trust a child's <lastmod> from the index: sites
    (rr.pt among them) leave it stale, which would wrongly prune current months.
    """
    if not (start_date and end_date):
        return True
    low = loc.lower()
    # Date in query params, e.g. '.../sitemap.xml?yyyy=2026&mm=07&dd=02' (jn.pt):
    # a per-day sitemap index. Prune to the exact day window when dd is present.
    q = re.search(r"yyyy=(\d{4}).{0,12}?mm=(\d{1,2})(?:.{0,12}?dd=(\d{1,2}))?", low)
    if q:
        year, month = int(q.group(1)), int(q.group(2))
        if q.group(3) and 1 <= month <= 12:
            try:
                d = date(year, month, int(q.group(3)))
                return start_date - _DATE_GRACE <= d <= end_date + _DATE_GRACE
            except ValueError:
                pass
        if 1 <= month <= 12:
            return (start_date.year, start_date.month) <= (year, month) \
                   <= (end_date.year, end_date.month)
    ym = re.search(r"(20\d{2})[-_/](\d{1,2})(?!\d)", low)
    if ym:
        year, month = int(ym.group(1)), int(ym.group(2))
        if 1 <= month <= 12:
            return (start_date.year, start_date.month) <= (year, month) \
                   <= (end_date.year, end_date.month)
    year_only = re.search(r"(20\d{2})", low)
    if year_only:
        return start_date.year <= int(year_only.group(1)) <= end_date.year
    return True


def _numbered_series_key(loc: str) -> Optional[str]:
    """If a sitemap name is a plain '<prefix>-<N>.xml' page (e.g. WordPress's
    'wp-sitemap-posts-post-5.xml'), return the shared <prefix> so pages of the
    same series group together. None for names that carry a date instead."""
    low = loc.lower()
    if _MONTH_RE.search(low):
        return None
    m = re.search(r"^(.*?)-(\d+)\.xml(?:\.gz)?$", low)
    return m.group(1) if m else None


def _numbered_index(loc: str) -> int:
    m = re.search(r"-(\d+)\.xml(?:\.gz)?$", loc.lower())
    return int(m.group(1)) if m else 0


def _collect_entries(entries, base: str, start_date: Optional[date],
                     end_date: Optional[date], found: dict) -> None:
    """Add article URLs from a urlset to `found`, keyed by publish date. Prefers
    the date embedded in the URL (the true publish date on WordPress and most
    news sites) over the sitemap's <lastmod> (which is the *modified* date)."""
    for loc, sm_date in entries:
        if not _same_domain(loc, base) or not _looks_like_article(loc):
            continue
        d = _url_date(loc) or sm_date
        if d is not None and start_date and end_date and not (
                start_date - _DATE_GRACE <= d <= end_date + _DATE_GRACE):
            continue
        found[loc] = d


def _scan_numbered_series(pages: list[str], start_date: Optional[date],
                          end_date: Optional[date], base: str,
                          found: dict, notify) -> None:
    """A numbered sitemap series (e.g. wp-sitemap-posts-post-1..N.xml) is ordered
    chronologically but gives no date in the filename. Rather than read all N
    pages, binary-search using the publish date in each page's URLs and fetch
    only the pages whose date range overlaps the window."""
    pages = sorted(set(pages), key=_numbered_index)
    cache: dict[str, tuple] = {}

    def info(loc):                          # (min_url_date, max_url_date, entries)
        if loc not in cache:
            try:
                _children, entries = _parse_sitemap(_fetch_bytes(loc))
            except Exception:  # noqa: BLE001
                entries = []
            uds = sorted(d for l, _ in entries if (d := _url_date(l)) is not None)
            cache[loc] = (uds[0] if uds else None, uds[-1] if uds else None, entries)
        return cache[loc]

    if not pages:
        return
    first, last = info(pages[0]), info(pages[-1])

    # No dates in the URLs → not the dated article archive (e.g. tag/category or
    # user sitemaps). On a date-windowed search these can't be placed in time and
    # are almost never real articles, so skip the series rather than pull in
    # thousands of undated URLs that would be downloaded and then discarded.
    if first[1] is None and last[1] is None:
        return

    ascending = (first[1] or date.min) <= (last[1] or date.max)
    ordered = pages if ascending else list(reversed(pages))
    n = len(ordered)

    if start_date and end_date:
        lo, hi = 0, n                       # first page whose max date reaches start
        while lo < hi:
            mid = (lo + hi) // 2
            mx = info(ordered[mid])[1]
            if mx is None or mx < start_date - _DATE_GRACE:
                lo = mid + 1
            else:
                hi = mid
        i0 = lo
        lo, hi = 0, n                       # first page whose min date passes end
        while lo < hi:
            mid = (lo + hi) // 2
            mn = info(ordered[mid])[0]
            if mn is None or mn <= end_date + _DATE_GRACE:
                lo = mid + 1
            else:
                hi = mid
        indices = range(max(0, i0), min(n, lo))
    else:
        indices = range(n)

    for i in indices:
        _collect_entries(info(ordered[i])[2], base, start_date, end_date, found)
        notify(0, 0, 0, f"Reading sitemaps… {len(found)} candidate articles")


def _discover_via_sitemap(target_url: str, start_date: Optional[date],
                          end_date: Optional[date], errors: list[str],
                          notify) -> dict[str, Optional[date]]:
    """Walk the site's sitemap tree and return {article_url: date_or_None} for
    URLs that look like articles and fall in (or near) the date window.

    Handles two common layouts: date-named monthly sitemaps (rr.pt) and numbered
    chronological page-sitemaps (WordPress / observador.pt), the latter via a
    binary search so we don't have to read hundreds of files.
    """
    found: dict[str, Optional[date]] = {}
    seen: set[str] = set()
    leaves: list[str] = []

    # Phase 1 — resolve index files into a flat list of leaf (urlset) sitemaps.
    to_visit: deque[str] = deque(_sitemap_seeds(target_url))
    index_fetches = 0
    while to_visit and index_fetches < 30:
        sm = to_visit.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        try:
            children, entries = _parse_sitemap(_fetch_bytes(sm))
        except Exception:  # noqa: BLE001 — a missing/invalid sitemap is expected
            continue
        index_fetches += 1
        if entries:                         # a seed that is itself a urlset
            _collect_entries(entries, target_url, start_date, end_date, found)
        for cloc, _clast in children:
            if cloc not in seen:
                leaves.append(cloc)
        if children:
            notify(0, 0, 0, f"Reading sitemap index… {len(leaves)} sub-sitemaps")

    leaves = list(dict.fromkeys(leaves))    # dedupe, keep order

    # Phase 2 — classify leaves into numbered series vs. plain/dated sitemaps.
    numbered: dict[str, list[str]] = {}
    plain: list[str] = []
    for loc in leaves:
        if not _sitemap_in_window(loc, start_date, end_date):
            continue                        # date-named + out of window → skip
        key = _numbered_series_key(loc)
        if key:
            numbered.setdefault(key, []).append(loc)
        else:
            plain.append(loc)

    # Phase 3a — fetch plain sitemaps (bounded). A "leaf" that turns out to be a
    # nested index feeds its own children back into the classification.
    pending = deque(plain)
    plain_fetches = 0
    while pending and plain_fetches < _MAX_SITEMAP_FETCHES:
        loc = pending.popleft()
        if loc in seen:
            continue
        seen.add(loc)
        try:
            children, entries = _parse_sitemap(_fetch_bytes(loc))
        except Exception:  # noqa: BLE001
            continue
        plain_fetches += 1
        for cloc, _clast in children:
            key = _numbered_series_key(cloc)
            if key:
                numbered.setdefault(key, []).append(cloc)
            elif cloc not in seen:
                pending.append(cloc)
        _collect_entries(entries, target_url, start_date, end_date, found)
        notify(0, 0, 0, f"Reading sitemaps… {len(found)} candidate articles")

    # Phase 3b — chronological binary-search scan of each numbered series.
    for series in numbered.values():
        _scan_numbered_series(series, start_date, end_date, target_url,
                              found, notify)

    return found


# --- recursive crawl fallback ---------------------------------------------- #

def _discover_via_crawl(target_url: str, max_pages: int, delay: float,
                        robots: "RobotsChecker", notify) -> list[str]:
    """Breadth-first crawl within the same domain when sitemaps are unavailable
    or thin. Follows links up to max_pages fetched, collecting article-looking
    URLs. Honours robots.txt and the polite delay."""
    frontier: deque[str] = deque([target_url])
    visited: set[str] = set()
    found: list[str] = []
    pages = 0

    while frontier and pages < max_pages:
        url = frontier.popleft()
        url, _ = urldefrag(url)
        if url in visited:
            continue
        visited.add(url)
        if not robots.can_fetch(url):
            continue
        try:
            resp = _browser_get(url)
            resp.raise_for_status()
        except Exception:  # noqa: BLE001
            continue
        pages += 1
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup.find_all("a", href=True):
            link, _ = urldefrag(urljoin(resp.url, tag["href"]))
            if not _same_domain(link, target_url):
                continue
            if _looks_like_article(link) and link not in found:
                found.append(link)
            if link not in visited and not _is_blocked_path(link):
                frontier.append(link)       # traverse deeper into the site
        notify(0, 0, 0, f"Crawling… {pages}/{max_pages} pages, "
                        f"{len(found)} article links found")
        if delay > 0:
            time.sleep(delay)
    return found


def _url_date(url: str) -> Optional[date]:
    """Extract a publish date from a /YYYY/MM/DD/ path segment if present
    (common on news sites, e.g. rr.pt). Returns None when the URL carries no
    full date, so callers must not treat None as 'out of range'."""
    m = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)", urlparse(url).path)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _harvest_links(html: str, base_url: str, target_url: str,
                   patterns: list[str]) -> list[str]:
    """Pull same-domain, article-looking links out of a page we've ALREADY
    downloaded (reusing its HTML — no extra request). Used to follow "related
    articles" and other in-page links so recall isn't limited to the sitemap.
    Works on any site: it only relies on <a href> and the article-URL heuristic."""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — malformed HTML shouldn't stop the run
        return []
    out: list[str] = []
    for tag in soup.find_all("a", href=True):
        link, _ = urldefrag(urljoin(base_url, tag["href"]))
        if not link.startswith(("http://", "https://")):
            continue
        if not _same_domain(link, target_url) or not _looks_like_article(link):
            continue
        if any(p in link.lower() for p in patterns):
            continue
        out.append(link)
    return out


def discover_article_urls(config: "ScrapeConfig", errors: list[str],
                          robots: "RobotsChecker",
                          notify=lambda *a: None) -> list[str]:
    """Site-wide, source-agnostic discovery. Strategy, in order:

      1. Sitemap enumeration (when enabled) — the universal, polite way to reach
         the whole archive, with a date pre-filter so we only keep in-window URLs.
      2. A link sweep of the target page — picks up the very latest headlines
         that may not be in the sitemap yet.
      3. A bounded breadth-first crawl (fallback) — only when the above yield few
         URLs, e.g. a site with no usable sitemap.

    Returns the full deduplicated, date-sorted (newest first) list of candidate
    URLs. The caller (run_scrape) applies the max_articles cap so it can report
    when the search was truncated.
    """
    target_url = config.target_url
    patterns = [p.lower() for p in (config.exclude_patterns or []) if p.strip()]
    candidates: dict[str, Optional[date]] = {}     # url -> hint date (or None)

    def add(url: str, d: Optional[date]) -> None:
        url, _ = urldefrag(url.strip())
        if not url or not url.startswith(("http://", "https://")):
            return
        if any(p in url.lower() for p in patterns):
            return
        # Keep the most informative date we've seen for this URL.
        if url not in candidates or (candidates[url] is None and d is not None):
            candidates[url] = d

    # 1. Sitemap enumeration.
    if config.use_sitemap:
        notify(0, 0, 0, "Looking for the site's sitemap…")
        try:
            for loc, d in _discover_via_sitemap(
                    target_url, config.start_date, config.end_date,
                    errors, notify).items():
                add(loc, d)
        except Exception as exc:  # noqa: BLE001 — never let discovery abort a run
            errors.append(f"Sitemap discovery failed: {exc}")

    # 2. Fresh headlines on the target page itself.
    notify(0, 0, 0, "Scanning the target page for links…")
    try:
        resp = _browser_get(target_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup.find_all("a", href=True):
            link = urljoin(resp.url, tag["href"])
            if not (_same_domain(link, target_url) and _looks_like_article(link)):
                continue
            # Drop links the URL itself dates outside the window (homepages often
            # link to older featured pieces); keep undated links for run_scrape.
            d = _url_date(link)
            if d is not None and config.start_date and config.end_date and not (
                    config.start_date - _DATE_GRACE <= d <= config.end_date + _DATE_GRACE):
                continue
            add(link, d)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Target-page link scan failed: {exc}")

    # 3. Crawl fallback only when the above essentially failed (no usable
    # sitemap and few links on the page) — not merely when we're under the cap,
    # so a narrow date window on a well-mapped site doesn't trigger a crawl.
    _CRAWL_TRIGGER = 15
    if config.crawl_fallback and len(candidates) < _CRAWL_TRIGGER:
        notify(0, 0, 0, "No usable sitemap — crawling the site for links…")
        for link in _discover_via_crawl(target_url, config.max_crawl_pages,
                                        config.delay_seconds, robots, notify):
            add(link, None)

    # Never return the landing page itself as an "article".
    normalized_target, _ = urldefrag(target_url.rstrip("/"))
    candidates.pop(normalized_target, None)
    candidates.pop(normalized_target + "/", None)

    # Sort newest-first (undated last) so the article budget favours dated,
    # in-window items. Return the FULL list; run_scrape applies the cap so it
    # can tell the user when the search was truncated.
    ordered = sorted(
        candidates.items(),
        key=lambda kv: (kv[1] is not None, kv[1] or date.min),
        reverse=True,
    )
    return [url for url, _ in ordered]


# --------------------------------------------------------------------------- #
# Step 3 — download and parse one article
# --------------------------------------------------------------------------- #

def _fallback_date(article: newspaper.Article) -> Optional[datetime]:
    """newspaper4k sometimes misses the publish date. Look through the page's
    meta tags for anything date-shaped and let dateutil try to parse it."""
    date_keys = ("published", "pubdate", "date", "created", "modified")
    candidates: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                if any(k in str(key).lower() for k in date_keys):
                    walk(sub)
        elif isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    walk(getattr(article, "meta_data", {}) or {})
    for text in candidates:
        try:
            parsed = dateparser.parse(text, ignoretz=True)
            # Sanity check: reject absurd years produced by lenient parsing.
            if parsed and 1990 <= parsed.year <= datetime.now().year + 1:
                return parsed
        except (ValueError, OverflowError, TypeError):
            continue
    return None


def fetch_article(url: str) -> tuple[Optional[newspaper.Article], Optional[str]]:
    """Download + parse with several retries. Returns (article, error_message);
    exactly one of the two is None. Retrying matters for accuracy: under heavy
    parallelism a busy site will time out on some requests that succeed on a
    second or third try, so more attempts = fewer articles wrongly skipped."""
    last_error = "unknown error"
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            article = newspaper.Article(
                url,
                fetch_images=False,
                request_timeout=REQUEST_TIMEOUT,
                browser_user_agent=USER_AGENT,
            )
            article.download()
            article.parse()
            return article, None
        except Exception as exc:  # noqa: BLE001 — skip the article, keep the run
            last_error = str(exc) or type(exc).__name__
            if attempt < FETCH_ATTEMPTS:
                time.sleep(attempt)         # linear backoff: 1s, 2s, 3s…
    return None, last_error


# --------------------------------------------------------------------------- #
# Step 4 — filtering
# --------------------------------------------------------------------------- #

def clean_text(text: str) -> str:
    """Strip zero-width/invisible characters some sites embed in headlines and
    collapse runs of whitespace, so CSV cells are clean."""
    text = re.sub("[\\u200b\\u200c\\u200d\\u2060\\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# A trailing " - Site" / " | Site" / " – Site" that news sites append to titles.
_SITE_SUFFIX_RE = re.compile(r"\s+[-–|]\s+([^-–|]{1,30})$")


def _strip_site_suffix(title: str) -> str:
    """Remove a trailing ' - Renascença' / ' | Observador' style site name from a
    title. Conservative: only when the tail after the last separator is short
    (≤3 words), so real title fragments like 'Ucrânia: a guerra' aren't touched."""
    m = _SITE_SUFFIX_RE.search(title)
    if m and len(m.group(1).split()) <= 3:
        return title[:m.start()].strip()
    return title


def _fold(text: str) -> str:
    """Lower-case and strip diacritics so 'género', 'genero' and 'gênero' all
    compare equal — essential for Portuguese/Spanish/French keyword search,
    which (like a site's own search box) is normally accent-insensitive."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def keyword_match(title: str, body: str, keywords: list[str], scope: str,
                  ignore_accents: bool = True, whole_word: bool = False) -> bool:
    """True if ANY keyword is found. Case-insensitive; diacritics folded when
    ignore_accents is set.

    whole_word controls precision:
      * False (substring) — "genero" matches inside "generoso", "transgenero"…
      * True  (whole word) — "genero" matches only the standalone word, so
        "generosa"/"generosidade" no longer count. Note this also excludes
        compounds/plurals like "transgénero"/"géneros"; add those as their own
        keywords if you want them.
    """
    if scope == "title":
        haystack = title
    elif scope == "body":
        haystack = body
    else:                                   # "title+body"
        haystack = f"{title}\n{body}"
    normalize = _fold if ignore_accents else str.lower
    haystack = normalize(haystack)
    for kw in keywords:
        if not kw:
            continue
        needle = normalize(kw)
        if whole_word:
            # (?<!\w) / (?!\w) = word boundaries that also work for phrases.
            if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack):
                return True
        elif needle in haystack:
            return True
    return False


def extract_publish_date(article: newspaper.Article) -> Optional[date]:
    """Best-effort publish date: newspaper4k first, then the meta-tag fallback.
    Returns a plain date (no time / timezone) or None."""
    dt = article.publish_date or _fallback_date(article)
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.date()
    return dt if isinstance(dt, date) else None


# --------------------------------------------------------------------------- #
# Retrieval fetch + lightweight extract (curl_cffi + trafilatura). This is the
# hot path: it runs on every candidate, so it must be fast and low-CPU.
# --------------------------------------------------------------------------- #

def fetch_html(url: str,
               throttle: "Optional[AdaptiveThrottle]" = None
               ) -> tuple[Optional[str], Optional[str]]:
    """Download a page's HTML with a browser-impersonating client (curl_cffi).
    This passes the TLS/bot fingerprint that Cloudflare uses to block plain
    `requests`, so it works on protected sites (e.g. observador) AND ordinary
    ones — it's universal. Retries on 429 / 5xx / network errors with backoff;
    does NOT retry 4xx (404/permanent), so dead links fail fast.

    If a shared `throttle` is given, the request rate self-adjusts to the site's
    limit: each 429 slows every worker down a little, so hundreds of rate-limit
    failures become a slightly slower — but complete — run."""
    last_error = "unknown error"
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        if throttle is not None:
            throttle.wait()
        try:
            resp = cffi_requests.get(url, impersonate="chrome",
                                     timeout=REQUEST_TIMEOUT)
            code = resp.status_code
            if code == 200:
                if throttle is not None:
                    throttle.ok()
                return resp.text, None
            if code == 429:                         # rate limited — back off
                if throttle is not None:
                    throttle.rate_limited()
                last_error = "HTTP 429 (rate limited)"
                if attempt < FETCH_ATTEMPTS:
                    retry_after = resp.headers.get("Retry-After", "") or ""
                    time.sleep(min(int(retry_after), 15)
                               if retry_after.isdigit() else attempt * 2)
                continue
            if code >= 500:                         # transient server error
                last_error = f"HTTP {code}"
                if attempt < FETCH_ATTEMPTS:
                    time.sleep(attempt * 2)
                continue
            return None, f"HTTP {code}"             # 4xx (404/403…) — permanent
        except Exception as exc:  # noqa: BLE001 — skip the article, keep the run
            last_error = str(exc) or type(exc).__name__
            if attempt < FETCH_ATTEMPTS:
                time.sleep(attempt)
    return None, last_error


def _html_title(html: str) -> str:
    """Fallback title from <title> / og:title when the extractor returns none."""
    try:
        soup = BeautifulSoup(html, "lxml")
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            return og["content"]
        if soup.title and soup.title.string:
            return soup.title.string
    except Exception:  # noqa: BLE001
        pass
    return ""


def extract_article(html: str, url: str) -> tuple[str, str, Optional[date]]:
    """Lightweight main-content extraction with trafilatura. Returns
    (clean_title, body_text, publish_date_or_None). ~11x cheaper than
    newspaper4k's parser and, on our tests, finds the same keyword matches."""
    title = text = ""
    parsed_date: Optional[date] = None
    try:
        doc = trafilatura.bare_extraction(html, with_metadata=True,
                                          favor_recall=True)
    except Exception:  # noqa: BLE001
        doc = None
    if doc is not None:
        if isinstance(doc, dict):
            title, text = doc.get("title") or "", doc.get("text") or ""
            parsed_date = _parse_date_loose(doc.get("date"))
        else:
            title = getattr(doc, "title", "") or ""
            text = getattr(doc, "text", "") or ""
            parsed_date = _parse_date_loose(getattr(doc, "date", None))
    if not text:                                    # never miss a body entirely
        try:
            text = trafilatura.extract(html, favor_recall=True) or ""
        except Exception:  # noqa: BLE001
            text = ""
    if not title:
        title = _html_title(html)
    return _strip_site_suffix(clean_text(title)), text, parsed_date


# --------------------------------------------------------------------------- #
# Optional post-retrieval full-text export (newspaper4k over matched URLs only)
# --------------------------------------------------------------------------- #

def fetch_full_text(url: str) -> tuple[Optional[str], Optional[str]]:
    """Clean full article text via newspaper4k, downloading through the
    browser-impersonating client so it works on Cloudflare sites too. Used ONLY
    for the optional text export, over the handful of matched URLs."""
    html, error = fetch_html(url)
    if html is None:
        return None, error
    try:
        article = newspaper.Article(url, fetch_images=False)
        article.download(input_html=html)
        article.parse()
        return (article.text or "").strip(), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc) or type(exc).__name__


def extract_texts(urls: list[str], workers: int = 8,
                  progress: Optional[Callable[[int, int], None]] = None
                  ) -> list[dict]:
    """Fetch clean full text for each matched URL. Returns [{'url', 'text'}] in
    input order (text is '' if the download failed). For the companion CSV."""
    notify = progress or (lambda *a: None)
    rows: list[dict] = []

    def work(u: str) -> tuple[str, str]:
        text, _err = fetch_full_text(u)
        return u, text or ""

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for index, (u, text) in enumerate(ex.map(work, urls), start=1):
            rows.append({"url": u, "text": text})
            notify(index, len(urls))
    return rows


# --------------------------------------------------------------------------- #
# Step 5 — the full run
# --------------------------------------------------------------------------- #

# Progress callback signature: (checked, total_to_check, matched, message)
ProgressFn = Callable[[int, int, int, str], None]
# Checkpoint callback: receives the rows collected so far, periodically, so a
# long run can be persisted and survive a crash. Optional; the UI doesn't use it.
CheckpointFn = Callable[[list[dict]], None]


def run_scrape(config: ScrapeConfig,
               progress: Optional[ProgressFn] = None,
               checkpoint: Optional[CheckpointFn] = None,
               checkpoint_every: int = 50) -> ScrapeResult:
    """Execute the whole pipeline. Never raises for per-article problems —
    they are collected in result.errors instead.

    If ``checkpoint`` is given, it is called with result.rows every
    ``checkpoint_every`` articles (and once at the end) so long runs can be
    saved incrementally."""
    result = ScrapeResult()
    notify = progress or (lambda *args: None)

    robots = RobotsChecker()
    notify(0, 0, 0, "Discovering article links across the site…")
    all_candidates = discover_article_urls(config, result.errors, robots, notify)
    result.discovered = len(all_candidates)
    if not all_candidates:
        result.errors.append(
            "No article links discovered. Check the URL (include https://). "
            "The site may have no readable sitemap and block crawling, or render "
            "its links with JavaScript (which this tool can't see)."
        )
        return result

    # The max_articles cap bounds how many bodies we fetch; <= 0 means NO LIMIT.
    cap = config.max_articles if config.max_articles and config.max_articles > 0 \
        else float("inf")
    patterns = [p.lower() for p in (config.exclude_patterns or []) if p.strip()]
    queued: set[str] = set(all_candidates)
    workers = max(1, config.workers or 1)
    lock = threading.Lock()
    throttle = AdaptiveThrottle()       # shared: auto-slows on 429 rate limits

    def evaluate(url: str):
        """Fetch + keyword/date filter ONE url in a worker thread. Returns
        (status, row_or_None, harvested_links). Does its own robots check and
        polite delay so the pool self-throttles per worker."""
        if not robots.can_fetch(url):
            return "robots", None, []
        html, error = fetch_html(url, throttle)     # curl_cffi + auto-throttle
        if config.delay_seconds > 0:
            time.sleep(config.delay_seconds)        # per-worker throttle
        if html is None:
            return f"error:{error}", None, []
        title, body, meta_date = extract_article(html, url)   # trafilatura
        row = None
        status = "checked"
        if keyword_match(title, body, config.keywords, config.match_scope,
                         config.ignore_accents, config.whole_word):
            # Publish date: the URL date is authoritative for dated-URL sites;
            # fall back to the date trafilatura pulled from the page's metadata.
            pub = _url_date(url) or meta_date
            if pub is None:
                if config.include_undated:
                    row = {"title": title, "url": url, "date": ""}
                else:
                    status = "undated"
            elif config.start_date <= pub <= config.end_date:       # inclusive
                row = {"title": title, "url": url,
                       "date": pub.strftime("%Y-%m-%d")}
        # Harvest related links — ONE hop only (the caller never re-expands
        # these). Keep a link only when its URL date is inside the range; drop
        # both out-of-range links AND links with no date in the URL (we can't
        # confirm they're in range, and following them is what balloons the run).
        links: list[str] = []
        if config.follow_links:
            for link in _harvest_links(html, url, config.target_url, patterns):
                d = _url_date(link)
                if d is None or not (config.start_date - _DATE_GRACE
                                     <= d <= config.end_date + _DATE_GRACE):
                    continue
                links.append(link)
        return status, row, links

    def remaining() -> int:
        return cap - result.checked if cap != float("inf") else 1_000_000_000

    def run_wave(urls: list[str], keep_links: bool) -> list[str]:
        """Fetch a batch of URLs across the thread pool. Returns any newly
        discovered links (deduped) when keep_links is set."""
        batch = urls if cap == float("inf") else urls[:max(0, remaining())]
        new_links: list[str] = []
        if not batch:
            return new_links
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(evaluate, u): u for u in batch}
            for fut in cf.as_completed(futures):
                url = futures[fut]
                try:
                    status, row, links = fut.result()
                except Exception as exc:                    # noqa: BLE001
                    status, row, links = f"error:{exc}", None, []
                with lock:
                    if status == "robots":
                        result.skipped_robots += 1
                    else:
                        result.checked += 1
                        if status == "undated":
                            result.skipped_undated += 1
                        elif status.startswith("error:"):
                            result.failed += 1
                            result.errors.append(
                                f"Failed after retry: {url} — {status[6:]}")
                        if row:
                            result.rows.append(row)
                    if keep_links:
                        for link in links:
                            if link not in queued:
                                queued.add(link)
                                new_links.append(link)
                    if checkpoint and result.checked % checkpoint_every == 0:
                        checkpoint(result.rows)
                    notify(result.checked, min(len(queued), cap),
                           len(result.rows), f"Checked {result.checked}…")
        return new_links

    # Wave 1: everything discovered up front. Wave 2 (optional): the related
    # links harvested from those pages — one hop only, so it can't runaway.
    followed = run_wave(all_candidates, keep_links=config.follow_links)
    if config.follow_links and followed and remaining() > 0:
        run_wave(followed, keep_links=False)

    result.truncated = cap != float("inf") and len(queued) > result.checked
    result.rate_limit_hits = throttle.hits
    if checkpoint:
        checkpoint(result.rows)                 # final flush
    notify(result.checked, result.checked, len(result.rows), "Done.")
    return result
