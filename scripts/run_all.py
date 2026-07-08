"""
run_all.py — entrypoint called by the GitHub Actions workflow.

Runs the fast, frequent checks every time (Telegram + price).
Bank offers run less often — controlled by an env var set in the workflow's
cron schedule, so we don't hammer bank sites hourly for no reason.
"""

import os
from db import init_db

import telegram_monitor
try:
    import price_checker
    HAS_PRICE_CHECKER = True
except ModuleNotFoundError:
    print("[run_all] Warning: price_checker could not be loaded (likely missing Playwright). Skipping price checks.")
    HAS_PRICE_CHECKER = False

import bank_offers
import asyncio

try:
    import food_coupon_scraper
    HAS_FOOD_SCRAPER = True
except ModuleNotFoundError:
    print("[run_all] Warning: food_coupon_scraper could not be loaded (likely missing Playwright). Skipping food coupons.")
    HAS_FOOD_SCRAPER = False

RUN_BANK_CHECK = os.getenv("RUN_BANK_CHECK", "false").lower() == "true"


def main():
    init_db()

    print("[run_all] Checking Telegram channels...")
    try:
        asyncio.run(telegram_monitor.scan_channels())
    except Exception as e:
        print(f"[run_all] Telegram check failed: {e}")

    if HAS_PRICE_CHECKER:
        print("[run_all] Checking prices...")
        try:
            price_checker.run()
        except Exception as e:
            print(f"[run_all] Price check failed: {e}")
    else:
        print("[run_all] Skipping price check (module unavailable).")

    if HAS_FOOD_SCRAPER:
        print("[run_all] Scraping food platform coupons (Swiggy/Zomato/Blinkit/Zepto/BigBasket/Dominos)...")
        try:
            food_coupon_scraper.run()
        except Exception as e:
            print(f"[run_all] Food coupon scrape failed: {e}")
    else:
        print("[run_all] Skipping food coupon scrape (module unavailable).")

    if RUN_BANK_CHECK:
        print("[run_all] Checking bank offers...")
        try:
            bank_offers.run()
        except Exception as e:
            print(f"[run_all] Bank offer check failed: {e}")

    print("[run_all] Done.")


if __name__ == "__main__":
    main()
