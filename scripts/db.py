"""
db.py — Deal Scout v2 SQLite helper.

Tables:
  price_history  — every price ever recorded per product (for 90-day low).
  alerts_sent    — dedup log; prevents sending the same alert twice.
                   v2 adds: priority_score, discount_percent, source_reliability.
  deal_titles    — lightweight cross-channel dedup store; keeps the title of
                   every deal alerted in the last 6 hours so the matcher can
                   fuzzy-check incoming deals before triggering a new alert.

The DB lives at data/deals.db and is committed back to the repo by the
GitHub Actions workflow after each run (see .github/workflows/deal-scout.yml).
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
        -- Price history for 90-day low tracking
        CREATE TABLE IF NOT EXISTS price_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT    NOT NULL,
            url          TEXT    NOT NULL,
            price        INTEGER NOT NULL,
            in_stock     INTEGER NOT NULL DEFAULT 1,
            checked_at   TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_product
            ON price_history(product_name, checked_at);

        -- Alert dedup log (v2: added priority_score, discount_percent, source_reliability)
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key          TEXT    NOT NULL UNIQUE,
            product_name       TEXT    NOT NULL,
            price              INTEGER,
            priority_score     INTEGER DEFAULT 0,
            discount_percent   INTEGER DEFAULT 0,
            source_reliability INTEGER DEFAULT 0,
            category           TEXT    DEFAULT '',
            sent_at            TEXT    NOT NULL
        );

        -- Cross-channel title dedup (6-hour rolling window)
        CREATE TABLE IF NOT EXISTS deal_titles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            source     TEXT NOT NULL,
            alerted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deal_titles_time
            ON deal_titles(alerted_at);
        """
    )
    conn.commit()
    conn.close()


# ── Price history ─────────────────────────────────────────────────────────────

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
    """Real 90-day low — not a rolling average — fixes the 'fake sale' problem."""
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT MIN(price) FROM price_history "
        "WHERE product_name = ? AND checked_at >= ? AND in_stock = 1",
        (product_name, cutoff),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None


# ── Alert dedup ───────────────────────────────────────────────────────────────

def already_alerted(dedup_key: str, within_hours: int = 24) -> bool:
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(hours=within_hours)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE dedup_key = ? AND sent_at >= ?",
        (dedup_key, cutoff),
    ).fetchone()
    conn.close()
    return row is not None


def mark_alerted(
    dedup_key: str,
    product_name: str,
    price: int,
    priority_score: int = 0,
    discount_percent: int = 0,
    source_reliability: int = 0,
    category: str = "",
):
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO alerts_sent "
        "(dedup_key, product_name, price, priority_score, discount_percent, "
        " source_reliability, category, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dedup_key, product_name, price, priority_score,
            discount_percent, source_reliability, category,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


# ── Cross-channel title dedup ─────────────────────────────────────────────────

def get_recent_deal_titles(hours: int = 6) -> list[str]:
    """Returns all deal titles alerted in the last `hours` hours."""
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT title FROM deal_titles WHERE alerted_at >= ?",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def record_deal_title(title: str, source: str):
    """Persist a deal title for cross-channel dedup lookups."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO deal_titles (title, source, alerted_at) VALUES (?, ?, ?)",
        (title, source, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"[db] v2 schema initialised at {DB_PATH}")
