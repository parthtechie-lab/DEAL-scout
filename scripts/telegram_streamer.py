"""
telegram_streamer.py — Enterprise-Grade Real-Time AI Deal Scout

Runs continuously in the background (0-second latency).
Listens to watched Telegram channels in real-time.
Pipes incoming messages into Google Gemini for semantic extraction.
"""

import os
import json
import asyncio
import re
import aiohttp
from dotenv import load_dotenv

from telethon import TelegramClient, events
from telethon.sessions import StringSession

import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import google.generativeai as genai
from google.generativeai.types import generation_types

# Make sure local imports work
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notifier import send_deal_alert
from db import init_db, already_alerted, mark_alerted

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_STRING = os.getenv("TELEGRAM_SESSION")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
else:
    model = None

WATCHLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "watchlist.json")

def load_watched_channels():
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    channels = set()
    for ch in data.get("telegram_channels", []):
        handle = ch["handle"] if isinstance(ch, dict) else ch
        if not handle.startswith("@replace_with"):
            # store without @ for easier matching or keep it if telethon likes it
            channels.add(handle.replace("@", "").lower())
    return channels

async def unshorten_url(url: str) -> str:
    """Follow redirects to find the true destination URL to bypass affiliate trackers."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True, timeout=5) as response:
                return str(response.url)
    except Exception:
        return url # fallback to original

async def extract_deal_ai(text: str) -> dict:
    """
    Sends the message to Gemini to semantically parse the deal.
    Expects a strict JSON response.
    """
    if not model:
        print("[WARNING] No GEMINI_API_KEY found. Skipping AI parsing.")
        return {"is_deal": False}

    prompt = f"""
    You are an elite deal-filtering AI for an Indian user. Your job is to be EXTREMELY strict.
    
    ONLY mark is_deal=true if the deal is for ONE of the following ALLOWED categories:
    
    ✅ ALLOWED CATEGORIES:
    1. ELECTRONICS / GADGETS: Earphones, headphones, speakers, Bluetooth devices, smartwatches, power banks, USB hubs, routers, SSDs, RAM, keyboards, mice, monitors, webcams, cables, adapters, hacking tools, pen drives, hard drives, laptops, mobile phones, tablets.
    2. FOOD & GROCERY COUPONS: ANY discount/coupon/offer on Dominos, Swiggy, Zomato, Swiggy Instamart, Blinkit, JioMart, BigBasket, Zepto, Dunzo. Only Indian food delivery/grocery apps.
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
      "category": string // Must be one of: "electronics", "food_coupon", "fashion", "health_nutrition", "loot"
    }}
    """
    
    try:
        # We wrap in asyncio.to_thread because the genai call is currently synchronous
        response = await asyncio.to_thread(
            model.generate_content,
            prompt,
            generation_config=genai.GenerationConfig(response_mime_type="application/json")
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[AI Parsing Error]: {e}")
        return {"is_deal": False}

async def main():
    if not API_ID or not SESSION_STRING:
        print("Missing TELEGRAM_API_ID or TELEGRAM_SESSION. Exiting.")
        return

    init_db()
    watched_channels = load_watched_channels()
    print(f"[*] Starting AI Real-Time Streamer. Listening to {len(watched_channels)} channels...")

    client = TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH)

    @client.on(events.NewMessage)
    async def handler(event):
        if not event.chat:
            return
            
        # Get chat handle to verify it's in our watchlist
        chat_handle = getattr(event.chat, "username", "").lower() if hasattr(event.chat, "username") else ""
        if not chat_handle or chat_handle not in watched_channels:
            return

        text = event.text
        if not text:
            return

        print(f"[{chat_handle}] Received message in real-time. Parsing with AI...")
        
        # 1. AI Parsing
        deal_info = await extract_deal_ai(text)
        
        if not deal_info.get("is_deal"):
            print(" -> AI determined this is not a valid deal or noise. Skipping.")
            return

        # 2. Filtering
        min_score = int(os.getenv("MIN_PRIORITY_SCORE", 85))
        score = deal_info.get("priority_score", 0)
        if score < min_score:
            print(f" -> AI scored this a {score}/100 (Threshold: {min_score}). Average deal. Skipping.")
            return

        # 3. Deduplication
        dedup_key = f"ai_deal:{chat_handle}:{event.id}"
        if already_alerted(dedup_key):
            return
            
        product = deal_info.get("product_name", "Unknown Product")
        price = deal_info.get("price")
        coupon = deal_info.get("coupon_code")
        instructions = deal_info.get("instructions", "")
        category = deal_info.get("category", "general")
        
        # 4. Extract and unshorten URLs
        urls = re.findall(r'(https?://\S+)', text)
        clean_urls = []
        for u in urls:
            clean = await unshorten_url(u)
            clean_urls.append(clean)
        
        # 5. Alerting
        title_prefix = "🚨 AI LOOT DETECTED" if score >= 85 else "🤖 AI DEAL DETECTED"
        
        sent = send_deal_alert(
            title=f"{title_prefix} — {product}",
            body=f"{text}\n\n<b>AI Instructions:</b> {instructions}\n<b>Clean Links:</b> {' '.join(clean_urls)}",
            channel=f"@{chat_handle}",
            category=category,
            coupon_code=coupon,
            price=price,
            discount_pct=None,
            priority_score=score,
            action_steps=[instructions] if instructions else []
        )
        
        if sent:
            mark_alerted(dedup_key, product, price or 0, priority_score=score, category=category)
            print(f" -> Successfully alerted: {product} (Score {score})")

    await client.start()
    print("[*] Streamer online. Waiting for real-time deals... (Press Ctrl+C to stop)")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Streamer stopped by user. Shutting down gracefully.")
