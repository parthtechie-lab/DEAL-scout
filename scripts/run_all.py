"""
run_all.py — Deal Scout v2.1 entrypoint.

v2.1 changes (issue #2):
  • Structured step tracking: every step records success/failure + alert count.
  • On failure, Telegram receives a detailed breakdown of which steps failed,
    not just a generic "run failed" ping.
  • RUN_SUMMARY dict is printed at the end of every run regardless of outcome.
  • SKIP_PLAYWRIGHT env var controls Playwright steps (true for all cron runs).
"""

import os
import sys
import asyncio
from datetime import datetime

from db import init_db

import telegram_monitor
import reddit_monitor
import coupon_detector
import bank_offers

SKIP_PLAYWRIGHT = os.getenv("SKIP_PLAYWRIGHT", "true").lower() == "true"
RUN_BANK_CHECK  = os.getenv("RUN_BANK_CHECK",  "false").lower() == "true"

if not SKIP_PLAYWRIGHT:
    try:
        import price_checker
        HAS_PRICE_CHECKER = True
    except ModuleNotFoundError:
        print("[run_all] Warning: price_checker.py not found. Skipping.")
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

# ── Step result tracker ───────────────────────────────────────────────────────
RUN_SUMMARY: dict[str, dict] = {}

def record_step(name: str, ok: bool, alerts: int = 0, error: str = ""):
    RUN_SUMMARY[name] = {"ok": ok, "alerts": alerts, "error": error}

def send_run_summary_alert():
    """
    Sends a structured summary to Telegram (only if at least one step failed).
    Requires BOT_TOKEN / CHAT_ID in env — gracefully skips if missing.
    """
    import os, requests
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat  = os.getenv("TELEGRAM_CHAT_ID",   "")
    if not token or not chat:
        return

    failed = [name for name, r in RUN_SUMMARY.items() if not r["ok"]]
    if not failed:
        return   # All steps OK — no alert needed

    total_alerts = sum(r["alerts"] for r in RUN_SUMMARY.values())
    lines = [
        "⚠️ <b>Deal Scout — Run Failures Detected</b>",
        f"🕐 UTC: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
        f"✅ Alerts sent this run: {total_alerts}",
        "",
        "<b>Step results:</b>",
    ]
    for name, r in RUN_SUMMARY.items():
        icon = "✅" if r["ok"] else "❌"
        line = f"{icon} {name}"
        if r["alerts"]:
            line += f" (+{r['alerts']} alerts)"
        if r["error"]:
            line += f"\n   └ {r['error'][:120]}"
        lines.append(line)

    lines += [
        "",
        "🔗 Check the Actions log for details.",
    ]

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[run_all] Could not send failure summary: {e}")


def main():
    init_db()

    # ── Step 1: Telegram ───────────────────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 📱 Step 1: Telegram channels")
    print("="*60)
    try:
        asyncio.run(telegram_monitor.scan_channels())
        record_step("Telegram Monitor", ok=True)
    except Exception as e:
        print(f"[run_all] Telegram failed: {e}")
        record_step("Telegram Monitor", ok=False, error=str(e))

    # ── Step 2: Reddit ─────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 🤖 Step 2: Reddit subreddits")
    print("="*60)
    try:
        reddit_monitor.run()
        record_step("Reddit Monitor", ok=True)
    except Exception as e:
        print(f"[run_all] Reddit failed: {e}")
        record_step("Reddit Monitor", ok=False, error=str(e))

    # ── Step 3: Coupon Detector ────────────────────────────────────────────
    print("\n" + "="*60)
    print("[run_all] 🎟️  Step 3: Instant coupon scan")
    print("="*60)
    try:
        coupon_detector.run()
        record_step("Coupon Detector", ok=True)
    except Exception as e:
        print(f"[run_all] Coupon detector failed: {e}")
        record_step("Coupon Detector", ok=False, error=str(e))

    # ── Step 4: Price Tracker (manual only) ───────────────────────────────
    print("\n" + "="*60)
    if HAS_PRICE_CHECKER:
        print("[run_all] 📊 Step 4: Price tracker (Playwright)")
        print("="*60)
        try:
            price_checker.run()
            record_step("Price Checker", ok=True)
        except Exception as e:
            print(f"[run_all] Price check failed: {e}")
            record_step("Price Checker", ok=False, error=str(e))
    else:
        print("[run_all] ⏭️  Step 4: Playwright not active — skipping price check.")
        print("="*60)

    # ── Step 5: Deep Food Scraper (manual only) ────────────────────────────
    print("\n" + "="*60)
    if HAS_FOOD_SCRAPER:
        print("[run_all] 🍕 Step 5: Deep food coupon scrape (Playwright)")
        print("="*60)
        try:
            food_coupon_scraper.run()
            record_step("Food Scraper", ok=True)
        except Exception as e:
            print(f"[run_all] Food scrape failed: {e}")
            record_step("Food Scraper", ok=False, error=str(e))
    else:
        print("[run_all] ⏭️  Step 5: Playwright not active — skipping deep scrape.")
        print("="*60)

    # ── Step 6: Bank Offers (weekly) ──────────────────────────────────────
    print("\n" + "="*60)
    if RUN_BANK_CHECK:
        print("[run_all] 🏦 Step 6: Bank card offers")
        print("="*60)
        try:
            bank_offers.run()
            record_step("Bank Offers", ok=True)
        except Exception as e:
            print(f"[run_all] Bank check failed: {e}")
            record_step("Bank Offers", ok=False, error=str(e))

    # ── Final summary ──────────────────────────────────────────────────────
    failed_steps = [n for n, r in RUN_SUMMARY.items() if not r["ok"]]
    total_alerts = sum(r["alerts"] for r in RUN_SUMMARY.values())

    print("\n" + "="*60)
    print(f"[run_all] ✅ Done. Steps run: {len(RUN_SUMMARY)}, "
          f"failed: {len(failed_steps)}, total alerts: {total_alerts}")
    if failed_steps:
        print(f"[run_all] ❌ Failed steps: {', '.join(failed_steps)}")
        send_run_summary_alert()
    print("="*60)

    if failed_steps:
        sys.exit(1)


if __name__ == "__main__":
    main()
