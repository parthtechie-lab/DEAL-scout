"""
food_realtime_monitor.py — Enterprise Real-Time Food Coupon Hunter

Scrapes the internet every 60 seconds for the best Indian food & grocery coupons.
Sources covered:
  1. Reddit: r/IndiaDeals, r/CouponsIndia, r/india
  2. DesiDime.com (largest Indian deal community)
  3. GrabOn.in (India's top coupon aggregator)
  4. Direct offer pages: Swiggy, Zomato, Dominos, Blinkit, Zepto, JioMart, BigBasket

Runs as an async loop alongside telegram_streamer.py.
"""

import os
import re
import json
import asyncio
import aiohttp
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timezone
from dotenv import load_dotenv

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import init_db, already_alerted, mark_alerted
from notifier import send_deal_alert

load_dotenv()

SCAN_INTERVAL_SECONDS = 60  # Rescan every 60 seconds

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,*/*",
    "Accept-Language": "en-IN,en;q=0.9",
}

FOOD_KEYWORDS = [
    "swiggy", "swigy", "swiggy one", "swiggy instamart", "instamart", "instamrt",
    "zomato", "zomto", "zomato gold", 
    "dominos", "dominoes", "domino's",
    "blinkit", "blinkt", "grofers", 
    "zepto", "zepto pass", 
    "jiomart", "jio mart", "smart bazaar",
    "bigbasket", "big basket", "bb", "bbnow", "bbdaily",
    "dunzo", "dunzo daily", "magicpin", "ondc", "eats",
    "food coupon", "food offer", "delivery free", "cashback",
    "off on", "flat ₹", "flat rs", "promo code", "coupon code",
    "free delivery", "50% off", "60% off", "70% off", "trynew"
]

REJECT_KEYWORDS = [
    "kitchen", "mixer", "furniture", "course", "flight", "hotel",
    "skincare", "shampoo", "mattress", "toy", "book", "education"
]

# ─── SOURCES ──────────────────────────────────────────────────────────────────

REDDIT_SUBREDDITS = [
    "IndiaDeals",
    "CouponsIndia",
    "india",
    "frugalIndia",
    "SwiggyDeals",
    "ZomatoDeals",
]

DESIDIME_FOOD_URL = "https://www.desidime.com/deals?filter=food-and-grocery"
DESIDIME_API     = "https://www.desidime.com/sdm_data/home_page_deals?filter=food-and-grocery&page=1"

GRABON_API       = "https://www.grabon.in/indias-best-coupons-and-offers.json?category=food-and-restaurants"

# Direct coupon endpoints of the platforms
DIRECT_SOURCES = [
    {"name": "Swiggy",       "url": "https://www.swiggy.com/offers"},
    {"name": "Zomato",       "url": "https://www.zomato.com/promo-codes"},
    {"name": "Dominos",      "url": "https://www.dominos.co.in/offers"},
    {"name": "Blinkit",      "url": "https://blinkit.com/offers"},
    {"name": "BigBasket",    "url": "https://www.bigbasket.com/offers"},
]

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def is_food_deal(text: str) -> bool:
    t = text.lower()
    if any(r in t for r in REJECT_KEYWORDS):
        return False
    if not any(k in t for k in FOOD_KEYWORDS):
        return False
        
    return True

def extract_coupons(text: str) -> list[str]:
    return re.findall(r'\b[A-Z0-9]{4,15}\b', text)

def extract_price(text: str) -> int:
    m = re.search(r'[₹rs\.]\s*(\d+)', text, re.IGNORECASE)
    return int(m.group(1)) if m else 0

def score_food_deal(text: str) -> int:
    t = text.lower()
    score = 60
    if "glitch" in t or "loot" in t or "price error" in t:
        score = 95
    elif "free delivery" in t and any(p in t for p in ["swiggy","zomato","blinkit","zepto"]):
        score = 80
    elif "100%" in t and "cashback" in t:
        score = 90
    elif "70%" in t or "80%" in t:
        score = 82
    elif "50%" in t or "60%" in t:
        score = 72
    elif "coupon" in t or "promo" in t:
        score = 68
    return score

def fire_alert(title: str, body: str, coupon: str, price: int, score: int, source: str):
    dedup_key = f"food_realtime:{source}:{title[:60]}"
    if already_alerted(dedup_key):
        return
    sent = send_deal_alert(
        title=title,
        body=body,
        channel=source,
        category="food_coupon",
        coupon_code=coupon,
        price=price,
        discount_pct=None,
        priority_score=score,
        action_steps=[]
    )
    if sent:
        mark_alerted(dedup_key, title, price, priority_score=score, category="food_coupon")
        print(f"[Food Monitor] ✅ Alerted: {title[:60]} (Score: {score})")

# ─── SCRAPERS ─────────────────────────────────────────────────────────────────

async def scrape_reddit(session: aiohttp.ClientSession):
    """Scan Indian deal subreddits for food coupons using Reddit's public JSON API."""
    for sub in REDDIT_SUBREDDITS:
        try:
            url = f"https://www.reddit.com/r/{sub}/new.json?limit=20"
            async with session.get(url, headers={**HEADERS, "Accept": "application/json"}) as r:
                if r.status != 200:
                    continue
                data = await r.json()
                posts = data.get("data", {}).get("children", [])
                for post in posts:
                    d = post.get("data", {})
                    title = d.get("title", "")
                    body  = d.get("selftext", "")
                    full  = f"{title} {body}"
                    created = d.get("created_utc", 0)
                    # Only look at posts from last 10 mins
                    age = datetime.now(timezone.utc).timestamp() - created
                    if age > 600:
                        continue
                    if not is_food_deal(full):
                        continue
                    score   = score_food_deal(full)
                    coupons = extract_coupons(full)
                    coupon  = coupons[0] if coupons else ""
                    price   = extract_price(full)
                    link    = f"https://reddit.com{d.get('permalink','')}"
                    fire_alert(
                        title=f"🍕 Reddit Food Deal — {title[:60]}",
                        body=f"{full[:300]}\n\n🔗 {link}",
                        coupon=coupon, price=price, score=score,
                        source=f"reddit/{sub}"
                    )
        except Exception as e:
            print(f"[Food Monitor] Reddit/{sub} error: {e}")

async def scrape_desidime(session: aiohttp.ClientSession):
    """Scrape DesiDime food & grocery section (largest Indian deal forum)."""
    try:
        async with session.get(DESIDIME_API, headers=HEADERS, timeout=10) as r:
            if r.status != 200:
                return
            data = await r.json(content_type=None)
            deals = data if isinstance(data, list) else data.get("deals", [])
            for deal in deals[:20]:
                title = deal.get("title", "") or deal.get("name", "")
                body  = deal.get("description", "") or ""
                full  = f"{title} {body}"
                if not is_food_deal(full):
                    continue
                score  = score_food_deal(full)
                coupon = deal.get("coupon_code", "")
                price  = extract_price(full)
                link   = deal.get("url", "https://www.desidime.com")
                fire_alert(
                    title=f"🍕 DesiDime — {title[:60]}",
                    body=f"{body[:300]}\n\n🔗 {link}",
                    coupon=coupon, price=price, score=score,
                    source="desidime"
                )
    except Exception as e:
        print(f"[Food Monitor] DesiDime error: {e}")

async def scrape_grabon(session: aiohttp.ClientSession):
    """Scrape GrabOn India's food coupon API."""
    try:
        async with session.get(GRABON_API, headers=HEADERS, timeout=10) as r:
            if r.status != 200:
                return
            data = await r.json(content_type=None)
            coupons = data if isinstance(data, list) else data.get("coupons", [])
            for c in coupons[:20]:
                title  = c.get("title", "") or c.get("couponTitle", "")
                desc   = c.get("description", "") or c.get("couponDesc", "")
                code   = c.get("couponCode", "") or c.get("code", "")
                link   = c.get("url", "") or c.get("redirectUrl", "")
                full   = f"{title} {desc}"
                if not is_food_deal(full):
                    continue
                score = score_food_deal(full)
                price = extract_price(full)
                fire_alert(
                    title=f"🎟️ GrabOn Coupon — {title[:60]}",
                    body=f"{desc[:300]}\n\n🔗 {link}",
                    coupon=code, price=price, score=score,
                    source="grabon"
                )
    except Exception as e:
        print(f"[Food Monitor] GrabOn error: {e}")

async def scan_all_sources():
    """Run all scrapers in parallel for maximum speed."""
    print(f"[Food Monitor] 🔍 Scanning all internet sources...")
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            scrape_reddit(session),
            scrape_desidime(session),
            scrape_grabon(session),
            return_exceptions=True
        )
    print(f"[Food Monitor] ✅ Scan complete.")

async def run_forever():
    """Run the food monitor loop indefinitely, scanning every 60 seconds."""
    init_db()
    print(f"[Food Monitor] 🚀 Real-Time Internet Food Coupon Hunter ONLINE")
    print(f"[Food Monitor] Scanning: Reddit, DesiDime, GrabOn every {SCAN_INTERVAL_SECONDS}s")
    while True:
        try:
            await scan_all_sources()
        except Exception as e:
            print(f"[Food Monitor] Loop error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print("\n[Food Monitor] Stopped by user.")
