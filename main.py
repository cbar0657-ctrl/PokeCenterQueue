
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

print("BOT STARTING", flush=True)
print(f"Python: {sys.version.split()[0]}", flush=True)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
STATUS_WEBHOOK_URL = os.getenv("STATUS_WEBHOOK_URL", "")
BROWSERLESS_WS_URL = os.getenv("BROWSERLESS_WS_URL", "")
MENTION_TEXT = os.getenv("MENTION_TEXT", "@everyone")

STORE_URL = "https://www.pokemoncenter.com/en-us"
STATE_FILE = Path("state.json")
TIME_ZONE = ZoneInfo("America/New_York")

CHECK_INTERVAL_SECONDS = 540
MAX_ATTEMPTS_PER_MONTH = 850

if not BROWSERLESS_WS_URL:
    raise RuntimeError("BROWSERLESS_WS_URL missing")


def load_state():
    default = {
        "captcha_alerted": False,
        "was_live": False,
        "month": None,
        "monthly_attempts": 0,
    }

    try:
        stored = json.loads(STATE_FILE.read_text())

        if isinstance(stored, dict):
            if isinstance(stored.get("US"), dict):
                stored = stored["US"]

            for key in default:
                if key in stored:
                    default[key] = stored[key]

    except (OSError, ValueError, TypeError):
        pass

    return default


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_webhook(content):
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook not configured", flush=True)
        return False

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": content},
            timeout=10,
        )
        response.raise_for_status()

        print("Discord notification sent", flush=True)
        return True

    except requests.RequestException as exc:
        print(f"Discord send failed: {exc}", flush=True)
        return False


def detect_queue(page):
    print(f"Visiting {STORE_URL}", flush=True)

    response = page.goto(
        STORE_URL,
        wait_until="domcontentloaded",
        timeout=25000,
    )

    page.wait_for_timeout(1500)

    status = response.status if response is not None else None
    url = page.url

    try:
        title = page.title().lower()
    except Exception:
        title = ""

    try:
        text = page.locator("body").inner_text(
            timeout=3000
        ).lower()
    except Exception:
        text = ""

    try:
        html = page.content().lower()
    except Exception:
        html = ""

    combined = " ".join((title, text, html))

    print(
        f"HTTP={status} URL={url} "
        f"TITLE={title[:75]!r} "
        f"TEXT={text[:100]!r}",
        flush=True,
    )

    queue_phrases = (
        "you're in the virtual queue",
        "you are in the virtual queue",
        "virtual queue to enter",
        "keep this window open to stay in the queue",
        "your place in line",
    )

    captcha_strong = (
        "additional security check is required",
        "protected and accelerated by imperva",
        "verify that an actual human",
    )

    captcha_weak = (
        "hcaptcha",
        "newassets.hcaptcha.com",
    )

    if any(
        phrase in combined
        for phrase in queue_phrases
    ):
        return "QUEUE", url

    if any(
        phrase in combined
        for phrase in captcha_strong
    ):
        return "CAPTCHA", url

    if any(
        phrase in combined
        for phrase in captcha_weak
    ):
        print(
            "Weak hCaptcha marker found in HTML "
            "(not necessarily a displayed challenge)",
            flush=True,
        )
        return "CAPTCHA", url

    if status is None or status >= 400:
        return "UNKNOWN", url

    if not title.strip() and not text.strip():
        return "UNKNOWN", url

    return "NORMAL", url


def one_check():
    stats = {
        "blocked": 0,
        "allowed": 0,
    }

    started = time.monotonic()

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            BROWSERLESS_WS_URL,
            timeout=15000,
        )

        try:
            context = browser.new_context(
                locale="en-US",
                viewport={
                    "width": 1440,
                    "height": 900,
                },
            )

            try:
                def resource_filter(route):
                    resource_type = route.request.resource_type

                    if resource_type in (
                        "image",
                        "font",
                        "media",
                    ):
                        stats["blocked"] += 1
                        route.abort()
                    else:
                        stats["allowed"] += 1
                        route.continue_()

                context.route("**/*", resource_filter)

                page = context.new_page()

                try:
                    result, url = detect_queue(page)
                finally:
                    page.close()

            finally:
                context.close()

        finally:
            browser.close()

    elapsed = time.monotonic() - started

    print(
        f"CHECK RESULT={result} "
        f"duration={elapsed:.1f}s "
        f"resource_requests_allowed={stats['allowed']} "
        f"blocked={stats['blocked']}",
        flush=True,
    )

    print(
        "Resource counts are NOT Browserless units. "
        "Compare against Browserless request logs.",
        flush=True,
    )

    return result, url


def main():
    state = load_state()

    print(
        "US Pokemon Center Monitor started",
        flush=True,
    )
    print(
        "Schedule: Monday-Friday 10 AM-4 PM Eastern",
        flush=True,
    )
    print(
        "Check interval: approximately 9 minutes",
        flush=True,
    )

    while True:
        now = datetime.now(TIME_ZONE)
        current_month = now.strftime("%Y-%m")

        if state["month"] != current_month:
            state["month"] = current_month
            state["monthly_attempts"] = 0
            save_state(state)

        if now.weekday() >= 5 or not (
            10 <= now.hour < 16
        ):
            time.sleep(300)
            continue

        if (
            state["monthly_attempts"]
            >= MAX_ATTEMPTS_PER_MONTH
        ):
            print(
                "Monthly attempt limit reached",
                flush=True,
            )
            time.sleep(3600)
            continue

        state["monthly_attempts"] += 1
        save_state(state)

        try:
            result, url = one_check()

            if result == "QUEUE":
                already_alerted = (
                    state["was_live"]
                    or state["captcha_alerted"]
                )

                if not already_alerted:
                    sent = send_webhook(
                        f"{MENTION_TEXT} "
                        "🚨 **POKÉMON CENTER QUEUE DETECTED**\n"
                        f"{STORE_URL}"
                    )

                    if sent:
                        state["was_live"] = True
                else:
                    state["was_live"] = True
                    print(
                        "Queue previously alerted, "
                        "skipping duplicate",
                        flush=True,
                    )

            elif result == "CAPTCHA":
                already_alerted = (
                    state["captcha_alerted"]
                    or state["was_live"]
                )

                if not already_alerted:
                    sent = send_webhook(
                        f"{MENTION_TEXT} "
                        "⚠️ **POSSIBLE POKÉMON CENTER QUEUE**\n"
                        "Security challenge indicator detected; "
                        "not a confirmed queue.\n"
                        f"{STORE_URL}"
                    )

                    if sent:
                        state["captcha_alerted"] = True
                else:
                    print(
                        "Challenge previously alerted, "
                        "skipping duplicate",
                        flush=True,
                    )

            elif result == "NORMAL":
                print(
                    "Normal storefront: resetting alert state",
                    flush=True,
                )

                state["was_live"] = False
                state["captcha_alerted"] = False

            else:
                print(
                    "UNKNOWN result: keeping prior alert state",
                    flush=True,
                )

            save_state(state)

        except Exception as exc:
            print(
                f"CHECK FAILED: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

