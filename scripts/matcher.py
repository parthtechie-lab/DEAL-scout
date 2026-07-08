"""
matcher.py — Deal Scout v2 matching engine.

Responsibilities:
  1. extract_deal_info(text)  → structured dict: price, discount_pct,
                                coupon_code, expiry — with false-positive guards
  2. load_weights(watchlist)  → loads priority_weights from watchlist at runtime
  3. score_deal(...)          → 0-100 priority score
  4. is_quiet_hours()         → True if current IST time is 22:00–08:00
  5. FOOD_PLATFORMS dict      → quick lookup for food platform detection
  6. deduplicate_title(...)   → fuzzy dedup check

Fix log:
  v2.1 — Price extraction: filter "min order / above / worth / cart value"
          context before accepting a price as the deal price. min(prices) is
          replaced by a context-aware selector that avoids ₹99 "minimum order"
          false positives.
  v2.1 — Weights are now loaded from watchlist.json at call time via
          load_weights(watchlist_dict) instead of being hardcoded. All callers
          pass weights through. Hardcoded defaults remain as fallback only.

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

# ── Price extraction ──────────────────────────────────────────────────────────
_PRICE_RE = re.compile(
    r'(?:₹\s?|Rs\.?\s?|INR\s?)([0-9,]+)',
    re.IGNORECASE,
)
# Context words that indicate the following price is NOT the deal price.
# e.g. "min order ₹99", "orders above ₹499", "cart value ₹200"
_PRICE_NOISE_CTX = re.compile(
    r'(?:min(?:imum)?\s+(?:order|cart|purchase|spend)|'
    r'orders?\s+(?:above|over|worth|of)|'
    r'cart\s+(?:value|above|of)|'
    r'above\s+₹|'
    r'worth\s+₹|'
    r'per\s+(?:item|unit|pc|piece)|'
    r'delivery\s+(?:charge|fee)|'
    r'shipping\s+(?:charge|fee|cost))',
    re.IGNORECASE,
)
_PRICE_MIN = 49       # Ignore prices below ₹49 (delivery charges, etc.)
_PRICE_MAX = 500_000  # Ignore prices above ₹5L (clearly MRP of luxury items)

# ── Discount % ────────────────────────────────────────────────────────────────
_DISCOUNT_RE = re.compile(
    r'([0-9]{1,3})\s?%\s?(?:off|discount)',
    re.IGNORECASE,
)

# ── Coupon code ───────────────────────────────────────────────────────────────
_COUPON_RE = re.compile(
    # "use code BOAT60" / "apply coupon SAVE150" — trigger word then code
    r'(?:use|apply|enter|coupon|promo)\s+(?:code\s+)?([A-Z][A-Z0-9]{3,14})'
    # "code: BOAT60" or "code BOAT60" — the word "code" followed by the actual code
    r'|code[:\s]+([A-Z][A-Z0-9]{3,14})'
    # "BOAT60" in double-quotes
    r'|"([A-Z][A-Z0-9]{4,14})"',
    re.IGNORECASE,
)
_COUPON_BLACKLIST = {
    "TERMS", "ABOUT", "LOGIN", "SHARE", "CLICK", "CLOSE", "EMAIL",
    "PHONE", "ORDER", "APPLY", "CHECK", "INDIA", "AMAZON", "FLIPKART",
    "STORE", "UPIID", "REFER", "OFFER",
}

# ── Expiry ────────────────────────────────────────────────────────────────────
_EXPIRY_RE = re.compile(
    r'(?:valid\s+till|expires?\s+(?:on)?)\s*([0-9]{1,2}[a-zA-Z\s0-9]{0,15})',
    re.IGNORECASE,
)

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
    "electronics":      "📱",
    "electrical":       "🔌",
    "food":             "🍽️",
    "sports":           "🏋️",
    "fashion":          "👗",
    "beauty":           "💄",
    "home_kitchen":     "🏠",
    "appliances":       "🏠",
    "mobiles":          "📲",
    "baby_kids":        "👶",
    "books_stationery": "📚",
    "travel":           "✈️",
}

# ── Default scoring weights (fallback if not in watchlist.json) ────────────────
_DEFAULT_WEIGHTS = {
    "discount_percent":    0.35,
    "source_reliability":  0.25,
    "keyword_match_count": 0.20,
    "price_below_target":  0.20,
}


def load_weights(watchlist: dict) -> dict:
    """
    Load priority_weights from watchlist.json at runtime.
    Falls back to _DEFAULT_WEIGHTS if the key is missing or malformed.
    This is the single source of truth — callers should call this once and
    pass the result into score_deal().
    """
    try:
        w = watchlist.get("matching_rules", {}).get("priority_weights", {})
        if w and all(k in w for k in _DEFAULT_WEIGHTS):
            return {k: float(v) for k, v in w.items()}
    except (TypeError, ValueError):
        pass
    return dict(_DEFAULT_WEIGHTS)


def extract_deal_info(text: str) -> dict:
    """
    Extracts structured deal data from raw message/post text.

    Price extraction strategy (v2.1):
      - Finds all ₹/Rs./INR prices in the text.
      - Discards any price where the 60 chars immediately before it match a
        "noise context" pattern (min order, above ₹, cart value, etc.).
      - Discards prices outside [₹49, ₹5,00,000].
      - Of the remaining valid prices, picks the LOWEST (deal price, not MRP).
        If only one valid price remains, that is used.
      - Returns None if no valid price survives the filters.
    """
    info = {
        "price":        None,
        "discount_pct": None,
        "coupon_code":  None,
        "expiry":       None,
    }

    # Price — context-aware extraction
    valid_prices = []
    for m in _PRICE_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        if not raw.isdigit():
            continue
        val = int(raw)
        if val < _PRICE_MIN or val > _PRICE_MAX:
            continue
        # Check 60 chars before the match for noise context
        prefix_start = max(0, m.start() - 60)
        prefix       = text[prefix_start:m.start()]
        if _PRICE_NOISE_CTX.search(prefix):
            continue
        valid_prices.append(val)

    if valid_prices:
        info["price"] = min(valid_prices)

    # Discount %
    disc_matches = _DISCOUNT_RE.findall(text)
    if disc_matches:
        try:
            info["discount_pct"] = max(int(d) for d in disc_matches)
        except ValueError:
            pass

    # Coupon code — pick first valid match across all 3 capture groups
    for m in _COUPON_RE.finditer(text):
        candidate = (m.group(1) or m.group(2) or m.group(3) or "").upper()
        if candidate and candidate not in _COUPON_BLACKLIST and len(candidate) >= 4:
            info["coupon_code"] = candidate
            break

    # Expiry
    expiry_match = _EXPIRY_RE.search(text)
    if expiry_match:
        info["expiry"] = expiry_match.group(1).strip()

    return info


def score_deal(
    discount_pct:       int | None,
    source_reliability: int,
    keyword_match_count:int,
    target_price:       int | None,
    detected_price:     int | None,
    weights:            dict | None = None,
) -> int:
    """
    Returns a 0-100 priority score.
    Weights are loaded from watchlist.json via load_weights(); pass them in.
    Falls back to hardcoded defaults if weights is None.
    """
    w = weights if weights else _DEFAULT_WEIGHTS

    d_score = min((discount_pct or 0), 90) / 90 * 100
    r_score = min(source_reliability, 10) / 10 * 100
    k_score = min(keyword_match_count, 5) / 5 * 100
    p_score = 0.0
    if target_price and detected_price and detected_price < target_price:
        p_score = min((target_price - detected_price) / target_price, 1.0) * 100

    return int(
        d_score * w.get("discount_percent",    0.35)
        + r_score * w.get("source_reliability",  0.25)
        + k_score * w.get("keyword_match_count", 0.20)
        + p_score * w.get("price_below_target",  0.20)
    )


def priority_badge(score: int) -> str:
    if score >= 70:
        return "🔥 HIGH PRIORITY"
    elif score >= 40:
        return "✅ DEAL FOUND"
    return "ℹ️ LOW SIGNAL"


def is_quiet_hours() -> bool:
    hour = datetime.now(IST).hour
    return hour >= 22 or hour < 8


def detect_food_platforms(text: str) -> list[dict]:
    text_lower = text.lower()
    found, seen = [], set()
    for key, info in FOOD_PLATFORMS.items():
        if key in text_lower and info["name"] not in seen:
            found.append(info)
            seen.add(info["name"])
    return found


def deduplicate_title(new_title: str, existing_titles: list[str], threshold: int = 85) -> bool:
    """
    Returns True if new_title is too similar to an existing title (= duplicate).
    NOTE: This is a title-level check. Callers should ALSO dedup on exact
    (platform, coupon_code) pairs via already_alerted() in db.py, which is
    more precise and not subject to fuzzy threshold edge cases.
    """
    if not existing_titles:
        return False
    new_lower = new_title.lower()
    for existing in existing_titles:
        if HAS_RAPIDFUZZ:
            if fuzz.token_set_ratio(new_lower, existing.lower()) >= threshold:
                return True
        else:
            if new_lower[:30] in existing.lower() or existing.lower()[:30] in new_lower:
                return True
    return False


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Test 1: basic extraction
    sample = (
        "🔥 boAt Rockerz 450 at just ₹799 (MRP ₹2,000). "
        "Flat 60% off! Use code BOAT60 at checkout. Valid till 10 July."
    )
    info = extract_deal_info(sample)
    assert info["price"]        == 799,    f"Expected 799, got {info['price']}"
    assert info["discount_pct"] == 60,     f"Expected 60, got {info['discount_pct']}"
    assert info["coupon_code"]  == "BOAT60", f"Expected BOAT60, got {info['coupon_code']}"
    print("✅ Test 1 passed:", info)

    # Test 2: min-order false-positive guard
    noisy = "Get ₹150 off on min order ₹399. Use SAVE150 at checkout."
    info2 = extract_deal_info(noisy)
    # ₹399 should be filtered (min order context), ₹150 is the real saving value
    assert info2["price"] != 399, f"Should NOT pick ₹399 (min order). Got {info2['price']}"
    assert info2["coupon_code"] == "SAVE150"
    print("✅ Test 2 passed (min-order guard):", info2)

    # Test 3: score_deal with weights
    score = score_deal(60, 9, 3, 999, 799)
    print(f"✅ Test 3 score: {score}/100 → {priority_badge(score)}")

    # Test 4: load_weights fallback
    wl = {"matching_rules": {"priority_weights": {"discount_percent": 0.5,
          "source_reliability": 0.2, "keyword_match_count": 0.2, "price_below_target": 0.1}}}
    w = load_weights(wl)
    assert w["discount_percent"] == 0.5
    print("✅ Test 4 passed: load_weights from watchlist")

    print(f"\nquiet hours: {is_quiet_hours()}, rapidfuzz: {HAS_RAPIDFUZZ}")
