"""
bank_offers.py — weekly check of bank offer pages for keywords matching your
watchlist categories. Low frequency by design (bank offers change slowly),
so this is intentionally NOT run every hour like the other two scrapers.

PLACEHOLDER STATE: uses simple text scraping (requests + regex-free keyword
match on page text) rather than fragile CSS selectors, since bank offer
pages vary a lot in structure and this is meant to be a lightweight signal,
not a precise parse. Treat matches as "worth checking manually," not gospel.
"""

import json
from pathlib import Path

import requests

from db import already_alerted, mark_alerted
from notifier import send_alert

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def run():
    watchlist = load_watchlist()
    bank_pages = watchlist.get("bank_offer_pages", [])
    categories = {p["category"] for p in watchlist.get("products", [])}

    for bank in bank_pages:
        try:
            resp = requests.get(
                bank["url"],
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            page_text = resp.text.lower()
        except Exception as e:
            print(f"[bank_offers] Error fetching {bank['bank']}: {e}")
            continue

        for category in categories:
            if category.lower() in page_text:
                dedup_key = f"bank:{bank['bank']}:{category}"
                if already_alerted(dedup_key, within_hours=24 * 7):
                    continue

                msg = (
                    f"🏦 <b>Bank offer signal</b> — {bank['bank']}\n"
                    f"Category match: {category}\n"
                    f"Check manually: {bank['url']}\n\n"
                    "This is a keyword match, not a confirmed offer — verify "
                    "the actual discount and card eligibility on the page."
                )
                send_alert(msg)
                mark_alerted(dedup_key, bank["bank"], 0)


if __name__ == "__main__":
    run()
