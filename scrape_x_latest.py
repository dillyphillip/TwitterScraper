# monitor_many_x_to_discord.py
import re, time, sys, requests
from urllib.parse import urlparse
from typing import List, Dict, Optional
from datetime import datetime

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, BrowserContext

# ------------------- Config -------------------
STATE_FILE = "x_state.json"     # created by save_login_state.py (manual login -> saved state)
INTERVAL_DEFAULT = 15           # seconds between polls
MAX_TWEETS_DEFAULT = 10         # how many recent tweets to look at per profile

# Monitor these profiles (URL, @handle, or username)
USERS = [
    "https://x.com/TicTocTick",
    # "https://x.com/dillysnipe",
]

# Discord (fill these in)
BOT_TOKEN  = "YOUR_DISCORD_BOT_TOKEN_HERE"
CHANNEL_ID = "YOUR_DISCORD_CHANNEL_ID_HERE"
# ------------------------------------------------

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 120)
pd.set_option("display.max_colwidth", 120)

# ------------- Discord send -------------
def send_message(token: str, channel_id: str, content: str):
    if not token or not channel_id:
        print("Discord token or channel_id missing. Skipping message send.")
        return
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    payload = {"content": content}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            print("Message sent successfully!")
        else:
            print(f"Failed to send message: {resp.status_code}")
            try:
                print(f"Error details: {resp.json()}")
            except Exception:
                print("No JSON error body.")
            print("Check token, channel ID, and bot permissions.")
    except Exception as e:
        print(f"Error sending message: {e}")

# ------------- Helpers / scraping core -------------
def parse_username(profile: str) -> str:
    """
    Accepts 'https://x.com/user', '@user', or 'user' and returns 'user'.
    """
    s = profile.strip()
    if s.startswith("@"):
        return s[1:]
    if s.startswith("http"):
        path = urlparse(s).path.strip("/")
        return path.split("/")[0] if path else ""
    return s

def extract_status_id(url: str) -> Optional[str]:
    try:
        path = urlparse(url).path   # /<user>/status/<id>
        m = re.search(r"/status/(\d+)", path)
        return m.group(1) if m else None
    except Exception:
        return None

def collect_tweets_from_page(page) -> List[Dict]:
    """
    Scrape the currently-loaded profile page for tweets (articles).
    Returns a list of dicts with id, url, created_at (ISO), text.
    """
    items = []
    articles = page.locator('article[data-testid="tweet"]')
    count = articles.count()

    for i in range(count):
        a = articles.nth(i)

        # Grab a status link to derive ID/URL
        status_link = a.locator("a[href*='/status/']").first
        if status_link.count() == 0:
            continue
        href = status_link.get_attribute("href") or ""
        status_id = extract_status_id(href)
        if not status_id:
            continue

        # Text (best-effort)
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

        items.append({
            "id": status_id,
            "url": href,          # may be relative like "/user/status/123"
            "created_at": ts,     # ISO string
            "text": text,
        })
    return items

def scrape_profile_df(ctx: BrowserContext, profile: str, max_tweets: int = MAX_TWEETS_DEFAULT) -> pd.DataFrame:
    """
    Uses an existing Playwright BrowserContext to scrape a single profile.
    Returns DataFrame with: id, username, created_at (datetime), text, url.
    """
    username = parse_username(profile)
    if not username:
        return pd.DataFrame(columns=["id", "username", "created_at", "text", "url"])

    profile_url = f"https://x.com/{username}"
    page = ctx.new_page()
    page.goto(profile_url, wait_until="domcontentloaded")

    # Protected/private visibility check
    try:
        protected = page.locator("text=posts are protected").first
        if protected.is_visible():
            page.close()
            return pd.DataFrame(columns=["id", "username", "created_at", "text", "url"])
    except PWTimeout:
        pass

    # Wait for at least one tweet (may time out for empty/protected)
    try:
        page.locator('article[data-testid="tweet"]').first.wait_for(timeout=8000)
    except PWTimeout:
        page.close()
        return pd.DataFrame(columns=["id", "username", "created_at", "text", "url"])

    # Scroll to collect enough tweets
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

    page.close()

    df = pd.DataFrame(items[:max_tweets])
    if df.empty:
        return pd.DataFrame(columns=["id", "username", "created_at", "text", "url"])

    df.insert(1, "username", username)
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df = df.drop_duplicates(subset=["id"]).sort_values("created_at", ascending=False).reset_index(drop=True)
    return df

# ---- NEW: formatting helpers for message ----
def format_created(ts: pd.Timestamp) -> str:
    """
    Render like 'September 19, 2025 at 18:40:44'.
    (No timezone conversion; uses whatever tz the timestamp has.)
    """
    if pd.isna(ts):
        return ""
    try:
        return ts.strftime("%B %d, %Y at %H:%M:%S")
    except Exception:
        return str(ts)

def build_tweet_url(username: str, tweet_id: str, scraped_url: str) -> str:
    """
    Ensure we send a full 'https://x.com/...' URL to Discord.
    Prefer reconstructing from username + id to avoid relative paths.
    """
    if scraped_url and scraped_url.startswith("http"):
        return scraped_url
    return f"https://x.com/{username}/status/{tweet_id}"

# ------------- Monitor-many loop -------------
def monitor_many(profiles: List[str],
                 interval_sec: int = INTERVAL_DEFAULT,
                 max_tweets: int = MAX_TWEETS_DEFAULT,
                 heartbeat: bool = False):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("Please set BOT_TOKEN and CHANNEL_ID before running.")
        sys.exit(1)

    usernames = [parse_username(p) for p in profiles if parse_username(p)]
    if not usernames:
        print("No valid profiles provided.")
        sys.exit(1)

    print(f"Monitoring {len(usernames)} profiles every {interval_sec}s: {', '.join('@'+u for u in usernames)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(storage_state=STATE_FILE)

        # Initialize references
        ref_ids: Dict[str, set] = {}
        for u in usernames:
            df0 = scrape_profile_df(ctx, u, max_tweets=max_tweets)
            ref_ids[u] = set(df0["id"]) if not df0.empty else set()
            print(f"Initial reference for @{u}: {len(ref_ids[u])} tweets.")

        try:
            while True:
                time.sleep(interval_sec)

                for u in usernames:
                    cur_df = scrape_profile_df(ctx, u, max_tweets=max_tweets)
                    cur_ids = set(cur_df["id"]) if not cur_df.empty else set()
                    new_ids = list(cur_ids - ref_ids[u])

                    if new_ids:
                        new_df = cur_df[cur_df["id"].isin(new_ids)].copy()
                        for _, row in new_df.sort_values("created_at", ascending=False).iterrows():
                            created_str = format_created(row["created_at"])
                            full_url = build_tweet_url(row["username"], row["id"], row["url"])
                            msg = (
                                f"New tweet from @{row['username']} at {created_str}:\n\n"
                                f"{(row['text'] or '').strip()}\n\n"
                                f"source: {full_url}"
                            )
                            print(msg)
                            send_message(BOT_TOKEN, CHANNEL_ID, msg)

                        # Update reference for this user
                        ref_ids[u] = cur_ids
                    else:
                        if heartbeat:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] @{u}: no new tweets.")

        except KeyboardInterrupt:
            print("\nStopped by user. Goodbye!")
        finally:
            ctx.close()
            browser.close()

# ------------- Run -------------
if __name__ == "__main__":
    # Edit USERS / BOT_TOKEN / CHANNEL_ID at top, then run:
    monitor_many(USERS, interval_sec=INTERVAL_DEFAULT, max_tweets=MAX_TWEETS_DEFAULT, heartbeat=True)
