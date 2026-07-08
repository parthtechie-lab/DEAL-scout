"""
run_all.py — entrypoint called by the GitHub Actions workflow.

Runs the fast, frequent checks every time (Telegram + price).
Bank offers run less often — controlled by an env var set in the workflow's
cron schedule, so we don't hammer bank sites hourly for no reason.
"""

import os
from db import init_db

import telegram_monitor
import price_checker
import bank_offers
import asyncio

RUN_BANK_CHECK = os.getenv("RUN_BANK_CHECK", "false").lower() == "true"


def main():
    init_db()

    print("[run_all] Checking Telegram channels...")
    try:
        asyncio.run(telegram_monitor.scan_channels())
    except Exception as e:
        print(f"[run_all] Telegram check failed: {e}")

    print("[run_all] Checking prices...")
    try:
        price_checker.run()
    except Exception as e:
        print(f"[run_all] Price check failed: {e}")

    if RUN_BANK_CHECK:
        print("[run_all] Checking bank offers...")
        try:
            bank_offers.run()
        except Exception as e:
            print(f"[run_all] Bank offer check failed: {e}")

    print("[run_all] Done.")


if __name__ == "__main__":
    main()
