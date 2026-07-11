"""
web_deal_monitor.py — Enterprise-Grade Web AI Deal Scout

Scrapes the internet continuously for the best Indian deals (Electronics, Food, Fashion, etc.)
and passes them through Google Gemini 3.1 for semantic filtering and scoring.
"""

import os
import re
import json
import asyncio
import aiohttp
import warnings
import feedparser
import random
import logging
from datetime import datetime, timezone
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scout.log")
    ]
)
logger = logging.getLogger("WebScraper")

import google.generativeai as genai
from dotenv import load_dotenv

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import init_db, already_alerted, mark_alerted
from notifier import send_deal_alert

load_dotenv()

SCAN_INTERVAL_SECONDS = 60  # Reduced to 1 min for fast RSS polling
MAX_BACKOFF_SECONDS = 3600   # Max 1 hour backoff on repeated failures

# Per-source failure tracking for exponential backoff
source_failures = {"reddit": 0, "desidime": 0, "smartprix": 0}
source_next_scan = {"reddit": 0, "desidime": 0, "smartprix": 0}  # epoch timestamps

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
else:
    model = None

# Standard browser headers for DesiDime
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html,*/*",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Reddit requires a very specific User-Agent format or it returns 403
REDDIT_HEADERS = {
    "User-Agent": "linux:deal-scout-bot:v2.0 (by /u/dealscout_india)",
    "Accept": "application/json",
}

# ─── AI ENGINE ────────────────────────────────────────────────────────────────

async def extract_deal_ai(text: str) -> dict:
    if not model:
        print("[WARNING] No GEMINI_API_KEY found. Skipping AI parsing.")
        return {"is_deal": False}

    prompt = f"""
    You are an elite deal-filtering AI for an Indian user. Your job is to be EXTREMELY strict.
    
    ONLY mark is_deal=true if the deal is for ONE of the following ALLOWED categories:
    
    ✅ ALLOWED CATEGORIES:
    1. ELECTRONICS / GADGETS: Earphones, headphones, speakers, Bluetooth devices, smartwatches, power banks, USB hubs, routers, SSDs, RAM, keyboards, mice, monitors, webcams, cables, adapters, hacking tools, pen drives, hard drives, laptops, mobile phones, tablets.
    2. COUPONS & DISCOUNTS: ANY discount, coupon code, or promo offer for ANY platform (Amazon, Flipkart, Myntra, Swiggy, Zomato, Dominos, BookMyShow, Paytm, PhonePe, Google Pay, etc). Accept ALL coupons and discount codes. **CRITICAL RULE**: If it is a food delivery coupon (Swiggy, Zomato, Dominos, KFC, etc), you MUST assign it a priority_score of 90-100, regardless of the discount amount!
    3. FASHION / APPAREL: T-shirts, shirts, lower, shorts, jeans, trousers, shoes, sneakers, sandals, clothing.
    4. DRY FRUITS & HEALTH: Almonds, cashews, walnuts, peanuts, raisins, dates, protein powder, whey protein, mass gainer, pre-workout supplements.
    
    ❌ REJECTED CATEGORIES (mark is_deal=false for ALL of these):
    - Kitchen appliances (mixer, grinder, cooker, utensils, gas stove)
    - Furniture, beds, sofas, mattresses
    - Books, courses, e-learning
    - Travel, flights, hotels
    - Skincare, beauty, makeup, shampoo, soap (unless tied to a massive 100% cashback)
    - Toys, baby products
    - Any non-Indian food platform (international apps)
    - Discussion posts, news, complaints, commentary
    - Anything vague where price/product is unclear

    Message:
    "{text}"
    
    Respond STRICTLY in JSON format with exactly these keys:
    {{
      "is_deal": boolean,
      "priority_score": integer, // 1 to 100. Major glitches/100% cashback = 90-100. Large coupons = 70-85. Standard = 50-70.
      "product_name": string,
      "price": integer,
      "coupon_code": string,
      "instructions": string,
      "category": string
    }}
    """
    
    try:
        response = await asyncio.to_thread(
            model.generate_content,
            prompt,
            generation_config=genai.GenerationConfig(response_mime_type="application/json")
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[AI Parsing Error]: {e}")
        return {"is_deal": False}

# ─── CORE PIPELINE ────────────────────────────────────────────────────────────

async def process_post(source: str, dedup_id: str, title: str, body: str, link: str):
    full_text = f"{title}\n{body}"
    dedup_key = f"web_deal:{source}:{dedup_id}"
    
    if already_alerted(dedup_key):
        return

    # Basic pre-filter to avoid sending absolute junk to AI to save API tokens
    lower_text = full_text.lower()
    if "question" in lower_text or "help" in lower_text or "recommend" in lower_text:
        if "deal" not in lower_text and "offer" not in lower_text and "coupon" not in lower_text:
            return

    deal_info = await extract_deal_ai(full_text)
    
    if not deal_info.get("is_deal"):
        return

    min_score = int(os.getenv("MIN_PRIORITY_SCORE", 70))
    score = deal_info.get("priority_score", 0)
    
    # KEYWORD FAST-TRACK: Instantly boost food/delivery keywords
    fast_track_keywords = ["swiggy", "zomato", "domino", "eatsure", "magicpin", "kfc", "mcdonald"]
    text_lower = full_text.lower()
    if any(kw in text_lower for kw in fast_track_keywords):
        print(f"[Web Scraper] ⚡ KEYWORD FAST-TRACK: Food keyword detected. Boosting score!")
        score = max(score, 90) # Force to 90+
        deal_info["priority_score"] = score
        deal_info["category"] = "food_coupon"

    if score < min_score:
        return

    product = deal_info.get("product_name", title[:60])
    price = deal_info.get("price", 0)
    coupon = deal_info.get("coupon_code", "")
    instructions = deal_info.get("instructions", "")
    category = deal_info.get("category", "general")

    title_prefix = "🚨 AI LOOT" if score >= 85 else "🤖 AI DEAL"
    
    sent = send_deal_alert(
        title=f"{title_prefix} [{source}] — {product}",
        body=f"{title}\n\n<b>Instructions:</b> {instructions}\n<b>Link:</b> {link}",
        channel="Web Scraper",
        category=category,
        coupon_code=coupon,
        price=price,
        discount_pct=None,
        priority_score=score,
        action_steps=[instructions] if instructions else []
    )
    
    if sent:
        mark_alerted(dedup_key, product, price, priority_score=score, category=category)
        print(f" -> Successfully alerted: {product} (Score {score})")


# ─── SCRAPERS ─────────────────────────────────────────────────────────────────

async def scrape_reddit(session: aiohttp.ClientSession):
    now = asyncio.get_event_loop().time()
    if now < source_next_scan["reddit"]:
        return  # Still in backoff period
    subs = ["indianshoppingdeals", "dealsforindia", "CouponsIndia", "Lootdealsforindia"]
    found = 0
    had_error = False
    for sub in subs:
        try:
            url = f"https://www.reddit.com/r/{sub}/new.rss"
            async with session.get(url, headers=REDDIT_HEADERS, timeout=15) as r:
                if r.status != 200:
                    had_error = True
                    continue
                xml_data = await r.text()
                feed = feedparser.parse(xml_data)
                for entry in feed.entries:
                    import time
                    if hasattr(entry, 'published_parsed'):
                        entry_time = time.mktime(entry.published_parsed)
                        age = datetime.now(timezone.utc).timestamp() - entry_time
                        if age > 1800:
                            continue
                    found += 1
                    await process_post(
                        source=f"Reddit/{sub}",
                        dedup_id=entry.id,
                        title=entry.title,
                        body=entry.get('summary', ''),
                        link=entry.link
                    )
            await asyncio.sleep(2)
        except Exception:
            had_error = True
    if had_error:
        source_failures["reddit"] += 1
        backoff = min(SCAN_INTERVAL_SECONDS * (2 ** source_failures["reddit"]), MAX_BACKOFF_SECONDS)
        source_next_scan["reddit"] = asyncio.get_event_loop().time() + backoff
    else:
        source_failures["reddit"] = 0  # Reset on success
    if found > 0:
        print(f"[Web Scraper] Reddit RSS: {found} fresh posts processed")

async def scrape_smartprix(session: aiohttp.ClientSession):
    now = asyncio.get_event_loop().time()
    if now < source_next_scan["smartprix"]:
        return
    found = 0
    had_error = False
    try:
        url = "https://www.smartprix.com/bytes/feed/"
        # SmartPrix blocks default aiohttp agent, so use a real one
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        async with session.get(url, headers=headers, timeout=15) as r:
            if r.status != 200:
                had_error = True
            else:
                xml_data = await r.text()
                feed = feedparser.parse(xml_data)
                for entry in feed.entries:
                    import time
                    if hasattr(entry, 'published_parsed'):
                        entry_time = time.mktime(entry.published_parsed)
                        age = datetime.now(timezone.utc).timestamp() - entry_time
                        if age > 1800:
                            continue
                    found += 1
                    await process_post(
                        source="SmartPrix",
                        dedup_id=entry.get('id', entry.link),
                        title=entry.title,
                        body=entry.get('summary', ''),
                        link=entry.link
                    )
    except Exception:
        had_error = True
        
    if had_error:
        source_failures["smartprix"] += 1
        base_backoff = SCAN_INTERVAL_SECONDS * (2 ** source_failures["smartprix"])
        jitter = random.uniform(0.8, 1.2)
        backoff = min(base_backoff * jitter, MAX_BACKOFF_SECONDS)
        source_next_scan["smartprix"] = asyncio.get_event_loop().time() + backoff
        logger.warning(f"[Web Scraper] SmartPrix RSS encountered an error. Backing off for {backoff:.1f}s (includes jitter)")
    else:
        source_failures["smartprix"] = 0
    if found > 0:
        logger.info(f"[Web Scraper] SmartPrix RSS: {found} fresh deals processed")

async def scrape_desidime(session: aiohttp.ClientSession):
    now = asyncio.get_event_loop().time()
    if now < source_next_scan["desidime"]:
        return  # Still in backoff period
    try:
        url = "https://www.desidime.com/sdm_data/home_page_deals?page=1"
        async with session.get(url, headers=HEADERS, timeout=10) as r:
            if r.status != 200:
                raise Exception(f"HTTP {r.status}")
            data = await r.json(content_type=None)
            deals = data if isinstance(data, list) else data.get("deals", [])
            if deals:
                print(f"[Web Scraper] DesiDime: {len(deals[:10])} deals found")
            for deal in deals[:10]:
                await process_post(
                    source="DesiDime",
                    dedup_id=str(deal.get("id", "")),
                    title=deal.get("title", "") or deal.get("name", ""),
                    body=deal.get("description", "") or "",
                    link=deal.get("url", "https://www.desidime.com")
                )
        source_failures["desidime"] = 0  # Reset on success
    except Exception:
        source_failures["desidime"] += 1
        backoff = min(SCAN_INTERVAL_SECONDS * (2 ** source_failures["desidime"]), MAX_BACKOFF_SECONDS)
        source_next_scan["desidime"] = asyncio.get_event_loop().time() + backoff
        # Silently back off — no error spam

async def scan_all_sources():
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            scrape_reddit(session),
            scrape_smartprix(session),
            scrape_desidime(session),
            return_exceptions=True
        )

async def run_forever():
    init_db()
    print(f"🚀 WEB AI SCRAPER ONLINE — Scanning every {SCAN_INTERVAL_SECONDS // 60} minutes")
    while True:
        try:
            await scan_all_sources()
        except Exception:
            pass  # Never crash — silently continue
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

if __name__ == "__main__":
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print("\n[*] Web Scraper shut down.")
