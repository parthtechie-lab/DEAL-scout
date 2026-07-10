"""
join_channels.py - Joins all channels from watchlist.json automatically using the burner session.
"""

import os
import json
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
import random

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")

async def main():
    if not API_ID:
        print("Missing TELEGRAM_API_ID. Exiting.")
        return

    # Load watchlist
    watchlist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "watchlist.json")
    with open(watchlist_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    channels_to_join = [c["handle"] for c in data.get("telegram_channels", [])]
    
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

    print("Connecting to Telegram...")
    await client.start()
    
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} {me.last_name} (@{me.username})")
    
    joined_count = 0
    for channel in channels_to_join:
        try:
            print(f"Joining {channel}...")
            await client(JoinChannelRequest(channel))
            joined_count += 1
            # Sleep 5-10 seconds to avoid spam filter
            delay = random.uniform(5.0, 10.0)
            print(f" -> Success! Sleeping {delay:.1f}s to avoid spam detection...")
            await asyncio.sleep(delay)
        except Exception as e:
            print(f" -> Failed to join {channel}: {e}")
            
    print(f"\n[+] Successfully joined {joined_count} out of {len(channels_to_join)} channels.")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
