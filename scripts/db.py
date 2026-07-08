"""
db.py — SQLite helper for Deal Scout.

Stores:
  - price_history: every price we've ever recorded per product, so we can
    compute a genuine 90-day low (not a rolling average, which is easy to
    fake with inflated "sale" prices).
  - alerts_sent: dedup log, so we don't spam the same deal every run.

The DB file lives at data/deals.db and is committed back to the repo by the
GitHub Actions workflow after each run (see .github/workflows/deal-scout.yml).
This is the "poor man's persistent database" for a $0 setup — fine at this
scale (a few thousand rows), not something to scale past personal use.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "deals.db"


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            url TEXT NOT NULL,
            price INTEGER NOT NULL,
            in_stock INTEGER NOT NULL DEFAULT 1,
            checked_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_price_product
            ON price_history(product_name, checked_at);

        CREATE TABLE IF NOT EXISTS alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT NOT NULL UNIQUE,
            product_name TEXT NOT NULL,
            price INTEGER,
            sent_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def record_price(product_name: str, url: str, price: int, in_stock: bool = True):
    conn = get_connection()
    conn.execute(
        "INSERT INTO price_history (product_name, url, price, in_stock, checked_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (product_name, url, price, int(in_stock), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_historic_low(product_name: str, days: int = 90):
    """Real 90-day low, not a rolling average — fixes the 'fake sale' problem
    where retailers inflate the pre-sale price to fake a big discount."""
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT MIN(price) FROM price_history "
        "WHERE product_name = ? AND checked_at >= ? AND in_stock = 1",
        (product_name, cutoff),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None


def already_alerted(dedup_key: str, within_hours: int = 24) -> bool:
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(hours=within_hours)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE dedup_key = ? AND sent_at >= ?",
        (dedup_key, cutoff),
    ).fetchone()
    conn.close()
    return row is not None


def mark_alerted(dedup_key: str, product_name: str, price: int):
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO alerts_sent (dedup_key, product_name, price, sent_at) "
        "VALUES (?, ?, ?, ?)",
        (dedup_key, product_name, price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {DB_PATH}")
