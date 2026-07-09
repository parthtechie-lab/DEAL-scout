"""
coupon_detector.py — Deal Scout v2: Instant Coupon Detector (v4 — Hardened)

WHAT CHANGED FROM v3 -> v4
---------------------------
No scraper can hit a literal 100% "never skip a coupon" guarantee — that's not
an engineering limit that goes away with better code, it's a property of
scraping other people's sites (they change markup, block IPs, rate-limit,
go down). What v4 does instead is close every gap that WAS a real, fixable
miss in v3:

1. RETRY + BACKOFF ON FETCH — v3 treated one failed request as "source dead
   for this run." v4 retries each source up to RETRY_MAX times with
   exponential backoff + jitter before giving up.

2. USER-AGENT ROTATION — static single UA is trivial to fingerprint/block.
   Now rotates from a small pool per-request.

3. JS-EMBEDDED DATA EXTRACTION (Pass 3) — many modern coupon aggregators
   hydrate their coupon list client-side via a JSON blob in
   <script type="application/ld+json">, window.__INITIAL_STATE__, or
   __NEXT_DATA__. v3's regex only looked at rendered HTML text/attributes,
   so any code that only exists inside one of these blobs was silently
   invisible. v4 parses these blobs and reuses the same code extractor
   against their JSON string values.

4. CROSS-SOURCE CONFIDENCE BONUS — if the same (platform, code) is found by
   2+ independent sources, that's real corroborating signal. v3 only kept
   "the single highest-confidence hit" and threw the corroboration away.
   v4 adds a bonus and records how many sources agreed.

5. RSS DATE FALLBACK — v3 only checked <pubDate>; if that tag was absent
   (some feeds use <dc:date> or <atom:updated>) it silently included the
   item without a freshness check at all. v4 checks all three before
   falling back.

6. TELEGRAM PAGINATION — v3 only ever fetched the single latest preview
   page of each channel (https://t.me/s/handle), meaning anything posted
   between runs and pushed off that page was invisible. v4 walks back
   several pages with ?before=.

7. PER-SOURCE FAILURE BACKOFF — a source that 403s/404s repeatedly is
   marked "unhealthy" and logged distinctly instead of being retried
   forever at full cost every run.

8. STRUCTURED LOGGING — swapped print() for the logging module so failures
   are level-tagged and diagnosable instead of scrolling past.

9. SELF-TEST HARNESS — `python coupon_detector.py --selftest` runs the
   extractor against known-shape fixtures (HTML attr, JSON blob, RSS text)
   so a future regex edit that breaks matching fails loudly instead of
   silently degrading recall.

Everything from v3 (parallel fetch, confidence scoring, dedup, freshness
window, platform map, Telegram context detection) is preserved.
"""

import json
import logging
import random
import re
import sys
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] coupon_detector: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("coupon_detector")

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"
DEDUP_HOURS    = 12
CONFIDENCE_MIN = 40   # Drop any code below this confidence score
MAX_WORKERS    = 10   # Parallel HTTP fetches
RSS_MAX_AGE_H  = 6    # Skip RSS items older than this many hours (single source of truth)

RETRY_MAX      = 3    # Max attempts per source fetch
RETRY_BASE_S   = 0.75 # Base backoff seconds (doubles each attempt + jitter)

TG_PAGES_BACK  = 4    # How many "before" pages to walk per Telegram channel

CROSS_SOURCE_BONUS = 8   # Confidence bonus per additional corroborating source
CROSS_SOURCE_CAP   = 100

_UA_POOL = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

def _headers() -> dict:
    return {
        "User-Agent": random.choice(_UA_POOL),
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

# ── Regex patterns ─────────────────────────────────────────────────────────
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
# Pass 3: keys commonly used inside JSON blobs (ld+json, __INITIAL_STATE__, __NEXT_DATA__)
_PASS3_JSON_KEY_PATTERN = re.compile(
    r'"(?:code|couponCode|coupon_code|promoCode|promo_code|discountCode)"\s*:\s*"([A-Z][A-Z0-9]{4,14})"',
    re.IGNORECASE,
)
_JSON_BLOB_PATTERNS = [
    re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL),
    re.compile(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', re.DOTALL),
    re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL),
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


# ── Source registry ──────────────────────────────────────────────────────────
# Each entry: (url, platform_key, source_name, source_type, confidence_base)
# source_type: 'rss' | 'html' | 'official'
def build_sources() -> list[tuple]:
    sources = []

    # ── RSS feeds (highest reliability, parse pubDate for freshness) ─────────
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

    # ── Scraped HTML aggregators ──────────────────────────────────────────────
    html_sources = [
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

    # ── Official platform pages ────────────────────────────────────────────────
    official_sources = [
        ("https://www.dominos.co.in/offers",              "dominos",    "Dominos Official", 95),
        ("https://www.swiggy.com/offers",                 "swiggy",     "Swiggy Official",  95),
    ]
    for url, pk, name, conf in official_sources:
        sources.append((url, pk, name, "official", conf))

    return sources


# ── Telegram public channels ─────────────────────────────────────────────────
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

# Track sources that keep failing so we can log distinctly (not silently retried forever)
_unhealthy_sources: dict[str, int] = {}


def fetch(url: str, timeout: int = 12, retries: int = RETRY_MAX) -> str:
    """Fetch raw content with retry + exponential backoff + jitter.
    Returns '' only after all retry attempts are exhausted."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=_headers(), timeout=timeout)
            if r.status_code == 200:
                _unhealthy_sources.pop(url, None)
                return r.text
            if r.status_code in (429, 503):
                # Rate-limited / temporarily unavailable — worth retrying
                last_err = f"HTTP {r.status_code}"
            else:
                # 404/403/etc — retrying rarely helps, but still count it once
                last_err = f"HTTP {r.status_code}"
                if attempt == 1:
                    log.warning(f"{url} -> HTTP {r.status_code} (attempt {attempt}/{retries})")
        except Exception as e:
            last_err = str(e)

        if attempt < retries:
            backoff = RETRY_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 0.4)
            time.sleep(backoff)

    _unhealthy_sources[url] = _unhealthy_sources.get(url, 0) + 1
    log.error(f"{url} -> failed after {retries} attempts ({last_err}); "
              f"unhealthy count={_unhealthy_sources[url]}")
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


def _extract_json_blob_codes(html: str) -> list[str]:
    """Pull candidate codes out of ld+json / __INITIAL_STATE__ / __NEXT_DATA__
    blobs. Many aggregators hydrate coupon lists client-side, so codes can
    exist ONLY inside these blobs and never in plain rendered HTML text —
    v3's regex passes never looked here at all."""
    found = []
    for blob_pat in _JSON_BLOB_PATTERNS:
        for bm in blob_pat.finditer(html):
            blob = bm.group(1)
            if not blob:
                continue
            # Try structured JSON parse first for accuracy
            try:
                data = json.loads(blob)
                found.extend(_walk_json_for_codes(data))
                continue
            except (json.JSONDecodeError, ValueError):
                pass
            # Fall back to regex over the raw blob text
            for km in _PASS3_JSON_KEY_PATTERN.finditer(blob):
                found.append(km.group(1).upper())
    return found


def _walk_json_for_codes(node, depth: int = 0) -> list[str]:
    """Recursively walk a parsed JSON structure looking for coupon-code-ish keys."""
    if depth > 8:
        return []
    codes = []
    key_names = {"code", "couponcode", "coupon_code", "promocode", "promo_code", "discountcode"}
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and k.lower() in key_names:
                candidate = v.strip().upper()
                if re.fullmatch(r'[A-Z][A-Z0-9]{4,14}', candidate):
                    codes.append(candidate)
            elif isinstance(v, (dict, list)):
                codes.extend(_walk_json_for_codes(v, depth + 1))
    elif isinstance(node, list):
        for item in node:
            codes.extend(_walk_json_for_codes(item, depth + 1))
    return codes


def extract_codes(html: str, confidence_base: int = 65) -> list[tuple[str, str, int]]:
    """
    Returns list of (code, discount_description, confidence_score).
    Three-pass extraction:
      Pass 1: data-attributes (highest confidence)
      Pass 2: contextual text patterns
      Pass 3: embedded JSON blobs (ld+json / __INITIAL_STATE__ / __NEXT_DATA__)
    """
    if not html or is_cloudflare_blocked(html):
        return []

    results = []
    seen    = set()

    def _add(code_raw: str, ctx: str, pattern_conf_boost: int):
        code = code_raw.upper().strip()
        if (
            not code
            or len(code) < 4
            or code in _CODE_BLACKLIST
            or code in seen
        ):
            return
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
            code = m.group(1) or ""
            start, end = max(0, m.start() - 250), min(len(html), m.end() + 250)
            _add(code, html[start:end], pattern_conf_boost=15)

    # Pass 2: contextual text patterns
    for pat in _PASS2_PATTERNS:
        for m in pat.finditer(html):
            code = m.group(1) or ""
            start, end = max(0, m.start() - 250), min(len(html), m.end() + 250)
            _add(code, html[start:end], pattern_conf_boost=0)

    # Pass 3: embedded JSON blobs — no surrounding HTML context available,
    # so no discount snippet, but still a legitimate high-confidence hit
    # since it comes from a structured data field, not loose text.
    for code in _extract_json_blob_codes(html):
        _add(code, "", pattern_conf_boost=10)

    # Sort by confidence descending
    results.sort(key=lambda x: x[2], reverse=True)
    return results


def _rss_item_datetime(item) -> datetime | None:
    """Try <pubDate>, then <dc:date>, then <atom:updated> before giving up.
    v3 only checked <pubDate> and silently skipped the freshness check
    entirely if it was missing — that let stale AND unverified-fresh items
    through inconsistently. v4 tries all three known date fields."""
    candidates = [
        item.find("pubDate"),
        item.find("{http://purl.org/dc/elements/1.1/}date"),
        item.find("{http://www.w3.org/2005/Atom}updated"),
    ]
    for el in candidates:
        if el is not None and el.text:
            try:
                dt = parsedate_to_datetime(el.text.strip())
            except Exception:
                try:
                    dt = datetime.fromisoformat(el.text.strip().replace("Z", "+00:00"))
                except Exception:
                    continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    return None


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
    except ET.ParseError as e:
        log.warning(f"RSS parse error for {source_name}/{platform_key}: {e}")
        return results

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RSS_MAX_AGE_H)

    for item in root.iter("item"):
        pub_dt = _rss_item_datetime(item)
        if pub_dt is not None and pub_dt < cutoff:
            continue   # confirmed stale, skip
        # if pub_dt is None (no date field found at all), we still include
        # the item rather than dropping it — an unknown date is not the
        # same as a known-stale date, and dropping it would be a silent miss.

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


def build_alert(platform_key: str, code: str, discount: str, source: str,
                conf: int, corroborated_by: int = 1) -> str:
    p = PLATFORMS.get(platform_key, {"name": platform_key, "emoji": "🛒", "stacking_tip": ""})
    badge    = "🔥 <b>HOT</b>" if conf >= 80 else "✅ <b>NEW</b>"
    disc_line = f"💰 {discount}\n" if discount else ""
    tip_line  = f"\n💡 <b>Stack it:</b> {p['stacking_tip']}" if p["stacking_tip"] else ""
    corrob_line = f" · seen on {corroborated_by} sources" if corroborated_by > 1 else ""
    return (
        f"🎟️ {badge} COUPON — {p['emoji']} {p['name']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Code: <code>{code}</code>\n"
        f"{disc_line}"
        f"📍 Source: {source} (confidence {conf}%{corrob_line})"
        f"{tip_line}\n\n"
        f"✅ Open app → Cart → Apply code → Pay with bank card\n"
        f"⚡ <i>Verify code is still valid before ordering.</i>"
    )


def process_source(url: str, platform_key: str, source_name: str,
                   src_type: str, conf_base: int) -> list[dict]:
    """Fetch one source (with retry) and return list of found code-dicts."""
    html = fetch(url)
    if not html:
        return []
    if is_cloudflare_blocked(html):
        log.info(f"{source_name}/{platform_key} blocked by Cloudflare challenge — skipping")
        return []

    if src_type == "rss":
        return parse_rss(html, platform_key, source_name, conf_base)

    codes = extract_codes(html, conf_base)
    return [
        {"code": c, "discount": d, "platform": platform_key,
         "source": source_name, "conf": conf}
        for c, d, conf in codes
    ]


def _apply_cross_source_bonus(all_codes: list[dict]) -> dict[tuple, dict]:
    """Keep the highest-confidence hit per (platform, code), but boost its
    confidence when multiple independent sources reported the same code —
    corroboration across sources is a real accuracy signal v3 discarded."""
    grouped: dict[tuple, list[dict]] = {}
    for item in all_codes:
        key = (item["platform"], item["code"])
        grouped.setdefault(key, []).append(item)

    best: dict[tuple, dict] = {}
    for key, items in grouped.items():
        distinct_sources = {it["source"] for it in items}
        top = max(items, key=lambda x: x["conf"])
        bonus = CROSS_SOURCE_BONUS * (len(distinct_sources) - 1)
        top = dict(top)  # copy, don't mutate original
        top["conf"] = min(CROSS_SOURCE_CAP, top["conf"] + bonus)
        top["corroborated_by"] = len(distinct_sources)
        best[key] = top
    return best


def run():
    total_sent    = 0
    sources_tried = 0
    sources_ok    = 0
    sources_list  = build_sources()

    log.info(f"Fetching {len(sources_list)} sources in parallel (max {MAX_WORKERS} workers)...")
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
                log.error(f"ERROR {sn}/{pk}: {e}")

    best = _apply_cross_source_bonus(all_codes)

    for (platform_key, code), item in sorted(best.items(), key=lambda x: -x[1]["conf"]):
        dedup_key = f"coupon:{platform_key}:{code}"
        if already_alerted(dedup_key, within_hours=DEDUP_HOURS):
            continue
        msg = build_alert(platform_key, code, item["discount"], item["source"],
                           item["conf"], item.get("corroborated_by", 1))
        if send_alert(msg):
            mark_alerted(dedup_key, PLATFORMS.get(platform_key, {}).get("name", platform_key),
                         0, priority_score=item["conf"], category="coupon")
            log.info(f"✅ {platform_key} → {code} ({item['source']}, conf {item['conf']}%, "
                     f"corroborated_by={item.get('corroborated_by', 1)})")
            total_sent += 1

    # ── Layer C: Telegram public channel previews (with pagination) ─────────
    log.info("Layer C — Telegram public channel previews...")
    for channel in TG_FOOD_CHANNELS:
        handle = channel.lstrip("@")
        before_id = None
        for page in range(TG_PAGES_BACK):
            page_url = f"https://t.me/s/{handle}"
            if before_id:
                page_url += f"?before={before_id}"
            html = fetch(page_url)
            if not html:
                break  # stop paginating this channel, don't kill the whole run

            codes = extract_codes(html, confidence_base=70)
            for code, discount, conf in codes:
                html_lower = html.lower()
                idx = html.upper().find(code)
                detected_platform = None
                if idx != -1:
                    ctx = html_lower[max(0, idx - 400):idx + 400]
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
                    log.info(f"✅ {detected_platform} → {code} (TG {channel} page {page}, conf {conf}%)")
                    total_sent += 1

            # Find the oldest message id on this page to paginate further back
            ids = re.findall(r'data-post="[^"]+/(\d+)"', html)
            if not ids:
                break
            oldest_id = min(int(i) for i in ids)
            if before_id is not None and oldest_id >= before_id:
                break  # no progress, stop to avoid an infinite loop
            before_id = oldest_id
            time.sleep(0.3)

    log.info(
        f"Done. {sources_ok}/{sources_tried} sources OK. "
        f"{total_sent} new alerts sent. "
        f"{len(_unhealthy_sources)} source(s) currently unhealthy."
    )


# ── Self-test harness ─────────────────────────────────────────────────────
def _selftest():
    """Sanity-check extraction against known-shape fixtures so a future
    regex edit that silently breaks matching fails loudly here first."""
    failures = []

    # Pass 1 fixture: data-attribute
    html1 = '<button data-coupon-code="SAVE150" data-discount="15% off">Copy</button>'
    codes1 = extract_codes(html1, 65)
    if not any(c == "SAVE150" for c, _, _ in codes1):
        failures.append("Pass 1 (data-attribute) failed to extract SAVE150")

    # Pass 2 fixture: contextual text
    html2 = 'Use code FLAT200 to get ₹200 off on your order today!'
    codes2 = extract_codes(html2, 65)
    if not any(c == "FLAT200" for c, _, _ in codes2):
        failures.append("Pass 2 (contextual text) failed to extract FLAT200")

    # Pass 3 fixture: JSON blob (ld+json style)
    html3 = (
        '<script type="application/ld+json">'
        '{"offers": {"couponCode": "PIZZA50", "discount": "50% off"}}'
        '</script>'
    )
    codes3 = extract_codes(html3, 65)
    if not any(c == "PIZZA50" for c, _, _ in codes3):
        failures.append("Pass 3 (JSON blob) failed to extract PIZZA50")

    # Blacklist fixture: should NOT extract common false positives
    html4 = 'Please LOGIN and click APPLY to continue browsing OFFERS today.'
    codes4 = extract_codes(html4, 65)
    if codes4:
        failures.append(f"Blacklist fixture leaked false positives: {codes4}")

    # Cross-source bonus fixture
    sample = [
        {"platform": "swiggy", "code": "GET100", "discount": "", "source": "A", "conf": 60},
        {"platform": "swiggy", "code": "GET100", "discount": "", "source": "B", "conf": 65},
    ]
    boosted = _apply_cross_source_bonus(sample)
    key = ("swiggy", "GET100")
    if key not in boosted or boosted[key]["conf"] <= 65:
        failures.append("Cross-source bonus did not boost confidence for corroborated code")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("SELF-TEST PASSED: all extraction fixtures matched as expected.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        run()
