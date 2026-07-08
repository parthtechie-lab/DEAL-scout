"""
db.py — Deal Scout v2 SQLite helper.

Tables:
  price_history  — every price ever recorded per product (for 90-day low).
  alerts_sent    — dedup log; prevents sending the same alert twice.
                   Indexed on (dedup_key, sent_at) for fast lookups.
  deal_titles    — cross-channel fuzzy dedup store (6-hour rolling window).
  source_stats   — tracks last successful match + total match count per source.
                   Used by the weekly staleness check to flag dead sources.

Fix log:
  v2.1 — Added source_stats table for staleness detection (issue #7).
  v2.1 — Added record_source_match() and get_stale_sources() helpers.
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

        -- Alert dedup log
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
        CREATE INDEX IF NOT EXISTS idx_alerts_dedup
            ON alerts_sent(dedup_key, sent_at);

        -- Cross-channel title dedup (6-hour rolling window)
        CREATE TABLE IF NOT EXISTS deal_titles (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            source     TEXT NOT NULL,
            alerted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deal_titles_time
            ON deal_titles(alerted_at);

        -- Source health tracking (issue #7: staleness detection)
        CREATE TABLE IF NOT EXISTS source_stats (
            source_name      TEXT    PRIMARY KEY,
            source_type      TEXT    NOT NULL DEFAULT 'telegram',
            last_match_at    TEXT,
            total_matches    INTEGER NOT NULL DEFAULT 0,
            consecutive_misses INTEGER NOT NULL DEFAULT 0,
            first_seen_at    TEXT    NOT NULL
        );
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
    dedup_key:          str,
    product_name:       str,
    price:              int,
    priority_score:     int = 0,
    discount_percent:   int = 0,
    source_reliability: int = 0,
    category:           str = "",
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
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT title FROM deal_titles WHERE alerted_at >= ?",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def record_deal_title(title: str, source: str):
    conn = get_connection()
    conn.execute(
        "INSERT INTO deal_titles (title, source, alerted_at) VALUES (?, ?, ?)",
        (title, source, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ── Source health tracking (issue #7) ────────────────────────────────────────

def record_source_match(source_name: str, source_type: str = "telegram"):
    """Call this every time a source produces at least one real match/alert."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO source_stats (source_name, source_type, last_match_at,
                                   total_matches, consecutive_misses, first_seen_at)
        VALUES (?, ?, ?, 1, 0, ?)
        ON CONFLICT(source_name) DO UPDATE SET
            last_match_at      = excluded.last_match_at,
            total_matches      = total_matches + 1,
            consecutive_misses = 0
        """,
        (source_name, source_type, now, now),
    )
    conn.commit()
    conn.close()


def record_source_miss(source_name: str, source_type: str = "telegram"):
    """Call when a source is scanned but produces zero matches this run."""
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO source_stats (source_name, source_type, last_match_at,
                                   total_matches, consecutive_misses, first_seen_at)
        VALUES (?, ?, NULL, 0, 1, ?)
        ON CONFLICT(source_name) DO UPDATE SET
            consecutive_misses = consecutive_misses + 1
        """,
        (source_name, source_type, now),
    )
    conn.commit()
    conn.close()


def get_stale_sources(days_threshold: int = 14) -> list[dict]:
    """
    Returns sources that haven't produced a match in `days_threshold` days,
    or have been checked but NEVER produced a match.
    Used by the weekly staleness report alert.
    """
    conn = get_connection()
    cutoff = (datetime.utcnow() - timedelta(days=days_threshold)).isoformat()
    rows = conn.execute(
        """
        SELECT source_name, source_type, last_match_at,
               total_matches, consecutive_misses
        FROM source_stats
        WHERE last_match_at IS NULL OR last_match_at < ?
        ORDER BY consecutive_misses DESC, last_match_at ASC
        """,
        (cutoff,),
    ).fetchall()
    conn.close()
    return [
        {
            "source":            r[0],
            "type":              r[1],
            "last_match_at":     r[2],
            "total_matches":     r[3],
            "consecutive_misses":r[4],
        }
        for r in rows
    ]


if __name__ == "__main__":
    init_db()
    print(f"[db] v2.1 schema initialised at {DB_PATH}")
