"""
app.py — Streamlit UI for the news article scraper.

Run with:  streamlit run app.py

All the scraping logic lives in scraper.py; this file only collects the
settings, shows progress, and displays / exports the results.
"""

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from scraper import ScrapeConfig, run_scrape, extract_texts

st.set_page_config(page_title="News Article Scraper", page_icon="📰", layout="wide")

st.title("📰 News Article Scraper")
st.caption(
    "Scrape a news site by keyword and date range, then export the matches to CSV. "
    "Please scrape responsibly: this tool honours robots.txt and lets you throttle requests."
)

# --------------------------------------------------------------------------- #
# Sidebar — politeness / safety settings
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Settings")
    delay_seconds = st.number_input(
        "Delay between requests (seconds)",
        min_value=0.0, max_value=30.0, value=0.5, step=0.5,
        help="Pause each download thread waits between articles. With parallel "
             "downloads, the overall request rate is about (workers ÷ delay).",
    )
    workers = st.number_input(
        "Parallel downloads",
        min_value=1, max_value=24, value=16, step=1,
        help="How many articles to download at once — the main speed lever. "
             "You can usually leave this at 16: if a site rate-limits us (HTTP "
             "429), the tool now detects it and automatically slows the request "
             "rate to match, so you don't have to tune this per site.",
    )
    no_limit = st.checkbox(
        "Check every article in the date range (no limit)",
        value=True,
        help="Download and keyword-check ALL in-window articles the tool finds — "
             "best for complete research results. A full run reads every article, "
             "so it can take a while: roughly (number of in-window articles × "
             "delay). Uncheck to set a smaller cap for a quick partial run.",
    )
    if no_limit:
        max_articles = 0            # 0 = unlimited
    else:
        max_articles = st.number_input(
            "Max articles to check",
            min_value=1, max_value=50000, value=500, step=50,
            help="Cap on how many in-window article bodies are downloaded and "
                 "keyword-checked. The run takes about (this number × delay) "
                 "seconds.",
        )
    include_undated = st.checkbox(
        "Include articles with no detectable date",
        value=False,
        help="Many articles have no machine-readable publish date. "
             "When on, keyword matches without a date are kept (empty date cell); "
             "when off, they are excluded.",
    )

    st.divider()
    st.subheader("Discovery")
    use_sitemap = st.checkbox(
        "Search the whole site via its sitemap",
        value=True,
        help="Enumerate the entire site from its sitemap (the fast, polite way to "
             "reach the full archive, not just links on the page you enter). The "
             "date range below pre-filters the sitemap so only in-window articles "
             "are downloaded.",
    )
    crawl_fallback = st.checkbox(
        "Crawl the site if no sitemap is found",
        value=True,
        help="If the site has no usable sitemap, follow links from the target "
             "page to discover articles. Slower and heavier on the site.",
    )
    follow_links = st.checkbox(
        "Follow related-article links on each page",
        value=False,
        help="Also check the articles directly linked from each matched page "
             "(one hop only — those links are not themselves expanded further). "
             "A linked article is kept only if its URL date falls inside your "
             "date range; anything outside the range, or without a date in its "
             "URL, is discarded automatically. Most news sites have a complete "
             "sitemap, so this adds little there — mainly useful for sites whose "
             "sitemap is thin.",
    )
    max_crawl_pages = st.number_input(
        "Max pages to crawl (fallback only)",
        min_value=10, max_value=2000, value=150, step=10,
        help="Cap on pages fetched during the crawl fallback.",
    )
    exclude_patterns_raw = st.text_input(
        "URL patterns to exclude (comma-separated)",
        placeholder="corrections-and-clarifications, /opinion/, /sport/",
        help="Discard any discovered link whose URL contains one of these "
             "substrings (case-insensitive). Use it to skip section pages that "
             "slip past the article filter and waste the article budget.",
    )

# --------------------------------------------------------------------------- #
# Main form — what to scrape
# --------------------------------------------------------------------------- #
target_url = st.text_input(
    "Target website (news site homepage or section URL)",
    placeholder="https://www.example-news.com/world",
)

keywords_raw = st.text_input(
    "Keywords (comma-separated — an article matches if it contains ANY of them)",
    placeholder="climate, energy, emissions",
)

match_scope_label = st.radio(
    "Match keywords in",
    options=["Title only", "Body only", "Title + body"],
    index=2,
    horizontal=True,
)
# Map the UI label to the value scraper.py expects.
SCOPE_MAP = {"Title only": "title", "Body only": "body", "Title + body": "title+body"}

ignore_accents = st.checkbox(
    "Ignore accents / diacritics",
    value=True,
    help="Match regardless of accents — e.g. “género” also finds “genero” and "
         "“gênero”. Recommended for Portuguese, Spanish and French. Mirrors how a "
         "news site's own search box normally behaves.",
)

whole_word = st.checkbox(
    "Match whole words only",
    value=False,
    help="Off (default): a keyword matches anywhere, so “género” also catches "
         "“transgénero” and “géneros” — but also unrelated words like "
         "“generosa”/“generosidade”. On: only the standalone word matches "
         "(cleaner, but you'd add variants like “transgénero” as their own "
         "keywords).",
)

date_range = st.date_input(
    "Publish date range (inclusive)",
    value=(date.today() - timedelta(days=30), date.today()),
    help="Only articles published inside this range are kept.",
)

run_clicked = st.button("🔎 Scrape", type="primary")

# --------------------------------------------------------------------------- #
# Run the pipeline
# --------------------------------------------------------------------------- #
if run_clicked:
    # ---- validate inputs before doing anything ---------------------------- #
    problems = []
    url = target_url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url          # be forgiving about a missing scheme
    if not url:
        problems.append("Please enter a target website URL.")

    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    if not keywords:
        problems.append("Please enter at least one keyword.")

    exclude_patterns = [p.strip() for p in exclude_patterns_raw.split(",") if p.strip()]

    # st.date_input returns a single date while the user is mid-selection.
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        problems.append("Please select both a start AND an end date.")
        start_date = end_date = None

    if problems:
        for p in problems:
            st.warning(p)
    else:
        config = ScrapeConfig(
            target_url=url,
            keywords=keywords,
            match_scope=SCOPE_MAP[match_scope_label],
            start_date=start_date,
            end_date=end_date,
            include_undated=include_undated,
            delay_seconds=float(delay_seconds),
            max_articles=int(max_articles),
            exclude_patterns=exclude_patterns,
            use_sitemap=use_sitemap,
            crawl_fallback=crawl_fallback,
            max_crawl_pages=int(max_crawl_pages),
            ignore_accents=ignore_accents,
            whole_word=whole_word,
            follow_links=follow_links,
            workers=int(workers),
        )

        progress_bar = st.progress(0.0)
        counter_box = st.empty()        # live "checked / matched" line
        status_box = st.empty()         # current URL being processed

        def on_progress(checked: int, total: int, matched: int, message: str):
            """Called by the pipeline after each step; updates the live UI."""
            fraction = checked / total if total else 0.0
            progress_bar.progress(min(fraction, 1.0))
            counter_box.markdown(
                f"**Pages checked:** {checked}"
                + (f" / {total}" if total else "")
                + f" &nbsp;|&nbsp; **Articles matched:** {matched}"
            )
            status_box.caption(message)

        try:
            with st.spinner("Scraping… this can take a while with a polite delay."):
                result = run_scrape(config, on_progress)
        except Exception as exc:  # noqa: BLE001 — belt & braces: never crash the app
            st.error(f"Unexpected error during scraping: {exc}")
            result = None

        if result is not None:
            progress_bar.progress(1.0)
            status_box.empty()

            # ---- summary + non-fatal warnings ----------------------------- #
            st.success(
                f"Done. Discovered {result.discovered} in-window candidate "
                f"articles, checked {result.checked}, matched {len(result.rows)}."
            )
            if result.truncated:
                st.warning(
                    f"⚠️ Only the newest {result.checked} of {result.discovered} "
                    "in-window articles were checked because of the “Max articles "
                    "to check” cap. Some matches may be missing — raise the cap "
                    f"to at least {result.discovered} for a complete search "
                    "(expect it to take longer)."
                )
            if result.rate_limit_hits:
                st.info(
                    f"This site rate-limited us ({result.rate_limit_hits} times), "
                    "so the tool automatically slowed the download rate to keep "
                    "the run complete. That's expected on protected sites like "
                    "observador — no action needed."
                )
            if result.failed:
                st.warning(
                    f"{result.failed} article(s) couldn't be downloaded even after "
                    "retries. Re-run to pick up any that were only temporarily "
                    "unavailable — the match count can vary slightly when "
                    "downloads fail."
                )
            if result.skipped_robots:
                st.info(f"{result.skipped_robots} URL(s) skipped — disallowed by robots.txt.")
            if result.skipped_undated:
                st.info(
                    f"{result.skipped_undated} keyword match(es) dropped because no "
                    "publish date was found. Tick “Include articles with no detectable "
                    "date” in the sidebar to keep them."
                )
            if result.errors:
                with st.expander(f"⚠️ {len(result.errors)} problem(s) during the run"):
                    for msg in result.errors:
                        st.warning(msg)

            # Keep results across Streamlit reruns (e.g. the download click).
            st.session_state["results_df"] = pd.DataFrame(
                result.rows, columns=["title", "url", "date"]
            )

# --------------------------------------------------------------------------- #
# Results table + CSV download (shown whenever we have results in the session)
# --------------------------------------------------------------------------- #
if "results_df" in st.session_state:
    df = st.session_state["results_df"]
    st.subheader(f"Results ({len(df)} articles)")
    if df.empty:
        st.info("No articles matched your keywords and date range.")
    else:
        st.dataframe(df, width="stretch", hide_index=True)

    # Exactly three columns, in order: title, url, date.
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")  # BOM helps Excel
    st.download_button(
        "⬇️ Download CSV (title, url, date)",
        data=csv_bytes,
        file_name="scraped_articles.csv",
        mime="text/csv",
    )

    # ---- optional: full article texts of the matches only ----------------- #
    if not df.empty:
        st.divider()
        st.subheader("Full article texts (optional)")
        st.caption(
            "Download the full body text of just these matched articles "
            "(extracted with newspaper4k). This runs only over the results "
            f"above ({len(df)} articles), so it's quick — separate from the "
            "matches CSV."
        )
        if st.button(f"📄 Extract full texts of these {len(df)} articles"):
            urls = df["url"].tolist()
            tp = st.progress(0.0)
            tstatus = st.empty()

            def on_text_progress(done: int, total: int):
                tp.progress(min(done / total, 1.0) if total else 1.0)
                tstatus.caption(f"Fetching text {done}/{total}…")

            try:
                with st.spinner("Downloading full texts…"):
                    rows = extract_texts(urls, workers=int(workers),
                                         progress=on_text_progress)
                tstatus.empty()
                st.session_state["texts_df"] = pd.DataFrame(rows,
                                                            columns=["url", "text"])
            except Exception as exc:  # noqa: BLE001
                st.error(f"Text extraction failed: {exc}")

    if "texts_df" in st.session_state:
        tdf = st.session_state["texts_df"]
        empty_n = int((tdf["text"].str.len() == 0).sum())
        st.success(
            f"Full text ready for {len(tdf)} articles"
            + (f" ({empty_n} couldn't be downloaded)." if empty_n else ".")
        )
        texts_bytes = tdf.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Download texts CSV (url, text)",
            data=texts_bytes,
            file_name="scraped_texts.csv",
            mime="text/csv",
        )
