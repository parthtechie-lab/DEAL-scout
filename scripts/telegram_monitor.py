"""
telegram_monitor.py — Deal Scout v2.

Reads recent messages from deal channels and matches them against:
  Layer 1 — specific product keywords  (high confidence, always alert if score ≥ 40)
  Layer 2 — food platform detection    (Swiggy / Zomato / Blinkit / etc + deal signal)
  Layer 3 — category keywords          (13 categories + deal signal words)

v2 upgrades:
  • Channels sorted by reliability_score DESC (best sources processed first).
  • Channels with reliability_score < matching_rules.min_reliability_score skipped.
  • matcher.py used for deal extraction (price, discount%, coupon, expiry).
  • Priority score computed; deals scoring < min_priority_score_to_alert dropped.
  • Cross-channel fuzzy dedup via db.get_recent_deal_titles() + matcher.deduplicate_title().
  • send_deal_alert() used for richer, consistent message format.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from db import already_alerted, mark_alerted, get_recent_deal_titles, record_deal_title, record_source_match, record_source_miss
from notifier import send_deal_alert, send_alert
from matcher import (
    extract_deal_info, score_deal, priority_badge,
    detect_food_platforms, deduplicate_title,
    load_weights, CATEGORY_EMOJI,
)

load_dotenv()

API_ID         = os.getenv("TELEGRAM_API_ID")
API_HASH       = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("TELEGRAM_SESSION")

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"
LOOKBACK_MINUTES = 45   # 30-min cron + 15-min margin


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def has_deal_signal(text: str, signal_words: list) -> bool:
    lower = text.lower()
    return any(s in lower for s in signal_words)


def message_matches_product(text: str, keywords: list[str]) -> tuple[bool, int]:
    """Returns (matched, count_of_keywords_matched)."""
    text_lower = text.lower()
    matched = [kw for kw in keywords if kw.lower() in text_lower]
    return bool(matched), len(matched)


def get_matching_categories(text: str, category_keywords: dict) -> list[str]:
    text_lower = text.lower()
    return [
        cat for cat, kws in category_keywords.items()
        if any(kw.lower() in text_lower for kw in kws)
    ]


async def scan_channels():
    watchlist       = load_watchlist()
    channels_raw    = watchlist.get("telegram_channels", [])
    products        = watchlist.get("products", [])
    category_kws    = watchlist.get("category_keywords", {})
    signal_words    = watchlist.get("deal_signal_words", [])
    rules           = watchlist.get("matching_rules", {})
    min_reliability = rules.get("min_reliability_score", 5)
    min_score       = rules.get("min_priority_score_to_alert", 40)
    dedup_hours     = rules.get("dedup_window_hours", 6)
    weights         = load_weights(watchlist)   # always from watchlist, not hardcoded

    # Support both old string-list format and new object format
    channels = []
    for ch in channels_raw:
        if isinstance(ch, str):
            channels.append({"handle": ch, "reliability_score": 7, "name": ch})
        else:
            channels.append(ch)

    # Sort by reliability DESC, filter out weak channels
    channels = sorted(
        [c for c in channels if c.get("reliability_score", 5) >= min_reliability],
        key=lambda c: c.get("reliability_score", 5),
        reverse=True,
    )

    if not os.getenv("TELEGRAM_API_ID") or not os.getenv("TELEGRAM_SESSION"):
        raise ValueError("Missing API credentials — run generate_session.py first.")

    client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)
    await client.start()

    cutoff       = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    total_alerts = 0
    channels_ok  = 0
    channels_fail = 0

    # Pre-load recent titles for cross-channel dedup
    recent_titles = get_recent_deal_titles(hours=dedup_hours)

    for ch in channels:
        handle      = ch["handle"] if isinstance(ch, dict) else ch
        reliability = ch.get("reliability_score", 7) if isinstance(ch, dict) else 7

        if handle.startswith("@replace_with"):
            continue

        try:
            msg_count = 0
            async for message in client.iter_messages(handle, limit=200):
                if message.date < cutoff:
                    break
                if not message.text:
                    continue

                msg_count += 1
                text     = message.text
                dedup_base = f"tg:{handle}:{message.id}"

                # Extract structured info once per message
                info     = extract_deal_info(text)
                price    = info["price"]
                disc_pct = info["discount_pct"]
                coupon   = info["coupon_code"] or ""

                # ── Layer 1: Product keyword match ─────────────────────────
                for product in products:
                    matched, match_count = message_matches_product(text, product["keywords"])
                    if not matched:
                        continue

                    dedup_key = f"{dedup_base}:product:{product['name']}"
                    if already_alerted(dedup_key):
                        continue

                    target_price = product.get("target_price")
                    p_score = score_deal(
                        discount_pct=disc_pct,
                        source_reliability=reliability,
                        keyword_match_count=match_count,
                        target_price=target_price,
                        detected_price=price,
                        weights=weights,
                    )
                    if p_score < min_score:
                        continue

                    # Code-based dedup: same coupon code from any source
                    if coupon and already_alerted(f"code:product:{product['name']}:{coupon}"):
                        continue

                    # Cross-channel fuzzy title dedup
                    deal_title = f"{product['name']} {handle}"
                    if deduplicate_title(deal_title, recent_titles):
                        continue

                    emoji = CATEGORY_EMOJI.get(product.get("category", ""), "🛒")
                    sent = send_deal_alert(
                        title=f"{emoji} Product Deal — {product['name']}",
                        body=text,
                        channel=handle,
                        category=product.get("category", ""),
                        coupon_code=coupon,
                        price=price,
                        discount_pct=disc_pct,
                        priority_score=p_score,
                        product_url=product.get("url", ""),
                        action_steps=[
                            "Activate cashback on CashKaro/GoPaisa first",
                            "Check BuyHatke extension for price history",
                            "Stack with bank card offer for extra savings",
                        ],
                    )
                    if sent:
                        mark_alerted(dedup_key, product["name"], price or 0,
                                     priority_score=p_score, discount_percent=disc_pct or 0,
                                     source_reliability=reliability,
                                     category=product.get("category", ""))
                        if coupon:
                            mark_alerted(f"code:product:{product['name']}:{coupon}",
                                         product["name"], price or 0)
                        record_deal_title(deal_title, handle)
                        recent_titles.append(deal_title)
                        total_alerts += 1

                # ── Layer 2: Food platform + deal signal ──────────────────
                if has_deal_signal(text, signal_words):
                    food_platforms = detect_food_platforms(text)
                    for fp in food_platforms:
                        dedup_key = f"{dedup_base}:food:{fp['name']}"
                        if already_alerted(dedup_key):
                            continue

                        deal_title = f"{fp['name']} deal {handle}"
                        if deduplicate_title(deal_title, recent_titles):
                            continue

                        p_score = score_deal(
                            discount_pct=disc_pct,
                            source_reliability=reliability,
                            keyword_match_count=2,
                            target_price=None,
                            detected_price=None,
                            weights=weights,
                        )
                        if p_score < min_score:
                            continue

                        sent = send_deal_alert(
                            title=f"{fp['emoji']} {fp['name']} Deal",
                            body=text,
                            channel=handle,
                            category="food",
                            coupon_code=coupon,
                            price=price,
                            discount_pct=disc_pct,
                            priority_score=p_score,
                            action_steps=[
                                f"Open {fp['name']} app",
                                f"Apply code {coupon}" if coupon else "Check offers tab",
                                "Pay with bank card for max savings",
                            ],
                        )
                        if sent:
                            mark_alerted(dedup_key, fp["name"], price or 0,
                                         priority_score=p_score, discount_percent=disc_pct or 0,
                                         source_reliability=reliability, category="food")
                            record_deal_title(deal_title, handle)
                            recent_titles.append(deal_title)
                            total_alerts += 1

                # ── Layer 3: Category keyword + deal signal ───────────────
                if has_deal_signal(text, signal_words):
                    matched_cats = get_matching_categories(text, category_kws)
                    for cat in matched_cats:
                        dedup_key = f"{dedup_base}:cat:{cat}"
                        if already_alerted(dedup_key):
                            continue

                        deal_title = f"{cat} deal {handle} {message.id}"
                        if deduplicate_title(deal_title, recent_titles):
                            continue

                        p_score = score_deal(
                            discount_pct=disc_pct,
                            source_reliability=reliability,
                            keyword_match_count=1,
                            target_price=None,
                            detected_price=None,
                            weights=weights,
                        )
                        if p_score < min_score:
                            continue

                        emoji = CATEGORY_EMOJI.get(cat, "🛒")
                        sent = send_deal_alert(
                            title=f"{emoji} Category Deal — {cat.replace('_', ' ').title()}",
                            body=text,
                            channel=handle,
                            category=cat,
                            coupon_code=coupon,
                            price=price,
                            discount_pct=disc_pct,
                            priority_score=p_score,
                            action_steps=["Verify this matches your wishlist before acting."],
                        )
                        if sent:
                            mark_alerted(dedup_key, cat, price or 0,
                                         priority_score=p_score, discount_percent=disc_pct or 0,
                                         source_reliability=reliability, category=cat)
                            record_deal_title(deal_title, handle)
                            recent_titles.append(deal_title)
                            total_alerts += 1

            channels_ok += 1
            if msg_count > 0:
                # Only call match/miss if we actually got messages from the channel
                if total_alerts > 0:
                    record_source_match(handle, source_type="telegram")
                else:
                    record_source_miss(handle, source_type="telegram")
            print(f"[telegram_monitor] {handle}: scanned {msg_count} messages")

        except Exception as e:
            channels_fail += 1
            print(f"[telegram_monitor] Error reading {handle}: {e}")

    await client.disconnect()
    print(
        f"[telegram_monitor] Done. {channels_ok} channels OK, "
        f"{channels_fail} failed, {total_alerts} alerts sent."
    )


if __name__ == "__main__":
    asyncio.run(scan_channels())
