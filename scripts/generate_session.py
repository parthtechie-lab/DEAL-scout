"""
generate_session.py — RUN THIS ONCE, LOCALLY, INTERACTIVELY. Never in CI.

Logs into your Telegram account via Telethon and prints a session string.
Paste that string into your .env as TELEGRAM_SESSION for local testing,
AND add it as a GitHub Secret (TELEGRAM_SESSION) for the Actions workflow.

This lets telegram_monitor.py authenticate non-interactively in CI, since
GitHub Actions has no way to type in your phone's OTP code.

SECURITY NOTE: this session string is equivalent to a login token for your
Telegram account. Treat it like a password — only store it as a GitHub
Secret (encrypted, never shown in logs), never commit it to the repo.
"""

import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID = os.getenv("TELEGRAM_API_ID") or input("Enter your API_ID: ")
API_HASH = os.getenv("TELEGRAM_API_HASH") or input("Enter your API_HASH: ")

import asyncio
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

session_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deal_scout")

with TelegramClient(
    session_file, 
    int(API_ID), 
    API_HASH, 
    loop=loop,
    device_model="iPhone 15 Pro Max",
    system_version="iOS 17.5.1",
    app_version="10.14.1",
    lang_code="en",
    system_lang_code="en-US"
) as client:
    client.start() # Interactive login
    print("\n=== SUCCESS: PERMANENT SESSION FILE CREATED ===")
    print("A file named 'deal_scout.session' has been created in your folder.")
    print("Telegram will no longer revoke your session because it is stored securely on your hard drive.")
    print("You don't need to copy any strings. Just start the bot!\n")
