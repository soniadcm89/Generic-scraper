# 📰 News Article Scraper

A local web app for scraping news articles by **keyword** and **date range**,
with CSV export. Built with [Streamlit](https://streamlit.io),
[trafilatura](https://trafilatura.readthedocs.io) (fast article-text
extraction), [curl_cffi](https://github.com/lexiforest/curl_cffi) (a
browser-impersonating fetcher that handles Cloudflare-protected sites) and
[BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/) — no coding
needed to use it. [newspaper4k](https://github.com/AndyTheFactory/newspaper4k)
is used for the optional full-text export.

**How it works, in two phases:** the **search** downloads each in-window
article and does a fast, low-CPU text extraction to find your keyword matches
(→ the 3-column `title, url, date` CSV). Then, optionally, you can pull the
**full body text** of just those matched articles into a separate `url, text`
CSV for your own analysis. Because it fetches like a real browser, it works on
ordinary sites and Cloudflare-protected ones alike.

## Install

Requires **Python 3.11+**. From this folder:

```
pip install -r requirements.txt
```

## Run

```
streamlit run app.py
```

Your browser opens automatically (usually at `http://localhost:8501`). Then:

1. Paste the **target website** — any page on the news site (homepage, a
   section, or an article). The tool discovers articles across the **whole
   site**, not just that one page (see "How discovery works" below).
2. Enter **keywords**, comma-separated. An article matches if it contains
   **any** of them (case-insensitive). Choose whether to match in the title,
   the body, or both.
3. Pick the **publish date range** (inclusive).
4. Press **Scrape** and watch the progress bar. When it finishes, review the
   results table and click **Download CSV** (`title, url, date`).
5. **(Optional)** Click **Extract full texts** to download the full body text
   of just the matched articles into a separate `url, text` CSV — handy for
   qualitative coding or text analysis. It only runs over your results, so it's
   quick.

> **Cloudflare / rate-limited sites** (e.g. observador.pt) are handled
> automatically: the tool fetches like a real browser (so Cloudflare's bot-block
> is bypassed), and if a site **rate-limits** us (HTTP 429) it detects that and
> **auto-slows the request rate** to match — so a run stays complete without you
> touching "Parallel downloads". Such sites are just slower to finish, by their
> own choice; that's expected, not an error.
>
> A few sites do the **opposite** and reject the browser-impersonating fetcher at
> the connection level (e.g. cmjornal.pt). The tool detects that reject and
> transparently **falls back to an ordinary HTTP client** for that host, so those
> sites work too — no setting to change.

## How discovery works (whole-site search)

Giving the tool one page is enough — it does **not** stop at that page's links.
For each run it, in order:

1. **Reads the site's sitemap.** Almost every news site publishes an XML
   sitemap (often declared in `robots.txt`) that lists its whole archive, and
   this is the fast, polite way to enumerate it. The tool walks the sitemap
   tree, uses the month in each sub-sitemap's name plus each URL's own
   `lastmod` / `news:publication_date` to **skip everything outside your date
   range**, so it only downloads in-window articles.
2. **Scans the target page** for the very latest headlines that may not be in
   the sitemap yet.
3. **Falls back to crawling** (following links from the target page) only when
   a site has no usable sitemap.

> **Reaching archives older than the sitemap.** Some sites only keep a rolling
> window in their sitemap — cmjornal.pt, for example, keeps roughly the last two
> years, so older articles can't be found that way. Tick **"Also search the
> site's archive"** (Discovery settings) to *additionally* query the site's own
> search box for your keywords, which reaches the full back-catalogue (cmjornal
> goes back to ~2016). Caveats: the site search is relevance-ranked and can't be
> date-filtered on the server, so the tool pulls **all** keyword matches and then
> filters them by your date range locally; a few old indexed links are dead and
> are skipped; and recall is the site's own search index, not an exhaustive
> body scan. Currently wired up for **cmjornal.pt**; other hosts show a note.

Then it downloads each candidate article and keeps the ones whose title/body
matches your keywords and whose publish date is in range. While doing so, if
**"Follow related-article links on each page"** is on, it also queues the
article links found inside each downloaded page — so coverage isn't limited to
the sitemap, at no extra download cost.

> **Whole-site keyword search is inherently heavy.** A generic tool can't know
> which articles contain your keyword in the *body* without downloading each
> one. A busy site can publish hundreds of articles in a two-week window. The
> app downloads articles **in parallel** (see "Parallel downloads"), so a
> month-long window on a busy site typically takes ~20–30 minutes rather than
> hours — but it's still bounded by how fast the site serves pages and by your
> machine. Narrow the date range for quicker runs. Note: while a big scrape is
> running the app's own page can feel sluggish because your CPU is busy — that's
> expected, not a crash.

The CSV has exactly three columns: `title`, `url`, `date` (formatted
`YYYY-MM-DD`; empty if the date could not be detected and you enabled
"Include articles with no detectable date").

### Useful settings (sidebar)

- **Delay between requests** — pause each download thread waits between
  articles (default 0.5 s). Overall request rate ≈ *(parallel downloads ÷
  delay)*.
- **Parallel downloads** — how many articles are fetched at once (default 8).
  This is the main speed lever: ~4× faster than one-at-a-time. Raise it (up to
  16) for more speed, lower it to be gentler on the site or your machine.
- **Check every article in the date range (no limit)** — on by default; the
  tool downloads and keyword-checks *every* in-window article it discovers, for
  complete results. A full run reads each article, so it can take a while:
  roughly *(number of in-window articles × delay)*. Uncheck it to reveal a
  **Max articles to check** cap for a quick partial run.
- **Include articles with no detectable date** — many articles have no
  machine-readable date; off by default.
- **Search the whole site via its sitemap** — on by default; the primary,
  polite discovery method. Turn it off to restrict discovery to the target
  page plus the crawl fallback.
- **Crawl the site if no sitemap is found** — on by default; only kicks in when
  the sitemap yields fewer candidates than your cap.
- **Follow related-article links on each page** — **off by default.** When on,
  it also checks the articles directly linked from each matched page — **one hop
  only** (those links are not expanded further). A linked article is kept only
  if its URL date is inside your range; anything out of range, or with no date
  in its URL, is discarded before download. Most news sites have a complete
  sitemap that already lists every article, so leaving this off is usually best;
  turn it on only for sites whose sitemap is thin.
- **Max pages to crawl** — cap on pages fetched during the crawl fallback
  (default 150).
- **URL patterns to exclude** — comma-separated substrings; any discovered link
  whose URL contains one of them (case-insensitive) is dropped before it's
  downloaded. The article-detection heuristic is generic, so section pages like
  `corrections-and-clarifications` sometimes slip through and eat into your
  "Max articles" budget. Blacklist them here, e.g.
  `corrections-and-clarifications, /opinion/, /sport/`. Empty by default.

## Faster runs: batch mode (`scrape_batch.py`)

The Streamlit app downloads articles in parallel with threads, which is limited
by Python's GIL (and by not being able to use multiple CPU processes safely from
inside Streamlit). For a **big job where you want maximum speed**, use the
command-line batch tool instead — it spreads the work across CPU **processes**
and is roughly **twice as fast** (e.g. ~1,900 articles in ~5 minutes instead of
~15–20).

1. Open `scrape_batch.py` and edit the **SETTINGS** block at the top (target
   site, keywords, date range, matching options, number of processes).
2. From this folder run:
   ```
   python scrape_batch.py
   ```
3. Watch the progress in the terminal; results are written to the CSV named in
   `OUTPUT_CSV` (same folder) and updated as matches are found.

It produces the same three-column CSV as the app. Use the app for interactive,
exploratory searches; use the batch script for large, final runs.

## Responsible scraping — please read

- The app **honours robots.txt**: URLs a site disallows are skipped
  automatically and reported in the UI.
- Keep the **request delay at 1 second or more** so you don't burden the
  site's servers.
- robots.txt is not the whole story: check the site's **terms of service**
  before scraping, and only use the collected data in ways the site and your
  institution's research-ethics rules allow.
- Scrape only what you need — narrow the **date range** (which limits how many
  articles are downloaded) and keep the cap and delay sensible rather than
  pulling a whole archive at full speed.

## Troubleshooting

- **"No article links discovered"** — the site may have no readable sitemap and
  block crawling, or render its links with JavaScript (which this tool can't
  see). Make sure the URL starts with `https://`.
- **Fewer results than expected** — raise "Max articles to check": with a busy
  site and a wide date range, the matches you want may be beyond the cap. Check
  the run summary ("Discovered N links, checked M") — if `checked` equals your
  cap, there were more in-window articles than you allowed it to read.
- **Few or no dates found** — some sites don't publish machine-readable
  dates; enable the undated-articles checkbox to see those matches anyway.
- **Everything fails to download** — the site may block automated clients;
  try another site or a longer delay.

## License

This project is dual-licensed:

- **Code** (e.g. `scraper.py`, `app.py`, `scrape_batch.py`) — **MIT License**.
  See [`LICENSE-MIT`](LICENSE-MIT).
- **Documentation** and other written materials (e.g. this `README.md` and the
  `.docx` reports) — **Creative Commons Attribution-NonCommercial 4.0
  International (CC BY-NC 4.0)**. See [`LICENSE-CC-BY-NC-4.0`](LICENSE-CC-BY-NC-4.0).

In short: the code is free to reuse (including commercially) with attribution
and the MIT notice; the documentation may be shared and adapted for
**non-commercial** purposes with attribution.

> Note: the scraped article data itself is **not** covered by these licenses —
> it remains subject to each source site's own copyright and terms of service.
