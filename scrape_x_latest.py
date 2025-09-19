# scrape_x_latest_df.py
import re
import time
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
EXPORT_PARQUET = None                  # e.g., "tweets_TicTocTick.parquet"

def extract_status_id(url: str) -> Optional[str]:
    """Return the numeric status ID from a tweet URL, if present."""
    try:
        path = urlparse(url).path  # /<user>/status/<id>
        m = re.search(r"/status/(\d+)", path)
        return m.group(1) if m else None
    except Exception:
        return None

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

        # Timestamp (ISO 8601 if available)
        ts = ""
        tnode = a.locator("time").first
        if tnode.count() > 0:
            ts = tnode.get_attribute("datetime") or ""

        items.append({
            "id": status_id,
            "url": href,
            "created_at": ts,
            "text": text
        })
    return items

def scrape_to_dataframe(profile_url: str = PROFILE,
                        state_file: str = STATE_FILE,
                        max_tweets: int = MAX_TWEETS) -> pd.DataFrame:
    """Scrape latest tweets and return a pandas DataFrame."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=state_file)

        # Block heavy assets for speed
        def _route(route):
            r = route.request
            if r.resource_type in {"image", "media", "font"}:
                return route.abort()
            return route.continue_()
        ctx.route("**/*", _route)

        page = ctx.new_page()
        page.goto(profile_url, wait_until="domcontentloaded")

        # If protected & you’re not approved, no posts will be visible
        try:
            protected = page.locator("text=posts are protected").first
            if protected.is_visible():
                browser.close()
                return pd.DataFrame(columns=["id","username","created_at","text","url"])
        except PWTimeout:
            pass

        # Wait for at least one tweet
        try:
            page.locator('article[data-testid="tweet"]').first.wait_for(timeout=8000)
        except PWTimeout:
            browser.close()
            return pd.DataFrame(columns=["id","username","created_at","text","url"])

        # Scroll & collect until we have enough
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

    # Build DataFrame
    username = urlparse(profile_url).path.strip("/").split("/")[0] or "unknown"
    df = pd.DataFrame(items[:max_tweets])
    if df.empty:
        # Ensure consistent columns even if empty
        return pd.DataFrame(columns=["id","username","created_at","text","url"])

    df.insert(1, "username", username)               # add username column
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df = df.drop_duplicates(subset=["id"]).sort_values("created_at", ascending=False).reset_index(drop=True)

    return df

if __name__ == "__main__":
    df = scrape_to_dataframe()
    print(df[["id","username","created_at","text","url"]])

    # Optional exports
    if EXPORT_CSV:
        df.to_csv(EXPORT_CSV, index=False)
        print(f"\nSaved CSV -> {EXPORT_CSV}")
    if EXPORT_PARQUET:
        df.to_parquet(EXPORT_PARQUET, index=False)
        print(f"Saved Parquet -> {EXPORT_PARQUET}")
