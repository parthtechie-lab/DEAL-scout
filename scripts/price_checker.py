"""
price_checker.py — checks current price + stock status for each product URL
in watchlist.json, records it in SQLite, and alerts if it's a genuine
90-day low (not just "on sale").

Uses Playwright (headless Chromium) because Amazon/Flipkart render prices
via JS in ways that plain requests+BeautifulSoup often can't reach reliably.

CSS selectors are kept in SELECTORS dict — if a site changes markup (which
Amazon/Flipkart do ~quarterly), only this dict needs updating, not the logic.

Test locally with `python scripts/price_checker.py` before trusting alerts.
"""

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from db import record_price, get_historic_low, already_alerted, mark_alerted
from notifier import send_alert, format_deal_message

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"

# Platform selectors. Keep updated — Amazon/Flipkart change markup ~quarterly.
# Test with: python scripts/price_checker.py
SELECTORS = {
    "amazon": {
        # Primary price (whole number part). Fallback handles "Deal" price labels.
        "price": [
            "span.a-price-whole",
            "span#priceblock_ourprice",
            "span#priceblock_dealprice",
            ".a-price .a-offscreen",
        ],
        "out_of_stock_text": [
            "Currently unavailable",
            "out of stock",
            "We don't know when or if this item will be back in stock",
        ],
        "coupon_hint": [
            "span.couponBadge",           # "₹X off with coupon" badge
            "label[id*='couponText']",
            "span[id*='coupon']",
        ],
    },
    "flipkart": {
        "price": [
            "div._30jeq3",                # current main price
            "div._16Jk6d",                # discounted price on product cards
            "div.UOCQB1",                 # price on some listing pages
        ],
        "out_of_stock_text": [
            "Sold Out",
            "out of stock",
            "Notify Me",
        ],
        "coupon_hint": [
            "div._3xFhiH",               # "Use code XXXX" hint
            "span._2A_DCg",
        ],
    },
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


def parse_price(text: str):
    """Extract integer price from strings like '₹1,299', '1299.00', etc."""
    # Remove commas and extract digits before any decimal
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def try_selectors(page, selector_list: list) -> str | None:
    """Try a list of CSS selectors, return inner_text of first match."""
    for selector in selector_list:
        try:
            el = page.query_selector(selector)
            if el:
                return el.inner_text().strip()
        except Exception:
            continue
    return None


def check_product(page, product: dict):
    platform = product["platform"]
    selectors = SELECTORS.get(platform)
    if not selectors:
        print(f"[price_checker] No selectors for platform '{platform}', skipping.")
        return

    try:
        page.goto(product["url"], timeout=40000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)  # let JS-rendered price settle
    except Exception as e:
        print(f"[price_checker] Failed to load {product['name']}: {e}")
        return

    body_text = page.inner_text("body")
    in_stock = not any(
        phrase.lower() in body_text.lower()
        for phrase in selectors["out_of_stock_text"]
    )

    price_text = try_selectors(page, selectors["price"])
    if not price_text:
        print(
            f"[price_checker] Price selector didn't match for {product['name']} "
            f"on {platform} — markup may have changed. Skipping this product."
        )
        return

    price = parse_price(price_text)
    if price is None or price <= 0:
        print(f"[price_checker] Could not parse price '{price_text}' for {product['name']}.")
        return

    # Grab coupon hint if available (best-effort, not blocking)
    coupon_hint = try_selectors(page, selectors.get("coupon_hint", []))

    record_price(product["name"], product["url"], price, in_stock=in_stock)

    print(f"[price_checker] {product['name']}: ₹{price:,} | In stock: {in_stock}"
          + (f" | Coupon: {coupon_hint}" if coupon_hint else ""))

    if not in_stock:
        return  # don't alert on out-of-stock items

    historic_low = get_historic_low(product["name"], days=90)
    target = product.get("target_price")
    category = product.get("category", "general")

    is_new_low = historic_low is not None and price <= historic_low
    hits_target = target is not None and price <= target

    if is_new_low or hits_target:
        dedup_key = f"price:{product['name']}:{price}"
        if already_alerted(dedup_key):
            return

        emoji = CATEGORY_EMOJI.get(category, "🛒")
        reason = []
        if is_new_low:
            reason.append(f"90-day low!")
        if hits_target:
            reason.append(f"hit your target of ₹{target:,}")

        msg = format_deal_message(
            product_name=f"{emoji} {product['name']}",
            price=price,
            historic_low=historic_low or price,
            url=product["url"],
            source=platform,
            coupon_hint=coupon_hint,
            category=category,
            reason=" + ".join(reason),
        )
        send_alert(msg)
        mark_alerted(dedup_key, product["name"], price)


def run():
    watchlist = load_watchlist()
    products = watchlist.get("products", [])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        page = context.new_page()

        for product in products:
            if "REPLACE_WITH" in product["url"] or "itm123" in product["url"]:
                print(f"[price_checker] Skipping placeholder URL for {product['name']}")
                continue
            try:
                check_product(page, product)
            except Exception as e:
                print(f"[price_checker] Error checking {product['name']}: {e}")

        browser.close()


if __name__ == "__main__":
    run()
