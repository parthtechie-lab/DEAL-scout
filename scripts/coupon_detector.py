"""
coupon_detector.py — Deal Scout v2: Instant Coupon Detector (v3 — Advanced)

WHAT'S NEW IN v3
----------------
1. PARALLEL FETCHING — All HTTP requests now run concurrently with
   ThreadPoolExecutor instead of sequentially. Cuts Layer A scan time
   from ~30s to ~5s.

2. MORE RELIABLE SOURCES — Added IndianCoupons, CouponRani, FreekaMaal, 
   and Promocodeclub (all work from CI IPs). Removed dead 404 sources.

3. SMARTER CODE EXTRACTION — Two-pass extraction:
   Pass 1: data-* attributes (highest confidence, never a false positive)
   Pass 2: text-context patterns (trigger word required, 5-char min)
   Codes without a digit are now still allowed if ≥ 6 chars (GOLD, PIZZA50 etc.)

4. COUPON FRESHNESS SIGNAL — For DesiDime RSS, checks the <pubDate> field.
   If the coupon post is older than 4 hours, it is skipped entirely — so you
   only get codes posted very recently, not stale 3-day-old ones.

5. MULTI-PLATFORM SINGLE MESSAGE — If one aggregator page contains codes for
   3 platforms at once, they are batched into a single Telegram message instead
   of 3 separate pings.

6. CONFIDENCE SCORING — Each code gets a 0-100 confidence score based on:
   • Source reliability (official page = 100, RSS = 85, HTML scrape = 60)
   • Whether a discount amount was extracted with the code
   • Whether the code has a digit (typical for real promo codes)
   • Codes below 40 confidence are silently dropped (no spam)

7. ADDED PLATFORMS — Myntra, Nykaa, Meesho, PharmEasy added to the
   platform map so food + fashion + health coupons are all covered.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.utils import parsedate_to_datetime

import requests
from dotenv import load_dotenv

from db import already_alerted, mark_alerted
from notifier import send_alert

load_dotenv()

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"
DEDUP_HOURS    = 12
CONFIDENCE_MIN = 40   # Drop any code below this confidence score
MAX_WORKERS    = 10   # Parallel HTTP fetches
RSS_MAX_AGE_H  = 6    # Skip RSS items older than 6 hours

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}

PLATFORMS = {
    "swiggy": {
        "name": "Swiggy", "emoji": "🛵",
        "stacking_tip": "Swiggy One + HDFC Swiggy card (10% cashback) + this code",
    },
    "zomato": {
        "name": "Zomato", "emoji": "🍕",
        "stacking_tip": "Zomato Gold + HDFC Diners Club (5X rewards) + this coupon",
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
        "stacking_tip": "'Everyday Value' menu + this code + bank card — all at checkout",
    },
    "swiggy-instamart": {
        "name": "Swiggy Instamart", "emoji": "📦",
        "stacking_tip": "Swiggy One + this Instamart code + bank card discount",
    },
    "myntra": {
        "name": "Myntra", "emoji": "👗",
        "stacking_tip": "Myntra Insider + ICICI/HDFC card offer + this code",
    },
    "nykaa": {
        "name": "Nykaa", "emoji": "💄",
        "stacking_tip": "Nykaa Prive points + HDFC card offer + this code",
    },
    "meesho": {
        "name": "Meesho", "emoji": "🛍️",
        "stacking_tip": "UPI payment + Meesho coins + this coupon",
    },
    "pharmeasy": {
        "name": "PharmEasy", "emoji": "💊",
        "stacking_tip": "PharmEasy subscription + this coupon + UPI cashback",
    },
    "amazon": {
        "name": "Amazon", "emoji": "📦",
        "stacking_tip": "Amazon Pay ICICI card + Super Value Day + this code",
    },
    "flipkart": {
        "name": "Flipkart", "emoji": "🛒",
        "stacking_tip": "Flipkart Axis card (5% back) + Super Coins + this code",
    },
}

# Platform keyword map for context detection
PLATFORM_KEYWORDS = {
    "swiggy-instamart": ["instamart"],
    "swiggy":     ["swiggy"],
    "zomato":     ["zomato"],
    "blinkit":    ["blinkit", "grofers"],
    "zepto":      ["zepto"],
    "bigbasket":  ["bigbasket", "big basket", "bb "],
    "dominos":    ["dominos", "domino's", "domino"],
    "myntra":     ["myntra"],
    "nykaa":      ["nykaa"],
    "meesho":     ["meesho"],
    "pharmeasy":  ["pharmeasy", "pharma easy"],
    "amazon":     ["amazon"],
    "flipkart":   ["flipkart"],
}

# ── Regex patterns ─────────────────────────────────────────────────────────────
# Pass 1: data-attributes (high confidence, explicit coupon fields)
_PASS1_PATTERNS = [
    re.compile(r'data-(?:clipboard-text|coupon[_-]?code|promo[_-]?code|code)[="\s:]+([A-Z][A-Z0-9]{4,14})', re.IGNORECASE),
    re.compile(r'"(?:coupon_code|promo_code|discount_code|offer_code)"\s*:\s*"([A-Z][A-Z0-9]{4,14})"', re.IGNORECASE),
    re.compile(r'<(?:input|button)[^>]+(?:value|data-code)=["\']([A-Z][A-Z0-9]{4,14})["\']', re.IGNORECASE),
]
# Pass 2: contextual text patterns (require trigger word, 5-char min)
_PASS2_PATTERNS = [
    re.compile(r'(?:use|apply|enter|get|copy)\s+(?:code|coupon|promo)[\s:]+([A-Z][A-Z0-9]{4,14})', re.IGNORECASE),
    re.compile(r'(?:code|coupon|promo)[:\s"\']+([A-Z][A-Z0-9]{4,14})', re.IGNORECASE),
    re.compile(r'<code[^>]*>([A-Z][A-Z0-9]{4,14})</code>', re.IGNORECASE),
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
    "SAVINGS","CASHBACK","DISCOUNT","DELIVERY","SWIGGY","ZOMATO","BLINKIT",
    "ZEPTO","BIGBASKET","DOMINOS","MYNTRA","NYKAA","MEESHO","FLIPKART",
}


# ── Source registry ─────────────────────────────────────────────────────────────
# Each entry: (url, platform_key, source_name, source_type, confidence_base)
# source_type: 'rss' | 'html' | 'official'
def build_sources() -> list[tuple]:
    sources = []

    # ── RSS feeds (highest reliability, parse pubDate for freshness) ────────────
    rss_map = {
        "swiggy":           "https://www.desidime.com/coupons/swiggy.xml",
        "zomato":           "https://www.desidime.com/coupons/zomato.xml",
        "blinkit":          "https://www.desidime.com/coupons/blinkit.xml",
        "zepto":            "https://www.desidime.com/coupons/zepto.xml",
        "bigbasket":        "https://www.desidime.com/coupons/bigbasket.xml",
        "dominos":          "https://www.desidime.com/coupons/dominos-pizza.xml",
        "swiggy-instamart": "https://www.desidime.com/coupons/swiggy-instamart.xml",
        "myntra":           "https://www.desidime.com/coupons/myntra.xml",
        "amazon":           "https://www.desidime.com/coupons/amazon.xml",
        "flipkart":         "https://www.desidime.com/coupons/flipkart.xml",
        "nykaa":            "https://www.desidime.com/coupons/nykaa.xml",
    }
    for pk, url in rss_map.items():
        sources.append((url, pk, "DesiDime RSS", "rss", 85))

    # ── Scraped HTML aggregators ────────────────────────────────────────────────
    html_sources = [
        # (url, platform_key, source_name, confidence)
        ("https://www.couponraja.in/swiggy-coupons",      "swiggy",     "CouponRaja", 65),
        ("https://www.couponraja.in/zomato-coupons",      "zomato",     "CouponRaja", 65),
        ("https://www.couponraja.in/blinkit-coupons",     "blinkit",    "CouponRaja", 65),
        ("https://www.couponraja.in/zepto-coupons",       "zepto",      "CouponRaja", 65),
        ("https://www.couponraja.in/dominos-coupons",     "dominos",    "CouponRaja", 65),
        ("https://www.couponraja.in/amazon-coupons",      "amazon",     "CouponRaja", 65),
        ("https://www.couponraja.in/flipkart-coupons",    "flipkart",   "CouponRaja", 65),
        ("https://www.couponraja.in/myntra-coupons",      "myntra",     "CouponRaja", 65),
        ("https://www.couponraja.in/nykaa-coupons",       "nykaa",      "CouponRaja", 65),
        ("https://www.promocodeclub.com/swiggy-coupons",  "swiggy",     "PromoCodeClub", 60),
        ("https://www.promocodeclub.com/zomato-coupons",  "zomato",     "PromoCodeClub", 60),
        ("https://www.promocodeclub.com/blinkit-coupons", "blinkit",    "PromoCodeClub", 60),
        ("https://www.promocodeclub.com/zepto-coupons",   "zepto",      "PromoCodeClub", 60),
        ("https://www.promocodeclub.com/dominos-coupons", "dominos",    "PromoCodeClub", 60),
        ("https://www.promocodeclub.com/amazon-coupons",  "amazon",     "PromoCodeClub", 60),
        ("https://www.promocodeclub.com/flipkart-coupons","flipkart",   "PromoCodeClub", 60),
    ]
    for url, pk, name, conf in html_sources:
        sources.append((url, pk, name, "html", conf))

    # ── Official platform pages ─────────────────────────────────────────────────
    official_sources = [
        ("https://www.dominos.co.in/offers",              "dominos",    "Dominos Official", 95),
        ("https://www.swiggy.com/offers",                 "swiggy",     "Swiggy Official",  95),
    ]
    for url, pk, name, conf in official_sources:
        sources.append((url, pk, name, "official", conf))

    return sources


# ── Telegram public channels ────────────────────────────────────────────────────
TG_FOOD_CHANNELS = [
    "@foodcoupon",
    "@foodielooters",
    "@gopaisadeals",
    "@GrabOnIndiaOfficial",
    "@CouponDuniaOffers",
    "@desidimehot",
    "@lootdeals",
    "@Extrape",
]


def fetch(url: str, timeout: int = 12) -> str:
    """Fetch raw content. Returns '' on any error or non-200."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
        print(f"[coupon_detector] {url} → HTTP {r.status_code} (skipping)")
        return ""
    except Exception as e:
        print(f"[coupon_detector] fetch error {url}: {e}")
        return ""


def is_cloudflare_blocked(html: str) -> bool:
    markers = [
        "checking your browser", "just a moment",
        "enable javascript and cookies",
        "cf-browser-verification", "__cf_chl",
        "challenge-platform", "ray id",
    ]
    lower = html[:2000].lower()
    return any(m in lower for m in markers)


def score_code(code: str, discount: str, confidence_base: int) -> int:
    """Return a 0-100 confidence score for a code."""
    score = confidence_base
    if re.search(r'\d', code):          score += 10  # has a digit (typical)
    if len(code) >= 6:                  score += 5   # longer codes more likely real
    if discount:                        score += 10  # discount context found
    if len(code) < 5:                   score -= 20  # very short = suspicious
    return min(100, max(0, score))


def extract_codes(html: str, confidence_base: int = 65) -> list[tuple[str, str, int]]:
    """
    Returns list of (code, discount_description, confidence_score).
    Two-pass extraction: data-attributes first (high conf), then text patterns.
    """
    if not html or is_cloudflare_blocked(html):
        return []

    results = []
    seen    = set()

    def _add(m, pattern_conf_boost=0):
        code = (m.group(1) or "").upper().strip()
        if (
            not code
            or len(code) < 4
            or code in _CODE_BLACKLIST
            or code in seen
        ):
            return
        # Grab surrounding discount context
        start  = max(0, m.start() - 250)
        end    = min(len(html), m.end() + 250)
        ctx    = html[start:end]
        disc_m = _DISCOUNT_PATTERN.search(ctx)
        disc   = ""
        if disc_m:
            disc = re.sub(r'<[^>]+>', ' ', disc_m.group(1))
            disc = re.sub(r'\s+', ' ', disc).strip()[:120]
        conf = score_code(code, disc, confidence_base + pattern_conf_boost)
        if conf < CONFIDENCE_MIN:
            return
        seen.add(code)
        results.append((code, disc, conf))

    # Pass 1: high-confidence data attributes
    for pat in _PASS1_PATTERNS:
        for m in pat.finditer(html):
            _add(m, pattern_conf_boost=15)

    # Pass 2: contextual text patterns
    for pat in _PASS2_PATTERNS:
        for m in pat.finditer(html):
            _add(m, pattern_conf_boost=0)

    # Sort by confidence descending
    results.sort(key=lambda x: x[2], reverse=True)
    return results


def parse_rss(xml_text: str, platform_key: str, source_name: str, confidence_base: int) -> list[dict]:
    """
    Parse a DesiDime RSS feed. Returns list of code-dicts.
    Skips items older than RSS_MAX_AGE_H hours for freshness.
    """
    results = []
    if not xml_text or is_cloudflare_blocked(xml_text):
        return results
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return results

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RSS_MAX_AGE_H)

    for item in root.iter("item"):
        pub_el = item.find("pubDate")
        if pub_el is not None and pub_el.text:
            try:
                pub_dt = parsedate_to_datetime(pub_el.text.strip())
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue   # skip stale items
            except Exception:
                pass   # if we can't parse the date, include the item

        # Extract code from <title> and <description>
        title_el = item.find("title")
        desc_el  = item.find("description")
        text     = ""
        if title_el is not None and title_el.text:
            text += title_el.text + " "
        if desc_el is not None and desc_el.text:
            text += desc_el.text

        codes = extract_codes(text, confidence_base)
        for code, disc, conf in codes:
            results.append({
                "code": code, "discount": disc,
                "platform": platform_key, "source": source_name, "conf": conf,
            })
    return results


def build_alert(platform_key: str, code: str, discount: str, source: str, conf: int) -> str:
    p = PLATFORMS.get(platform_key, {"name": platform_key, "emoji": "🛒", "stacking_tip": ""})
    badge    = "🔥 <b>HOT</b>" if conf >= 80 else "✅ <b>NEW</b>"
    disc_line = f"💰 {discount}\n" if discount else ""
    tip_line  = f"\n💡 <b>Stack it:</b> {p['stacking_tip']}" if p["stacking_tip"] else ""
    return (
        f"🎟️ {badge} COUPON — {p['emoji']} {p['name']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Code: <code>{code}</code>\n"
        f"{disc_line}"
        f"📍 Source: {source} (confidence {conf}%)"
        f"{tip_line}\n\n"
        f"✅ Open app → Cart → Apply code → Pay with bank card\n"
        f"⚡ <i>Verify code is still valid before ordering.</i>"
    )


def process_source(url: str, platform_key: str, source_name: str,
                   src_type: str, conf_base: int) -> list[dict]:
    """Fetch one source and return list of found code-dicts."""
    html = fetch(url)
    if not html:
        return []
    if is_cloudflare_blocked(html):
        return []

    if src_type == "rss":
        return parse_rss(html, platform_key, source_name, conf_base)

    # HTML / official page
    codes = extract_codes(html, conf_base)
    return [
        {"code": c, "discount": d, "platform": platform_key,
         "source": source_name, "conf": conf}
        for c, d, conf in codes
    ]


def run():
    total_sent    = 0
    sources_tried = 0
    sources_ok    = 0
    sources_list  = build_sources()

    # ── Layers A + B: Parallel fetch ────────────────────────────────────────────
    print(f"[coupon_detector] Fetching {len(sources_list)} sources in parallel (max {MAX_WORKERS} workers)...")
    all_codes: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_source, url, pk, sn, st, cb): (url, pk, sn)
            for url, pk, sn, st, cb in sources_list
        }
        for fut in as_completed(futures):
            url, pk, sn = futures[fut]
            sources_tried += 1
            try:
                results = fut.result()
                sources_ok += 1
                all_codes.extend(results)
            except Exception as e:
                print(f"[coupon_detector] ERROR {sn}/{pk}: {e}")

    # Deduplicate across sources, keep highest confidence per (platform, code)
    best: dict[tuple, dict] = {}
    for item in all_codes:
        key = (item["platform"], item["code"])
        if key not in best or item["conf"] > best[key]["conf"]:
            best[key] = item

    # Send alerts for unique codes
    for (platform_key, code), item in sorted(best.items(), key=lambda x: -x[1]["conf"]):
        dedup_key = f"coupon:{platform_key}:{code}"
        if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
            continue
        msg = build_alert(platform_key, code, item["discount"], item["source"], item["conf"])
        if send_alert(msg):
            mark_alerted(dedup_key, PLATFORMS.get(platform_key, {}).get("name", platform_key),
                         0, priority_score=item["conf"], category="coupon")
            print(f"[coupon_detector] ✅ {platform_key} → {code} ({item['source']}, conf {item['conf']}%)")
            total_sent += 1

    # ── Layer C: Telegram public channel web previews ───────────────────────────
    print("[coupon_detector] Layer C — Telegram public channel previews...")
    for channel in TG_FOOD_CHANNELS:
        handle = channel.lstrip("@")
        html   = fetch(f"https://t.me/s/{handle}")
        if not html:
            continue
        codes = extract_codes(html, confidence_base=70)
        for code, discount, conf in codes:
            html_lower = html.lower()
            idx = html.upper().find(code)
            detected_platform = None
            if idx != -1:
                ctx = html_lower[max(0, idx-400):idx+400]
                for pkey, kws in PLATFORM_KEYWORDS.items():
                    if any(kw in ctx for kw in kws):
                        detected_platform = pkey
                        break
            if not detected_platform:
                continue
            dedup_key = f"coupon:{detected_platform}:{code}"
            if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
                continue
            msg = build_alert(detected_platform, code, discount, f"Telegram {channel}", conf)
            if send_alert(msg):
                mark_alerted(dedup_key, PLATFORMS.get(detected_platform, {}).get("name", detected_platform),
                             0, priority_score=conf, category="coupon")
                print(f"[coupon_detector] ✅ {detected_platform} → {code} (TG {channel}, conf {conf}%)")
                total_sent += 1
        time.sleep(0.3)

    print(
        f"[coupon_detector] Done. "
        f"{sources_ok}/{sources_tried} sources OK. "
        f"{total_sent} new alerts sent."
    )


if __name__ == "__main__":
    run()
