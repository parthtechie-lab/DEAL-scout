"""
launch.py — Deal Scout Master Launcher

Runs the Web AI Deal Monitor Engine.
Usage:
  .venv/bin/python scripts/launch.py
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

async def main():
    print("=" * 60)
    print("🚀 DEAL SCOUT — WEB AI SCRAPER LAUNCHING")
    print("=" * 60)

    await web_deal_monitor.run_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Launch] Engine stopped gracefully.")
