"""
telegram_streamer.py — Enterprise-Grade Real-Time AI Deal Scout

Runs continuously in the background (0-second latency).
Listens to watched Telegram channels in real-time.
Pipes incoming messages into Google Gemini for semantic extraction.
"""

import os
import json
import asyncio
import random
import aiohttp
import warnings
import logging
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events
from telethon.sessions import StringSession

warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scout.log")
    ]
)
logger = logging.getLogger("TelegramStreamer")

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

async def extract_deal_ai(text: str, _retry: int = 0) -> dict:
    """
    Sends the message to Gemini to semantically parse the deal.
    Automatically retries on quota exceeded errors (free tier: 15 req/min).
    """
    if not model:
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
      "category": string // Must be one of: "electronics", "food_coupon", "fashion", "health_nutrition", "loot"
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
        err = str(e)
        # Auto-retry on Gemini quota exceeded (free tier: 15 req/min)
        if ("quota" in err.lower() or "429" in err or "retry" in err.lower()) and _retry < 5:
            # Extract retry delay from error message, default to 60s
            match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', err)
            wait = int(match.group(1)) + 2 if match else 65
            print(f"[Gemini] Quota hit — waiting {wait}s before retry...")
            await asyncio.sleep(wait)
            return await extract_deal_ai(text, _retry + 1)
        return {"is_deal": False}

async def catch_up_missed_messages(client, watched_channels):
    """On startup, scan the last 30 minutes of messages from all channels so we never miss deals."""
    print("[Telegram] Running startup catch-up scan (last 30 min)...")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    caught = 0
    for handle in watched_channels:
        try:
            msgs = await client.get_messages(handle, limit=10)
            for m in msgs:
                if not m.text:
                    continue
                msg_time = m.date.replace(tzinfo=timezone.utc) if m.date.tzinfo is None else m.date
                if msg_time < cutoff:
                    continue
                dedup_key = f"ai_deal:{handle}:{m.id}"
                if already_alerted(dedup_key):
                    continue
                deal_info = await extract_deal_ai(m.text)
                score = deal_info.get("priority_score", 0)
                min_score = int(os.getenv("MIN_PRIORITY_SCORE", 35))
                if not deal_info.get("is_deal") or score < min_score:
                    continue
                product = deal_info.get("product_name", "Unknown")
                price = deal_info.get("price")
                coupon = deal_info.get("coupon_code", "")
                instructions = deal_info.get("instructions", "")
                category = deal_info.get("category", "general")
                sent = send_deal_alert(
                    title=f"🤖 AI DEAL — {product}",
                    body=f"{m.text}\n\n<b>AI Instructions:</b> {instructions}",
                    channel=f"@{handle}",
                    category=category,
                    coupon_code=coupon,
                    price=price,
                    discount_pct=None,
                    priority_score=score,
                    action_steps=[instructions] if instructions else []
                )
                if sent:
                    mark_alerted(dedup_key, product, price or 0, priority_score=score, category=category)
                    caught += 1
                    print(f"[Catch-up] Sent: {product} (score={score})")
                await asyncio.sleep(4)  # Pace Gemini calls — stay under quota
        except Exception:
            pass
    print(f"[Telegram] Catch-up complete. Sent {caught} missed deals.")

async def main():
    if not API_ID:
        print("[Telegram] Missing TELEGRAM_API_ID. Exiting.")
        return

    init_db()
    watched_channels = load_watched_channels()
    print(f"[Telegram] Starting AI Real-Time Streamer. Listening to {len(watched_channels)} channels...")

    session_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deal_scout")
    
    client = TelegramClient(
        session_file, 
        int(API_ID), 
        API_HASH,
        device_model="iPhone 15 Pro Max",
        system_version="iOS 17.5.1",
        app_version="10.14.1",
        lang_code="en",
        system_lang_code="en-US"
    )

    processing_lock = asyncio.Lock()

    @client.on(events.NewMessage)
    async def handler(event):
        if not event.chat:
            return
            
        chat_handle = getattr(event.chat, "username", "").lower() if hasattr(event.chat, "username") else ""
        if not chat_handle or chat_handle not in watched_channels:
            return

        text = event.text
        if not text:
            return

        # Anti-Ban Pacing: Process messages sequentially with a human-like delay
        async with processing_lock:
            delay = random.uniform(2.0, 5.0)
            print(f"[{chat_handle}] Received message. Simulating human read for {delay:.1f}s...")
            await asyncio.sleep(delay)
            print(f"[{chat_handle}] Parsing with AI...")
        
        # 1. AI Parsing
        deal_info = await extract_deal_ai(text)
        
        if not deal_info.get("is_deal"):
            return

        # 2. Filtering & Keyword Fast-Track
        min_score = int(os.getenv("MIN_PRIORITY_SCORE", 35))
        score = deal_info.get("priority_score", 0)
        
        # KEYWORD FAST-TRACK: Instantly boost food/delivery keywords
        fast_track_keywords = ["swiggy", "zomato", "domino", "eatsure", "magicpin", "kfc", "mcdonald"]
        text_lower = text.lower()
        if any(kw in text_lower for kw in fast_track_keywords):
            print(f"[{chat_handle}] ⚡ KEYWORD FAST-TRACK: Food keyword detected. Boosting score!")
            score = max(score, 90) # Force to 90+
            deal_info["priority_score"] = score
            deal_info["category"] = "food_coupon"

        if score < min_score:
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

    # ── SELF-HEALING RECONNECT LOOP ──────────────────────────────────────────
    # Never gives up. If Telegram disconnects for any reason, it waits and retries.
    retry_delay = 30  # seconds
    max_retry_delay = 600  # max 10 minutes between retries

    while True:
        try:
            await client.start()
            print("[Telegram] Streamer online. Waiting for real-time deals...")
            retry_delay = 30  # Reset on successful connect
            # Immediately process any deals posted in the last 30 min that we missed
            await catch_up_missed_messages(client, watched_channels)
            await client.run_until_disconnected()
            print("[Telegram] Disconnected. Reconnecting in 30s...")

        except ConnectionError as e:
            jitter = random.uniform(0.8, 1.2)
            retry_delay = min(retry_delay * 1.5 * jitter, max_retry_delay)
            logger.warning(f"[Telegram] Connection error: {e}. Retrying in {retry_delay:.1f}s...")
        except Exception as e:
            err = str(e)
            # Handle Telegram FloodWait — must wait exactly as long as Telegram says
            if "FloodWait" in err or "flood" in err.lower():
                import re as _re
                match = _re.search(r'(\d+)', err)
                wait = int(match.group(1)) + 5 if match else 60
                logger.warning(f"[Telegram] FloodWait — waiting {wait}s as required by Telegram...")
                await asyncio.sleep(wait)
                continue
            # Handle session/auth errors — notify user and stop (can't auto-fix)
            if "auth" in err.lower() or "session" in err.lower() or "password" in err.lower():
                logger.critical(f"[Telegram] FATAL AUTH ERROR: {e}")
                try:
                    from notifier import send_alert
                    send_alert("🔴 <b>TELEGRAM ENGINE DOWN</b>\nSession was revoked by Telegram. Please run <code>scripts/generate_session.py</code> to log in again.", force=True)
                except Exception:
                    pass
                return  # Stop this engine — can't recover without user action
            logger.error(f"[Telegram] Unexpected error: {e}. Retrying in 30s...")
            await asyncio.sleep(30)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, max_retry_delay)  # Exponential backoff

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Telegram] Stopped by user.")
