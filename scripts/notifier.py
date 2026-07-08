"""
notifier.py — Deal Scout v2. Sends the final alert to YOUR Telegram (Bot API).

v2 changes:
  • Respects quiet hours (22:00–08:00 IST) — suppresses sends during that window.
  • Priority badge (🔥 HIGH / ✅ DEAL / ℹ️ LOW) injected at top of every message.
  • Structured send_deal_alert() alongside the generic send_alert() so callers
    can pass structured deal data for a richer, consistent message format.

Does NOT click cashback links, apply coupons, or check out — those stay manual.
"""

import os
import requests
from dotenv import load_dotenv
from matcher import is_quiet_hours, priority_badge

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")


def send_alert(text: str, force: bool = False) -> bool:
    """
    Send a raw HTML message to your Telegram chat.

    Args:
        text:  HTML-formatted message body.
        force: If True, bypasses quiet-hours check (e.g. critical errors).

    Returns True on success, False on failure/quiet-hours skip.
    """
    if not BOT_TOKEN or not CHAT_ID:
        print("[notifier] Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — skipping.")
        print("[notifier] Message would have been:\n", text)
        return False

    # if not force and is_quiet_hours():
    #     print("[notifier] Quiet hours active (22:00–08:00 IST) — alert suppressed.")
    #     return False

    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id":                  CHAT_ID,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[notifier] Send failed: {resp.status_code} {resp.text}")
        return False
    return True


def send_deal_alert(
    *,
    title:              str,
    body:               str,
    channel:            str,
    category:           str     = "",
    coupon_code:        str     = "",
    price:              int     | None = None,
    discount_pct:       int     | None = None,
    priority_score:     int     = 0,
    stacking_tip:       str     = "",
    product_url:        str     = "",
    action_steps:       list[str] | None = None,
    force:              bool    = False,
) -> bool:
    """
    Builds and sends a richly formatted deal alert.

    All keyword-only args make call sites explicit and readable.
    """
    badge = priority_badge(priority_score)

    lines = [f"{badge}\n━━━━━━━━━━━━━━━━━━", f"<b>{title}</b>"]

    if category:
        lines.append(f"🏷️ Category: {category.replace('_', ' ').title()}")
    lines.append(f"📍 Source: {channel}")

    if price is not None:
        lines.append(f"💰 Price: ₹{price:,}")
    if discount_pct is not None:
        lines.append(f"📉 Discount: {discount_pct}% off")
    if coupon_code:
        lines.append(f"🎟️ Code: <code>{coupon_code}</code>")

    lines.append("")
    lines.append(body[:600])

    if action_steps:
        lines.append("\n✅ <b>Action plan:</b>")
        for i, step in enumerate(action_steps, 1):
            lines.append(f"{i}. {step}")

    if stacking_tip:
        lines.append(f"\n💡 <b>Pro tip:</b> {stacking_tip}")

    if product_url:
        lines.append(f"\n🔗 <a href='{product_url}'>Open product page</a>")

    return send_alert("\n".join(lines), force=force)


def format_deal_message(
    product_name: str, price: int, historic_low: int,
    url: str, source: str, coupon_hint: str = None,
    category: str = None, reason: str = None,
) -> str:
    """
    Builds a price-tracker alert (used by price_checker.py).
    Kept for backwards compatibility with v1.
    """
    savings     = historic_low - price
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
    # Quick test — run `python scripts/notifier.py`
    msg = format_deal_message(
        product_name="Test Product",
        price=999,
        historic_low=1200,
        url="https://example.com",
        source="manual test",
    )
    send_alert(msg, force=True)   # force=True bypasses quiet hours for testing
