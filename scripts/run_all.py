"""
run_all.py — entrypoint called by the GitHub Actions workflow.

Runs all scanners every 30 minutes:
  1. Telegram channel monitor (16 verified deal channels)
  2. Reddit deal subreddit scanner (5 Indian deal subreddits)
  3. Price checker (Amazon/Flipkart product tracking)
  4. Food coupon scraper (12+ aggregator sites × 7 food platforms)
  5. Bank offers (weekly, controlled by env var)
"""

import os
from db import init_db

import telegram_monitor
import reddit_monitor

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

    # ── Step 1: Telegram Channels ──────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 📱 Step 1/5: Scanning 16 Telegram deal channels...")
    print("="*60)
    try:
        asyncio.run(telegram_monitor.scan_channels())
    except Exception as e:
        print(f"[run_all] Telegram check failed: {e}")

    # ── Step 2: Reddit Subreddits ──────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 🤖 Step 2/5: Scanning Reddit deal subreddits...")
    print("="*60)
    try:
        reddit_monitor.run()
    except Exception as e:
        print(f"[run_all] Reddit check failed: {e}")

    # ── Step 3: Price Tracker ──────────────────────────────────────────
    print("\n" + "="*60)
    if HAS_PRICE_CHECKER:
        print("[run_all] 📊 Step 3/5: Checking product prices...")
        print("="*60)
        try:
            price_checker.run()
        except Exception as e:
            print(f"[run_all] Price check failed: {e}")
    else:
        print("[run_all] ⏭️ Step 3/5: Skipping price check (Playwright unavailable).")
        print("="*60)

    # ── Step 4: Food Coupon Scraper ────────────────────────────────────
    print("\n" + "="*60)
    if HAS_FOOD_SCRAPER:
        print("[run_all] 🍕 Step 4/5: Scraping food coupons (Swiggy/Zomato/Blinkit/Zepto/BigBasket/Dominos)...")
        print("="*60)
        try:
            food_coupon_scraper.run()
        except Exception as e:
            print(f"[run_all] Food coupon scrape failed: {e}")
    else:
        print("[run_all] ⏭️ Step 4/5: Skipping food coupon scrape (Playwright unavailable).")
        print("="*60)

    # ── Step 5: Bank Offers (weekly) ───────────────────────────────────
    print("\n" + "="*60)
    if RUN_BANK_CHECK:
        print("[run_all] 🏦 Step 5/5: Checking bank card offers...")
        print("="*60)
        try:
            bank_offers.run()
        except Exception as e:
            print(f"[run_all] Bank offer check failed: {e}")
    else:
        print("[run_all] ⏭️ Step 5/5: Bank offers (runs weekly on Monday).")
        print("="*60)

    print("\n✅ [run_all] All checks complete!")


if __name__ == "__main__":
    main()
