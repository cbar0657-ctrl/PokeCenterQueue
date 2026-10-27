import json
import os
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

BROWSERLESS_WS_URL = os.environ["BROWSERLESS_WS_URL"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MENTION_TEXT = os.getenv("MENTION_TEXT", "@everyone")

# Optional: set to a realistic Chrome UA matching your Browserless Chrome version.
USER_AGENT = os.getenv("USER_AGENT", "").strip()

STORE_URL = "https://www.pokemoncenter.com/"
STORE_HOST = "pokemoncenter.com"
TIME_ZONE = ZoneInfo("America/New_York")

# Point these at a Railway volume (e.g. /data/state.json) to survive redeploys.
STATE_FILE = Path(os.getenv("STATE_PATH", "state.json"))
STORAGE_FILE = Path(os.getenv("STORAGE_PATH", "storage_state.json"))

MONTHLY_ATTEMPT_LIMIT = int(os.getenv("MONTHLY_ATTEMPT_LIMIT", "25000"))

PEAK_SLEEP_SECONDS = 140
LATE_SLEEP_SECONDS = 300

# How long to keep polling a page that is still blank/loading.
READ_WAIT_SECONDS = 25

UNKNOWN_ALERT_THRESHOLD = 3

# Only log full diagnostics for the first N unreadable pages per process run.
DIAGNOSTIC_LIMIT = 5
_diag_count = 0

STOP = False

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

IMPERVA_HTML_MARKERS = (
    "_incapsula_resource",
    "incapsula incident",
    "/_incapsula_",
)


# ---------------------------------------------------------------- utilities

def now_et():
    return datetime.now(TIME_ZONE)


def log(message):
    print(f"{now_et():%Y-%m-%d %H:%M:%S %Z} {message}", flush=True)


def handle_signal(signum, frame):
    global STOP
    STOP = True
    log(f"Signal {signum} received; shutting down after current step")


def sleep_interruptible(seconds):
    end = time.monotonic() + seconds
    while not STOP:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


# -------------------------------------------------------------------- state

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
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        if isinstance(saved, dict):
            for key in state:
                if key in saved:
                    state[key] = saved[key]

            if "last_signal" not in saved and saved.get("alerted"):
                state["last_signal"] = "CHALLENGE"

    except (FileNotFoundError, ValueError, OSError):
        pass

    if state["month"] != now_et().strftime("%Y-%m"):
        state["month"] = now_et().strftime("%Y-%m")
        state["attempts"] = 0

    return state


def save_state(state):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temporary.replace(STATE_FILE)
    except OSError as exc:
        log(f"State save failed: {type(exc).__name__}: {exc}")


# ----------------------------------------------------------------- schedule

def monitoring_interval(now):
    # Monday through Friday only.
    if now.weekday() >= 5:
        return None

    # 10:00 AM - 1:59 PM Eastern.
    if 10 <= now.hour < 14:
        return PEAK_SLEEP_SECONDS

    # 2:00 PM - 3:59 PM Eastern.
    if 14 <= now.hour < 16:
        return LATE_SLEEP_SECONDS

    return None


def seconds_until_next_open(now):
    start = now.replace(hour=10, minute=0, second=0, microsecond=0)

    if now >= start:
        start += timedelta(days=1)

    while start.weekday() >= 5:
        start += timedelta(days=1)

    return max(1, (start - now).total_seconds())


# ------------------------------------------------------------------ discord

def discord_message(content, mention=False):
    prefix = f"{MENTION_TEXT} " if mention and MENTION_TEXT else ""

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": prefix + content,
            "allowed_mentions": {
                "parse": ["everyone", "users", "roles"] if mention else []
            },
        },
        timeout=15,
    )

    response.raise_for_status()
    log("Discord message delivered")


# ----------------------------------------------------------- classification

def classify(status, url, title, body):
    # Challenge/queue wording wins over the HTTP status, because
    # challenge pages are often served with a 403.
    if any(marker in body for marker in QUEUE_MARKERS):
        return "QUEUE"

    if any(marker in body for marker in CHALLENGE_MARKERS):
        return "CHALLENGE"

    # Redirected to a third-party queue host. Verify the real queue URL
    # pattern before relying on this.
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""

    if host and not host.endswith(STORE_HOST) and "queue" in url.lower():
        return "QUEUE"

    if status is None or status >= 400:
        return "UNKNOWN"

    # A blank page is NOT a confirmed queue.
    if not body.strip():
        return "UNKNOWN"

    if "please enable js and disable any ad blocker" in body:
        return "UNKNOWN"

    if "pokémon center official site" in title or "pokemon center official site" in title:
        if "trading card game" in body and "search" in body:
            return "NORMAL"

    return "UNKNOWN"


# ------------------------------------------------------------------ browser

def read_body(page):
    try:
        return (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception:
        # Navigation in progress (e.g. Imperva reload) or no body yet.
        return ""


def read_title(page):
    try:
        return (page.title() or "").lower()
    except Exception:
        return ""


def log_diagnostics(page, response, status):
    global _diag_count

    if _diag_count >= DIAGNOSTIC_LIMIT:
        return

    _diag_count += 1

    try:
        html = page.content() or ""
    except Exception as exc:
        html = ""
        log(f"DIAG content() failed: {type(exc).__name__}: {exc}")

    try:
        cookie_names = sorted(c["name"] for c in page.context.cookies())
    except Exception:
        cookie_names = []

    imperva = any(m in html.lower() for m in IMPERVA_HTML_MARKERS)

    log(f"DIAG status={status} html_len={len(html)} imperva_in_html={imperva}")
    log(f"DIAG html_head={html[:400]!r}")
    log(f"DIAG cookies={cookie_names}")

    if response is not None:
        try:
            headers = response.headers
            log(
                "DIAG headers "
                f"server={headers.get('server')} "
                f"x-cdn={headers.get('x-cdn')} "
                f"x-iinfo={headers.get('x-iinfo')}"
            )
        except Exception:
            pass


def read_page(page):
    response = page.goto(
        STORE_URL,
        wait_until="domcontentloaded",
        timeout=30000,
    )

    status = response.status if response else None

    deadline = time.monotonic() + READ_WAIT_SECONDS
    title = ""
    body = ""
    result = "UNKNOWN"

    # Poll until the page produces something we can classify. This rides
    # out interstitials that set cookies and reload.
    while True:
        title = read_title(page)
        body = read_body(page)
        result = classify(status, page.url, title, body)

        if result != "UNKNOWN":
            break

        if time.monotonic() >= deadline or STOP:
            break

        time.sleep(1.0)

    log(
        f"HTTP={status} URL={page.url} TITLE={title[:100]!r} "
        f"TEXT={body[:250]!r} BODY_CHARS={len(body)}"
    )

    if result == "UNKNOWN":
        log_diagnostics(page, response, status)

    return result


def new_context(browser):
    options = {
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "viewport": {"width": 1366, "height": 768},
    }

    if USER_AGENT:
        options["user_agent"] = USER_AGENT

    if STORAGE_FILE.exists():
        try:
            return browser.new_context(
                storage_state=str(STORAGE_FILE),
                **options,
            )
        except Exception as exc:
            log(
                "Saved cookies unusable; starting fresh: "
                f"{type(exc).__name__}: {exc}"
            )

    return browser.new_context(**options)


def check_once():
    start = time.monotonic()

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            BROWSERLESS_WS_URL,
            timeout=30000,
        )

        try:
            context = new_context(browser)

            try:
                page = context.new_page()
                result = read_page(page)

                # Reuse cookies only after a clean, verified load.
                if result == "NORMAL":
                    try:
                        STORAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        context.storage_state(path=str(STORAGE_FILE))
                    except Exception as exc:
                        log(
                            "Cookie save failed: "
                            f"{type(exc).__name__}: {exc}"
                        )

            finally:
                context.close()

        finally:
            browser.close()

    log(f"CHECK={result} seconds={time.monotonic() - start:.1f}")

    return result


# ----------------------------------------------------------------- handlers

def register_unknown(state):
    """Update counters only. No network calls, so it cannot fail."""
    state["unknown_streak"] += 1
    log(f"UNKNOWN streak={state['unknown_streak']}")


def maybe_send_health_warning(state):
    if (
        state["unknown_streak"] >= UNKNOWN_ALERT_THRESHOLD
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
        register_unknown(state)
        maybe_send_health_warning(state)
        return

    # Readable content again: send recovery notice if we had warned.
    if state["health_alerted"]:
        try:
            discord_message(
                "✅ Pokémon Center monitor recovered: "
                "site is readable again."
            )
            state["health_alerted"] = False
        except Exception as exc:
            log(f"Recovery notice failed: {type(exc).__name__}: {exc}")

    state["unknown_streak"] = 0

    if result == "NORMAL":
        state["last_signal"] = None
        return

    # QUEUE and CHALLENGE have separate alert states.
    # If Discord fails here, last_signal stays unchanged, so the
    # alert is retried on the next cycle.
    if state["last_signal"] != result:
        if result == "QUEUE":
            label = "🚨 POKÉMON CENTER QUEUE DETECTED"
        else:
            label = (
                "⚠️ POKÉMON CENTER SECURITY CHALLENGE DETECTED "
                "(not confirmed queue)"
            )

        discord_message(
            f"{label}\n"
            f"{STORE_URL}\n"
            f"Detected at {now_et():%Y-%m-%d %I:%M:%S %p} ET",
            mention=True,
        )

        state["last_signal"] = result


# --------------------------------------------------------------------- main

def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log(
        "Monitor started: weekdays "
        "10 AM-2 PM = 140s; 2-4 PM = 300s"
    )

    state = load_state()

    while not STOP:
        now = now_et()

        if monitoring_interval(now) is None:
            # Cap the sleep so DST shifts can't make us wake late.
            rest = min(seconds_until_next_open(now), 1800)

            log(
                "Outside monitoring hours; "
                f"sleeping {rest / 60:.1f} minutes"
            )

            sleep_interruptible(rest)
            continue

        month = now.strftime("%Y-%m")

        if state["month"] != month:
            state["month"] = month
            state["attempts"] = 0
            save_state(state)

        if state["attempts"] >= MONTHLY_ATTEMPT_LIMIT:
            log("Monthly attempt limit reached; pausing checks")
            sleep_interruptible(3600)
            continue

        state["attempts"] += 1
        save_state(state)

        # Stage 1: check the site.
        try:
            result = check_once()
        except Exception as exc:
            log(f"Check failed: {type(exc).__name__}: {exc}")
            result = "UNKNOWN"

        log(f"Detection={result} attempt={state['attempts']}")

        # Stage 2: act on the result. A failure here (e.g. Discord)
        # is logged but never counted as a site failure.
        try:
            handle_result(result, state)
        except Exception as exc:
            log(f"Result handling failed: {type(exc).__name__}: {exc}")

        save_state(state)

        next_interval = monitoring_interval(now_et())

        if next_interval is None:
            sleep_interruptible(min(seconds_until_next_open(now_et()), 1800))
        else:
            sleep_interruptible(next_interval)

    save_state(state)
    log("Monitor stopped cleanly")


if __name__ == "__main__":
    main()
