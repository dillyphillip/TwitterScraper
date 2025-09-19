# save_login_state.py
from playwright.sync_api import sync_playwright

STATE_FILE = "x_state.json"

def main():
    with sync_playwright() as p:
        # Launch non-headless so you can solve 2FA/captcha if needed
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Go to X login and sign in manually
        page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")

        print("\n==> Log in manually in the opened window.")
        print("    After you see your Home timeline, come back to this terminal and press Enter.")
        input()

        # Persist cookies/localStorage to file
        ctx.storage_state(path=STATE_FILE)
        print(f"Saved session to {STATE_FILE}")

        browser.close()

if __name__ == "__main__":
    main()
