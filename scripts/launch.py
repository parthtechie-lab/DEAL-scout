"""
launch.py — Deal Scout Master Launcher

Runs ALL monitoring engines simultaneously in parallel:
  1. telegram_streamer.py  — Real-time 0-second Telegram AI engine
  2. food_realtime_monitor — Scrapes Reddit, DesiDime, GrabOn every 60 seconds

Usage:
  .venv/bin/python scripts/launch.py
  or to run silently in background:
  nohup .venv/bin/python scripts/launch.py > scout.log 2>&1 &
"""

import asyncio
import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import telegram_streamer
import food_realtime_monitor

async def main():
    print("=" * 60)
    print("🚀 DEAL SCOUT — ALL ENGINES LAUNCHING")
    print("=" * 60)
    print("  Engine 1: Telegram Real-Time AI Streamer (0s latency)")
    print("  Engine 2: Internet Food Coupon Hunter (60s cycle)")
    print("=" * 60)

    # Run both engines concurrently forever
    await asyncio.gather(
        telegram_streamer.main(),
        food_realtime_monitor.run_forever(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Launch] All engines stopped gracefully.")
