
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
    raise RuntimeError("BROWSERLESS_WS_URL is not set")

STATE_FILE = Path("state.json")
HEARTBEAT_FILE = Path("heartbeat.txt")

STORE = {
    "name": "Pokémon Center US",
    "flag": "🇺🇸",
    "url": "https://www.pokemoncenter.com/en-us",
}

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

CHECK_INTERVAL_SECONDS = 540
MONTHLY_CHECK_LIMIT = 850
TIME_ZONE = ZoneInfo("America/New_York")


def load_state():
    default = {
        "was_live": False,
        "captcha_alerted": False,
        "last_final_url": None,
        "check_month": None,
        "monthly_checks": 0,
    }

    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())

            if isinstance(saved, dict) and isinstance(
                saved.get("US"), dict
            ):
                saved = saved["US"]

            if isinstance(saved, dict):
                default.update({
                    key: saved[key]
                    for key in default
                    if key in saved
                })

        except (OSError, ValueError, TypeError):
            pass

    return default


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_discord_message(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook missing", flush=True)
        return False

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=15
    )
    response.raise_for_status()
    return True


def send_captcha_alert() -> bool:
    payload = {
        "content": (
            f"{MENTION_TEXT} ⚠️ "
            "**POSSIBLE POKÉMON CENTER QUEUE!**\n"
            "Imperva/hCaptcha challenge detected. "
            "This may signal a queue, "
            "but is not confirmation.\n"
            f"{STORE['url']}"
        )
    }

    try:
        sent = send_discord_message(payload)

        if sent:
            print(
                "📨 CAPTCHA alert sent to Discord!",
                flush=True
            )

        return sent

    except Exception as exc:
        print(
            f"Discord CAPTCHA alert error: {exc}",
            flush=True
        )
        return False


def send_queue_alert(final_url: str) -> bool:
    payload = {
        "content": (
            f"{MENTION_TEXT} 🚨 "
            "**POKÉMON CENTER QUEUE CONFIRMED!**"
        ),
        "embeds": [{
            "title": (
                "🇺🇸 Pokémon Center US Waiting Room Detected"
            ),
            "url": STORE["url"],
            "description": (
                f"[Open Pokémon Center]({STORE['url']})\n"
                f"Detected URL: {final_url}"
            ),
            "color": 16711680,
        }],
    }

    try:
        sent = send_discord_message(payload)

        if sent:
            print(
                "📨 Confirmed queue alert sent to Discord!",
                flush=True
            )

        return sent

    except Exception as exc:
        print(
            f"Discord queue alert error: {exc}",
            flush=True
        )
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
        print(
            f"Status message error: {exc}",
            flush=True
        )


def detect_queue(page, store_url: str) -> Tuple[
    Union[bool, str], str
]:
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
        body = page.locator("body").inner_text(
            timeout=10000
        ).lower()
    except Exception:
        body = ""

    try:
        html = page.content().lower()
    except Exception:
        html = ""

    print(
        f"➡️ HTTP Status: {status} | Final URL: {final_url}",
        flush=True
    )
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
        "protected and accelerated by imperva",
        "verify that an actual human",
        "newassets.hcaptcha.com",
        "hcaptcha",
    )

    if any(
        phrase in combined
        for phrase in queue_phrases
    ):
        print("🚨 CONFIRMED QUEUE!", flush=True)
        return True, final_url

    matched = [
        phrase
        for phrase in captcha_phrases
        if phrase in combined
    ]

    if matched:
        print(
            f"⚠️ CAPTCHA detected (matches: {matched})",
            flush=True
        )
        return "CAPTCHA", final_url

    if status is not None and status >= 400:
        raise RuntimeError(
            f"HTTP {status}: blocked or unavailable"
        )

    if not title.strip() and not body.strip():
        raise RuntimeError(
            "Empty page: queue status UNKNOWN"
        )

    print("✅ No queue indicators found", flush=True)
    return False, final_url


def main():
    state = load_state()
    startup_sent = False

    print(
        "US Pokemon Center Queue Monitor started!",
        flush=True
    )
    print(
        "Schedule: Monday-Friday, 10 AM-4 PM Eastern",
        flush=True
    )
    print(
        "Interval: approximately 9 minutes",
        flush=True
    )

    while True:
        now = datetime.now(TIME_ZONE)
        month = now.strftime("%Y-%m")

        HEARTBEAT_FILE.write_text(
            str(int(time.time()))
        )

        if state["check_month"] != month:
            state["check_month"] = month
            state["monthly_checks"] = 0
            save_state(state)

        if now.weekday() >= 5 or not (
            10 <= now.hour < 16
        ):
            time.sleep(300)
            continue

        if state["monthly_checks"] >= MONTHLY_CHECK_LIMIT:
            print(
                "Monthly attempt limit reached",
                flush=True
            )
            time.sleep(3600)
            continue

        state["monthly_checks"] += 1
        save_state(state)

        try:
            if not startup_sent:
                send_status_message(
                    "US Pokemon Center Monitor is online"
                )
                startup_sent = True

            with sync_playwright() as playwright:
                browser = (
                    playwright.chromium.connect_over_cdp(
                        BROWSERLESS_WS_URL
                    )
                )

                try:
                    context = browser.new_context(
                        user_agent=BROWSER_USER_AGENT,
                        locale="en-US",
                        viewport={
                            "width": 1440,
                            "height": 900
                        },
                    )

                    try:
                        page = context.new_page()

                        try:
                            result, final_url = detect_queue(
                                page,
                                STORE["url"]
                            )

                            if result == "CAPTCHA":
                                already_alerted = (
                                    state["captcha_alerted"]
                                    or state["was_live"]
                                )

                                if not already_alerted:
                                    if send_captcha_alert():
                                        state["captcha_alerted"] = True
                                        save_state(state)
                                else:
                                    print(
                                        "CAPTCHA already reported; "
                                        "no repeat alert",
                                        flush=True
                                    )

                            elif result is True:
                                already_alerted = (
                                    state["was_live"]
                                    or state["captcha_alerted"]
                                )

                                if not already_alerted:
                                    if send_queue_alert(final_url):
                                        state["was_live"] = True
                                else:
                                    state["was_live"] = True
                                    print(
                                        "Queue already reported; "
                                        "no repeat alert",
                                        flush=True
                                    )

                                state["last_final_url"] = final_url
                                save_state(state)

                            else:
                                state["was_live"] = False
                                state["captcha_alerted"] = False
                                state["last_final_url"] = final_url

                                save_state(state)

                                print(
                                    "Normal storefront; "
                                    "alert ready for next event",
                                    flush=True
                                )

                        finally:
                            page.close()

                    finally:
                        context.close()

                finally:
                    browser.close()

        except Exception as exc:
            print(
                f"Monitor error: {exc}",
                flush=True
            )

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

