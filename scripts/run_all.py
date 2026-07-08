"""
run_all.py — Deal Scout v2 entrypoint (called by GitHub Actions).

Steps every 30 minutes:
  1. Telegram channel monitor (16 verified channels, sorted by reliability)
  2. Reddit deal subreddit scanner (5 Indian subreddits)
  3. Instant coupon detector (Swiggy/Zomato/Blinkit/Zepto/Dominos — fast, no Playwright)
  4. Price checker (Amazon/Flipkart product tracking, requires Playwright)
  5. Food coupon scraper (12+ aggregator sites × 7 platforms, requires Playwright)
  6. Bank offers (weekly, controlled by RUN_BANK_CHECK env var)
"""

import os
from db import init_db

import telegram_monitor
import reddit_monitor
import coupon_detector
import bank_offers
import asyncio

try:
    import price_checker
    HAS_PRICE_CHECKER = True
except ModuleNotFoundError:
    print("[run_all] Warning: price_checker missing Playwright. Skipping price checks.")
    HAS_PRICE_CHECKER = False

try:
    import food_coupon_scraper
    HAS_FOOD_SCRAPER = True
except ModuleNotFoundError:
    print("[run_all] Warning: food_coupon_scraper missing Playwright. Skipping.")
    HAS_FOOD_SCRAPER = False

RUN_BANK_CHECK = os.getenv("RUN_BANK_CHECK", "false").lower() == "true"


def main():
    init_db()

    # ── Step 1: Telegram Channels ──────────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 📱 Step 1/6: Scanning 16 Telegram channels (by reliability)...")
    print("="*60)
    try:
        asyncio.run(telegram_monitor.scan_channels())
    except Exception as e:
        print(f"[run_all] Telegram check failed: {e}")

    # ── Step 2: Reddit Subreddits ──────────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 🤖 Step 2/6: Scanning Reddit deal subreddits...")
    print("="*60)
    try:
        reddit_monitor.run()
    except Exception as e:
        print(f"[run_all] Reddit check failed: {e}")

    # ── Step 3: Instant Coupon Detector ───────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 🎟️  Step 3/6: Instant coupon scan (Swiggy/Zomato/Blinkit/Zepto/Dominos)...")
    print("="*60)
    try:
        coupon_detector.run()
    except Exception as e:
        print(f"[run_all] Coupon detector failed: {e}")

    # ── Step 4: Price Tracker ──────────────────────────────────────────────
    print("\n" + "="*60)
    if HAS_PRICE_CHECKER:
        print("[run_all] 📊 Step 4/6: Checking product prices...")
        print("="*60)
        try:
            price_checker.run()
        except Exception as e:
            print(f"[run_all] Price check failed: {e}")
    else:
        print("[run_all] ⏭️  Step 4/6: Skipping price check (Playwright unavailable).")
        print("="*60)

    # ── Step 5: Food Coupon Scraper (deep) ────────────────────────────────
    print("\n" + "="*60)
    if HAS_FOOD_SCRAPER:
        print("[run_all] 🍕 Step 5/6: Deep food coupon scrape (15 aggregator sites)...")
        print("="*60)
        try:
            food_coupon_scraper.run()
        except Exception as e:
            print(f"[run_all] Food coupon scrape failed: {e}")
    else:
        print("[run_all] ⏭️  Step 5/6: Skipping food coupon scrape (Playwright unavailable).")
        print("="*60)

    # ── Step 6: Bank Offers (weekly) ──────────────────────────────────────
    print("\n" + "="*60)
    if RUN_BANK_CHECK:
        print("[run_all] 🏦 Step 6/6: Checking bank card offers (10 banks)...")
        print("="*60)
        try:
            bank_offers.run()
        except Exception as e:
            print(f"[run_all] Bank offer check failed: {e}")
    else:
        print("[run_all] ⏭️  Step 6/6: Bank offers (weekly — runs on Monday 03:00 UTC).")
        print("="*60)

    print("\n✅ [run_all] All v2 checks complete!")


if __name__ == "__main__":
    main()
