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

# Time between checks, AFTER each browser session.
PEAK_SLEEP_SECONDS = 140
LATE_SLEEP_SECONDS = 300

# Warn Discord after 3 unreadable checks.
UNKNOWN_ALERT_THRESHOLD = 3

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
            for key in state:
                if key in saved:
                    state[key] = saved[key]

            # Support previous alert-state format.
            if (
                "last_signal" not in saved
                and saved.get("alerted")
            ):
                state["last_signal"] = "CHALLENGE"

    except (FileNotFoundError, ValueError, OSError):
        pass

    if state["month"] != now_et().strftime("%Y-%m"):
        state["month"] = now_et().strftime("%Y-%m")
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

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": prefix + content,
            "allowed_mentions": {
                "parse": (
                    ["everyone", "users", "roles"]
                    if mention
                    else []
                )
            },
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

    # Wait for visible content rather than relying
    # only on a successful HTTP response.
    try:
        page.wait_for_function(
            """() => Boolean(
                document.body &&
                document.body.innerText &&
                document.body.innerText.trim()
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
    if status is None or status >= 400:
        return "UNKNOWN"

    # Detect actual queue wording.
    if any(
        marker in body
        for marker in QUEUE_MARKERS
    ):
        return "QUEUE"

    # Detect visible security challenge wording.
    if any(
        marker in body
        for marker in CHALLENGE_MARKERS
    ):
        return "CHALLENGE"

    # A blank page is NOT a confirmed queue.
    if not body.strip():
        return "UNKNOWN"

    if (
        "please enable js and disable any ad blocker"
        in body
    ):
        return "UNKNOWN"

    # Only classify as normal if the storefront
    # title AND recognizable content are present.
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

                # Retry blank pages once.
                # Reuse the same Browserless session.
                if (
                    result == "UNKNOWN"
                    and not body.strip()
                ):
                    log(
                        "Blank page detected; "
                        "retrying once in same session"
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

    log(
        f"CHECK={result} "
        f"seconds={time.monotonic() - start:.1f}"
    )

    return result


def handle_unknown(state):
    state["unknown_streak"] += 1

    log(
        f"UNKNOWN streak={state['unknown_streak']}"
    )

    if (
        state["unknown_streak"]
        >= UNKNOWN_ALERT_THRESHOLD
        and not state["health_alerted"]
    ):
        discord_message(
            "⚠️ Pokémon Center monitor health warning: "
            f"{state['unknown_streak']} consecutive checks "
            "failed to verify the site. "
            "This is NOT a confirmed queue. "
            "Check Railway logs."
        )

        state["health_alerted"] = True


def handle_result(result, state):
    if result == "UNKNOWN":
        handle_unknown(state)
        return

    # Send a recovery notice after readable content returns.
    if state["health_alerted"]:
        try:
            discord_message(
                "✅ Pokémon Center monitor recovered: "
                "site is readable again."
            )

            state["health_alerted"] = False

        except Exception as exc:
            log(
                "Recovery notice failed: "
                f"{type(exc).__name__}: {exc}"
            )

    state["unknown_streak"] = 0

    if result == "NORMAL":
        state["last_signal"] = None
        return

    # QUEUE and CHALLENGE have separate alert states.
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
        "Monitor started: weekdays "
        "10 AM–2 PM = 140s; "
        "2–4 PM = 300s"
    )

    state = load_state()

    while True:
        now = now_et()

        if monitoring_interval(now) is None:
            rest = seconds_until_next_open(now)

            log(
                "Outside monitoring hours; "
                f"sleeping {rest / 60:.1f} minutes"
            )

            time.sleep(rest)
            continue

        month = now.strftime("%Y-%m")

        if state["month"] != month:
            state["month"] = month
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

            try:
                handle_unknown(state)
            except Exception as warning_exc:
                log(
                    "Health warning failed: "
                    f"{type(warning_exc).__name__}: "
                    f"{warning_exc}"
                )

        finally:
            save_state(state)

        # Choose the interval based on Eastern time.
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
