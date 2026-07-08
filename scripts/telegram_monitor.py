"""
telegram_monitor.py — reads recent messages from deal channels and matches
them against your watchlist keywords AND category keywords.

Uses Telethon (a USER client, not a bot) because bots cannot read channel
history/messages the way a logged-in user account can. This is why setup
needs TELEGRAM_API_ID / TELEGRAM_API_HASH from https://my.telegram.org,
plus a one-time login that produces a session string (see
generate_session.py), stored afterwards as a GitHub Secret so Actions
never needs to log in interactively again.

Matching is multi-layered:
  Layer 1 — specific product keywords (high confidence, always alert)
  Layer 2 — food platform detection (swiggy/zomato/blinkit/etc) + deal signal
  Layer 3 — category keywords (electronics/food/sports/electrical) +
             deal signal words → medium confidence alerts

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

# How far back to look each run (should be >= cron interval + margin)
LOOKBACK_MINUTES = 45  # 30-min cron + 15 min margin

CATEGORY_EMOJI = {
    "electronics": "📱",
    "electrical":  "🔌",
    "food":        "🍽️",
    "sports":      "🏋️",
}

# Food platform detection for smart categorization
FOOD_PLATFORMS = {
    "swiggy":    {"name": "Swiggy",           "emoji": "🛵"},
    "zomato":    {"name": "Zomato",            "emoji": "🍕"},
    "blinkit":   {"name": "Blinkit",           "emoji": "⚡"},
    "zepto":     {"name": "Zepto",             "emoji": "🟣"},
    "bigbasket": {"name": "BigBasket",         "emoji": "🛒"},
    "big basket":{"name": "BigBasket",         "emoji": "🛒"},
    "dominos":   {"name": "Dominos",           "emoji": "🍕"},
    "domino's":  {"name": "Dominos",           "emoji": "🍕"},
    "instamart": {"name": "Swiggy Instamart",  "emoji": "📦"},
    "faasos":    {"name": "Faasos",            "emoji": "🌯"},
    "pizza hut": {"name": "Pizza Hut",         "emoji": "🍕"},
    "kfc":       {"name": "KFC",               "emoji": "🍗"},
    "mcdonald":  {"name": "McDonald's",        "emoji": "🍔"},
    "burger king":{"name": "Burger King",      "emoji": "🍔"},
}


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def has_deal_signal(text: str, signal_words: list) -> bool:
    """Returns True if the message looks like a deal/discount announcement."""
    lower = text.lower()
    return any(signal in lower for signal in signal_words)


def extract_coupon_code(text: str) -> str:
    """Try to extract a coupon code from message text."""
    # Pattern: "Use code: XXXXX" or "Code: XXXXX" or "Apply: XXXXX"
    patterns = [
        r'(?:use|apply|code|coupon|promo)[:\s]+([A-Z][A-Z0-9]{3,14})',
        r'(?:code|coupon|promo)[\s:]+([A-Z][A-Z0-9]{3,14})',
        r'"([A-Z][A-Z0-9]{4,14})"',
        r'\b([A-Z][A-Z0-9]{5,14})\b',  # Standalone ALLCAPS word
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            code = match.group(1).upper()
            # Skip common false positives
            if code not in ("TERMS", "ABOUT", "LOGIN", "SHARE", "CLICK",
                           "CLOSE", "EMAIL", "PHONE", "ORDER", "APPLY",
                           "CHECK", "INDIA", "AMAZON", "FLIPKART"):
                return code
    return ""


import re


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


def detect_food_platforms(text: str) -> list[dict]:
    """Returns all food platforms mentioned in the text."""
    text_lower = text.lower()
    found = []
    seen = set()
    for key, info in FOOD_PLATFORMS.items():
        if key in text_lower and info["name"] not in seen:
            found.append(info)
            seen.add(info["name"])
    return found


async def scan_channels():
    watchlist = load_watchlist()
    channels = watchlist.get("telegram_channels", [])
    products = watchlist.get("products", [])
    category_keywords = watchlist.get("category_keywords", {})
    signal_words = watchlist.get("deal_signal_words", [
        "%", "off", "discount", "coupon", "deal", "offer", "sale",
        "loot", "₹", "free", "lowest", "cashback"
    ])

    if not API_ID or not API_HASH or not SESSION_STRING:
        print("[telegram_monitor] Missing Telegram API credentials — "
              "run scripts/generate_session.py first. Skipping.")
        return

    client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
    await client.start()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    total_alerts = 0
    channels_ok = 0
    channels_fail = 0

    for channel in channels:
        if channel.startswith("@replace_with"):
            continue

        try:
            msg_count = 0
            async for message in client.iter_messages(channel, limit=200):
                if message.date < cutoff:
                    break
                if not message.text:
                    continue

                msg_count += 1
                text = message.text
                dedup_base = f"tg:{channel}:{message.id}"

                # --- Layer 1: Specific product keyword match ---
                for product in products:
                    if message_matches_product(text, product["keywords"]):
                        dedup_key = f"{dedup_base}:product:{product['name']}"
                        if already_alerted(dedup_key):
                            continue

                        emoji = CATEGORY_EMOJI.get(product["category"], "🛒")
                        coupon = extract_coupon_code(text)
                        coupon_line = f"🎟️ Code spotted: <code>{coupon}</code>\n" if coupon else ""

                        alert_text = (
                            f"{emoji} <b>Product Deal Spotted!</b> — {product['name']}\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📍 Channel: {channel}\n"
                            f"🏷️ Category: {product['category'].title()}\n"
                            f"{coupon_line}\n"
                            f"{text[:500]}\n\n"
                            f"✅ <b>Action plan:</b>\n"
                            f"1. Activate cashback on CashKaro/GoPaisa first\n"
                            f"2. Check BuyHatke extension for price history\n"
                            f"3. Stack with bank card offer for extra savings"
                        )
                        send_alert(alert_text)
                        mark_alerted(dedup_key, product["name"], 0)
                        total_alerts += 1

                # --- Layer 2: Food platform mention + deal signal ---
                if has_deal_signal(text, signal_words):
                    food_platforms = detect_food_platforms(text)
                    for fp in food_platforms:
                        dedup_key = f"{dedup_base}:food:{fp['name']}"
                        if already_alerted(dedup_key):
                            continue

                        coupon = extract_coupon_code(text)
                        coupon_line = f"🎟️ Code: <code>{coupon}</code>\n" if coupon else ""

                        alert_text = (
                            f"{fp['emoji']} <b>{fp['name']} Deal!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📍 Channel: {channel}\n"
                            f"{coupon_line}\n"
                            f"{text[:500]}\n\n"
                            f"✅ Open {fp['name']} app → Apply code → "
                            f"Pay with bank card for max savings"
                        )
                        send_alert(alert_text)
                        mark_alerted(dedup_key, fp["name"], 0)
                        total_alerts += 1

                # --- Layer 3: Category keyword match + deal signal ---
                if has_deal_signal(text, signal_words):
                    matched_cats = get_matching_categories(text, category_keywords)
                    for cat in matched_cats:
                        dedup_key = f"{dedup_base}:cat:{cat}"
                        if already_alerted(dedup_key):
                            continue

                        emoji = CATEGORY_EMOJI.get(cat, "🛒")
                        coupon = extract_coupon_code(text)
                        coupon_line = f"🎟️ Code: <code>{coupon}</code>\n" if coupon else ""

                        alert_text = (
                            f"{emoji} <b>Category Deal Signal</b> — {cat.title()}\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📍 Channel: {channel}\n"
                            f"{coupon_line}\n"
                            f"{text[:500]}\n\n"
                            f"<i>Verify this matches your wishlist before acting.</i>"
                        )
                        send_alert(alert_text)
                        mark_alerted(dedup_key, cat, 0)
                        total_alerts += 1

            channels_ok += 1
            print(f"[telegram_monitor] {channel}: scanned {msg_count} messages")

        except Exception as e:
            channels_fail += 1
            print(f"[telegram_monitor] Error reading {channel}: {e}")

    await client.disconnect()
    print(f"[telegram_monitor] Done. {channels_ok} channels OK, "
          f"{channels_fail} failed, {total_alerts} alerts sent.")


if __name__ == "__main__":
    asyncio.run(scan_channels())
