"""
food_coupon_scraper.py — scrapes live, working coupon codes for food/grocery
platforms (Swiggy, Zomato, Blinkit, Zepto, BigBasket, Dominos) from public
coupon aggregator websites (DesiDime, GrabOn, CouponDunia).

These sites are public HTML pages — no login required, no ToS violation.
We read their deal listing pages (not the apps themselves), parse the codes,
deduplicate against our DB, and fire a Telegram alert for any new code.

This runs inside GitHub Actions (Python 3.11 + Playwright available).
For local runs on Python 3.14 it will gracefully skip if Playwright is missing.
"""

import asyncio
import os
import re
from datetime import datetime

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ModuleNotFoundError:
    HAS_PLAYWRIGHT = False

from db import already_alerted, mark_alerted
from notifier import send_alert

# ─────────────────────────────────────────────────────────────────────────────
# Target pages — all are public coupon listing pages
# ─────────────────────────────────────────────────────────────────────────────
FOOD_PLATFORMS = [
    {
        "name": "Swiggy",
        "emoji": "🛵",
        "color": "orange",
        "pages": [
            "https://www.desidime.com/stores/swiggy-coupons",
            "https://www.grabon.in/swiggy-coupons/",
        ],
    },
    {
        "name": "Zomato",
        "emoji": "🍕",
        "color": "red",
        "pages": [
            "https://www.desidime.com/stores/zomato-coupons",
            "https://www.grabon.in/zomato-coupons/",
        ],
    },
    {
        "name": "Blinkit",
        "emoji": "⚡",
        "color": "yellow",
        "pages": [
            "https://www.desidime.com/stores/blinkit-coupons",
            "https://www.grabon.in/blinkit-coupons/",
        ],
    },
    {
        "name": "Zepto",
        "emoji": "🟣",
        "color": "purple",
        "pages": [
            "https://www.desidime.com/stores/zepto-coupons",
            "https://www.grabon.in/zepto-coupons/",
        ],
    },
    {
        "name": "BigBasket",
        "emoji": "🛒",
        "color": "green",
        "pages": [
            "https://www.desidime.com/stores/bigbasket-coupons",
            "https://www.grabon.in/big-basket-coupons/",
        ],
    },
    {
        "name": "Dominos",
        "emoji": "🍕",
        "color": "blue",
        "pages": [
            "https://www.desidime.com/stores/dominos-coupons",
            "https://www.grabon.in/dominos-coupons/",
        ],
    },
]

# CSS selectors that grab deal/coupon cards on DesiDime and GrabOn
SELECTORS = {
    "desidime.com": {
        "card": ".deal-card, .coupon-card, article.deal, .offer-card, li.deal-listing",
        "title": ".deal-card__title, h2, h3, .deal-title, .offer-title",
        "code": ".coupon-code, .code, [class*='coupon'], [class*='code']",
        "discount": ".discount, .offer-text, .deal-card__desc, p",
    },
    "grabon.in": {
        "card": ".coupon-box, .offer-box, .deal-card, article, .coupon-listing",
        "title": "h3, h2, .coupon-title, .offer-heading",
        "code": ".copy-code, .coupon-code, [class*='coupon-code'], [data-clipboard-text]",
        "discount": ".coupon-desc, .offer-desc, p, .description",
    },
}


def extract_domain(url: str) -> str:
    for domain in SELECTORS:
        if domain in url:
            return domain
    return ""


def clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip())


async def scrape_page(page, url: str, platform_name: str) -> list[dict]:
    """Scrape a single coupon page and return list of found deals."""
    deals = []
    domain = extract_domain(url)
    if not domain:
        return deals

    sel = SELECTORS[domain]

    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)  # let JS render

        cards = await page.query_selector_all(sel["card"])
        if not cards:
            # Fallback: try reading all text and extract coupon-like patterns
            body_text = await page.inner_text("body")
            codes = re.findall(r'\b[A-Z0-9]{5,15}\b', body_text)
            discount_patterns = re.findall(
                r'(?:flat\s+)?(?:₹\s*\d+|\d+%)\s*(?:off|cashback|discount)',
                body_text, re.IGNORECASE
            )
            if codes and discount_patterns:
                deals.append({
                    "title": f"{platform_name} Coupon",
                    "code": codes[0],
                    "discount": discount_patterns[0] if discount_patterns else "",
                    "source": url,
                })
            return deals

        for card in cards[:10]:  # top 10 coupons per page
            try:
                title_el = await card.query_selector(sel["title"])
                title = clean_text(await title_el.inner_text()) if title_el else ""

                code_el = await card.query_selector(sel["code"])
                code = ""
                if code_el:
                    code = clean_text(await code_el.inner_text())
                    # also check data attributes
                    data_code = await code_el.get_attribute("data-clipboard-text")
                    if data_code:
                        code = data_code.strip()
                # fallback: regex any ALLCAPS word in title
                if not code:
                    m = re.search(r'\b([A-Z0-9]{5,15})\b', title)
                    if m:
                        code = m.group(1)

                disc_el = await card.query_selector(sel["discount"])
                discount = clean_text(await disc_el.inner_text()) if disc_el else ""

                if title or discount:
                    deals.append({
                        "title": title[:120],
                        "code": code[:30],
                        "discount": discount[:200],
                        "source": url,
                    })
            except Exception:
                continue

    except Exception as e:
        print(f"[food_coupon] Error scraping {url}: {e}")

    return deals


async def run_food_coupon_scraper():
    if not HAS_PLAYWRIGHT:
        print("[food_coupon] Playwright not available — skipping food coupon scrape.")
        return

    print("[food_coupon] Starting food platform coupon scrape...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 10; SM-G973F) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            locale="en-IN",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        page = await context.new_page()

        for platform in FOOD_PLATFORMS:
            all_deals = []
            for url in platform["pages"]:
                deals = await scrape_page(page, url, platform["name"])
                all_deals.extend(deals)

            # Deduplicate and alert
            seen_codes = set()
            alerted_count = 0
            for deal in all_deals:
                code = deal.get("code", "")
                title = deal.get("title", "")

                # Build a unique key — use code if available, else title
                dedup_key = f"food:{platform['name']}:{code or title}"

                if code in seen_codes:
                    continue
                if code:
                    seen_codes.add(code)

                if already_alerted(dedup_key):
                    continue

                # Build the alert message
                code_line = f"🎟️ <b>Code:</b> <code>{code}</code>\n" if code else ""
                discount_snippet = deal["discount"][:150] if deal["discount"] else ""

                alert_text = (
                    f"{platform['emoji']} <b>{platform['name']} Deal Found!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📢 {deal['title']}\n"
                    f"{code_line}"
                    f"💰 {discount_snippet}\n\n"
                    f"✅ <b>How to use:</b>\n"
                    f"1. Open {platform['name']} app\n"
                    f"2. Add items to cart\n"
                    f"3. Apply code at checkout{': <code>' + code + '</code>' if code else ''}\n"
                    f"4. Stack with bank offer (SBI/HDFC/ICICI) for max savings\n\n"
                    f"🔗 <a href='{deal['source']}'>See all {platform['name']} coupons</a>"
                )

                send_alert(alert_text)
                mark_alerted(dedup_key, f"{platform['name']} coupon: {title}", 0)
                alerted_count += 1

            print(f"[food_coupon] {platform['name']}: found {len(all_deals)} deals, "
                  f"sent {alerted_count} new alerts")

        await browser.close()

    print("[food_coupon] Done.")


def run():
    asyncio.run(run_food_coupon_scraper())


if __name__ == "__main__":
    run()
