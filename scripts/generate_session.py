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

with TelegramClient(StringSession(), int(API_ID), API_HASH) as client:
    session_string = client.session.save()
    print("\n=== COPY THIS INTO .env as TELEGRAM_SESSION and into your GitHub Secret ===\n")
    print(session_string)
    print("\n=== Do not share this string with anyone or commit it to git ===\n")
