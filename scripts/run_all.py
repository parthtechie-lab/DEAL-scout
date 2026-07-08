"""
run_all.py — Deal Scout v2 entrypoint (called by GitHub Actions).

Respects SKIP_PLAYWRIGHT=true (set by workflow for all scheduled runs).
Playwright is intentionally excluded from cron to keep Actions minutes low.
It runs only on manual workflow_dispatch trigger.
"""

import os
from db import init_db

import telegram_monitor
import reddit_monitor
import coupon_detector
import bank_offers
import asyncio

SKIP_PLAYWRIGHT  = os.getenv("SKIP_PLAYWRIGHT", "true").lower() == "true"
RUN_BANK_CHECK   = os.getenv("RUN_BANK_CHECK", "false").lower() == "true"

if not SKIP_PLAYWRIGHT:
    try:
        import price_checker
        HAS_PRICE_CHECKER = True
    except ModuleNotFoundError:
        print("[run_all] Warning: price_checker.py not found. Skipping price checks.")
        HAS_PRICE_CHECKER = False

    try:
        import food_coupon_scraper
        HAS_FOOD_SCRAPER = True
    except ModuleNotFoundError:
        print("[run_all] Warning: food_coupon_scraper.py not found. Skipping.")
        HAS_FOOD_SCRAPER = False
else:
    HAS_PRICE_CHECKER = False
    HAS_FOOD_SCRAPER  = False


def main():
    init_db()

    # ── Step 1: Telegram Channels ──────────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 📱 Step 1: Scanning Telegram channels...")
    print("="*60)
    try:
        asyncio.run(telegram_monitor.scan_channels())
    except Exception as e:
        print(f"[run_all] Telegram check failed: {e}")

    # ── Step 2: Reddit Subreddits ──────────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 🤖 Step 2: Scanning Reddit deal subreddits...")
    print("="*60)
    try:
        reddit_monitor.run()
    except Exception as e:
        print(f"[run_all] Reddit check failed: {e}")

    # ── Step 3: Instant Coupon Detector ───────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 🎟️  Step 3: Coupon scan (Swiggy/Zomato/Blinkit/Zepto/Dominos)...")
    print("="*60)
    try:
        coupon_detector.run()
    except Exception as e:
        print(f"[run_all] Coupon detector failed: {e}")

    # ── Step 4: Price Tracker (manual only) ───────────────────────────────
    print("\n" + "="*60)
    if HAS_PRICE_CHECKER:
        print("[run_all] 📊 Step 4: Checking product prices (Playwright)...")
        print("="*60)
        try:
            price_checker.run()
        except Exception as e:
            print(f"[run_all] Price check failed: {e}")
    else:
        print("[run_all] ⏭️  Step 4: Skipping price check (Playwright not active).")
        print("="*60)

    # ── Step 5: Deep Food Coupon Scraper (manual only) ────────────────────
    print("\n" + "="*60)
    if HAS_FOOD_SCRAPER:
        print("[run_all] 🍕 Step 5: Deep food coupon scrape (Playwright)...")
        print("="*60)
        try:
            food_coupon_scraper.run()
        except Exception as e:
            print(f"[run_all] Food coupon scrape failed: {e}")
    else:
        print("[run_all] ⏭️  Step 5: Skipping deep scrape (Playwright not active).")
        print("="*60)

    # ── Step 6: Bank Offers (weekly) ──────────────────────────────────────
    print("\n" + "="*60)
    if RUN_BANK_CHECK:
        print("[run_all] 🏦 Step 6: Checking bank card offers...")
        print("="*60)
        try:
            bank_offers.run()
        except Exception as e:
            print(f"[run_all] Bank offer check failed: {e}")
    else:
        print("[run_all] ⏭️  Step 6: Bank offers (weekly — runs on Monday).")
        print("="*60)

    print("\n✅ [run_all] Done!")


if __name__ == "__main__":
    main()
