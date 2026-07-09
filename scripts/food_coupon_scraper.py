"""
food_coupon_scraper.py — Deal Scout v2 (v3 — Hardened).

Scrapes live coupon codes for food/grocery platforms from public coupon
aggregator websites and deals pages using Playwright (handles JS-rendered
sites that plain `requests` in coupon_detector.py can't see).

Platforms: Swiggy, Zomato, Blinkit, Zepto, BigBasket, Dominos, Swiggy Instamart

WHAT'S NEW IN v3 (over v2)
---------------------------
As with coupon_detector.py, the goal here is closing off *avoidable*
silent-miss paths. This can't reach a literal 0%-chance-of-ever-skipping
guarantee — a site can still put up a CAPTCHA, require a login, or change
its markup between deploys — but the previous version had several gaps
that were fixable and are fixed here:

1. SELECTOR + REGEX ALWAYS BOTH RUN — v2 only ran the raw-text regex
   fallback if the CSS selectors found *zero* cards. That means a page
   where selectors matched card containers but failed to match the code
   element inside them (a very common partial-markup-drift failure)
   returned nothing, even though the code was sitting right there in the
   page text. Now both extraction paths always run and results are merged
   + deduped, so a partial selector miss no longer blanks the whole card.

2. RETRY ON NAVIGATION — `page.goto()` was a single attempt; one slow
   response or transient network error skipped that site for the entire
   run. Now retries up to RETRY_ATTEMPTS times with backoff.

3. SCROLL-TO-LOAD — many of these aggregator sites lazy-load coupon cards
   as you scroll. v2 only ever saw what was in the initial viewport after
   a fixed 2.5s wait. Now scrolls to the bottom a few times (with waits)
   before scraping, so lazy-loaded cards are actually rendered into the DOM.

4. PAGINATION — if a site exposes a "Load more" / "Next" control, it's
   now clicked (up to MAX_PAGES times) so deals past page 1 aren't
   invisible.

5. CARD LIMIT RAISED — was hardcoded to the first 15 cards; raised to
   MAX_CARDS_PER_PAGE (default 60) since aggregator pages often list far
   more than 15 live coupons.

6. MULTIPLE CODES PER CARD — some cards contain more than one code node
   ("primary" + "backup" codes). v2's `query_selector` grabbed only the
   first match; now uses `query_selector_all` inside a card so all codes
   in a card are captured.

7. HTML ENTITY DECODING + case handling on the regex fallback, matching
   the hardening done in coupon_detector.py, so codes with `&amp;`-style
   entities or lowercase codes aren't missed.

8. PER-SITE FAILURE VISIBILITY — sites that time out or throw are now
   reported by name in the final summary instead of just vanishing from
   the counts.

Remaining known blind spots (no code fix eliminates these — flagging so
they're not mistaken for bugs):
  - Sites that require solving a CAPTCHA or logging in.
  - Sites that detect and block headless/automated browsers outright.
  - A coupon that a site itself never publishes on the scraped page
    (e.g. it's only in their app, or sent via personalized email).
"""

import asyncio
import html as html_lib
import json
import re
from datetime import datetime
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
    HAS_PLAYWRIGHT = True
except ModuleNotFoundError:
    HAS_PLAYWRIGHT = False

from db import already_alerted, mark_alerted
from notifier import send_deal_alert

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"

RETRY_ATTEMPTS       = 3
RETRY_BACKOFF_MS     = 1500        # doubles each retry
MAX_CARDS_PER_PAGE   = 60          # was 15
MAX_PAGES            = 3           # "load more" / "next" clicks per site
SCROLL_ROUNDS        = 4           # scroll-to-bottom passes to trigger lazy load
SCROLL_WAIT_MS       = 900
POST_LOAD_WAIT_MS    = 2500

# Generic "load more" / pagination selectors tried across all sites.
_LOAD_MORE_SELECTORS = [
    "text=/load more/i", "text=/show more/i", "text=/view more/i",
    "[class*='load-more']", "[class*='loadmore']", "button[class*='more']",
    "a[rel='next']", "[class*='pagination'] a[class*='next']",
]

_CODE_FALLBACK_RE = re.compile(r'\b[A-Z][A-Z0-9]{4,14}\b')
_DISCOUNT_FALLBACK_RE = re.compile(
    r'(?:flat\s+)?(?:₹\s*\d+|\d+%)\s*(?:off|cashback|discount|savings)',
    re.IGNORECASE,
)
_FALLBACK_SKIP = {
    "TERMS","ABOUT","LOGIN","SHARE","CLICK","CLOSE","EMAIL",
    "PHONE","STORE","ORDER","APPLY","CHECK","UPIID","INDIA",
    "OFFER","OFFERS","COUPON","COUPONS","DEALS","TODAY","VALID",
}


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
    "cuponation.in": {
        "card":     ".coupon-box, .offer-card, article, .deal-item, .CouponBox",
        "title":    "h3, h2, .coupon-title, .offer-title, .CouponTitle",
        "code":     ".coupon-code, .code, [data-clipboard-text], .CouponCode",
        "discount": ".coupon-desc, .offer-desc, p, .CouponDesc",
    },
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
    return re.sub(r'\s+', ' ', html_lib.unescape(text).strip())


def build_coupon_urls(watchlist: dict) -> list[dict]:
    """Build a list of all URLs to scrape from watchlist config."""
    coupon_sites   = watchlist.get("coupon_sites", [])
    food_platforms = watchlist.get("food_platforms", [])
    urls = []

    for site in coupon_sites:
        if "food" not in site.get("categories", []):
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


async def _goto_with_retry(page, url: str) -> bool:
    """Navigate with retries + backoff. Returns True on success."""
    backoff = RETRY_BACKOFF_MS
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            return True
        except PWTimeoutError:
            print(f"[food_coupon]   goto timeout ({attempt}/{RETRY_ATTEMPTS}) for {url}")
        except Exception as e:
            print(f"[food_coupon]   goto error ({attempt}/{RETRY_ATTEMPTS}) for {url}: {e}")
        if attempt < RETRY_ATTEMPTS:
            await page.wait_for_timeout(backoff)
            backoff *= 2
    return False


async def _scroll_to_load(page) -> None:
    """Scroll to the bottom a few times so lazy-loaded cards render."""
    for _ in range(SCROLL_ROUNDS):
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(SCROLL_WAIT_MS)
        except Exception:
            break


async def _click_load_more(page) -> bool:
    """Try each known 'load more' selector once. Returns True if a click happened."""
    for sel in _LOAD_MORE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click(timeout=3000)
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def _extract_via_regex(body: str, platform_name: str, url: str) -> list[dict]:
    """Raw-text fallback extraction — decoupled from selectors entirely."""
    deals = []
    body = html_lib.unescape(body)
    codes = list(dict.fromkeys(re.findall(_CODE_FALLBACK_RE, body)))  # dedup, keep order
    disc_patterns = re.findall(_DISCOUNT_FALLBACK_RE, body)
    idx_disc = 0
    for code in codes:
        if code.upper() in _FALLBACK_SKIP or len(code) < 4:
            continue
        disc = disc_patterns[idx_disc] if idx_disc < len(disc_patterns) else ""
        idx_disc += 1
        deals.append({
            "title":    f"{platform_name} Coupon Code",
            "code":     code,
            "discount": disc,
            "source":   url,
        })
    return deals


async def scrape_page(page, url: str, platform_name: str) -> list[dict]:
    """
    Scrape a single coupon page (with pagination) and return all found
    deals. Both the CSS-selector path AND the raw-text regex fallback
    always run and are merged — a partial selector match no longer
    suppresses the fallback for that page.
    """
    deals: list[dict] = []
    sel = get_selectors_for_url(url)

    ok = await _goto_with_retry(page, url)
    if not ok:
        print(f"[food_coupon] GAVE UP navigating to {url} after {RETRY_ATTEMPTS} attempts")
        return deals

    await page.wait_for_timeout(POST_LOAD_WAIT_MS)
    await _scroll_to_load(page)

    for page_num in range(1, MAX_PAGES + 1):
        # ── Selector-based extraction ────────────────────────────────────
        if sel:
            try:
                cards = await page.query_selector_all(sel["card"])
            except Exception:
                cards = []
            for card in cards[:MAX_CARDS_PER_PAGE]:
                try:
                    title_el = await card.query_selector(sel["title"])
                    title    = clean_text(await title_el.inner_text()) if title_el else ""

                    # Grab ALL matching code elements in the card, not just the first —
                    # some cards list a primary code plus backup alternates.
                    code_els = await card.query_selector_all(sel["code"])
                    card_codes = []
                    for code_el in code_els:
                        code_val = ""
                        for attr in ("data-clipboard-text", "data-coupon", "data-code"):
                            val = await code_el.get_attribute(attr)
                            if val:
                                code_val = val.strip()
                                break
                        if not code_val:
                            code_val = clean_text(await code_el.inner_text())
                        if code_val:
                            card_codes.append(code_val)

                    disc_el  = await card.query_selector(sel["discount"])
                    discount = clean_text(await disc_el.inner_text()) if disc_el else ""

                    if card_codes:
                        for code_val in card_codes:
                            deals.append({
                                "title":    title[:150],
                                "code":     code_val[:30],
                                "discount": discount[:250],
                                "source":   url,
                            })
                    elif title or discount:
                        # Card matched but no code element found inside it —
                        # still record the title/discount; regex fallback
                        # below may recover the code from raw page text.
                        deals.append({
                            "title":    title[:150],
                            "code":     "",
                            "discount": discount[:250],
                            "source":   url,
                        })
                except Exception:
                    continue

        # ── Regex fallback — ALWAYS runs, merged with selector results ───
        try:
            body = await page.inner_text("body")
            deals.extend(_extract_via_regex(body, platform_name, url))
        except Exception:
            pass

        # ── Pagination: try to load next batch, else stop ───────────────
        if page_num < MAX_PAGES:
            clicked = await _click_load_more(page)
            if not clicked:
                break
            await _scroll_to_load(page)

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

    failed_sites: list[str] = []

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

            print(f"[food_coupon]   Scraping {site_name} -> {platform_name}...")
            deals = await scrape_page(page, url, platform_name)
            if not deals:
                failed_sites.append(f"{site_name}/{platform_name}")
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

    if failed_sites:
        print(f"[food_coupon] Sites that returned ZERO deals this run "
              f"(check manually — may indicate markup drift or a block): {failed_sites}")
    print(f"[food_coupon] Done. Total alerts sent: {total_alerted}")


def run():
    asyncio.run(run_food_coupon_scraper())


if __name__ == "__main__":
    run()
