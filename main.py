
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

BROWSERLESS_WS_URL = os.environ["BROWSERLESS_WS_URL"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MENTION_TEXT = os.getenv("MENTION_TEXT", "@everyone")

STORE_URL = "https://www.pokemoncenter.com/en-us"
TIME_ZONE = ZoneInfo("America/New_York")
STATE_FILE = Path("state.json")
MONTHLY_ATTEMPT_LIMIT = int(os.getenv("MONTHLY_ATTEMPT_LIMIT", "25000"))

# Time between checks. Browser load time is additional.
PEAK_SLEEP_SECONDS = 140
LATE_SLEEP_SECONDS = 300

QUEUE_MARKERS = (
    "you're in the virtual queue",
    "you are in the virtual queue",
    "virtual queue to enter",
    "you are currently in line",
    "you are in line to enter",
    "you're in line to enter",
    "please wait in line",
    "your estimated wait time",
)

CHALLENGE_MARKERS = (
    "additional security check is required",
    "protected and accelerated by imperva",
    "verify that an actual human",
)


def now_et():
    return datetime.now(TIME_ZONE)


def load_state():
    default = {
        "month": now_et().strftime("%Y-%m"),
        "attempts": 0,
        "alerted": False,
    }

    try:
        saved = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        if isinstance(saved, dict):
            default.update(saved)
    except (FileNotFoundError, ValueError, OSError):
        pass

    if default["month"] != now_et().strftime("%Y-%m"):
        default = {
            "month": now_et().strftime("%Y-%m"),
            "attempts": 0,
            "alerted": False,
        }

    return default


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def monitoring_interval(now):
    # No checks Saturday or Sunday
    if now.weekday() >= 5:
        return None

    # 10 AM through 1:59 PM Eastern
    if 10 <= now.hour < 14:
        return PEAK_SLEEP_SECONDS

    # 2 PM through 3:59 PM Eastern
    if 14 <= now.hour < 16:
        return LATE_SLEEP_SECONDS

    return None


def seconds_until_next_open(now):
    next_start = now.replace(
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    )

    if now >= next_start:
        next_start += timedelta(days=1)

    while next_start.weekday() >= 5:
        next_start += timedelta(days=1)

    return max(1, (next_start - now).total_seconds())


def detect_queue(page):
    # Check visible text only, avoiding false alerts
    # from hidden CAPTCHA scripts.

    response = page.goto(
        STORE_URL,
        wait_until="commit",
        timeout=20000,
    )

    page.wait_for_timeout(3000)

    status = response.status if response else None
    title = (page.title() or "").lower()

    try:
        body = (
            page.locator("body")
            .inner_text(timeout=10000) or ""
        ).lower()
    except Exception:
        body = ""

    print(
        f"HTTP={status} URL={page.url} "
        f"TITLE={title[:100]!r} "
        f"TEXT={body[:180]!r}",
        flush=True,
    )

    if status is not None and status >= 400:
        return "UNKNOWN"

    if any(marker in body for marker in QUEUE_MARKERS):
        return "QUEUE"

    if any(marker in body for marker in CHALLENGE_MARKERS):
        return "CHALLENGE"

    if not body.strip():
        return "UNKNOWN"

    if (
        "pokémon center official site" in title
        or "pokemon center official site" in title
    ):
        return "NORMAL"

    return "UNKNOWN"


def discord_alert(result):
    if result == "QUEUE":
        subject = "🚨 POKÉMON CENTER QUEUE DETECTED"
    else:
        subject = (
            "⚠️ POKÉMON CENTER SECURITY "
            "CHALLENGE DETECTED"
        )

    message = (
        f"{MENTION_TEXT} {subject}\n"
        f"{STORE_URL}\n"
        f"Detected at "
        f"{now_et():%Y-%m-%d %I:%M:%S %p} ET"
    )

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": message,
            "allowed_mentions": {
                "parse": ["everyone", "users", "roles"]
            },
        },
        timeout=15,
    )

    response.raise_for_status()
    print(
        f"Discord alert delivered: {result}",
        flush=True,
    )


def check_once():
    start = time.monotonic()

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            BROWSERLESS_WS_URL,
            timeout=30000,
        )

        try:
            context = browser.new_context()

            try:
                page = context.new_page()
                result = detect_queue(page)
            finally:
                context.close()
        finally:
            browser.close()

    print(
        f"CHECK={result} "
        f"seconds={time.monotonic() - start:.1f}",
        flush=True,
    )

    return result


def main():
    print(
        "Pokémon Center monitor started.",
        flush=True,
    )
    print(
        "10 AM–2 PM: 140-second sleep; "
        "2–4 PM: 300-second sleep.",
        flush=True,
    )

    state = load_state()

    while True:
        now = now_et()
        interval = monitoring_interval(now)

        if interval is None:
            rest = seconds_until_next_open(now)

            print(
                "Outside monitoring hours; "
                f"sleeping {rest / 60:.1f} minutes.",
                flush=True,
            )

            time.sleep(rest)
            continue

        month = now.strftime("%Y-%m")

        if state["month"] != month:
            state = {
                "month": month,
                "attempts": 0,
                "alerted": False,
            }
            save_state(state)

        if state["attempts"] >= MONTHLY_ATTEMPT_LIMIT:
            print(
                "Monthly attempt limit reached; "
                "waiting for next month.",
                flush=True,
            )

            time.sleep(
                min(seconds_until_next_open(now), 3600)
            )
            continue

        state["attempts"] += 1
        save_state(state)

        try:
            result = check_once()

            print(
                f"Detection={result} "
                f"attempt={state['attempts']}",
                flush=True,
            )

            if result in ("QUEUE", "CHALLENGE"):
                if not state["alerted"]:
                    discord_alert(result)
                    state["alerted"] = True
                    save_state(state)

            elif result == "NORMAL" and state["alerted"]:
                state["alerted"] = False
                save_state(state)

            # UNKNOWN never resets an existing alert.

        except Exception as exc:
            print(
                f"Check failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        # Recheck the time after each browser session.
        next_interval = monitoring_interval(now_et())

        if next_interval is not None:
            time.sleep(next_interval)
        else:
            time.sleep(
                seconds_until_next_open(now_et())
            )


if __name__ == "__main__":
    main()
