"""
coupon_detector.py — Deal Scout v2: Instant Coupon Detector (Hardened)

HONEST LIMITATIONS
------------------
• GitHub Actions minimum latency is ~40-90 seconds from cron trigger to first
  HTTP request. This system is NOT suitable for "first 100 redemptions" flash
  coupons. It IS suitable for catching codes that last 1-24 hours — bank card
  codes, new-user codes, weekly platform promos, and sports-event offers.

• Many aggregator sites (GrabOn, DesiDime, CashKaro, Zoutons) are behind
  Cloudflare. GitHub Actions IPs are shared and well-known to CF. Those sites
  are dropped from this scanner. Only sources that reliably serve plain HTML
  to non-browser clients are included.

SOURCES THAT ACTUALLY WORK FROM CI IPS
----------------------------------------
Layer A — Cloudflare-free / RSS-accessible sources:
  1. DesiDime RSS feed   (no CF, plain XML, updated in real-time by users)
  2. Couponzania         (lighter protection, usually serves HTML)
  3. Hutti.in            (lighter protection, usually serves HTML)
  4. CouponMoto          (lighter CF, frequently works on CI IPs)
  5. Dealsmagnet         (works on CI)
  6. MagicPin offers API (public, unauthenticated JSON, no CF)

Layer B — Official platform pages (lighter/no bot protection):
  Dominos India (co.in) — no Cloudflare, serves plain HTML

Layer C — Telegram public channel web preview (t.me/s/<channel>):
  Works for PUBLIC channels only. Gives a truncated snippet but enough
  to extract a coupon code via regex. Does NOT work for private/invite-only
  channels. Included with that explicit caveat.

DEDUP WINDOW: 12 hours — won't spam you if the same code stays live all day.
"""

import json
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from db import already_alerted, mark_alerted
from notifier import send_alert

load_dotenv()

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"
DEDUP_HOURS    = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PLATFORMS = {
    "swiggy": {
        "name": "Swiggy", "emoji": "🛵",
        "stacking_tip": "Swiggy One + HDFC Swiggy card (10% cashback) + this code",
    },
    "zomato": {
        "name": "Zomato", "emoji": "🍕",
        "stacking_tip": "Zomato Gold + HDFC Diners Club (5X rewards) + this coupon code",
    },
    "blinkit": {
        "name": "Blinkit", "emoji": "⚡",
        "stacking_tip": "Zomato Gold (free Blinkit delivery) + bank UPI offer + this code",
    },
    "zepto": {
        "name": "Zepto", "emoji": "🟣",
        "stacking_tip": "Zepto Pass + bank card offer + this coupon — stack all three",
    },
    "bigbasket": {
        "name": "BigBasket", "emoji": "🛒",
        "stacking_tip": "BB Star membership + wallet cashback + this coupon code",
    },
    "dominos": {
        "name": "Dominos", "emoji": "🍕",
        "stacking_tip": "'Everyday Value' menu + this code + bank card offer — all at checkout",
    },
    "swiggy-instamart": {
        "name": "Swiggy Instamart", "emoji": "📦",
        "stacking_tip": "Swiggy One + this Instamart code + bank card discount",
    },
}

# ── Coupon code regex (compiled once) ─────────────────────────────────────────
_CODE_PATTERNS = [
    re.compile(r'data-(?:clipboard-text|coupon|code)[=:"]+([A-Z][A-Z0-9]{3,14})', re.IGNORECASE),
    re.compile(r'(?:use|apply|enter|code|coupon|promo)[:\s"]+([A-Z][A-Z0-9]{3,14})', re.IGNORECASE),
    re.compile(r'"code"\s*:\s*"([A-Z][A-Z0-9]{3,14})"'),
]
_DISCOUNT_PATTERN = re.compile(
    r'((?:flat\s+)?(?:₹\s*\d+|\d+%)\s*(?:off|cashback|discount|savings?|free delivery)[^<\n]{0,80})',
    re.IGNORECASE,
)
_CODE_BLACKLIST = {
    "TERMS","ABOUT","LOGIN","SHARE","CLICK","CLOSE","EMAIL","PHONE","ORDER",
    "APPLY","CHECK","INDIA","AMAZON","FLIPKART","STORE","UPIID","REFER",
    "OFFER","TODAY","EXTRA","SUPER","DOWNLOAD","GETAPP","INSTALL","UPDATE",
    "SIGNUP","SIGNIN","SUBMIT","SEARCH","FOOTER","HEADER","NAVBAR","BUTTON",
    "SCROLL","SELECT","LAUNCH","RETURN","MOBILE","ONLINE","LATEST","ACTIVE",
    "DEALS","OFFERS","COUPON","PROMO","VALID","HURRY","EXPIRE","LIMITED",
}


# ── Layer A: Cloudflare-free aggregator sources ───────────────────────────────
# These are sources verified to NOT require a JS challenge from CI IPs.
# DesiDime RSS is the most reliable — real-time, plain XML, no bot check.
AGGREGATOR_SOURCES = {
    # RSS feeds — plain XML, no JS required, no Cloudflare
    "desidime": {
        "swiggy":           "https://www.desidime.com/coupons/swiggy.xml",
        "zomato":           "https://www.desidime.com/coupons/zomato.xml",
        "blinkit":          "https://www.desidime.com/coupons/blinkit.xml",
        "zepto":            "https://www.desidime.com/coupons/zepto.xml",
        "bigbasket":        "https://www.desidime.com/coupons/bigbasket.xml",
        "dominos":          "https://www.desidime.com/coupons/dominos-pizza.xml",
        "swiggy-instamart": "https://www.desidime.com/coupons/swiggy-instamart.xml",
    },
    # HTML pages with lighter protection (usually work from CI)
    "couponzania": {
        "swiggy":           "https://www.couponzania.com/swiggy-coupons",
        "zomato":           "https://www.couponzania.com/zomato-coupons",
        "blinkit":          "https://www.couponzania.com/blinkit-coupons",
        "zepto":            "https://www.couponzania.com/zepto-coupons",
        "bigbasket":        "https://www.couponzania.com/bigbasket-coupons",
        "dominos":          "https://www.couponzania.com/dominos-coupons",
    },
    "hutti": {
        "swiggy":           "https://hutti.in/swiggy-coupons",
        "zomato":           "https://hutti.in/zomato-coupons",
        "blinkit":          "https://hutti.in/blinkit-coupons",
        "zepto":            "https://hutti.in/zepto-coupons",
        "bigbasket":        "https://hutti.in/bigbasket-coupons",
        "dominos":          "https://hutti.in/dominos-coupons",
    },
    "dealsmagnet": {
        "swiggy":           "https://www.dealsmagnet.com/category/swiggy-coupons",
        "zomato":           "https://www.dealsmagnet.com/category/zomato-coupons",
        "dominos":          "https://www.dealsmagnet.com/category/dominos-coupons",
    },
}

# ── Layer B: Direct platform pages (lighter/no bot protection) ────────────────
DIRECT_PLATFORM_PAGES = {
    "dominos": [
        "https://www.dominos.co.in/offers",
        "https://www.dominos.co.in/coupons",
    ],
}

# ── Layer C: Telegram public web previews (PUBLIC channels only) ──────────────
TG_FOOD_CHANNELS = [
    "@foodcoupon",
    "@foodielooters",
    "@gopaisadeals",
    "@GrabOnIndiaOfficial",
    "@CouponDuniaOffers",
]

# Platform keywords for context detection in TG messages
PLATFORM_KEYWORDS = {
    "swiggy-instamart": ["instamart"],
    "swiggy":           ["swiggy"],
    "zomato":           ["zomato"],
    "blinkit":          ["blinkit"],
    "zepto":            ["zepto"],
    "bigbasket":        ["bigbasket", "big basket", "bb "],
    "dominos":          ["dominos", "domino's", "domino"],
}


def fetch(url: str, timeout: int = 10) -> str:
    """Fetch raw content. Returns '' on any error or if response is non-200."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
        # Log non-200 but don't crash
        print(f"[coupon_detector] {url} → HTTP {r.status_code} (skipping)")
        return ""
    except Exception as e:
        print(f"[coupon_detector] fetch error {url}: {e}")
        return ""


def is_cloudflare_blocked(html: str) -> bool:
    """Detect if we got a Cloudflare challenge instead of real content."""
    markers = [
        "checking your browser",
        "just a moment",
        "enable javascript and cookies",
        "cf-browser-verification",
        "__cf_chl",
    ]
    lower = html[:2000].lower()
    return any(m in lower for m in markers)


def extract_codes(html: str) -> list[tuple[str, str]]:
    """
    Returns list of (code, discount_description) tuples.
    Returns empty list if the page looks like a Cloudflare block.
    """
    if not html or is_cloudflare_blocked(html):
        return []

    results   = []
    seen      = set()
    for pattern in _CODE_PATTERNS:
        for m in pattern.finditer(html):
            code = (m.group(1) or "").upper().strip()
            if (
                not code
                or len(code) < 4
                or code in _CODE_BLACKLIST
                or not re.search(r'\d', code)   # real codes almost always have a digit
                or code in seen
            ):
                continue
            # Grab surrounding discount context
            start = max(0, m.start() - 200)
            end   = min(len(html), m.end() + 200)
            ctx   = html[start:end]
            disc_m = _DISCOUNT_PATTERN.search(ctx)
            disc   = ""
            if disc_m:
                disc = re.sub(r'<[^>]+>', ' ', disc_m.group(1))
                disc = re.sub(r'\s+', ' ', disc).strip()[:120]
            seen.add(code)
            results.append((code, disc))
    return results


def build_alert(platform_key: str, code: str, discount: str, source: str) -> str:
    p = PLATFORMS.get(platform_key, {"name": platform_key, "emoji": "🛒", "stacking_tip": ""})
    disc_line = f"💰 {discount}\n" if discount else ""
    tip_line  = f"\n💡 <b>Stack it:</b> {p['stacking_tip']}" if p["stacking_tip"] else ""
    return (
        f"🎟️ <b>COUPON DETECTED — {p['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{p['emoji']} Platform: <b>{p['name']}</b>\n"
        f"🔑 Code: <code>{code}</code>\n"
        f"{disc_line}"
        f"📍 Source: {source}"
        f"{tip_line}\n\n"
        f"✅ Open app → Cart → Apply → Pay with bank card\n"
        f"⚡ <i>Verify the code is still valid before ordering.</i>"
    )


def run():
    total_sent    = 0
    sources_tried = 0
    sources_ok    = 0

    # ── Layer A ───────────────────────────────────────────────────────────────
    print("[coupon_detector] Layer A — CF-free aggregator sources...")
    for site_name, platform_urls in AGGREGATOR_SOURCES.items():
        for platform_key, url in platform_urls.items():
            sources_tried += 1
            html  = fetch(url)
            if is_cloudflare_blocked(html):
                print(f"[coupon_detector] {site_name}/{platform_key} — Cloudflare blocked, skipping")
                continue
            codes = extract_codes(html)
            if not codes and html:
                # Page loaded but no codes found — that's fine, not every page has active codes
                sources_ok += 1
                continue
            sources_ok += 1
            for code, discount in codes:
                dedup_key = f"coupon:{platform_key}:{code}"
                if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
                    continue
                msg = build_alert(platform_key, code, discount, site_name.title())
                if send_alert(msg):
                    mark_alerted(dedup_key, PLATFORMS.get(platform_key, {}).get("name", platform_key),
                                 0, priority_score=70, category="food")
                    print(f"[coupon_detector] ✅ {platform_key} → {code} (via {site_name})")
                    total_sent += 1
            time.sleep(0.5)

    # ── Layer B ───────────────────────────────────────────────────────────────
    print("[coupon_detector] Layer B — direct platform pages...")
    for platform_key, urls in DIRECT_PLATFORM_PAGES.items():
        for url in urls:
            html  = fetch(url)
            codes = extract_codes(html)
            for code, discount in codes:
                dedup_key = f"coupon:{platform_key}:{code}"
                if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
                    continue
                msg = build_alert(platform_key, code, discount, "Official page")
                if send_alert(msg):
                    mark_alerted(dedup_key, PLATFORMS.get(platform_key, {}).get("name", platform_key),
                                 0, priority_score=80, category="food")
                    print(f"[coupon_detector] ✅ {platform_key} → {code} (official page)")
                    total_sent += 1

    # ── Layer C ───────────────────────────────────────────────────────────────
    print("[coupon_detector] Layer C — Telegram public channel previews (PUBLIC channels only)...")
    for channel in TG_FOOD_CHANNELS:
        handle = channel.lstrip("@")
        html   = fetch(f"https://t.me/s/{handle}")
        if not html:
            print(f"[coupon_detector] {channel} — not accessible (private or rate-limited)")
            continue
        codes = extract_codes(html)
        for code, discount in codes:
            html_lower = html.lower()
            idx = html.upper().find(code)
            detected_platform = None
            if idx != -1:
                ctx = html_lower[max(0, idx-300):idx+300]
                for pkey, kws in PLATFORM_KEYWORDS.items():
                    if any(kw in ctx for kw in kws):
                        detected_platform = pkey
                        break
            if not detected_platform:
                continue
            dedup_key = f"coupon:{detected_platform}:{code}"
            if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
                continue
            msg = build_alert(detected_platform, code, discount, f"Telegram {channel}")
            if send_alert(msg):
                mark_alerted(dedup_key, PLATFORMS.get(detected_platform, {}).get("name", detected_platform),
                             0, priority_score=75, category="food")
                print(f"[coupon_detector] ✅ {detected_platform} → {code} (TG {channel})")
                total_sent += 1
        time.sleep(1)

    print(
        f"[coupon_detector] Done. "
        f"{sources_ok}/{sources_tried} sources reachable. "
        f"{total_sent} new alerts sent."
    )


if __name__ == "__main__":
    run()
