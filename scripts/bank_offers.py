"""
bank_offers.py — Deal Scout v2.

Weekly check of bank offer pages for keywords matching your watchlist
categories. Low frequency by design (bank offers change slowly).

v2 upgrades:
  • 10 banks (was 5): HDFC, ICICI, SBI, Axis, Kotak, American Express,
    Yes Bank, IDFC First, RBL Bank, Bank of Baroda.
  • card_types metadata (credit / debit) shown in alert.
  • All 13 categories checked (was 4).
  • reliability_score shown in alert for context.

Treats matches as "worth checking manually," not gospel — bank offer pages
have heavy JS so this is keyword-on-plain-text, not precise CSS parsing.
"""

import json
from pathlib import Path

import requests
from dotenv import load_dotenv

from db import already_alerted, mark_alerted
from notifier import send_alert

load_dotenv()

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def run():
    watchlist    = load_watchlist()
    bank_pages   = watchlist.get("bank_offer_pages", [])
    category_kws = watchlist.get("category_keywords", {})
    all_categories = list(category_kws.keys())

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    for bank in bank_pages:
        bank_name   = bank.get("bank", "Unknown Bank")
        bank_url    = bank.get("url", "")
        card_types  = bank.get("card_types", [])
        reliability = bank.get("reliability_score", 5)

        card_str = " / ".join(c.title() for c in card_types) if card_types else "Card"

        try:
            resp = requests.get(bank_url, headers=headers, timeout=15)
            page_text = resp.text.lower()
        except Exception as e:
            print(f"[bank_offers] Error fetching {bank_name}: {e}")
            continue

        for category in all_categories:
            # Check if any keyword from this category appears on the bank page
            category_kw_list = category_kws.get(category, [category])
            found_kws = [kw for kw in category_kw_list if kw.lower() in page_text]

            if not found_kws:
                continue

            dedup_key = f"bank:{bank_name}:{category}"
            if already_alerted(dedup_key, within_hours=24 * 7):
                continue

            kw_preview = ", ".join(found_kws[:3])
            msg = (
                f"🏦 <b>Bank Offer Signal — {bank_name}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💳 Card type: {card_str}\n"
                f"🏷️ Category: {category.replace('_', ' ').title()}\n"
                f"🔍 Keywords spotted: <i>{kw_preview}</i>\n"
                f"⭐ Source reliability: {reliability}/10\n\n"
                f"<b>Action:</b> Check the page manually — this is a keyword "
                f"match, not a confirmed offer. Verify discount and card eligibility.\n\n"
                f"🔗 <a href='{bank_url}'>Open {bank_name} offers page</a>"
            )
            send_alert(msg)
            mark_alerted(dedup_key, bank_name, 0,
                         source_reliability=reliability, category=category)
            print(f"[bank_offers] Alert sent — {bank_name} × {category}")


if __name__ == "__main__":
    run()
