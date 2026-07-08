"""
food_coupon_scraper.py — Deal Scout v2.

Scrapes live coupon codes for food/grocery platforms from 15 public coupon
aggregator websites and deals pages.

Platforms: Swiggy, Zomato, Blinkit, Zepto, BigBasket, Dominos, Swiggy Instamart

Sites scraped (v2 additions in ★):
  DesiDime, GrabOn, CashKaro, Zoutons, CouponDunia, FreeKaaMaal,
  IndiaDesire, GoPaisa, Picodi, CouponZania, Hutti, Dealsmagnet,
  ★ CupoNation, ★ Amazon Today's Deals, ★ Flipkart Offers Zone

v2 changes:
  • CupoNation selector added.
  • Stacking tips sourced from watchlist (richer wording).
  • send_deal_alert() used for priority badge in messages.
"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ModuleNotFoundError:
    HAS_PLAYWRIGHT = False

from db import already_alerted, mark_alerted
from notifier import send_deal_alert

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"


def load_watchlist():
    with open(WATCHLIST_PATH) as f:
        return json.load(f)


# ── CSS selectors per aggregator site ─────────────────────────────────────────
SITE_SELECTORS = {
    "desidime.com": {
        "card":     ".deal-card, .coupon-card, article.deal, .offer-card, li.deal-listing, .store-coupon",
        "title":    ".deal-card__title, h2, h3, .deal-title, .offer-title, .coupon-title",
        "code":     ".coupon-code, .code, [class*='coupon-code'], [data-clipboard-text]",
        "discount": ".discount, .offer-text, .deal-card__desc, p, .coupon-desc",
    },
    "grabon.in": {
        "card":     ".coupon-box, .offer-box, .deal-card, article, .coupon-listing, .cpn-box",
        "title":    "h3, h2, .coupon-title, .offer-heading, .cpn-head",
        "code":     ".copy-code, .coupon-code, [class*='coupon-code'], [data-clipboard-text], .cpn-code",
        "discount": ".coupon-desc, .offer-desc, p, .description, .cpn-desc",
    },
    "cashkaro.com": {
        "card":     ".coupon-card, .offer-card, .deal-item, article, .store-offer",
        "title":    "h3, h2, .coupon-title, .offer-title",
        "code":     ".coupon-code, .code-text, [data-coupon], [data-clipboard-text]",
        "discount": ".coupon-desc, .offer-desc, p",
    },
    "zoutons.com": {
        "card":     ".coupon-card, .deal-card, .offer-box, article, .coupon-item",
        "title":    "h3, h2, .coupon-title, .deal-title",
        "code":     ".coupon-code, .code, [data-clipboard-text], .promo-code",
        "discount": ".coupon-desc, .deal-desc, p, .offer-text",
    },
    "coupondunia.in": {
        "card":     ".coupon-card, .offer-card, .deal-box, article, .store-coupon",
        "title":    "h3, h2, .coupon-title, .offer-heading",
        "code":     ".coupon-code, .code, [data-code], [data-clipboard-text]",
        "discount": ".coupon-desc, .offer-desc, p",
    },
    "freekaamaal.com": {
        "card":     ".deal-card, .coupon-box, .offer-item, article, .deal-listing",
        "title":    "h3, h2, .deal-title, .offer-title",
        "code":     ".coupon-code, .code, [data-clipboard-text]",
        "discount": ".deal-desc, .offer-desc, p",
    },
    "indiadesire.com": {
        "card":     ".coupon-card, .deal-card, article, .offer-box",
        "title":    "h3, h2, .coupon-title, .deal-title",
        "code":     ".coupon-code, .code, [data-clipboard-text]",
        "discount": ".coupon-desc, .deal-desc, p",
    },
    "gopaisa.com": {
        "card":     ".coupon-card, .offer-card, article, .deal-item",
        "title":    "h3, h2, .coupon-title",
        "code":     ".coupon-code, .code, [data-clipboard-text]",
        "discount": ".coupon-desc, .offer-desc, p",
    },
    "picodi.com": {
        "card":     ".coupon-card, .offer-card, article, .deal-card",
        "title":    "h3, h2, .coupon-title, .offer-title",
        "code":     ".coupon-code, .code, [data-clipboard-text]",
        "discount": ".coupon-desc, .offer-desc, p",
    },
    "couponzania.com": {
        "card":     ".coupon-card, .deal-card, article, .offer-item",
        "title":    "h3, h2, .coupon-title",
        "code":     ".coupon-code, .code, [data-clipboard-text]",
        "discount": ".coupon-desc, .deal-desc, p",
    },
    "hutti.in": {
        "card":     ".coupon-card, .deal-card, article, .offer-box",
        "title":    "h3, h2, .coupon-title",
        "code":     ".coupon-code, .code, [data-clipboard-text]",
        "discount": ".coupon-desc, .deal-desc, p",
    },
    "dealsmagnet.com": {
        "card":     ".coupon-card, .deal-card, article, .offer-item",
        "title":    "h3, h2, .coupon-title, .deal-title",
        "code":     ".coupon-code, .code, [data-clipboard-text]",
        "discount": ".coupon-desc, .deal-desc, p",
    },
    # ★ New in v2
    "cuponation.in": {
        "card":     ".coupon-box, .offer-card, article, .deal-item, .CouponBox",
        "title":    "h3, h2, .coupon-title, .offer-title, .CouponTitle",
        "code":     ".coupon-code, .code, [data-clipboard-text], .CouponCode",
        "discount": ".coupon-desc, .offer-desc, p, .CouponDesc",
    },
    # ★ Amazon & Flipkart direct deals pages (no platform slug needed)
    "amazon.in/deals": {
        "card":     ".DealCard, [data-component-type='s-deal-result-item'], .a-section.octopus-dlp-asin-section",
        "title":    "h2, .a-size-base-plus, .DealContent__title, span.a-text-bold",
        "code":     ".promoCodeButton, [data-coupon-code]",
        "discount": ".a-offscreen, .DealContent__priceInfo, .a-price-fraction",
    },
    "flipkart.com/offers": {
        "card":     "._3O0U0u, .tUxRFH, ._1AtVbE, .CXW8mj",
        "title":    "._4rR01T, .IRpwTa, a.s1Q9rs, ._2WkVRV",
        "code":     "[data-coupon], .couponCode, ._3xFQZJ",
        "discount": "._3Ay6Sb, .VGWI6T, ._3I9_wc",
    },
}


def get_selectors_for_url(url: str) -> dict | None:
    for domain, sel in SITE_SELECTORS.items():
        if domain in url:
            return sel
    return None


def clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text.strip())


def build_coupon_urls(watchlist: dict) -> list[dict]:
    """Build a list of all URLs to scrape from watchlist config."""
    coupon_sites   = watchlist.get("coupon_sites", [])
    food_platforms = watchlist.get("food_platforms", [])
    urls = []

    for site in coupon_sites:
        if "food" not in site.get("categories", []):
            # Non-food sites: still scrape for general electronics/fashion etc.
            # but skip food platform slug iteration
            base_url = site.get("base_url", "")
            pattern  = site.get("url_pattern", "{base_url}")
            if "{platform}" not in pattern:
                urls.append({
                    "url":           pattern.replace("{base_url}", base_url),
                    "site_name":     site["name"],
                    "platform_name": "General",
                    "platform_emoji":"🛒",
                    "stacking_tip":  "",
                })
            continue

        base_url = site.get("base_url", "")
        pattern  = site.get("url_pattern", "{base_url}")

        if "{platform}" not in pattern:
            # Fixed URL (e.g. Amazon Today's Deals)
            urls.append({
                "url":           pattern.replace("{base_url}", base_url),
                "site_name":     site["name"],
                "platform_name": site["name"],
                "platform_emoji":"🛒",
                "stacking_tip":  "",
            })
        else:
            for platform in food_platforms:
                slug = platform["slug"]
                url  = pattern.replace("{base_url}", base_url).replace("{platform}", slug)
                urls.append({
                    "url":           url,
                    "site_name":     site["name"],
                    "platform_name": platform["name"],
                    "platform_emoji":platform["emoji"],
                    "stacking_tip":  platform.get("stacking_tip", ""),
                })
    return urls


async def scrape_page(page, url: str, platform_name: str) -> list[dict]:
    """Scrape a single coupon page and return list of found deals."""
    deals = []
    sel   = get_selectors_for_url(url)

    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        if sel:
            cards = await page.query_selector_all(sel["card"])
            for card in cards[:15]:
                try:
                    title_el = await card.query_selector(sel["title"])
                    title    = clean_text(await title_el.inner_text()) if title_el else ""

                    code = ""
                    code_el = await card.query_selector(sel["code"])
                    if code_el:
                        for attr in ("data-clipboard-text", "data-coupon", "data-code"):
                            val = await code_el.get_attribute(attr)
                            if val:
                                code = val.strip()
                                break
                        if not code:
                            code = clean_text(await code_el.inner_text())

                    disc_el  = await card.query_selector(sel["discount"])
                    discount = clean_text(await disc_el.inner_text()) if disc_el else ""

                    if title or discount:
                        deals.append({
                            "title":    title[:150],
                            "code":     code[:30] if code else "",
                            "discount": discount[:250],
                            "source":   url,
                        })
                except Exception:
                    continue

        # Fallback: regex extraction from raw page body
        if not deals:
            try:
                body = await page.inner_text("body")
                codes = list(set(re.findall(r'\b[A-Z][A-Z0-9]{4,14}\b', body)))
                disc_patterns = re.findall(
                    r'(?:flat\s+)?(?:₹\s*\d+|\d+%)\s*(?:off|cashback|discount|savings)',
                    body, re.IGNORECASE,
                )
                _SKIP = {"TERMS","ABOUT","LOGIN","SHARE","CLICK","CLOSE","EMAIL",
                         "PHONE","STORE","ORDER","APPLY","CHECK","UPIID","INDIA"}
                for i, code in enumerate(codes[:5]):
                    if code in _SKIP:
                        continue
                    disc = disc_patterns[i] if i < len(disc_patterns) else ""
                    deals.append({
                        "title":    f"{platform_name} Coupon Code",
                        "code":     code,
                        "discount": disc,
                        "source":   url,
                    })
            except Exception:
                pass

    except Exception as e:
        print(f"[food_coupon] Error scraping {url}: {e}")

    return deals


async def run_food_coupon_scraper():
    if not HAS_PLAYWRIGHT:
        print("[food_coupon] Playwright not available — skipping food coupon scrape.")
        return

    watchlist      = load_watchlist()
    urls_to_scrape = build_coupon_urls(watchlist)
    food_platforms = {p["name"]: p for p in watchlist.get("food_platforms", [])}

    print(f"[food_coupon] Starting scrape — {len(urls_to_scrape)} URLs across "
          f"{len(food_platforms)} food platforms...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            locale="en-IN",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )
        page = await context.new_page()

        platform_deals: dict[str, list] = {}
        for item in urls_to_scrape:
            platform_name = item["platform_name"]
            url           = item["url"]
            site_name     = item["site_name"]

            print(f"[food_coupon]   Scraping {site_name} → {platform_name}...")
            deals = await scrape_page(page, url, platform_name)
            for deal in deals:
                deal["site_name"] = site_name
            platform_deals.setdefault(platform_name, []).extend(deals)

        # Deduplicate and alert per platform
        total_alerted = 0
        for platform_name, all_deals in platform_deals.items():
            platform_info = food_platforms.get(platform_name, {})
            emoji         = platform_info.get("emoji", "🛒")
            stacking_tip  = platform_info.get("stacking_tip", "")
            seen_codes    = set()
            alerted_count = 0

            for deal in all_deals:
                code      = deal.get("code", "")
                title     = deal.get("title", "")
                site_name = deal.get("site_name", "")

                dedup_key = f"food:{platform_name}:{code or title}:{site_name}"

                if code and code in seen_codes:
                    continue
                if code:
                    seen_codes.add(code)
                if already_alerted(dedup_key):
                    continue

                discount_snippet = deal["discount"][:200] if deal["discount"] else ""

                action_steps = [
                    f"Open {platform_name} app",
                    f"Add items to cart",
                    f"Apply code: <code>{code}</code>" if code else "Check offers tab",
                    "Pay with bank card for extra discount",
                ]

                sent = send_deal_alert(
                    title=f"{emoji} {platform_name} Deal Found!",
                    body=f"📢 {title}\n💰 {discount_snippet}\n📍 Found on: {site_name}",
                    channel=site_name,
                    category="food",
                    coupon_code=code,
                    priority_score=55,   # food coupons are always medium-high value
                    stacking_tip=stacking_tip,
                    product_url=deal["source"],
                    action_steps=action_steps,
                )

                if sent:
                    mark_alerted(dedup_key, f"{platform_name}: {title}", 0,
                                 priority_score=55, category="food")
                    alerted_count += 1

            total_alerted += alerted_count
            print(f"[food_coupon] {platform_name}: {len(all_deals)} deals found, "
                  f"{alerted_count} new alerts sent")

        await browser.close()

    print(f"[food_coupon] Done. Total alerts sent: {total_alerted}")


def run():
    asyncio.run(run_food_coupon_scraper())


if __name__ == "__main__":
    run()
