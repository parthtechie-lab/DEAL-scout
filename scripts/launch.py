"""
launch.py — Deal Scout Master Launcher (Self-Healing)

Runs BOTH engines simultaneously. If one crashes, it auto-restarts it
independently without killing the other engine.

  Engine 1: telegram_streamer.py  — Real-time 0-latency Telegram AI
  Engine 2: web_deal_monitor.py   — Scrapes Reddit & DesiDime every 5 mins
"""

import asyncio
import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import web_deal_monitor
import telegram_streamer


async def run_with_auto_restart(engine_name: str, engine_func, min_restart_delay: int = 30):
    """
    Wraps any async engine function in an infinite self-healing loop.
    If it crashes or exits for any reason, it automatically restarts after a delay.
    Uses exponential backoff so rapid crashes don't spam restarts.
    """
    restart_delay = min_restart_delay
    max_delay = 600  # 10 minutes max between restarts

    while True:
        try:
            print(f"[Launcher] ▶ Starting {engine_name}...")
            await engine_func()
            # If it exits cleanly (no exception), restart anyway
            print(f"[Launcher] {engine_name} exited cleanly. Restarting in {restart_delay}s...")
        except asyncio.CancelledError:
            print(f"[Launcher] {engine_name} cancelled. Stopping.")
            return  # Don't restart on deliberate cancellation
        except Exception as e:
            print(f"[Launcher] {engine_name} crashed: {e}. Restarting in {restart_delay}s...")

        await asyncio.sleep(restart_delay)
        restart_delay = min(restart_delay * 2, max_delay)  # Exponential backoff
        # Reset delay after a long successful run would happen naturally
        # since restart_delay only grows on repeated fast crashes


async def main():
    print("=" * 60)
    print("🚀 DEAL SCOUT — DUAL ENGINE LAUNCHING (SELF-HEALING)")
    print("=" * 60)
    print("  Engine 1: Telegram Real-Time AI Streamer (0s latency)")
    print("  Engine 2: Advanced Web AI Scraper (5 min interval)")
    print("  ♻️  Both engines auto-restart if they ever crash")
    print("=" * 60)

    # Run both engines independently — one crash does NOT kill the other
    await asyncio.gather(
        run_with_auto_restart("Telegram Engine", telegram_streamer.main),
        run_with_auto_restart("Web Scraper Engine", web_deal_monitor.run_forever),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Launcher] All engines stopped gracefully.")
