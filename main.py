
def main():
    state = load_state()
    startup_sent = False

    check_interval = 540
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
                                if not state["US"].get(
                                    "captcha_alerted", False
                                ):
                                    if send_captcha_alert(store):
                                        state["US"]["captcha_alerted"] = True
                                        save_state(state)
                                else:
                                    print(
                                        "CAPTCHA still detected. Alert already sent.",
                                        flush=True
                                    )

                            else:
                                was_live = state["US"]["was_live"]

                                print(
                                    f"US Queue Active: {result}",
                                    flush=True
                                )

                                if result is True and not was_live:
                                    if send_discord_alert(store, final_url):
                                        state["US"]["was_live"] = True
                                else:
                                    state["US"]["was_live"] = result

                                if result is False:
                                    state["US"]["captcha_alerted"] = False

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
