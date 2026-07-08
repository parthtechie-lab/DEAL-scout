# 🛒 Deal Scout v2 — ₹0 Automated Deal Tracker

An advanced, automated personal deal tracking engine for **India** running entirely on GitHub Actions — no server, no cost, no idle billing.

Version 2 introduces a **6-stage detection pipeline**, **priority scoring engine**, **cross-channel fuzzy deduplication**, and an **instant coupon detector** that catches food and grocery codes within minutes of them going live.

---

## ✨ Features (v2)

### 1. 🎟️ Instant Coupon Detector (15-min cron)
Detects NEW coupon codes the moment they drop for Swiggy, Zomato, Blinkit, Zepto, BigBasket, and Dominos. 
* **Layer A:** Regex scrapes 9 major coupon aggregators (GrabOn, DesiDime, CashKaro, Zoutons, etc.).
* **Layer B:** Direct checks of official platform offer pages (e.g., Dominos).
* **Layer C:** Live Telegram web preview parsing (finds codes posted by humans *without* needing a Telethon session).
* **Result:** Fires alerts instantly, often before the coupon limits are exhausted.

### 2. 📱 Telegram Deal Channel Monitor
Monitors 16+ verified deal channels using a personal Telegram account (Telethon).
* Filters out the noise using a 3-layer match: Product Keyword → Food Platform → Category.
* Extracts the deal price, MRP, discount percentage, and coupon codes from the raw text.

### 3. 🤖 Reddit Deal Scanner
Monitors Indian subreddits (`r/dealsforindia`, `r/GreatIndiaDeals`, etc.) using the public Reddit JSON API. 
* Avoids spam by applying strict reliability scoring and minimum upvote thresholds.

### 4. 📊 Price Tracker & 90-Day History (Amazon/Flipkart)
Uses Playwright to scrape the exact prices of products on your personal watchlist.
* Defeats the "Fake Sale" trick: Compares the live price against a true **90-day SQLite history** to ensure the discount is real.

### 5. 🍕 Deep Food Coupon Scraper
A secondary Playwright scraper that performs a deep DOM-parse on 15 aggregator websites across 7 food platforms, finding hidden or JavaScript-rendered coupons.

### 6. 🏦 Weekly Bank Card Offers Check
Checks 10 major Indian banks (HDFC, ICICI, SBI, Axis, Kotak, Amex, Yes Bank, IDFC, RBL, BoB) for changing credit/debit card offers across your tracked categories.

### 🧠 The "Matcher" Engine & Database
* **Priority Scoring:** Every detected deal gets a score (0-100) based on `discount_percentage`, `source_reliability`, `keyword_match_count`, and `price_below_target`. Deals scoring < 40 are silently dropped.
* **Cross-Channel Fuzzy Deduplication:** If Telegram, Reddit, and a coupon site all post the same deal, the `rapidfuzz` string matcher catches it and ensures you only get **one** alert. (12-hour rolling window).
* **Quiet Hours:** Suppresses non-critical alerts between 22:00 and 08:00 IST to prevent midnight spam.

---

## 🏷️ 13 Categories Tracked
Track exactly what you care about. Default categories include:
1. `electronics` (Phones, Laptops, Earbuds)
2. `electrical` (Inverters, Fans, Plugs)
3. `food` (Grocery, Delivery, Spices)
4. `sports` (Gym, Supplements, Gear)
5. `fashion` (Clothes, Shoes, Watches)
6. `beauty` (Makeup, Skincare, Grooming)
7. `home_kitchen` (Cookware, Decor, Furniture)
8. `appliances` (ACs, Fridges, Washing Machines)
9. `mobiles` (Smartphones, Cases, Screen Protectors)
10. `baby_kids` (Toys, Diapers, Clothing)
11. `books_stationery` (Books, Pens, Office Supplies)
12. `travel` (Flights, Hotels, Luggage)
13. `general` (Gift Cards, OTT Subscriptions)

---

## 🔔 Rich Telegram Alerts

Deal Scout sends beautifully formatted alerts directly to your Telegram DM, complete with priority badges and actionable stacking tips.

```text
🎟️ NEW COUPON DETECTED — Zomato!
━━━━━━━━━━━━━━━━━━
🍕 Platform: Zomato
🔑 Code: WELCOME200
💰 Flat ₹200 off on first 3 orders
📍 Source: GrabOn
💡 Pro tip: Zomato Gold + HDFC Diners Club (5X rewards) + this coupon code

✅ How to use:
1. Open Zomato app
2. Add items to cart
3. Apply code: WELCOME200
4. Pay with bank card for max savings

⚡ Act fast — codes expire quickly!
```

---

## ⚙️ How It Works (GitHub Actions Architecture)

```
GitHub Actions Schedule
   ↓
- Every 15 mins: Runs Instant Coupon Detector (Pure Python, finishes in 10s)
- Every 30 mins: Runs Full Pipeline (Telegram + Reddit + Deep Scrapers)
- Weekly (Mon): Runs Bank Card Offer Check
   ↓
Spins up Ubuntu Container
   ↓
Python executes scrapers, runs data through `matcher.py` (Scoring & Dedup)
   ↓
Valid deals sent via Telegram Bot API
   ↓
SQLite `deals.db` (History + Dedup Log) is committed back to the repo
   ↓
Container destroys itself
```

---

## 🚀 Setup (One-Time, ~15 mins)

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
# Clone the repo, setup virtualenv, install deps
python3 -m venv .venv
source .venv/bin/activate
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

Add each of: 
* `TELEGRAM_BOT_TOKEN`
* `TELEGRAM_CHAT_ID`
* `TELEGRAM_API_ID`
* `TELEGRAM_API_HASH`
* `TELEGRAM_SESSION`

### 5. Customise your watchlist

Edit [`watchlist.json`](watchlist.json). This is the brain of the operation. You can configure:
- `telegram_channels`: Add/remove channels and assign a `reliability_score` (1-10).
- `reddit_subreddits`: Track specific Indian subreddits.
- `products`: Exact Amazon/Flipkart links for price tracking, with `target_price`.
- `matching_rules`: Tune the priority scoring weights, quiet hours, and dedup windows.
- `food_platforms`: Add custom stacking tips for Swiggy, Zepto, etc.

### 6. Push & Enable Actions

Push your changes to GitHub. Go to the **Actions** tab in your repository and enable workflows.  
The system will now run autonomously.

---

## 💸 Free Tier Notes

- **GitHub Actions**: 2000 min/month on private repos. Running every 30 mins + 15 min coupon scans uses roughly ~1200 mins/month. (Completely free and unlimited if the repo is public).
- **Telegram Bot API**: Free.
- **Telethon**: Free (uses your account).
- **SQLite Database**: Stored natively in the repo, costs ₹0.

**Total monthly cost: ₹0**
