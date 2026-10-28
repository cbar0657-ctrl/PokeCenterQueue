import json
import os
import re
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

BROWSERLESS_WS_URL = os.environ["BROWSERLESS_WS_URL"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MENTION_TEXT = os.getenv("MENTION_TEXT", "@everyone")

# Your original working URL. Override with the STORE_URL env var if needed.
STORE_URL = os.getenv("STORE_URL", "https://www.pokemoncenter.com/en-us")
TIME_ZONE = ZoneInfo("America/New_York")

# Point at a Railway volume (e.g. /data/state.json) to survive redeploys.
STATE_FILE = Path(os.getenv("STATE_PATH", "state.json"))

# Counts Browserless sessions only (the free HTTP check is not counted).
MONTHLY_ATTEMPT_LIMIT = int(os.getenv("MONTHLY_ATTEMPT_LIMIT", "25000"))

# Set HTTP_PRECHECK=0 to skip the free check and use Browserless only.
HTTP_PRECHECK = os.getenv("HTTP_PRECHECK", "1") != "0"

CHECK_INTERVAL = 30             # loop tick; the free check runs every tick
BROWSER_ONLY_SECONDS = int(os.getenv("BROWSER_ONLY_SECONDS", "90"))
ALERTED_RECHECK_SECONDS = 300   # after an alert, slow browser rechecks
HTTP_BLIND_LIMIT = 3            # free check judged blind after this many misses

BROWSER_WAIT_SECONDS = 8        # max time to wait for the page to classify
CHALLENGE_CONFIRMATIONS = 2     # consecutive browser CHALLENGEs before pinging
BASELINE_MAX_AGE_SECONDS = int(os.getenv("BASELINE_MAX_AGE_SECONDS", "1800"))
UNKNOWN_ALERT_THRESHOLD = 3

# The real storefront HTML is large; challenge shells are tiny.
NORMAL_MIN_BYTES = 20000
CHALLENGE_MAX_HTML_CHARS = 20000

DIAGNOSTIC_LIMIT = 3
_diag_count = 0

STOP = False
LAST_EVIDENCE = []
_last_http_result = None

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

CHALLENGE_TEXT_MARKERS = (
    "additional security check is required",
    "protected and accelerated by imperva",
    "verify that an actual human",
    "incapsula incident id",
    "request unsuccessful",
    "pardon our interruption",
)

# "captcha" is intentionally not here: Browserless injects its own
# hcaptcha style tag into every page.
CHALLENGE_HTML_MARKERS = (
    "_incapsula_resource",
    "incapsula incident",
    "/_incapsula_",
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


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
        "alerted": False,
        "unknown_streak": 0,
        "health_alerted": False,
        "challenge_streak": 0,
        "last_normal": None,
        "http_blind": 0,
    }


def load_state():
    state = initial_state()

    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        if isinstance(saved, dict):
            for key in state:
                if key in saved:
                    state[key] = saved[key]

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


def has_recent_baseline(state):
    last = state.get("last_normal")

    if not last:
        return False

    try:
        age = (now_et() - datetime.fromisoformat(last)).total_seconds()
    except (ValueError, TypeError):
        return False

    return 0 <= age < BASELINE_MAX_AGE_SECONDS


# ----------------------------------------------------------------- schedule

def in_monitoring_hours(now):
    # Weekdays, 10 AM - 3:59 PM Eastern.
    return now.weekday() < 5 and 10 <= now.hour < 16


def seconds_until_open(now):
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


# ------------------------------------------- tier 1: free HTTP pre-check

def http_precheck():
    """Plain HTTP request, no Browserless. Returns NORMAL or SUSPECT.
    NORMAL means the real storefront HTML came back. Anything else
    (challenge shell, 403, queue text, error) is SUSPECT and gets
    confirmed by the browser."""
    global _last_http_result

    result = "SUSPECT"
    detail = ""

    try:
        response = requests.get(
            STORE_URL,
            headers=HTTP_HEADERS,
            timeout=15,
            allow_redirects=True,
        )

        text = response.text or ""
        lower = text.lower()

        match = re.search(r"<title[^>]*>(.*?)</title>", lower, re.S)
        title = match.group(1).strip() if match else ""

        detail = (
            f"status={response.status_code} bytes={len(text)} "
            f"title={title[:70]!r}"
        )

        has_queue_text = any(m in lower for m in QUEUE_MARKERS)
        title_ok = "official site" in title and "center" in title

        if (
            response.status_code == 200
            and len(text) >= NORMAL_MIN_BYTES
            and title_ok
            and not has_queue_text
        ):
            result = "NORMAL"

    except requests.RequestException as exc:
        detail = f"error={type(exc).__name__}"

    # Only log when the result changes, to keep logs readable.
    if result != _last_http_result:
        log(f"HTTP-PRECHECK={result} {detail}")
        _last_http_result = result

    return result


# ------------------------------------------- tier 2: Browserless check

def classify_text(status, title, body):
    """Returns NORMAL, QUEUE, CHALLENGE, or UNKNOWN (not decided yet)."""
    if any(m in body for m in QUEUE_MARKERS):
        return "QUEUE"

    if any(m in body for m in CHALLENGE_TEXT_MARKERS):
        return "CHALLENGE"

    if status is not None and status >= 400:
        return "UNKNOWN"

    if not body.strip():
        return "UNKNOWN"

    if "pokémon center official site" in title or "pokemon center official site" in title:
        return "NORMAL"

    return "UNKNOWN"


def collect_evidence(page, response, status):
    evidence = []

    try:
        html = page.content() or ""
    except Exception:
        html = ""

    html_lower = html.lower()

    if len(html) < CHALLENGE_MAX_HTML_CHARS:
        for marker in CHALLENGE_HTML_MARKERS:
            if marker in html_lower:
                evidence.append(f"html:{marker}")

    try:
        for frame in page.frames:
            if "_incapsula_" in (frame.url or "").lower():
                evidence.append("frame:_incapsula_")
                break
    except Exception:
        pass

    if status == 403 and response is not None:
        try:
            if response.headers.get("x-iinfo"):
                evidence.append("http403+x-iinfo")
        except Exception:
            pass

    return evidence, html


def read_title(page):
    try:
        return (page.title() or "").lower()
    except Exception:
        return ""


def read_body(page):
    try:
        return (page.locator("body").inner_text(timeout=1500) or "").lower()
    except Exception:
        return ""


def block_heavy(route):
    # Images, media and fonts are not needed to read the page text.
    if route.request.resource_type in ("image", "media", "font"):
        route.abort()
    else:
        route.continue_()


def read_page(page):
    global LAST_EVIDENCE, _diag_count
    LAST_EVIDENCE = []

    response = page.goto(
        STORE_URL,
        wait_until="commit",
        timeout=20000,
    )

    status = response.status if response else None

    deadline = time.monotonic() + BROWSER_WAIT_SECONDS
    title = ""
    body = ""
    result = "UNKNOWN"

    # Exit as soon as the page can be classified.
    while True:
        title = read_title(page)
        body = read_body(page)
        result = classify_text(status, title, body)

        if result != "UNKNOWN":
            break

        if time.monotonic() >= deadline or STOP:
            break

        time.sleep(0.5)

    log(
        f"BROWSER HTTP={status} TITLE={title[:80]!r} "
        f"TEXT={body[:150]!r} BODY_CHARS={len(body)}"
    )

    if result == "UNKNOWN":
        evidence, html = collect_evidence(page, response, status)
        LAST_EVIDENCE = evidence

        if evidence:
            result = "CHALLENGE"
            log(f"Imperva challenge evidence: {evidence}")

        if _diag_count < DIAGNOSTIC_LIMIT:
            _diag_count += 1
            log(f"DIAG html_len={len(html)} html_head={html[:300]!r}")

            try:
                log(f"DIAG frames={[f.url[:120] for f in page.frames]}")
            except Exception:
                pass

    return result


def browser_check():
    start = time.monotonic()

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            BROWSERLESS_WS_URL,
            timeout=30000,
        )

        try:
            # Fresh context every time, so a saved session can't hide
            # the challenge.
            context = browser.new_context()

            try:
                page = context.new_page()
                page.route("**/*", block_heavy)
                result = read_page(page)
            finally:
                context.close()

        finally:
            browser.close()

    log(f"BROWSER CHECK={result} seconds={time.monotonic() - start:.1f}")

    return result


# ----------------------------------------------------------------- handlers

def mark_normal(state):
    """The storefront loaded normally (from either tier)."""
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
    state["challenge_streak"] = 0
    state["alerted"] = False
    state["last_normal"] = now_et().isoformat()


def register_unknown(state):
    state["unknown_streak"] += 1
    log(f"UNKNOWN streak={state['unknown_streak']}")

    if (
        state["unknown_streak"] >= UNKNOWN_ALERT_THRESHOLD
        and not state["health_alerted"]
    ):
        discord_message(
            "⚠️ Pokémon Center monitor health warning: "
            f"{state['unknown_streak']} consecutive browser checks "
            "could not load the storefront (blank page, or a challenge "
            "with no recent clean load to compare against). "
            "This is NOT a confirmed queue. Check Railway logs."
        )

        state["health_alerted"] = True


def handle_browser_result(result, state):
    if result == "NORMAL":
        mark_normal(state)
        return

    if result == "UNKNOWN":
        state["challenge_streak"] = 0
        register_unknown(state)
        return

    # A challenge only means something if this setup recently loaded
    # the real storefront. Otherwise it's a monitor health problem.
    if result == "CHALLENGE" and not has_recent_baseline(state):
        log("CHALLENGE ignored: no recent NORMAL baseline")
        state["challenge_streak"] = 0
        register_unknown(state)
        return

    state["unknown_streak"] = 0

    if result == "CHALLENGE":
        state["challenge_streak"] += 1
        log(f"CHALLENGE streak={state['challenge_streak']}")

        if state["challenge_streak"] < CHALLENGE_CONFIRMATIONS:
            return
    else:
        # QUEUE text is definitive; no confirmation needed.
        state["challenge_streak"] = 0

    # If Discord fails, alerted stays False so it retries next cycle.
    if not state["alerted"]:
        if result == "CHALLENGE":
            label = "🚨 POKÉMON CENTER IMPERVA CHECK DETECTED (queue likely)"
        else:
            label = "🚨 POKÉMON CENTER QUEUE DETECTED"

        discord_message(
            f"{label}\n"
            f"{STORE_URL}\n"
            f"Detected at {now_et():%Y-%m-%d %I:%M:%S %p} ET\n"
            f"Evidence: {LAST_EVIDENCE or 'visible text match'}",
            mention=True,
        )

        state["alerted"] = True


def browser_interval(state):
    """How long to wait between Browserless sessions."""
    # After an alert we only need to learn when the queue ends.
    if state["alerted"]:
        return ALERTED_RECHECK_SECONDS

    # Mid-confirmation: confirm quickly.
    if state["challenge_streak"] > 0:
        return CHECK_INTERVAL

    # Free check is off or has proven blind: browser-only, slower.
    if not HTTP_PRECHECK or state["http_blind"] >= HTTP_BLIND_LIMIT:
        return BROWSER_ONLY_SECONDS

    return CHECK_INTERVAL


# --------------------------------------------------------------------- main

def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log(
        "Monitor started: weekdays 10 AM-4 PM ET, "
        f"free check every {CHECK_INTERVAL}s, "
        f"HTTP_PRECHECK={'on' if HTTP_PRECHECK else 'off'}"
    )
    log(f"STORE_URL={STORE_URL}")

    state = load_state()
    last_browser = 0.0  # monotonic time of the last Browserless session

    while not STOP:
        now = now_et()

        if not in_monitoring_hours(now):
            rest = min(seconds_until_open(now), 1800)
            log(f"Outside monitoring hours; sleeping {rest / 60:.1f} minutes")
            sleep_interruptible(rest)
            continue

        month = now.strftime("%Y-%m")

        if state["month"] != month:
            state["month"] = month
            state["attempts"] = 0

        # Tier 1: free check.
        http_result = http_precheck() if HTTP_PRECHECK else "SUSPECT"

        if http_result == "NORMAL":
            state["http_blind"] = 0
            mark_normal(state)
            save_state(state)
            sleep_interruptible(CHECK_INTERVAL)
            continue

        # Tier 2: Browserless, only when due.
        due = (time.monotonic() - last_browser) >= browser_interval(state)

        if due:
            if state["attempts"] >= MONTHLY_ATTEMPT_LIMIT:
                log("Monthly attempt limit reached; pausing checks")
                sleep_interruptible(3600)
                continue

            last_browser = time.monotonic()
            state["attempts"] += 1

            try:
                result = browser_check()
            except Exception as exc:
                log(f"Browser check failed: {type(exc).__name__}: {exc}")
                result = "UNKNOWN"

            log(f"Detection={result} browser_session={state['attempts']}")

            # Learn whether the free check can be trusted.
            if HTTP_PRECHECK:
                if result == "NORMAL":
                    state["http_blind"] += 1
                    if state["http_blind"] == HTTP_BLIND_LIMIT:
                        log(
                            "Free check keeps disagreeing with the "
                            "browser; switching to slower browser-only "
                            "checks"
                        )
                elif result in ("QUEUE", "CHALLENGE"):
                    state["http_blind"] = 0

            try:
                handle_browser_result(result, state)
            except Exception as exc:
                log(f"Result handling failed: {type(exc).__name__}: {exc}")

            save_state(state)

        sleep_interruptible(CHECK_INTERVAL)

    save_state(state)
    log("Monitor stopped cleanly")


if __name__ == "__main__":
    main()
