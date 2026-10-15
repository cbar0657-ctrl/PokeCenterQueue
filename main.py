import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from playwright.sync_api import sync_playwright

# 🔥 DEBUG START
print("🔥 BOT STARTING...", flush=True)
print(f"🐍 Python version: {sys.version}", flush=True)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
STATUS_WEBHOOK_URL = os.getenv("STATUS_WEBHOOK_URL", "")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "20"))
MENTION_TEXT = os.getenv("MENTION_TEXT", "@everyone")
BROWSERLESS_WS_URL = os.getenv("BROWSERLESS_WS_URL", "")

print(f"🔑 Webhook set: {bool(DISCORD_WEBHOOK_URL)}", flush=True)
print(f"📋 Status webhook set: {bool(STATUS_WEBHOOK_URL)}", flush=True)
print(f"🌐 Browserless URL set: {bool(BROWSERLESS_WS_URL)}", flush=True)

if not BROWSERLESS_WS_URL:
    raise RuntimeError("❌ BROWSERLESS_WS_URL is not set")

STATE_FILE = Path("state.json")
HEARTBEAT_FILE = Path("heartbeat.txt")

STORES = {
    "US": {
        "name": "Pokémon Center US",
        "flag": "🇺🇸",
        "url": "https://www.pokemoncenter.com/en-us",
    }
}

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    return {
        code: {
            "was_live": False,
            "last_final_url": None,
        }
        for code in STORES
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_discord_message(payload: dict):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK_URL not set", flush=True)
        return

    response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    response.raise_for_status()


def send_discord_alert(store: Dict[str, str], final_url: str):
    payload = {
        "content": f"{MENTION_TEXT} 🚨 **QUEUE LIVE** 🚨",
        "embeds": [
            {
                "title": f"{store['flag']} {store['name']} Queue Detected",
                "url": store["url"],
                "description": (
                    "🟢 **Queue is LIVE — act fast!**\n\n"
                    f"👉 [Open Store]({store['url']})\n"
                    f"👉 [Join Queue]({final_url})"
                ),
                "color": 16711680,
                "footer": {
                    "text": "PokeCenter Monitor • Live Alert"
                },
            }
        ],
    }

    try:
        send_discord_message(payload)
        print(f"📨 Discord alert sent for {store['name']}", flush=True)
    except Exception as e:
        print(f"Discord error: {e}", flush=True)


def send_status_message(text: str):
    if not STATUS_WEBHOOK_URL:
        return

    payload = {"content": text}
    try:
        response = requests.post(STATUS_WEBHOOK_URL, json=payload, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Status message error: {e}", flush=True)



def detect_queue(page, store_url: str) -> Tuple[bool, str]:
    print(f"🌍 Visiting {store_url}", flush=True)

    response = page.goto(
        store_url,
        wait_until="domcontentloaded",
        timeout=45000
    )

    status = None if response is None else response.status

    # Allow time for a possible waiting-room redirect
    page.wait_for_timeout(3000)

    final_url = page.url
    final_url_lower = final_url.lower()

    print(
        f"➡️ Status: {status} | Final URL: {final_url}",
        flush=True
    )

    try:
        title = page.title()
    except Exception:
        title = ""

    try:
        text = page.locator("body").inner_text(
            timeout=10000
        ).lower()
    except Exception:
        text = ""

    print(f"📄 Page title: {title}", flush=True)
    print(f"📄 Page preview: {text[:300]}", flush=True)

    # Strong indicators of a waiting room
    queue_url_indicators = [
        "queue-it.net",
        "queue-it.com",
        "waitingroom",
        "waiting-room",
    ]


queue_text_indicators = [
    "you're in the virtual queue",
    "you are in the virtual queue",
    "virtual queue to enter",
    "estimated wait time",
    "you are now in line",
    "you are in line",
    "virtual waiting room",
    "line is paused",
    "your place in line",
    "keep this window open to stay in the queue",
    "you'll automatically be redirected to the site",
]


    url_match = any(
        indicator in final_url_lower
        for indicator in queue_url_indicators
    )

    text_match = any(
        indicator in text
        for indicator in queue_text_indicators
    )

    if url_match or text_match:
        print("🚨 Queue detected!", flush=True)
        return True, final_url

    # A forbidden response without queue evidence
    # must not be interpreted as an inactive queue.
    if status is not None and status >= 400:
        raise RuntimeError(
            f"HTTP {status}: Access blocked or page unavailable"
        )

if not text.strip() and not title.strip():
    raise RuntimeError(
        "Empty page: unable to determine queue status"
    )


if not text.strip() and not title.strip():
    raise RuntimeError(
        "UNKNOWN: HTTP response succeeded, but page "
        "content was empty. Cannot determine queue status."
    )

print("✅ No queue detected", flush=True)
return False, final_url



    indicators = [
        "queue-it",
        "waiting room",
        "virtual queue",
        "line is paused",
        "you are now in line",
        "queue",
    ]

    is_live = any(x in final_url_lower for x in indicators) or any(
        x in text for x in indicators
    )

    return is_live, final_url



def main():
    state = load_state()
    startup_sent = False

    print("US Pokemon Center Queue Monitor started!", flush=True)
    print("Schedule: Monday-Friday, 10 AM-4 PM Eastern", flush=True)
    print("Check interval: 9 minutes", flush=True)

    # Monthly limit on attempted Browserless sessions.
    # This is NOT a guaranteed unit cap.
    MONTHLY_CHECK_LIMIT = 850
    CHECK_INTERVAL = 540

    current_month = None
    monthly_checks = 0

    while True:
        now = datetime.now(ZoneInfo("America/New_York"))
        month = now.strftime("%Y-%m")

        HEARTBEAT_FILE.write_text(str(int(time.time())))

        # Reset check counter each month
        if month != current_month:
            current_month = month
            monthly_checks = 0

        # Monday-Friday, 10 AM to 4 PM Eastern
        if now.weekday() >= 5 or not (10 <= now.hour < 16):
            time.sleep(300)
            continue

        # Stop checking if monthly limit is reached
        if monthly_checks >= MONTHLY_CHECK_LIMIT:
            print("Monthly check limit reached.", flush=True)
            time.sleep(3600)
            continue

        monthly_checks += 1

        try:
            if not startup_sent:
                send_status_message(
                    "US Pokemon Center Monitor is online"
                )
                startup_sent = True

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(
                    BROWSERLESS_WS_URL
                )

                try:
                    context = browser.new_context(
                        user_agent=BROWSER_USER_AGENT,
                        locale="en-US",
                        viewport={"width": 1440, "height": 900},
                    )

                    try:
                        store = STORES["US"]
                        page = context.new_page()

                        try:
                            is_live, final_url = detect_queue(
                                page, store["url"]
                            )

                            was_live = state["US"]["was_live"]

                            print(
                                f"US Queue Active: {is_live}",
                                flush=True
                            )

                            if is_live and not was_live:
                                send_discord_alert(
                                    store, final_url
                                )

                            state["US"]["was_live"] = is_live
                            state["US"]["last_final_url"] = final_url

                            save_state(state)

                        finally:
                            page.close()

                    finally:
                        context.close()

                finally:
                    browser.close()

        except Exception as e:
            print(f"Monitor error: {e}", flush=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()



if __name__ == "__main__":
    main()
