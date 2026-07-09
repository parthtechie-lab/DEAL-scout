# 🛒 Deal Scout v2 — ₹0 Automated Deal Tracker

An automated personal deal tracking engine for **India** that monitors Telegram deal channels, Indian subreddits, and coupon aggregators, then sends alerts directly to your Telegram DM.

Runs entirely on GitHub Actions. No server. No idle billing.

---

## ⚠️ Honest Limitations (Read Before You Use)

Before getting to the features, here's what this system **cannot** do:

| Limitation | Reality |
|---|---|
| **Flash coupons (< 5 min)** | GitHub Actions has ~40-90s startup latency. By the time the first HTTP request goes out, a viral "first 100 users" coupon is already dead. This system catches codes that last **1-24 hours**. |
| **Amazon/Flipkart price scraping** | Shared GitHub Actions IPs are flagged by Amazon's bot detection. Playwright scraping works locally but will return CAPTCHAs on CI within a few runs. Price tracking is therefore a **manual/local-only** feature. |
| **Cloudflare-protected aggregators** | GrabOn, DesiDime HTML pages, CashKaro, and Zoutons sit behind Cloudflare JS challenges. Scraping them from CI IPs returns an empty challenge page. This system uses only sources that serve plain content without JS rendering. |
| **Telegram account safety** | Telethon uses your personal account credentials. Running it for personal watchlist scraping is a grey area in Telegram's ToS. Use a dedicated secondary number, not your primary. The session can expire or get flagged. |
| **Git history & SQLite** | The deals database is stored in the **Actions cache**, NOT committed to git on every run. Committing a binary `.db` file every 30 minutes would balloon your repo into hundreds of MB within weeks. |
| **Bank offer scraping** | Bank offer pages (HDFC, ICICI, SBI) are JS-heavy with iframes and session tokens. This system does a keyword-match on whatever plain text loads — treat alerts as "worth checking manually," not confirmed offers. |

---

## What It Actually Does Well

- ✅ **Monitors 16+ Telegram deal channels** in real-time (Telethon, no scraping — direct API access)
- ✅ **Detects coupon codes** from CF-free aggregators (DesiDime RSS, Hutti, Couponzania, Dealsmagnet) and official platform pages (Dominos)
- ✅ **Scans Indian Reddit deal subs** via the public `.json` API (no auth required)
- ✅ **Deduplicates** across all sources using fuzzy matching — one alert per deal, not ten
- ✅ **Suppresses alerts** 22:00–08:00 IST (quiet hours)
- ✅ **Sends failure alerts** to your Telegram if a run crashes silently
- ✅ **Zero cost** if the repo is public (Actions minutes unlimited)

---

## Architecture

```
GitHub Actions Schedule
│
├── Every 15 min → Coupon Detector (~30s, pure Python, no browser)
│     └── Checks DesiDime RSS + Hutti + Couponzania + Dominos direct page
│         + Telegram public channel web previews
│
├── Every 60 min → Full Scan (~3-4 min, no Playwright)
│     ├── Telegram channel monitor (Telethon direct API)
│     ├── Reddit subreddit scanner (public JSON API)
│     └── Coupon detector (same as above)
│
├── Monday 03:00 UTC → Bank offer keyword check (10 banks)
│
└── Manual trigger (workflow_dispatch) → Full scan + optional Playwright
      ├── Price tracker (Amazon/Flipkart — only works locally/with proxies)
      └── Deep food coupon scraper (15 aggregators × 7 platforms, Playwright)

DB: SQLite, stored in GitHub Actions cache (NOT committed to git)
Alerts: Telegram Bot API (your personal DM)
```

---

## GitHub Actions Minutes (Honest Calculation)

| Run type | Frequency | Duration | Min/day |
|---|---|---|---|
| Coupon scan | 96×/day (15-min) | ~30s | ~48 min |
| Full scan | 24×/day (hourly) | ~3-4 min | ~90 min |
| Bank check | 1×/week | ~2 min | negligible |
| **Total/month** | | | **~4,100 min** |

- **Public repo** → unlimited free minutes ✅
- **Private repo** → 2,000 min/month free. You'll exceed it. Either make the repo public, or reduce full scan to every 2 hours (~2,900 min/month — just under limit).

---

## 13 Categories Tracked

Electronics, Electrical, Food & Delivery, Sports & Gym, Fashion, Beauty, Home & Kitchen, Appliances, Mobiles, Baby & Kids, Books, Travel, General. All configurable in `watchlist.json`.

---

## Platforms with Coupon Detection

| Platform | Sources |
|---|---|
| 🛵 Swiggy | DesiDime RSS, Hutti, Couponzania, Telegram channels |
| 🍕 Zomato | DesiDime RSS, Hutti, Couponzania, Telegram channels |
| ⚡ Blinkit | DesiDime RSS, Couponzania, Telegram channels |
| 🟣 Zepto | DesiDime RSS, Hutti, Telegram channels |
| 🛒 BigBasket | DesiDime RSS, Hutti, Telegram channels |
| 🍕 Dominos | DesiDime RSS, Official offer page (co.in), Telegram channels |
| 📦 Swiggy Instamart | DesiDime RSS, Telegram channels |

---

## Alert Format

```
🎟️ COUPON DETECTED — Zomato
━━━━━━━━━━━━━━━━━━
🍕 Platform: Zomato
🔑 Code: WELCOME200
💰 Flat ₹200 off on orders above ₹399
📍 Source: DesiDime
💡 Stack it: Zomato Gold + HDFC Diners Club (5X rewards) + this coupon code

✅ Open app → Cart → Apply → Pay with bank card
⚡ Verify the code is still valid before ordering.
```

---

## Setup

### 1. Fork / Clone this repo (recommend: make it public)

### 2. Get credentials

| Credential | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | `@BotFather` on Telegram → `/newbot` |
| `TELEGRAM_CHAT_ID` | `@userinfobot` on Telegram |
| `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` | [my.telegram.org](https://my.telegram.org) → API Dev Tools |
| `TELEGRAM_SESSION` | `python scripts/generate_session.py` (one-time, needs phone OTP) |

### 3. Generate Telegram session (local, one-time)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in API_ID and API_HASH
python scripts/generate_session.py
# Copy the printed session string
```

### 4. Add GitHub Secrets

Repo → **Settings → Secrets → Actions → New secret**:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`

### 5. Customise `watchlist.json`

- `telegram_channels` — add/remove channels, set `reliability_score` (1-10)
- `products` — product name, keywords, URL, `target_price`, category
- `reddit_subreddits` — add/remove subs with reliability scores
- `matching_rules` — tune scoring weights, quiet hours, dedup window
- `food_platforms` — custom stacking tips per platform

### 6. Push and enable Actions

Go to the **Actions** tab → enable workflows. The system runs automatically.

---

## Total Monthly Cost: ₹0 (public repo)
 
