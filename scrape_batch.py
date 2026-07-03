"""
scrape_batch.py — a FAST, command-line version of the scraper for big jobs.

It does the same thing as the Streamlit app, but spreads the article downloads
across several CPU processes, which roughly halves the time of a large run
(the app can't safely do this because of how Streamlit launches on Windows).

HOW TO USE
----------
1. Edit the SETTINGS block below (target site, keywords, date range…).
2. From this folder run:   python scrape_batch.py
3. When it finishes, open the CSV named in OUTPUT_CSV (same folder).

The CSV keeps updating as matches are found, so you can peek at it mid-run.
"""

import concurrent.futures as cf
import os
import time
from datetime import date

import pandas as pd

from scraper import (ScrapeConfig, discover_article_urls, RobotsChecker,
                     fetch_html, extract_article, keyword_match, _url_date)

# ============================ SETTINGS ============================
TARGET_URL      = "https://www.rr.pt"
KEYWORDS        = ["género"]            # article matches if it contains ANY of these
MATCH_SCOPE     = "title+body"          # "title", "body", or "title+body"
START_DATE      = date(2026, 6, 15)     # inclusive
END_DATE        = date(2026, 7, 2)      # inclusive
IGNORE_ACCENTS  = True                  # "género" == "genero" == "gênero"
WHOLE_WORD      = False                 # True = don't match inside "generosa" etc.
INCLUDE_UNDATED = False                 # keep matches whose date can't be found
PROCESSES       = 8                     # parallel worker processes (try 4–8)
OUTPUT_CSV      = "resultados_batch.csv"
# ==================================================================


# Each worker process holds the matching settings in a module global, set once
# by the pool initializer (cheaper than shipping them with every task).
_SETTINGS = None


def _init(settings):
    global _SETTINGS
    _SETTINGS = settings


def _work(url):
    """Download + keyword/date-filter one article. Runs in a worker process.
    Returns (kind, row) where kind is "match", "nomatch", or "fail".
    Uses the same lightweight retrieval path as the app (curl_cffi + trafilatura)."""
    keywords, scope, start, end, ignore_accents, whole_word, include_undated = _SETTINGS
    html, _err = fetch_html(url)
    if html is None:
        return "fail", None                 # download failed even after retries
    title, body, meta_date = extract_article(html, url)
    if not keyword_match(title, body, keywords, scope, ignore_accents, whole_word):
        return "nomatch", None
    pub = _url_date(url) or meta_date
    if pub is None:
        return ("match", {"title": title, "url": url, "date": ""}) \
            if include_undated else ("nomatch", None)
    if start <= pub <= end:
        return "match", {"title": title, "url": url, "date": pub.strftime("%Y-%m-%d")}
    return "nomatch", None


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, OUTPUT_CSV)

    cfg = ScrapeConfig(
        target_url=TARGET_URL, keywords=KEYWORDS, match_scope=MATCH_SCOPE,
        start_date=START_DATE, end_date=END_DATE, ignore_accents=IGNORE_ACCENTS,
        whole_word=WHOLE_WORD, include_undated=INCLUDE_UNDATED,
        follow_links=False, max_articles=0,
    )

    robots = RobotsChecker()
    print("Discovering articles across the site…", flush=True)
    urls = discover_article_urls(cfg, [], robots)
    urls = [u for u in urls if robots.can_fetch(u)]     # robots check (cache warm)
    print(f"{len(urls)} in-window candidates. Downloading with "
          f"{PROCESSES} processes…", flush=True)

    def save(rows):
        pd.DataFrame(rows, columns=["title", "url", "date"]).to_csv(
            out, index=False, encoding="utf-8-sig")

    rows = []
    settings = (KEYWORDS, MATCH_SCOPE, START_DATE, END_DATE,
                IGNORE_ACCENTS, WHOLE_WORD, INCLUDE_UNDATED)
    t0 = time.time()
    done = failed = 0
    with cf.ProcessPoolExecutor(max_workers=PROCESSES,
                                initializer=_init, initargs=(settings,)) as ex:
        for kind, row in ex.map(_work, urls, chunksize=4):
            done += 1
            if kind == "fail":
                failed += 1
            elif row:
                rows.append(row)
                save(rows)                              # checkpoint on each match
            if done % 200 == 0:
                el = time.time() - t0
                print(f"  {done}/{len(urls)} matched={len(rows)} failed={failed} "
                      f"({el:.0f}s, {el / done:.2f}s/article)", flush=True)

    rows.sort(key=lambda r: r["date"])
    save(rows)
    note = "" if failed == 0 else (
        f"  ⚠️ {failed} article(s) could not be downloaded even after retries — "
        "re-run to pick up any that were just transient.")
    print(f"\nDONE: {len(rows)} matches from {done} articles "
          f"({failed} failed) in {time.time() - t0:.0f}s → {out}{note}", flush=True)


if __name__ == "__main__":       # required so worker processes don't re-run main()
    main()
