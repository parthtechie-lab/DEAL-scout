# 🛒 Deal Scout — ₹0 Automated Deal Tracker

Automated deal tracker for **India** running entirely on GitHub Actions — no server, no cost, no idle billing.

Monitors Telegram deal channels + Amazon/Flipkart product pages every hour.  
Sends Telegram alerts when a **verified** deal is found.

---

## Categories Tracked

| Category | What it catches |
|---|---|
| 📱 **Electronics** | Earphones, chargers, smartwatches, TVs, laptops, routers, cameras |
| 🔌 **Electrical** | LED bulbs, extension cords, switches, fans, inverters, stabilizers |
| 🍽️ **Food** | Dal, oils, ghee, coffee, tea, spices, dry fruits, protein |
| 🏋️ **Sports** | Cricket/football gear, gym equipment, yoga mats, sportswear |

---

## How It Works

```
GitHub Actions (cron: every 60 min)
   ↓
Python script runs in a fresh container
   ↓
1. Telethon scans Telegram deal channels (2-layer matching)
2. Playwright scrapes your watchlist product URLs for price drops
3. Compares against 90-day SQLite history (catches fake "sale" inflation)
4. Dedup check — no repeat alerts within 24h
   ↓
Sends Telegram alert if a real deal is found
   ↓
Container shuts down (you pay ₹0)
```

---

## Setup (One-Time, ~15 mins)

### 1. Fork / Clone this repo

### 2. Get your credentials

| Credential | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Message `@BotFather` on Telegram → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message `@userinfobot` on Telegram |
| `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` | [my.telegram.org](https://my.telegram.org) → API Dev Tools |
| `TELEGRAM_SESSION` | Run `python scripts/generate_session.py` locally (see below) |

### 3. Generate your Telegram session string

```bash
# Clone the repo, install deps
pip install -r requirements.txt

# Copy env template
cp .env.example .env
# Fill in TELEGRAM_API_ID and TELEGRAM_API_HASH in .env

# Generate session (one-time, interactive — needs your phone OTP)
python scripts/generate_session.py
# → Prints a long session string. Copy it.
```

### 4. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

Add each of: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`

### 5. Customise your watchlist

Edit [`watchlist.json`](watchlist.json):
- Add/remove **Telegram channels** to monitor
- Add/remove **products** with their Amazon/Flipkart URLs and your target price
- Adjust **category_keywords** to tune what counts as a deal signal

### 6. Push & enable Actions

Push to GitHub. Go to **Actions** tab and enable workflows.  
The cron runs automatically every hour. You can also trigger it manually from the Actions tab.

---

## Watchlist Format

```json
{
  "telegram_channels": ["@lootdeals", "@dealsninja"],
  "products": [
    {
      "name": "boAt Rockerz 450",
      "keywords": ["boat rockerz", "rockerz 450"],
      "url": "https://www.amazon.in/dp/B07QT6RP3D",
      "platform": "amazon",
      "target_price": 999,
      "category": "electronics"
    }
  ],
  "category_keywords": {
    "electronics": ["earphone", "charger", "tv", "laptop"],
    "food": ["dal", "oil", "ghee", "coffee"]
  }
}
```

---

## Alert Example

```
🚨 DEAL ALERT — 📱 boAt Rockerz 450 Bluetooth Headphone
💰 Price: ₹899  |  90-day low: ₹999
📉 Saving: ₹100 (10% off historic low)
✅ Reason: 90-day low!
🏷️ Category: Electronics
🛒 Source: Amazon
🔗 Open Product Page

Savings checklist:
1. Activate cashback on CashKaro or GrabOn first
2. Add to cart — coupon extension will auto-test codes
3. Google: "boAt Rockerz 450 coupon code today"
4. Check your bank's credit card offer page
5. Checkout through the cashback-tracked link
```

---

## Free Tier Notes

- **GitHub Actions**: 2000 min/month (private repo) / unlimited (public repo). Hourly run ≈ 2-3 min = ~1500 min/month. Safe margin on public repos; use every 90 min on private repos.
- **Telegram Bot API**: Free, no meaningful limits.
- **Telethon**: Free, uses your own account credentials.
- **Playwright + SQLite**: Runs inside the Action container, no external cost.

**Total monthly cost: ₹0**
