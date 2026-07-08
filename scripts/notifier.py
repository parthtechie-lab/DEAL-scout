"""
notifier.py — sends the final alert to YOUR Telegram (via the Bot API).

This is deliberately the ONLY step that reaches you. Everything upstream
(discovery, price check, verification) runs silently; this module is what
turns a verified deal into a message on your phone.

Does NOT click cashback links, does NOT apply coupons, does NOT check out.
Those stay manual by design (see the "unsolvable cashback problem" — cashback
portals ban bots that auto-click their tracked links).
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_alert(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("[notifier] Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — skipping send.")
        print("[notifier] Message would have been:\n", text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[notifier] Failed to send alert: {resp.status_code} {resp.text}")


def format_deal_message(product_name: str, price: int, historic_low: int,
                         url: str, source: str, coupon_hint: str = None,
                         category: str = None, reason: str = None) -> str:
    """
    Builds the alert text. Deliberately includes a manual "double check coupon"
    reminder — per the earlier plan's pro-tip that aggregator extensions
    sometimes miss a better code than what they auto-apply.
    """
    savings = historic_low - price
    savings_pct = int((savings / historic_low) * 100) if historic_low > price else 0

    lines = [
        f"🚨 <b>DEAL ALERT</b> — {product_name}",
        f"💰 Price: ₹{price:,}  |  90-day low: ₹{historic_low:,}",
    ]
    if savings > 0:
        lines.append(f"📉 Saving: ₹{savings:,} ({savings_pct}% off historic low)")
    if reason:
        lines.append(f"✅ Reason: {reason}")
    if category:
        lines.append(f"🏷️ Category: {category.title()}")
    lines += [
        f"🛒 Source: {source.title()}",
        f"🔗 <a href=\"{url}\">Open Product Page</a>",
        "",
        "<b>Savings checklist:</b>",
        "1. Activate cashback on CashKaro or GrabOn first",
        "2. Add to cart — coupon extension will auto-test codes",
        f"3. Google: \"{product_name} coupon code today\"",
        "4. Check your bank's credit card offer page",
        "5. Checkout through the cashback-tracked link",
    ]
    if coupon_hint:
        lines.insert(5, f"🎟️ Coupon visible on page: <b>{coupon_hint}</b>")
    return "\n".join(lines)



if __name__ == "__main__":
    # Quick manual test — run `python scripts/notifier.py` after setting .env
    msg = format_deal_message(
        product_name="Test Product",
        price=999,
        historic_low=1200,
        url="https://example.com",
        source="manual test",
    )
    send_alert(msg)
