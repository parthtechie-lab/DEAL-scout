"""
reddit_monitor.py — monitors Indian deal subreddits for deals and coupons.

Uses Reddit's public JSON API (no authentication needed for read-only access
to public subreddits). Checks the latest posts from deal-focused subreddits
like r/dealsforindia, r/GreatIndiaDeals, r/Lootdealsforindia.

Matches posts against category keywords and deal signal words, then sends
Telegram alerts for relevant finds.

This script uses only `requests` — no special dependencies needed, runs
perfectly on both local Python 3.14 and GitHub Actions Python 3.11.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from db import already_alerted, mark_alerted
from notifier import send_alert

load_dotenv()

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"
LOOKBACK_SECONDS = 3600  # 1 hour

# Reddit wants a custom User-Agent or it rate-limits aggressively
HEADERS = {
    "User-Agent": "DealScout/1.0 (GitHub Actions bot; deal monitoring)"
}

CATEGORY_EMOJI = {
    "electronics": "📱",
    "electrical":  "🔌",
    "food":        "🍽️",
    "sports":      "🏋️",
}


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def has_deal_signal(text: str, signal_words: list) -> bool:
    lower = text.lower()
    return any(signal in lower for signal in signal_words)


def get_matching_categories(text: str, category_keywords: dict) -> list[str]:
    text_lower = text.lower()
    matched = []
    for category, keywords in category_keywords.items():
        if any(kw.lower() in text_lower for kw in keywords):
            matched.append(category)
    return matched


def fetch_subreddit_posts(subreddit: str, limit: int = 25) -> list[dict]:
    """Fetch recent posts from a subreddit using public JSON API."""
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", {}).get("children", [])
        elif resp.status_code == 429:
            print(f"[reddit] Rate limited on r/{subreddit}, waiting 5s...")
            time.sleep(5)
            return []
        else:
            print(f"[reddit] Error fetching r/{subreddit}: HTTP {resp.status_code}")
            return []
    except Exception as e:
        print(f"[reddit] Error fetching r/{subreddit}: {e}")
        return []


def check_product_match(text: str, products: list) -> dict | None:
    """Check if any product keywords match the text."""
    text_lower = text.lower()
    for product in products:
        if any(kw.lower() in text_lower for kw in product["keywords"]):
            return product
    return None


def detect_food_platform(text: str) -> dict | None:
    """Detect if a food delivery platform is mentioned."""
    text_lower = text.lower()
    platforms = {
        "swiggy": {"name": "Swiggy", "emoji": "🛵"},
        "zomato": {"name": "Zomato", "emoji": "🍕"},
        "blinkit": {"name": "Blinkit", "emoji": "⚡"},
        "zepto": {"name": "Zepto", "emoji": "🟣"},
        "bigbasket": {"name": "BigBasket", "emoji": "🛒"},
        "big basket": {"name": "BigBasket", "emoji": "🛒"},
        "dominos": {"name": "Dominos", "emoji": "🍕"},
        "domino's": {"name": "Dominos", "emoji": "🍕"},
        "instamart": {"name": "Swiggy Instamart", "emoji": "📦"},
    }
    for key, info in platforms.items():
        if key in text_lower:
            return info
    return None


def run():
    watchlist = load_watchlist()
    subreddits = watchlist.get("reddit_subreddits", [])
    products = watchlist.get("products", [])
    category_keywords = watchlist.get("category_keywords", {})
    signal_words = watchlist.get("deal_signal_words", [
        "%", "off", "discount", "coupon", "deal", "offer", "sale",
        "loot", "₹", "free", "lowest", "cashback"
    ])

    if not subreddits:
        print("[reddit] No subreddits configured. Skipping.")
        return

    cutoff = datetime.now(timezone.utc).timestamp() - LOOKBACK_SECONDS
    total_alerts = 0

    for subreddit in subreddits:
        print(f"[reddit] Scanning r/{subreddit}...")
        posts = fetch_subreddit_posts(subreddit)

        for post_wrapper in posts:
            post = post_wrapper.get("data", {})
            created = post.get("created_utc", 0)

            if created < cutoff:
                continue

            title = post.get("title", "")
            selftext = post.get("selftext", "")
            url = post.get("url", "")
            permalink = f"https://www.reddit.com{post.get('permalink', '')}"
            post_id = post.get("id", "")
            score = post.get("score", 0)
            full_text = f"{title} {selftext}"

            # Skip low-quality posts
            if score < 0:
                continue

            dedup_base = f"reddit:{subreddit}:{post_id}"

            # --- Check 1: Specific product match ---
            matched_product = check_product_match(full_text, products)
            if matched_product:
                dedup_key = f"{dedup_base}:product:{matched_product['name']}"
                if not already_alerted(dedup_key):
                    emoji = CATEGORY_EMOJI.get(matched_product["category"], "🛒")
                    alert_text = (
                        f"{emoji} <b>Reddit Deal — Product Match!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📢 {title[:200]}\n"
                        f"🏷️ Matched: {matched_product['name']}\n"
                        f"📍 r/{subreddit} (⬆️ {score} upvotes)\n\n"
                        f"{selftext[:300]}\n\n"
                        f"🔗 <a href='{permalink}'>Open on Reddit</a>"
                    )
                    send_alert(alert_text)
                    mark_alerted(dedup_key, matched_product["name"], 0)
                    total_alerts += 1

            # --- Check 2: Food platform mention + deal signal ---
            food_platform = detect_food_platform(full_text)
            if food_platform and has_deal_signal(full_text, signal_words):
                dedup_key = f"{dedup_base}:food:{food_platform['name']}"
                if not already_alerted(dedup_key):
                    alert_text = (
                        f"{food_platform['emoji']} <b>Reddit — {food_platform['name']} Deal!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📢 {title[:200]}\n"
                        f"📍 r/{subreddit} (⬆️ {score} upvotes)\n\n"
                        f"{selftext[:300]}\n\n"
                        f"✅ <b>Pro tip:</b> Stack with bank card offer for max savings\n"
                        f"🔗 <a href='{permalink}'>Open on Reddit</a>"
                    )
                    send_alert(alert_text)
                    mark_alerted(dedup_key, food_platform["name"], 0)
                    total_alerts += 1

            # --- Check 3: Category match + deal signal ---
            if has_deal_signal(full_text, signal_words):
                matched_cats = get_matching_categories(full_text, category_keywords)
                for cat in matched_cats:
                    # Skip food if already handled as food platform above
                    if cat == "food" and food_platform:
                        continue
                    dedup_key = f"{dedup_base}:cat:{cat}"
                    if not already_alerted(dedup_key):
                        emoji = CATEGORY_EMOJI.get(cat, "🛒")
                        alert_text = (
                            f"{emoji} <b>Reddit Deal Signal — {cat.title()}</b>\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📢 {title[:200]}\n"
                            f"📍 r/{subreddit} (⬆️ {score} upvotes)\n\n"
                            f"{selftext[:300]}\n\n"
                            f"🔗 <a href='{permalink}'>Open on Reddit</a>"
                        )
                        send_alert(alert_text)
                        mark_alerted(dedup_key, cat, 0)
                        total_alerts += 1

        # Be nice to Reddit — small delay between subreddits
        time.sleep(2)

    print(f"[reddit] Done. Sent {total_alerts} alerts from {len(subreddits)} subreddits.")


if __name__ == "__main__":
    run()
