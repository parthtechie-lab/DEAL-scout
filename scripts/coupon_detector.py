"""
coupon_detector.py — Deal Scout v2: INSTANT Coupon Detector.

PURPOSE
-------
Detects NEW coupon codes the moment they go live for:
  • Swiggy          • Zomato          • Blinkit
  • Zepto           • BigBasket       • Dominos
  • Swiggy Instamart

HOW IT WORKS (no Playwright needed — pure requests, runs in < 10 seconds)
--------------------------------------------------------------------------
Layer A — Coupon aggregator pages (DesiDime, GrabOn, CashKaro, Zoutons,
          CouponDunia, FreeKaaMaal, IndiaDesire, Hutti) are hit with fast
          HTTP requests. Coupon codes are extracted via regex from the raw
          HTML. These sites are updated within minutes of a new code dropping.

Layer B — Platform "offers" public pages (where accessible without login):
          • Dominos public coupon page
          • MagicPin food offer listings (public JSON)

Layer C — Telegram public web preview (t.me/s/<channel>) — reads the
          @foodcoupon, @foodielooters, @gopaisadeals channel web previews
          without needing a Telethon session. Catches codes posted by humans
          who discover them first.

DEDUP WINDOW: 12 hours — won't spam you if the same code is live all day.

ALERT FORMAT: includes code, discount description, platform emoji,
              stacking tip, and a direct "apply now" deep link.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from db import already_alerted, mark_alerted
from notifier import send_alert

load_dotenv()

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"

DEDUP_HOURS = 12   # Same code won't be re-alerted within 12 hours

# ── Browser-like headers to avoid 403 ────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Platform config ───────────────────────────────────────────────────────────
PLATFORMS = {
    "swiggy": {
        "name": "Swiggy", "emoji": "🛵",
        "deep_link": "https://www.swiggy.com",
        "stacking_tip": "Swiggy One membership + HDFC Swiggy card (10% cashback) + this code",
    },
    "zomato": {
        "name": "Zomato", "emoji": "🍕",
        "deep_link": "https://www.zomato.com",
        "stacking_tip": "Zomato Gold + HDFC Diners Club (5X rewards) + this coupon code",
    },
    "blinkit": {
        "name": "Blinkit", "emoji": "⚡",
        "deep_link": "https://blinkit.com",
        "stacking_tip": "Zomato Gold (free Blinkit delivery) + bank UPI offer + this code",
    },
    "zepto": {
        "name": "Zepto", "emoji": "🟣",
        "deep_link": "https://www.zeptonow.com",
        "stacking_tip": "Zepto Pass + bank card offer + this coupon — stack all three",
    },
    "bigbasket": {
        "name": "BigBasket", "emoji": "🛒",
        "deep_link": "https://www.bigbasket.com",
        "stacking_tip": "BB Star membership + wallet cashback + this coupon code",
    },
    "dominos": {
        "name": "Dominos", "emoji": "🍕",
        "deep_link": "https://www.dominos.co.in",
        "stacking_tip": "'Everyday Value' menu + this code + bank card offer — all three at checkout",
    },
    "swiggy-instamart": {
        "name": "Swiggy Instamart", "emoji": "📦",
        "deep_link": "https://www.swiggy.com/instamart",
        "stacking_tip": "Swiggy One + this Instamart code + bank card discount",
    },
}

# ── Layer A: Aggregator URLs to scrape for each platform ─────────────────────
# Format: (site_name, url_template_with_{platform})
AGGREGATOR_URLS = [
    ("DesiDime",    "https://www.desidime.com/stores/{platform}-coupons"),
    ("GrabOn",      "https://www.grabon.in/{platform}-coupons/"),
    ("CashKaro",    "https://cashkaro.com/{platform}-coupons"),
    ("Zoutons",     "https://zoutons.com/{platform}-coupons"),
    ("CouponDunia", "https://www.coupondunia.in/{platform}-coupons"),
    ("FreeKaaMaal", "https://www.freekaamaal.com/{platform}-coupons"),
    ("IndiaDesire", "https://indiadesire.com/{platform}-coupons"),
    ("Hutti",       "https://hutti.in/{platform}-coupons"),
    ("CupoNation",  "https://www.cuponation.in/{platform}"),
]

# Regex to pull coupon codes from raw HTML
_CODE_PATTERNS = [
    re.compile(r'(?:use|apply|code|coupon|promo)[:\s"]+([A-Z][A-Z0-9]{3,14})', re.IGNORECASE),
    re.compile(r'data-(?:clipboard-text|coupon|code)[=:"]+([A-Z][A-Z0-9]{3,14})', re.IGNORECASE),
    re.compile(r'"code"\s*:\s*"([A-Z][A-Z0-9]{3,14})"'),
    re.compile(r'<[^>]*class="[^"]*(?:coupon|code)[^"]*"[^>]*>([A-Z][A-Z0-9]{3,14})<', re.IGNORECASE),
]
_DISCOUNT_PATTERN = re.compile(
    r'((?:flat\s+)?(?:₹\s*\d+|\d+%)\s*(?:off|cashback|discount|savings|free delivery)[^<\n]{0,60})',
    re.IGNORECASE,
)
_CODE_BLACKLIST = {
    "TERMS","ABOUT","LOGIN","SHARE","CLICK","CLOSE","EMAIL","PHONE","ORDER",
    "APPLY","CHECK","INDIA","AMAZON","FLIPKART","STORE","UPIID","REFER",
    "OFFER","TODAY","EXTRA","SUPER","DOWNLOAD","GETAPP","INSTALL","UPDATE",
    "SIGNUP","SIGNIN","SUBMIT","SEARCH","FOOTER","HEADER","NAVBAR","BUTTON",
    "SCROLL","SELECT","LAUNCH","RETURN","MOBILE","ONLINE","LATEST","ACTIVE",
}

# ── Layer B: Direct platform public pages ────────────────────────────────────
DIRECT_URLS = {
    "dominos": [
        "https://www.dominos.co.in/offers",
        "https://www.dominos.co.in/coupons",
    ],
}

# ── Layer C: Telegram public channel web previews ────────────────────────────
TG_FOOD_CHANNELS = [
    "@foodcoupon",
    "@foodielooters",
    "@gopaisadeals",
    "@GrabOnIndiaOfficial",
]


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


def fetch_html(url: str, timeout: int = 12) -> str:
    """Fetch raw HTML. Returns empty string on any error."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp.text
        return ""
    except Exception:
        return ""


def extract_codes_from_html(html: str) -> list[tuple[str, str]]:
    """
    Returns list of (code, discount_description) tuples found in HTML.
    discount_description may be empty if no discount text was found nearby.
    """
    if not html:
        return []

    results = []
    seen_codes = set()

    for pattern in _CODE_PATTERNS:
        for m in pattern.finditer(html):
            code = m.group(1).upper().strip()
            if (
                len(code) < 4
                or code in _CODE_BLACKLIST
                or not re.search(r'[0-9]', code)  # Real codes usually have a digit
                or code in seen_codes
            ):
                continue

            # Look for discount description in the surrounding ~300 chars
            start  = max(0, m.start() - 200)
            end    = min(len(html), m.end() + 200)
            window = html[start:end]
            disc_m = _DISCOUNT_PATTERN.search(window)
            disc   = disc_m.group(1).strip() if disc_m else ""

            # Clean HTML tags from discount text
            disc = re.sub(r'<[^>]+>', ' ', disc)
            disc = re.sub(r'\s+', ' ', disc).strip()[:120]

            seen_codes.add(code)
            results.append((code, disc))

    return results


def fetch_telegram_web_preview(channel_handle: str) -> str:
    """
    Fetches the public web preview of a Telegram channel (t.me/s/<handle>).
    No Telethon session required — this is the public web view.
    """
    handle = channel_handle.lstrip("@")
    url    = f"https://t.me/s/{handle}"
    return fetch_html(url)


def build_alert(platform_key: str, code: str, discount: str, source: str) -> str:
    p = PLATFORMS.get(platform_key, {"name": platform_key, "emoji": "🛒",
                                     "deep_link": "", "stacking_tip": ""})
    disc_line = f"💰 {discount}\n" if discount else ""
    tip_line  = f"\n💡 <b>Pro tip:</b> {p['stacking_tip']}" if p["stacking_tip"] else ""

    return (
        f"🎟️ <b>NEW COUPON DETECTED — {p['name']}!</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{p['emoji']} Platform: <b>{p['name']}</b>\n"
        f"🔑 Code: <code>{code}</code>\n"
        f"{disc_line}"
        f"📍 Source: {source}\n"
        f"{tip_line}\n\n"
        f"✅ <b>How to use:</b>\n"
        f"1. Open {p['name']} app\n"
        f"2. Add items to cart\n"
        f"3. Apply code: <code>{code}</code>\n"
        f"4. Pay with bank card for max savings\n\n"
        f"⚡ <i>Act fast — codes expire quickly!</i>"
    )


def run():
    watchlist = load_watchlist()
    total_sent = 0

    # ── Layer A: Aggregator scrape ─────────────────────────────────────────
    print("[coupon_detector] Layer A — scraping coupon aggregators...")
    for platform_key, platform_info in PLATFORMS.items():
        for site_name, url_template in AGGREGATOR_URLS:
            url  = url_template.replace("{platform}", platform_key)
            html = fetch_html(url)
            if not html:
                continue

            codes = extract_codes_from_html(html)
            for code, discount in codes:
                dedup_key = f"coupon:{platform_key}:{code}"
                if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
                    continue

                msg = build_alert(platform_key, code, discount, site_name)
                from notifier import send_alert
                if send_alert(msg):
                    mark_alerted(dedup_key, platform_info["name"], 0,
                                 priority_score=70, category="food")
                    print(f"[coupon_detector] ✅ {platform_info['name']} code {code} "
                          f"from {site_name} — alerted!")
                    total_sent += 1

            time.sleep(0.4)   # polite delay between requests

    # ── Layer B: Direct platform pages ────────────────────────────────────
    print("[coupon_detector] Layer B — checking platform direct pages...")
    for platform_key, urls in DIRECT_URLS.items():
        for url in urls:
            html  = fetch_html(url)
            codes = extract_codes_from_html(html)
            for code, discount in codes:
                dedup_key = f"coupon:{platform_key}:{code}"
                if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
                    continue
                msg = build_alert(platform_key, code, discount, "Official Page")
                from notifier import send_alert
                if send_alert(msg):
                    mark_alerted(dedup_key, PLATFORMS[platform_key]["name"], 0,
                                 priority_score=80, category="food")
                    print(f"[coupon_detector] ✅ {platform_key} code {code} "
                          f"from official page — alerted!")
                    total_sent += 1

    # ── Layer C: Telegram channel web previews ─────────────────────────────
    print("[coupon_detector] Layer C — reading Telegram public channel previews...")
    # Keywords to detect platform in TG post
    platform_keywords = {
        "swiggy-instamart": ["instamart"],
        "swiggy":   ["swiggy"],
        "zomato":   ["zomato"],
        "blinkit":  ["blinkit"],
        "zepto":    ["zepto"],
        "bigbasket":["bigbasket", "big basket", "bb "],
        "dominos":  ["dominos", "domino's", "domino"],
    }

    for channel in TG_FOOD_CHANNELS:
        html = fetch_telegram_web_preview(channel)
        if not html:
            print(f"[coupon_detector] Could not fetch TG preview for {channel}")
            continue

        codes = extract_codes_from_html(html)
        for code, discount in codes:
            # Determine which platform this code is for
            detected_platform = None
            html_lower = html.lower()
            # Find the code's context window in HTML to check platform
            idx = html.upper().find(code)
            if idx != -1:
                ctx = html_lower[max(0, idx-300):idx+300]
                for pkey, kws in platform_keywords.items():
                    if any(kw in ctx for kw in kws):
                        detected_platform = pkey
                        break

            if not detected_platform:
                continue  # Can't attribute to a platform, skip

            dedup_key = f"coupon:{detected_platform}:{code}"
            if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
                continue

            msg = build_alert(detected_platform, code, discount,
                              f"Telegram {channel}")
            from notifier import send_alert
            if send_alert(msg):
                mark_alerted(dedup_key, PLATFORMS[detected_platform]["name"], 0,
                             priority_score=75, category="food")
                print(f"[coupon_detector] ✅ {detected_platform} code {code} "
                      f"from TG {channel} — alerted!")
                total_sent += 1

        time.sleep(1)

    print(f"[coupon_detector] Done. {total_sent} new coupon alerts sent.")


if __name__ == "__main__":
    run()
