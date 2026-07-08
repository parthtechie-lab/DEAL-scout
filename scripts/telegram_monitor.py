"""
telegram_monitor.py — reads recent messages from deal channels and matches
them against your watchlist keywords AND category keywords.

Uses Telethon (a USER client, not a bot) because bots cannot read channel
history/messages the way a logged-in user account can. This is why setup
needs TELEGRAM_API_ID / TELEGRAM_API_HASH from https://my.telegram.org,
plus a one-time login that produces a session string (see
generate_session.py), stored afterwards as a GitHub Secret so Actions
never needs to log in interactively again.

Category matching is layered:
  Layer 1 — specific product keywords (high confidence, always alert)
  Layer 2 — category keywords (electronics/food/sports/electrical) +
             deal signal words (%, off, coupon, cashback) → medium confidence
             alerts, with a note to verify.

This layered approach means you won't miss a great deal just because it
doesn't mention your exact product name, but you also won't get spammed by
irrelevant channel noise.
"""

import asyncio
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from db import already_alerted, mark_alerted
from notifier import send_alert

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("TELEGRAM_SESSION")

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"

# How far back to look each run. Should be >= your cron interval with some
# overlap margin, so a slow run doesn't miss messages posted right at the edge.
LOOKBACK_MINUTES = 90

# Deal signal words — a category keyword alone isn't enough; the message
# should also contain one of these to be considered an actual deal post.
DEAL_SIGNAL_WORDS = [
    "%", "off", "discount", "coupon", "promo", "cashback", "deal",
    "offer", "sale", "loot", "flat", "rs.", "₹", "free", "lowest",
    "price drop", "bank offer", "extra", "flash", "limited", "today only"
]

CATEGORY_EMOJI = {
    "electronics": "📱",
    "electrical":  "🔌",
    "food":        "🍽️",
    "sports":      "🏋️",
}


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def has_deal_signal(text: str) -> bool:
    """Returns True if the message looks like a deal/discount announcement."""
    lower = text.lower()
    return any(signal in lower for signal in DEAL_SIGNAL_WORDS)


def message_matches_product(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def get_matching_categories(text: str, category_keywords: dict) -> list[str]:
    """Returns all categories whose keywords appear in the message text."""
    text_lower = text.lower()
    matched = []
    for category, keywords in category_keywords.items():
        if any(kw.lower() in text_lower for kw in keywords):
            matched.append(category)
    return matched


async def scan_channels():
    watchlist = load_watchlist()
    channels = watchlist.get("telegram_channels", [])
    products = watchlist.get("products", [])
    category_keywords = watchlist.get("category_keywords", {})

    if not API_ID or not API_HASH or not SESSION_STRING:
        print("[telegram_monitor] Missing Telegram API credentials — "
              "run scripts/generate_session.py first. Skipping.")
        return

    client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
    await client.start()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)

    for channel in channels:
        if channel.startswith("@replace_with"):
            continue  # skip unfilled placeholders

        try:
            async for message in client.iter_messages(channel, limit=200):
                if message.date < cutoff:
                    break
                if not message.text:
                    continue

                text = message.text
                dedup_base = f"tg:{channel}:{message.id}"

                # --- Layer 1: Specific product keyword match ---
                for product in products:
                    if message_matches_product(text, product["keywords"]):
                        dedup_key = f"{dedup_base}:product:{product['name']}"
                        if already_alerted(dedup_key):
                            continue

                        emoji = CATEGORY_EMOJI.get(product["category"], "🛒")
                        alert_text = (
                            f"{emoji} <b>Product Deal Spotted!</b> — {product['name']}\n"
                            f"Category: {product['category'].title()}\n"
                            f"Channel: {channel}\n\n"
                            f"{text[:400]}\n\n"
                            f"<i>Tip: Check CashKaro/GrabOn first for extra cashback, "
                            f"then use your bank's card offer.</i>"
                        )
                        send_alert(alert_text)
                        mark_alerted(dedup_key, product["name"], 0)

                # --- Layer 2: Category keyword match + deal signal ---
                if has_deal_signal(text):
                    matched_cats = get_matching_categories(text, category_keywords)
                    for cat in matched_cats:
                        dedup_key = f"{dedup_base}:cat:{cat}"
                        if already_alerted(dedup_key):
                            continue

                        emoji = CATEGORY_EMOJI.get(cat, "🛒")
                        alert_text = (
                            f"{emoji} <b>Category Deal Signal</b> — {cat.title()}\n"
                            f"Channel: {channel}\n\n"
                            f"{text[:400]}\n\n"
                            f"<i>Unverified signal — check if this matches "
                            f"something on your wishlist before acting.</i>"
                        )
                        send_alert(alert_text)
                        mark_alerted(dedup_key, cat, 0)

        except Exception as e:
            print(f"[telegram_monitor] Error reading {channel}: {e}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(scan_channels())
