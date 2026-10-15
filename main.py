
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple, Union
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

print("🔥 BOT STARTING...", flush=True)
print(f"🐍 Python version: {sys.version}", flush=True)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
STATUS_WEBHOOK_URL = os.getenv("STATUS_WEBHOOK_URL", "")
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
    default = {
        code: {"was_live": False, "last_final_url": None}
        for code in STORES
    }
    if STATE_FILE.exists():
        try:
            stored = json.loads(STATE_FILE.read_text())
            for code in STORES:
                if isinstance(stored.get(code), dict):
                    default[code].update(stored[code])
        except (ValueError, OSError, TypeError, AttributeError):
            pass
    return default


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_discord_message(payload: dict):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK_URL not set", flush=True)
        return False
    response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    response.raise_for_status()
    return True


def send_discord_alert(store: Dict[str, str], final_url: str):
    payload = {
        "content": f"{MENTION_TEXT} 🚨 **QUEUE LIVE** 🚨",
        "embeds": [{
            "title": f"{store['flag']} {store['name']} Queue Detected",
            "url": store["url"],
            "description": (
                "🟢 **Queue is LIVE. Act fast!**\n\n"
                f"👉 [Open Store]({store['url']})\n"
                f"👉 [Detected Page]({final_url})"
            ),
            "color": 16711680,
            "footer": {"text": "PokeCenter Monitor • Live Alert"},
        }],
    }
    try:
        sent = send_discord_message(payload)
        if sent:
            print(f"📨 Discord alert sent for {store['name']}", flush=True)
        return sent
    except Exception as exc:
        print(f"Discord error: {exc}", flush=True)
        return False


def send_captcha_alert(store):
    payload = {
        "content": (
            f"{MENTION_TEXT} ⚠️ **POKÉMON CENTER QUEUE ALERT!**\n"
            "Imperva CAPTCHA detected. "
            "A queue may be active!\n"
            "https://www.pokemoncenter.com/en-us"
        )
    }

    try:
        sent = send_discord_message(payload)
        if sent:
            print("📨 CAPTCHA alert sent to Discord!", flush=True)
        return sent
    except Exception as exc:
        print(f"Discord CAPTCHA alert error: {exc}", flush=True)
        return False

def send_status_message(message: str):
    if not STATUS_WEBHOOK_URL:
        return
    try:
        response = requests.post(
            STATUS_WEBHOOK_URL,
            json={"content": message},
            timeout=15
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"Status message error: {exc}", flush=True)


def detect_queue(page, store_url: str) -> Tuple[Union[bool, str], str]:
    print(f"🌍 Visiting {store_url}", flush=True)
    response = page.goto(
        store_url,
        wait_until="domcontentloaded",
        timeout=45000
    )
    page.wait_for_timeout(3000)

    status = None if response is None else response.status
    final_url = page.url

    try:
        title = page.title().lower()
    except Exception:
        title = ""

    try:
        body = page.locator("body").inner_text(timeout=10000).lower()
    except Exception:
        body = ""

    try:
        html = page.content().lower()
    except Exception:
        html = ""

    print(f"➡️ HTTP Status: {status} | Final URL: {final_url}", flush=True)
    print(f"📄 Page title: {title[:150]}", flush=True)
    print(f"📄 Page preview: {body[:300]}", flush=True)

    combined = " ".join((title, body, html))

    queue_phrases = (
        "you're in the virtual queue",
        "you are in the virtual queue",
        "virtual queue to enter",
        "keep this window open to stay in the queue",
        "your place in line",
    )

    captcha_phrases = (
        "additional security check is required",
        "newassets.hcaptcha.com",
        "protected and accelerated by imperva",
        "verify that an actual human",
        "hcaptcha",
    )

    if any(phrase in combined for phrase in queue_phrases):
        print("🚨 CONFIRMED QUEUE!", flush=True)
        return True, final_url

    if any(phrase in combined for phrase in captcha_phrases):
        print(
            "⚠️ CAPTCHA DETECTED: Possible queue, not confirmed",
            flush=True
        )
        return "CAPTCHA", final_url

    if status is not None and status >= 400:
        raise RuntimeError(
            f"HTTP {status}: access blocked or page unavailable"
        )

    if not title.strip() and not body.strip():
        raise RuntimeError("Empty page: queue status UNKNOWN")

    print("✅ No queue indicators found", flush=True)
    return False, final_url


def main():
    state = load_state()
    startup_sent = False

    check_interval = 540  # 9 minutes
    monthly_check_limit = 850

    current_month = None
    monthly_checks = 0

    print("US Pokemon Center Queue Monitor started!", flush=True)
    print("Schedule: Monday-Friday, 10 AM-4 PM Eastern", flush=True)
    print("Check interval: approximately 9 minutes", flush=True)

    while True:
        now = datetime.now(ZoneInfo("America/New_York"))
        month = now.strftime("%Y-%m")

        HEARTBEAT_FILE.write_text(str(int(time.time())))

        if month != current_month:
            current_month = month
            monthly_checks = 0

        if now.weekday() >= 5 or not (10 <= now.hour < 16):
            time.sleep(300)
            continue

        if monthly_checks >= monthly_check_limit:
            print("Monthly attempt limit reached", flush=True)
            time.sleep(3600)
            continue

        monthly_checks += 1

        try:
            if not startup_sent:
                send_status_message(
                    "US Pokemon Center Monitor is online"
                )
                startup_sent = True

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    BROWSERLESS_WS_URL
                )

                try:
                    context = browser.new_context(
                        user_agent=BROWSER_USER_AGENT,
                        locale="en-US",
                        viewport={"width": 1440, "height": 900},
                    )

                    try:
                        page = context.new_page()

                        try:
                            store = STORES["US"]

                            result, final_url = detect_queue(
                                page, store["url"]
                            )

                           
if result == "CAPTCHA":
    if not state["US"].get("captcha_alerted", False):
        if send_captcha_alert(store):
            state["US"]["captcha_alerted"] = True
            save_state(state)
    else:
        print(
            "CAPTCHA still detected. Alert already sent.",
            flush=True
        )

                            else:
                                else:
    if result is False:
        state["US"]["captcha_alerted"] = False

    was_live = state["US"]["was_live"]

                                print(
                                    f"US Queue Active: {result}",
                                    flush=True
                                )

                                if result and not was_live:
                                    if send_discord_alert(store, final_url):
                                        state["US"]["was_live"] = True
                                else:
                                    state["US"]["was_live"] = result

                                state["US"]["last_final_url"] = final_url
                                save_state(state)

                        finally:
                            page.close()

                    finally:
                        context.close()

                finally:
                    browser.close()

        except Exception as exc:
            print(f"Monitor error: {exc}", flush=True)

        time.sleep(check_interval)


if __name__ == "__main__":
    main()
