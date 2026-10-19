
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
BROWSERLESS_WS_URL = os.getenv("BROWSERLESS_WS_URL", "")
MENTION_TEXT = os.getenv("MENTION_TEXT", "@everyone")

STORE_URL = "https://www.pokemoncenter.com/en-us"
STATE_FILE = Path("state.json")
TZ = ZoneInfo("America/New_York")

CHECK_INTERVAL = 30
MONTHLY_ATTEMPT_LIMIT = 25000

if not BROWSERLESS_WS_URL:
    raise RuntimeError("BROWSERLESS_WS_URL is missing")


def load_state():
    state = {
        "alerted": False,
        "month": "",
        "attempts": 0,
    }

    try:
        saved = json.loads(STATE_FILE.read_text())

        if isinstance(saved, dict):
            for key in state:
                if key in saved:
                    state[key] = saved[key]

    except (OSError, ValueError, TypeError):
        pass

    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_alert(kind):
    if not DISCORD_WEBHOOK_URL:
        print("Discord webhook missing", flush=True)
        return False

    if kind == "QUEUE":
        message = (
            f"{MENTION_TEXT} "
            "🚨 **POKÉMON CENTER QUEUE DETECTED**\n"
            f"{STORE_URL}"
        )

    else:
        message = (
            f"{MENTION_TEXT} "
            "⚠️ **POKÉMON CENTER SECURITY CHALLENGE**\n"
            "A visible security-check page was detected. "
            "Queue is NOT confirmed.\n"
            f"{STORE_URL}"
        )

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=10,
        )
        response.raise_for_status()

        print(f"Discord {kind} alert sent", flush=True)
        return True

    except requests.RequestException as exc:
        print(f"Discord delivery failed: {exc}", flush=True)
        return False


def detect_queue(page):
    response = page.goto(
        STORE_URL,
        wait_until="commit",
        timeout=20000,
    )

    page.wait_for_timeout(3000)

    status = response.status if response else None
    url = page.url.lower()

    try:
        title = page.title().lower().strip()
    except Exception:
        title = ""

    try:
        body = page.locator("body").inner_text(
            timeout=3000
        ).lower().strip()
    except Exception:
        body = ""

    print(
        f"HTTP={status} URL={url} "
        f"TITLE={title[:80]!r} "
        f"TEXT={body[:160]!r}",
        flush=True,
    )

    visible = title + "\n" + body

    queue_phrases = (
        "you're in the virtual queue",
        "you are in the virtual queue",
        "virtual queue to enter",
        "keep this window open to stay in the queue",
        "your place in line",
    )

    challenge_phrases = (
        "additional security check is required",
        "protected and accelerated by imperva",
        "verify that an actual human",
    )

    if any(
        phrase in visible
        for phrase in queue_phrases
    ):
        return "QUEUE"

    if any(
        phrase in visible
        for phrase in challenge_phrases
    ):
        return "CHALLENGE"

    if status is None or status >= 400:
        return "UNKNOWN"

    if not title and not body:
        return "UNKNOWN"

    if (
        "pokémon center official site" in title
        or "pokemon center official site" in title
    ):
        return "NORMAL"

    print(
        "Unexpected page content; preserving previous state",
        flush=True,
    )

    return "UNKNOWN"


def run_check():
    counts = {
        "blocked": 0,
        "allowed": 0,
    }

    start = time.monotonic()

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            BROWSERLESS_WS_URL,
            timeout=15000,
        )

        try:
            context = browser.new_context(
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    locale="en-US",
    viewport={
        "width": 1440,
        "height": 900,
    },
)

            try:
                def filter_resources(route):
                    if route.request.resource_type in (
                        "image",
                        "font",
                        "media",
                    ):
                        counts["blocked"] += 1
                        route.abort()
                    else:
                        counts["allowed"] += 1
                        route.continue_()

                # context.route("**/*", filter_resources)

                page = context.new_page()

                try:
                    result = detect_queue(page)
                finally:
                    page.close()

            finally:
                context.close()

        finally:
            browser.close()

    print(
        f"CHECK={result} "
        f"seconds={time.monotonic()-start:.1f} "
        f"allowed={counts['allowed']} "
        f"blocked={counts['blocked']}",
        flush=True,
    )

    return result


def main():
    state = load_state()

    print(
        f"Pokémon Center US monitor started; "
        f"Python {sys.version.split()[0]}",
        flush=True,
    )

    print(
        "Schedule: Monday-Friday, 10 AM-4 PM ET; "
        "interval ~9 minutes",
        flush=True,
    )

    while True:
        now = datetime.now(TZ)
        month = now.strftime("%Y-%m")

        if state["month"] != month:
            state["month"] = month
            state["attempts"] = 0
            save_state(state)

        if now.weekday() >= 5 or not (
            10 <= now.hour < 16
        ):
            time.sleep(300)
            continue

        if state["attempts"] >= MONTHLY_ATTEMPT_LIMIT:
            print(
                "Monthly attempt cap reached; "
                "checks suspended",
                flush=True,
            )
            time.sleep(3600)
            continue

        state["attempts"] += 1
        save_state(state)

        try:
            result = run_check()

            print(f"Detection={result}", flush=True)

            if result in ("QUEUE", "CHALLENGE"):
                if not state["alerted"]:
                    if send_alert(result):
                        state["alerted"] = True
                        save_state(state)
                else:
                    print(
                        "Alert already sent for this event",
                        flush=True,
                    )

            elif result == "NORMAL":
                if state["alerted"]:
                    print(
                        "Normal storefront returned; "
                        "rearming alert",
                        flush=True,
                    )

                state["alerted"] = False
                save_state(state)

            else:
                print(
                    "UNKNOWN: no alert; state unchanged",
                    flush=True,
                )

        except Exception as exc:
            print(
                f"CHECK ERROR: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
