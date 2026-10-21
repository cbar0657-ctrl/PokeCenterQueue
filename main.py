
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

STORE_URL = "https://www.pokemoncenter.com/"
TIME_ZONE = ZoneInfo("America/New_York")
STATE_FILE = Path("state.json")

MONTHLY_ATTEMPT_LIMIT = int(
    os.getenv("MONTHLY_ATTEMPT_LIMIT", "25000")
)

# Monitoring schedule
# Sleep begins AFTER the browser check finishes.

PEAK_SLEEP_SECONDS = 140
LATE_SLEEP_SECONDS = 300

# Alert after 3 consecutive unreadable checks.
UNKNOWN_ALERT_THRESHOLD = 3

# Retry a blank page once within the same Browserless session.
RETRY_BLANK_PAGE = True


QUEUE_MARKERS = (
    "you're in the virtual queue",
    "you are in the virtual queue",
    "virtual queue to enter",
    "you are currently in line",
    "you're in line to enter",
    "you are in line to enter",
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


def log(message):
    print(
        f"{now_et():%Y-%m-%d %H:%M:%S %Z} {message}",
        flush=True,
    )


def initial_state():
    return {
        "month": now_et().strftime("%Y-%m"),
        "attempts": 0,
        "last_signal": None,
        "unknown_streak": 0,
        "health_alerted": False,
    }


def load_state():
    state = initial_state()

    try:
        saved = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if isinstance(saved, dict):
            state.update({
                key: saved[key]
                for key in state
                if key in saved
            })

            # Support the previous alert-state format.
            if (
                "last_signal" not in saved
                and saved.get("alerted")
            ):
                state["last_signal"] = "CHALLENGE"

    except (FileNotFoundError, ValueError, OSError):
        pass

    current_month = now_et().strftime("%Y-%m")

    if state["month"] != current_month:
        state["month"] = current_month
        state["attempts"] = 0

    return state


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )

    temporary.replace(STATE_FILE)


def monitoring_interval(now):
    # Monday through Friday only.
    if now.weekday() >= 5:
        return None

    # 10 AM through 1:59 PM Eastern.
    if 10 <= now.hour < 14:
        return PEAK_SLEEP_SECONDS

    # 2 PM through 3:59 PM Eastern.
    if 14 <= now.hour < 16:
        return LATE_SLEEP_SECONDS

    return None


def seconds_until_next_open(now):
    start = now.replace(
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    )

    if now >= start:
        start += timedelta(days=1)

    while start.weekday() >= 5:
        start += timedelta(days=1)

    return max(
        1,
        (start - now).total_seconds(),
    )


def discord_message(content, mention=False):
    prefix = (
        f"{MENTION_TEXT} "
        if mention and MENTION_TEXT
        else ""
    )

    allowed_mentions = (
        {"parse": ["everyone", "users", "roles"]}
        if mention
        else {"parse": []}
    )

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": prefix + content,
            "allowed_mentions": allowed_mentions,
        },
        timeout=15,
    )

    response.raise_for_status()

    log("Discord message delivered")


def read_page(page):
    response = page.goto(
        STORE_URL,
        wait_until="commit",
        timeout=20000,
    )

    status = response.status if response else None

    # Wait for actual visible page text instead
    # of assuming a successful HTTP response
    # means the page is ready.

    try:
        page.wait_for_function(
            """() => !!(
                document.body &&
                document.body.innerText &&
                document.body.innerText.trim().length > 0
            )""",
            timeout=8000,
        )
    except Exception:
        pass

    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""

    try:
        body = (
            page.locator("body")
            .inner_text(timeout=3000) or ""
        ).lower()
    except Exception:
        body = ""

    log(
        f"HTTP={status} "
        f"URL={page.url} "
        f"TITLE={title[:100]!r} "
        f"TEXT={body[:250]!r} "
        f"BODY_CHARS={len(body)}"
    )

    return status, title, body


def classify(status, title, body):
    if status is not None and status >= 400:
        return "UNKNOWN"

    # Check queue wording first.
    if any(
        marker in body
        for marker in QUEUE_MARKERS
    ):
        return "QUEUE"

    # Then check security challenge wording.
    if any(
        marker in body
        for marker in CHALLENGE_MARKERS
    ):
        return "CHALLENGE"

    # Blank pages are never treated as normal.
    if not body.strip():
        return "UNKNOWN"

    if (
        "please enable js and disable any ad blocker"
        in body
    ):
        return "UNKNOWN"

    # Require both a familiar title and
    # recognizable storefront content.
    if (
        "pokémon center official site" in title
        or "pokemon center official site" in title
    ):
        if (
            "trading card game" in body
            and "search" in body
        ):
            return "NORMAL"

    return "UNKNOWN"


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

                status, title, body = read_page(page)

                result = classify(
                    status,
                    title,
                    body,
                )

                # Retry only if the page is blank.
                # Reuse the Browserless session.

                if (
                    RETRY_BLANK_PAGE
                    and result == "UNKNOWN"
                    and not body.strip()
                ):
                    log(
                        "Blank page detected; "
                        "retrying once"
                    )

                    try:
                        status, title, body = read_page(page)

                        result = classify(
                            status,
                            title,
                            body,
                        )

                    except Exception as exc:
                        log(
                            "Blank-page retry failed: "
                            f"{type(exc).__name__}: {exc}"
                        )

            finally:
                context.close()

        finally:
            browser.close()

    elapsed = time.monotonic() - start

    log(
        f"CHECK={result} "
        f"seconds={elapsed:.1f}"
    )

    return result


def handle_result(result, state):
    # UNKNOWN means we cannot verify the page.

    if result == "UNKNOWN":
        state["unknown_streak"] += 1

        log(
            "UNKNOWN streak="
            f"{state['unknown_streak']}"
        )

        if (
            state["unknown_streak"] >= UNKNOWN_ALERT_THRESHOLD
            and not state["health_alerted"]
        ):
            discord_message(
                "⚠️ Pokémon Center monitor health warning: "
                f"{state['unknown_streak']} consecutive checks "
                "could not verify the page. "
                "This is NOT a confirmed queue. "
                "Check Railway logs."
            )

            state["health_alerted"] = True

        # Do not clear a previous queue alert.
        return

    # A readable page means the monitor recovered.

    if state["health_alerted"]:
        try:
            discord_message(
                "✅ Pokémon Center monitor recovered: "
                "page is readable again."
            )

            state["health_alerted"] = False

        except Exception as exc:
            log(
                f"Recovery notice failed: {exc}"
            )

    state["unknown_streak"] = 0

    # Normal storefront resets the signal.
    if result == "NORMAL":
        state["last_signal"] = None
        return

    # Queue and challenge have separate alert states.
    # A queue can trigger after a challenge.

    if state["last_signal"] != result:
        if result == "QUEUE":
            label = (
                "🚨 POKÉMON CENTER QUEUE DETECTED"
            )

        else:
            label = (
                "⚠️ POKÉMON CENTER SECURITY "
                "CHALLENGE DETECTED "
                "(not confirmed queue)"
            )

        discord_message(
            f"{label}\n"
            f"{STORE_URL}\n"
            f"Detected at "
            f"{now_et():%Y-%m-%d %I:%M:%S %p} ET",
            mention=True,
        )

        state["last_signal"] = result


def main():
    log(
        "Pokémon Center monitor started. "
        "Weekdays 10 AM–2 PM: 140 seconds; "
        "2 PM–4 PM: 300 seconds."
    )

    state = load_state()

    while True:
        now = now_et()

        interval = monitoring_interval(now)

        if interval is None:
            rest = seconds_until_next_open(now)

            log(
                "Outside monitoring hours; "
                f"sleeping {rest / 60:.1f} minutes"
            )

            time.sleep(rest)
            continue

        current_month = now.strftime("%Y-%m")

        if state["month"] != current_month:
            state["month"] = current_month
            state["attempts"] = 0
            save_state(state)

        if (
            state["attempts"]
            >= MONTHLY_ATTEMPT_LIMIT
        ):
            log(
                "Monthly attempt limit reached; "
                "pausing checks"
            )

            time.sleep(3600)
            continue

        state["attempts"] += 1
        save_state(state)

        try:
            result = check_once()

            log(
                f"Detection={result} "
                f"attempt={state['attempts']}"
            )

            handle_result(result, state)

        except Exception as exc:
            log(
                "Check failed: "
                f"{type(exc).__name__}: {exc}"
            )

            state["unknown_streak"] += 1

            if (
                state["unknown_streak"]
                >= UNKNOWN_ALERT_THRESHOLD
                and not state["health_alerted"]
            ):
                try:
                    discord_message(
                        "⚠️ Pokémon Center monitor "
                        "health warning: "
                        "repeated checks failed. "
                        "This is NOT a confirmed queue."
                    )

                    state["health_alerted"] = True

                except Exception as alert_exc:
                    log(
                        "Health notice failed: "
                        f"{alert_exc}"
                    )

        finally:
            save_state(state)

        # Determine the interval based on
        # the current Eastern time.

        next_interval = monitoring_interval(
            now_et()
        )

        if next_interval is None:
            time.sleep(
                seconds_until_next_open(
                    now_et()
                )
            )

        else:
            time.sleep(next_interval)


if __name__ == "__main__":
    main()

