# scrape_x_latest_df_mediaurls.py
import re, time
from urllib.parse import urlparse
from typing import List, Dict, Optional

import pandas as pd

# ---------- Pandas display (optional) ----------
pd.set_option('display.max_columns', None)
pd.set_option('display.max_rows', None)
pd.set_option('display.width', 1000)
pd.set_option('display.max_colwidth', 50)

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

STATE_FILE = "x_state.json"          # created by save_login_state.py
PROFILE    = "https://x.com/TicTocTick"
MAX_TWEETS = 10
EXPORT_CSV = "tweets_TicTocTick.csv"   # set to None to skip

def extract_status_id(url: str) -> Optional[str]:
    try:
        path = urlparse(url).path  # /<user>/status/<id>
        m = re.search(r"/status/(\d+)", path)
        return m.group(1) if m else None
    except Exception:
        return None

def collect_media_urls(article) -> List[str]:
    """Return a list of media URLs (photos, video/gif src or poster) for one tweet <article>."""
    urls: List[str] = []

    # Photos (pbs.twimg.com/media/...)
    photos = article.locator("div[data-testid='tweetPhoto'] img[src]")
    for i in range(photos.count()):
        src = photos.nth(i).get_attribute("src") or ""
        if src:
            urls.append(src)

    # Videos/GIFs (best-effort: direct <video> src or first <source>, else poster)
    videos = article.locator("div[data-testid='videoPlayer'] video")
    for i in range(videos.count()):
        v = videos.nth(i)
        src = ""
        src_nodes = v.locator("source[src]")
        if src_nodes.count() > 0:
            src = src_nodes.nth(0).get_attribute("src") or ""
        if not src:
            src = v.get_attribute("src") or ""
        poster = v.get_attribute("poster") or ""
        if src:
            urls.append(src)
        elif poster:
            urls.append(poster)

    # Dedup while preserving order
    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def collect_tweets_from_page(page) -> List[Dict]:
    """Collect tweet metadata from currently loaded DOM."""
    items = []
    articles = page.locator('article[data-testid="tweet"]')
    count = articles.count()

    for i in range(count):
        a = articles.nth(i)

        # URL / status id
        status_link = a.locator("a[href*='/status/']").first
        if status_link.count() == 0:
            continue
        href = status_link.get_attribute("href") or ""
        status_id = extract_status_id(href)
        if not status_id:
            continue

        # Text
        text = ""
        tx = a.locator("div[data-testid='tweetText']")
        if tx.count() > 0:
            try:
                text = tx.inner_text().strip()
            except Exception:
                pass
        if not text:
            try:
                text = a.inner_text().strip()
            except Exception:
                text = ""

        # Timestamp
        ts = ""
        tnode = a.locator("time").first
        if tnode.count() > 0:
            ts = tnode.get_attribute("datetime") or ""

        # Media URLs
        media_urls = collect_media_urls(a)

        items.append({
            "id": status_id,
            "url": href,
            "created_at": ts,
            "text": text,
            "media_urls": media_urls
        })
    return items

def scrape_to_dataframe(profile_url: str = PROFILE,
                        state_file: str = STATE_FILE,
                        max_tweets: int = MAX_TWEETS) -> pd.DataFrame:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=state_file)

        # IMPORTANT: allow images so <img src> exists (don’t abort images here)
        # If you previously blocked images for speed, remove that route.
        page = ctx.new_page()
        page.goto(profile_url, wait_until="domcontentloaded")

        # Protected account check
        try:
            protected = page.locator("text=posts are protected").first
            if protected.is_visible():
                browser.close()
                return pd.DataFrame(columns=["id","username","created_at","text","url","media_urls","has_media"])
        except PWTimeout:
            pass

        try:
            page.locator('article[data-testid="tweet"]').first.wait_for(timeout=8000)
        except PWTimeout:
            browser.close()
            return pd.DataFrame(columns=["id","username","created_at","text","url","media_urls","has_media"])

        # Scroll & collect
        seen = set()
        items: List[Dict] = []

        def add_batch():
            for t in collect_tweets_from_page(page):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    items.append(t)

        idle_rounds = 0
        while len(items) < max_tweets and idle_rounds < 8:
            before = len(items)
            add_batch()
            if len(items) >= max_tweets:
                break
            page.mouse.wheel(0, 2000)
            time.sleep(0.6)
            add_batch()
            idle_rounds = idle_rounds + 1 if len(items) == before else 0

        browser.close()

    username = urlparse(profile_url).path.strip("/").split("/")[0] or "unknown"
    df = pd.DataFrame(items[:max_tweets])
    if df.empty:
        return pd.DataFrame(columns=["id","username","created_at","text","url","media_urls","has_media"])

    df.insert(1, "username", username)
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df["has_media"] = df["media_urls"].apply(lambda xs: bool(xs))
    df = df.drop_duplicates(subset=["id"]).sort_values("created_at", ascending=False).reset_index(drop=True)
    return df

if __name__ == "__main__":
    df = scrape_to_dataframe()
    print(df[["id","username","created_at","has_media","media_urls","url"]])
    if EXPORT_CSV:
        df.to_csv(EXPORT_CSV, index=False)
        print(f"\nSaved CSV -> {EXPORT_CSV}")
