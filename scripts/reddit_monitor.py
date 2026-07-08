"""
reddit_monitor.py — Deal Scout v2.

Monitors Indian deal subreddits for deals and coupons using Reddit's public
JSON API (no auth required for read-only public access).

v2 upgrades:
  • Subreddits filtered/sorted by reliability_score (from watchlist).
  • matcher.py used for deal extraction (price, discount%, coupon).
  • Priority scoring — posts below min_priority_score_to_alert are dropped.
  • Cross-channel fuzzy dedup via db.get_recent_deal_titles().
  • send_deal_alert() for richer, badge-labelled messages.
  • Browser User-Agent to avoid Reddit 403 blocks.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from db import already_alerted, mark_alerted, get_recent_deal_titles, record_deal_title
from notifier import send_deal_alert
from matcher import (
    extract_deal_info, score_deal,
    detect_food_platforms, deduplicate_title,
    CATEGORY_EMOJI,
)

load_dotenv()

WATCHLIST_PATH   = Path(__file__).resolve().parent.parent / "watchlist.json"
LOOKBACK_SECONDS = 3600  # 1 hour

# Browser User-Agent to avoid Reddit 403
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def fetch_subreddit_posts(subreddit: str, limit: int = 25) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("children", [])
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


def check_product_match(text: str, products: list) -> tuple[dict | None, int]:
    """Returns (matched_product, match_count) or (None, 0)."""
    text_lower = text.lower()
    for product in products:
        matches = [kw for kw in product["keywords"] if kw.lower() in text_lower]
        if matches:
            return product, len(matches)
    return None, 0


def get_matching_categories(text: str, category_keywords: dict) -> list[str]:
    text_lower = text.lower()
    return [
        cat for cat, kws in category_keywords.items()
        if any(kw.lower() in text_lower for kw in kws)
    ]


def run():
    watchlist       = load_watchlist()
    subreddits_raw  = watchlist.get("reddit_subreddits", [])
    products        = watchlist.get("products", [])
    category_kws    = watchlist.get("category_keywords", {})
    signal_words    = watchlist.get("deal_signal_words", [])
    rules           = watchlist.get("matching_rules", {})
    min_reliability = rules.get("min_reliability_score", 5)
    min_score       = rules.get("min_priority_score_to_alert", 40)
    dedup_hours     = rules.get("dedup_window_hours", 6)
    weights         = rules.get("priority_weights", None)

    # Support both old string-list and new object format
    subreddits = []
    for s in subreddits_raw:
        if isinstance(s, str):
            subreddits.append({"name": s, "reliability_score": 6})
        else:
            subreddits.append(s)

    # Filter by min reliability, sort best-first
    subreddits = sorted(
        [s for s in subreddits if s.get("reliability_score", 5) >= min_reliability],
        key=lambda s: s.get("reliability_score", 5),
        reverse=True,
    )

    if not subreddits:
        print("[reddit] No subreddits configured. Skipping.")
        return

    cutoff        = datetime.now(timezone.utc).timestamp() - LOOKBACK_SECONDS
    total_alerts  = 0
    recent_titles = get_recent_deal_titles(hours=dedup_hours)

    def _has_signal(text: str) -> bool:
        lower = text.lower()
        return any(s in lower for s in signal_words)

    for sub in subreddits:
        sub_name    = sub["name"] if isinstance(sub, dict) else sub
        reliability = sub.get("reliability_score", 6) if isinstance(sub, dict) else 6

        print(f"[reddit] Scanning r/{sub_name}...")
        posts = fetch_subreddit_posts(sub_name)

        for post_wrapper in posts:
            post    = post_wrapper.get("data", {})
            created = post.get("created_utc", 0)

            if created < cutoff:
                continue

            title     = post.get("title", "")
            selftext  = post.get("selftext", "")
            url       = post.get("url", "")
            permalink = f"https://www.reddit.com{post.get('permalink', '')}"
            post_id   = post.get("id", "")
            score     = post.get("score", 0)
            full_text = f"{title} {selftext}"

            if score < 0:
                continue

            dedup_base = f"reddit:{sub_name}:{post_id}"
            info       = extract_deal_info(full_text)
            disc_pct   = info["discount_pct"]
            price      = info["price"]
            coupon     = info["coupon_code"] or ""

            # ── Check 1: Product match ─────────────────────────────────────
            matched_product, match_count = check_product_match(full_text, products)
            if matched_product:
                dedup_key = f"{dedup_base}:product:{matched_product['name']}"
                if not already_alerted(dedup_key):
                    deal_title = f"{matched_product['name']} reddit {sub_name}"
                    if not deduplicate_title(deal_title, recent_titles):
                        p_score = score_deal(
                            discount_pct=disc_pct,
                            source_reliability=reliability,
                            keyword_match_count=match_count,
                            target_price=matched_product.get("target_price"),
                            detected_price=price,
                            weights=weights,
                        )
                        if p_score >= min_score:
                            emoji = CATEGORY_EMOJI.get(matched_product.get("category", ""), "🛒")
                            sent = send_deal_alert(
                                title=f"{emoji} Reddit Deal — {matched_product['name']}",
                                body=f"📢 {title[:200]}\n\n{selftext[:300]}",
                                channel=f"r/{sub_name} (⬆️ {score} upvotes)",
                                category=matched_product.get("category", ""),
                                coupon_code=coupon,
                                price=price,
                                discount_pct=disc_pct,
                                priority_score=p_score,
                                product_url=permalink,
                                action_steps=["Open on Reddit", "Verify deal is still live"],
                            )
                            if sent:
                                mark_alerted(dedup_key, matched_product["name"], price or 0,
                                             priority_score=p_score, discount_percent=disc_pct or 0,
                                             source_reliability=reliability,
                                             category=matched_product.get("category", ""))
                                record_deal_title(deal_title, f"reddit:{sub_name}")
                                recent_titles.append(deal_title)
                                total_alerts += 1

            # ── Check 2: Food platform + deal signal ───────────────────────
            if _has_signal(full_text):
                food_platforms = detect_food_platforms(full_text)
                for fp in food_platforms:
                    dedup_key = f"{dedup_base}:food:{fp['name']}"
                    if not already_alerted(dedup_key):
                        deal_title = f"{fp['name']} reddit {sub_name} {post_id}"
                        if not deduplicate_title(deal_title, recent_titles):
                            p_score = score_deal(
                                discount_pct=disc_pct,
                                source_reliability=reliability,
                                keyword_match_count=2,
                                target_price=None,
                                detected_price=None,
                                weights=weights,
                            )
                            if p_score >= min_score:
                                sent = send_deal_alert(
                                    title=f"{fp['emoji']} Reddit — {fp['name']} Deal",
                                    body=f"📢 {title[:200]}\n\n{selftext[:300]}",
                                    channel=f"r/{sub_name} (⬆️ {score} upvotes)",
                                    category="food",
                                    coupon_code=coupon,
                                    price=price,
                                    discount_pct=disc_pct,
                                    priority_score=p_score,
                                    product_url=permalink,
                                    action_steps=["Stack with bank card offer for max savings"],
                                )
                                if sent:
                                    mark_alerted(dedup_key, fp["name"], price or 0,
                                                 priority_score=p_score, discount_percent=disc_pct or 0,
                                                 source_reliability=reliability, category="food")
                                    record_deal_title(deal_title, f"reddit:{sub_name}")
                                    recent_titles.append(deal_title)
                                    total_alerts += 1

            # ── Check 3: Category match + deal signal ──────────────────────
            if _has_signal(full_text):
                matched_cats = get_matching_categories(full_text, category_kws)
                food_names   = {fp["name"] for fp in detect_food_platforms(full_text)}
                for cat in matched_cats:
                    if cat == "food" and food_names:
                        continue  # already handled above
                    dedup_key = f"{dedup_base}:cat:{cat}"
                    if not already_alerted(dedup_key):
                        deal_title = f"{cat} reddit {sub_name} {post_id}"
                        if not deduplicate_title(deal_title, recent_titles):
                            p_score = score_deal(
                                discount_pct=disc_pct,
                                source_reliability=reliability,
                                keyword_match_count=1,
                                target_price=None,
                                detected_price=None,
                                weights=weights,
                            )
                            if p_score >= min_score:
                                emoji = CATEGORY_EMOJI.get(cat, "🛒")
                                sent = send_deal_alert(
                                    title=f"{emoji} Reddit Deal — {cat.replace('_', ' ').title()}",
                                    body=f"📢 {title[:200]}\n\n{selftext[:300]}",
                                    channel=f"r/{sub_name} (⬆️ {score} upvotes)",
                                    category=cat,
                                    coupon_code=coupon,
                                    price=price,
                                    discount_pct=disc_pct,
                                    priority_score=p_score,
                                    product_url=permalink,
                                )
                                if sent:
                                    mark_alerted(dedup_key, cat, price or 0,
                                                 priority_score=p_score, discount_percent=disc_pct or 0,
                                                 source_reliability=reliability, category=cat)
                                    record_deal_title(deal_title, f"reddit:{sub_name}")
                                    recent_titles.append(deal_title)
                                    total_alerts += 1

        time.sleep(2)  # Be polite to Reddit

    print(f"[reddit] Done. Sent {total_alerts} alerts from {len(subreddits)} subreddits.")


if __name__ == "__main__":
    run()
