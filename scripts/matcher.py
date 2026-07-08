"""
matcher.py — Deal Scout v2 matching engine.

Responsibilities:
  1. extract_deal_info(text)  → structured dict: price, MRP, discount_pct,
                                coupon_code, expiry, detected from raw text
  2. score_deal(info, source_reliability, keyword_match_count, target_price)
                              → 0-100 priority score
  3. is_quiet_hours()         → True if current IST time is 22:00–08:00
  4. FOOD_PLATFORMS dict      → quick lookup for food platform detection
  5. deduplicate_titles(new, existing_titles, threshold)
                              → True if new title is too similar to an existing one

Priority score weights (from matching_rules in watchlist.json):
  discount_percent    × 0.35
  source_reliability  × 0.25
  keyword_match_count × 0.20
  price_below_target  × 0.20

Score ≥ 40 → alert is sent.  Score < 40 → silently dropped.
"""

import re
from datetime import datetime, timezone, timedelta

try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# ── IST offset ────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ── Regex patterns (compiled once) ───────────────────────────────────────────
_PRICE_RE = re.compile(
    r'(?:₹\s?|Rs\.?\s?|INR\s?)([0-9,]+)',
    re.IGNORECASE
)
_DISCOUNT_RE = re.compile(
    r'([0-9]{1,3})\s?%\s?(?:off|discount)',
    re.IGNORECASE
)
_COUPON_RE = re.compile(
    r'(?:use|apply|code|coupon|promo)[:\s]+([A-Z][A-Z0-9]{3,14})'
    r'|"([A-Z][A-Z0-9]{4,14})"',
    re.IGNORECASE
)
_EXPIRY_RE = re.compile(
    r'(?:valid\s+till|expires?\s+(?:on)?)\s*([0-9]{1,2}[a-zA-Z\s0-9]{0,15})',
    re.IGNORECASE
)

# ── Words that look like coupon codes but aren't ──────────────────────────────
_COUPON_BLACKLIST = {
    "TERMS", "ABOUT", "LOGIN", "SHARE", "CLICK", "CLOSE", "EMAIL",
    "PHONE", "ORDER", "APPLY", "CHECK", "INDIA", "AMAZON", "FLIPKART",
    "STORE", "UPIID", "REFER", "OFFER",
}

# ── Food platform lookup ──────────────────────────────────────────────────────
FOOD_PLATFORMS = {
    "swiggy":     {"name": "Swiggy",          "emoji": "🛵"},
    "zomato":     {"name": "Zomato",           "emoji": "🍕"},
    "blinkit":    {"name": "Blinkit",          "emoji": "⚡"},
    "zepto":      {"name": "Zepto",            "emoji": "🟣"},
    "bigbasket":  {"name": "BigBasket",        "emoji": "🛒"},
    "big basket": {"name": "BigBasket",        "emoji": "🛒"},
    "dominos":    {"name": "Dominos",          "emoji": "🍕"},
    "domino's":   {"name": "Dominos",          "emoji": "🍕"},
    "instamart":  {"name": "Swiggy Instamart", "emoji": "📦"},
    "faasos":     {"name": "Faasos",           "emoji": "🌯"},
    "pizza hut":  {"name": "Pizza Hut",        "emoji": "🍕"},
    "kfc":        {"name": "KFC",              "emoji": "🍗"},
    "mcdonald":   {"name": "McDonald's",       "emoji": "🍔"},
    "burger king":{"name": "Burger King",      "emoji": "🍔"},
    "dunzo":      {"name": "Dunzo",            "emoji": "🚴"},
    "jiomart":    {"name": "JioMart",          "emoji": "🛒"},
}

# ── Category emoji ─────────────────────────────────────────────────────────────
CATEGORY_EMOJI = {
    "electronics":     "📱",
    "electrical":      "🔌",
    "food":            "🍽️",
    "sports":          "🏋️",
    "fashion":         "👗",
    "beauty":          "💄",
    "home_kitchen":    "🏠",
    "appliances":      "🏠",
    "mobiles":         "📲",
    "baby_kids":       "👶",
    "books_stationery":"📚",
    "travel":          "✈️",
}


def extract_deal_info(text: str) -> dict:
    """
    Extracts structured deal data from raw message/post text.
    Returns a dict with keys: price, discount_pct, coupon_code, expiry.
    """
    info = {
        "price":       None,
        "discount_pct": None,
        "coupon_code": None,
        "expiry":      None,
    }

    # Price — take the first match
    prices = [
        int(m.replace(",", ""))
        for m in _PRICE_RE.findall(text)
        if m.replace(",", "").isdigit()
    ]
    if prices:
        info["price"] = min(prices)  # lowest number = deal price, not MRP

    # Discount %
    disc_matches = _DISCOUNT_RE.findall(text)
    if disc_matches:
        try:
            info["discount_pct"] = max(int(d) for d in disc_matches)
        except ValueError:
            pass

    # Coupon code
    for m in _COUPON_RE.finditer(text):
        candidate = (m.group(1) or m.group(2) or "").upper()
        if candidate and candidate not in _COUPON_BLACKLIST and len(candidate) >= 4:
            info["coupon_code"] = candidate
            break

    # Expiry
    expiry_match = _EXPIRY_RE.search(text)
    if expiry_match:
        info["expiry"] = expiry_match.group(1).strip()

    return info


def score_deal(
    discount_pct: int | None,
    source_reliability: int,
    keyword_match_count: int,
    target_price: int | None,
    detected_price: int | None,
    weights: dict | None = None,
) -> int:
    """
    Returns a 0-100 priority score. Deals scoring < 40 should be dropped.

    Default weights mirror matching_rules.priority_weights in watchlist.json:
      discount_percent   × 0.35
      source_reliability × 0.25
      keyword_match_count× 0.20
      price_below_target × 0.20
    """
    if weights is None:
        weights = {
            "discount_percent":    0.35,
            "source_reliability":  0.25,
            "keyword_match_count": 0.20,
            "price_below_target":  0.20,
        }

    # Normalise each component to 0-100 before weighting
    # 1. Discount % (cap at 90 %)
    d_score = min((discount_pct or 0), 90) / 90 * 100

    # 2. Source reliability (0-10 scale → 0-100)
    r_score = min(source_reliability, 10) / 10 * 100

    # 3. Keyword match count (cap at 5 matches → full score)
    k_score = min(keyword_match_count, 5) / 5 * 100

    # 4. Price below target (how far below target_price the deal is)
    p_score = 0
    if target_price and detected_price and detected_price < target_price:
        p_score = min((target_price - detected_price) / target_price, 1.0) * 100

    score = (
        d_score * weights["discount_percent"]
        + r_score * weights["source_reliability"]
        + k_score * weights["keyword_match_count"]
        + p_score * weights["price_below_target"]
    )
    return int(score)


def priority_badge(score: int) -> str:
    """Returns an emoji badge string for the score."""
    if score >= 70:
        return "🔥 HIGH PRIORITY"
    elif score >= 40:
        return "✅ DEAL FOUND"
    else:
        return "ℹ️ LOW SIGNAL"


def is_quiet_hours() -> bool:
    """
    Returns True if current IST time is between 22:00 and 08:00.
    Alerts should be suppressed during this window to avoid midnight spam.
    """
    now_ist = datetime.now(IST)
    hour = now_ist.hour
    return hour >= 22 or hour < 8


def detect_food_platforms(text: str) -> list[dict]:
    """Returns list of food platform dicts found in the text."""
    text_lower = text.lower()
    found = []
    seen = set()
    for key, info in FOOD_PLATFORMS.items():
        if key in text_lower and info["name"] not in seen:
            found.append(info)
            seen.add(info["name"])
    return found


def deduplicate_title(new_title: str, existing_titles: list[str], threshold: int = 85) -> bool:
    """
    Returns True if new_title is too similar to any title in existing_titles
    (meaning it's a duplicate and should NOT be alerted again).
    Falls back to exact-substring check if rapidfuzz is unavailable.
    """
    if not existing_titles:
        return False
    new_lower = new_title.lower()
    for existing in existing_titles:
        if HAS_RAPIDFUZZ:
            if fuzz.token_set_ratio(new_lower, existing.lower()) >= threshold:
                return True
        else:
            # Fallback: simple substring overlap
            if new_lower[:30] in existing.lower() or existing.lower()[:30] in new_lower:
                return True
    return False


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample = (
        "🔥 Big Deal! boAt Rockerz 450 at just ₹799 (MRP ₹2,000). "
        "Flat 60% off! Use code BOAT60 at checkout. Valid till 10 July."
    )
    info = extract_deal_info(sample)
    print("Extraction:", info)

    score = score_deal(
        discount_pct=info["discount_pct"],
        source_reliability=9,
        keyword_match_count=3,
        target_price=999,
        detected_price=info["price"],
    )
    print(f"Priority score: {score}/100 → {priority_badge(score)}")
    print(f"Quiet hours now: {is_quiet_hours()}")
    print(f"Food platforms in text: {detect_food_platforms('swiggy zomato deal ₹50 off')}")
    print("rapidfuzz available:", HAS_RAPIDFUZZ)
