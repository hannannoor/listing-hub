"""
SQLite storage for normalised listings.

Every source adapter produces dicts in the same shape, so adding a second or
third marketplace later means writing one adapter -- nothing here changes.
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    uid                TEXT PRIMARY KEY,   -- "<source>:<external_id>"
    source             TEXT NOT NULL,      -- "g2g", and later others
    external_id        TEXT NOT NULL,
    category           TEXT NOT NULL,      -- our own label, e.g. "minecraft-accounts"
    category_label     TEXT,
    title              TEXT NOT NULL,
    url                TEXT NOT NULL,      -- link back to the original listing
    price              REAL,
    currency           TEXT,
    image_url          TEXT,
    seller             TEXT,
    seller_url         TEXT,
    seller_rating      REAL,               -- 0..1 satisfaction rate
    seller_orders      INTEGER,
    available_qty      INTEGER,
    posted_at          INTEGER,            -- unix ms, from the source
    attributes         TEXT,               -- JSON: {"Rank": "Mythical Glory", ...}
    first_seen         INTEGER NOT NULL,   -- unix ms, when we first saw it
    last_seen          INTEGER NOT NULL,   -- unix ms, last scrape that saw it
    is_active          INTEGER NOT NULL DEFAULT 1,
    raw                TEXT                -- original record, for debugging
);

CREATE INDEX IF NOT EXISTS idx_listings_source   ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category);
CREATE INDEX IF NOT EXISTS idx_listings_price    ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_seen     ON listings(last_seen);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    category    TEXT NOT NULL,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    found       INTEGER DEFAULT 0,
    added       INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    error       TEXT
);
"""

UPSERT = """
INSERT INTO listings (
    uid, source, external_id, category, category_label, title, url,
    price, currency, image_url, seller, seller_url, seller_rating,
    seller_orders, available_qty, posted_at, attributes,
    first_seen, last_seen, is_active, raw
) VALUES (
    :uid, :source, :external_id, :category, :category_label, :title, :url,
    :price, :currency, :image_url, :seller, :seller_url, :seller_rating,
    :seller_orders, :available_qty, :posted_at, :attributes, :now, :now, 1, :raw
)
ON CONFLICT(uid) DO UPDATE SET
    title         = excluded.title,
    url           = excluded.url,
    price         = excluded.price,
    currency      = excluded.currency,
    image_url     = excluded.image_url,
    seller        = excluded.seller,
    seller_url    = excluded.seller_url,
    seller_rating = excluded.seller_rating,
    seller_orders = excluded.seller_orders,
    available_qty = excluded.available_qty,
    category_label= excluded.category_label,
    attributes    = excluded.attributes,
    last_seen     = excluded.last_seen,
    is_active     = 1,
    raw           = excluded.raw
"""


def connect(db_path=None):
    db_path = db_path or DB_PATH   # resolved at call time, so tests can redirect it
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns introduced after a database was first created."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    for column, ddl in (("attributes", "TEXT"),):
        if column not in have:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {column} {ddl}")
    conn.commit()


def save_listings(conn, listings):
    """Insert or refresh a batch. Returns (added, updated)."""
    now = int(time.time() * 1000)
    added = updated = 0

    for item in listings:
        uid = f"{item['source']}:{item['external_id']}"
        exists = conn.execute(
            "SELECT 1 FROM listings WHERE uid = ?", (uid,)
        ).fetchone()

        row = {
            "uid": uid,
            "source": item["source"],
            "external_id": item["external_id"],
            "category": item["category"],
            "category_label": item.get("category_label"),
            "title": item["title"],
            "url": item["url"],
            "price": item.get("price"),
            "currency": item.get("currency"),
            "image_url": item.get("image_url"),
            "seller": item.get("seller"),
            "seller_url": item.get("seller_url"),
            "seller_rating": item.get("seller_rating"),
            "seller_orders": item.get("seller_orders"),
            "available_qty": item.get("available_qty"),
            "posted_at": item.get("posted_at"),
            "attributes": json.dumps(item.get("attributes") or {},
                                     ensure_ascii=False),
            "now": now,
            "raw": json.dumps(item.get("raw"), ensure_ascii=False, default=str),
        }
        conn.execute(UPSERT, row)
        if exists:
            updated += 1
        else:
            added += 1

    conn.commit()
    return added, updated


def mark_stale(conn, source, category, cutoff_ms):
    """Flag listings in this category we did not see in the latest run."""
    cur = conn.execute(
        "UPDATE listings SET is_active = 0 "
        "WHERE source = ? AND category = ? AND last_seen < ? AND is_active = 1",
        (source, category, cutoff_ms),
    )
    conn.commit()
    return cur.rowcount


def start_run(conn, source, category):
    cur = conn.execute(
        "INSERT INTO scrape_runs (source, category, started_at) VALUES (?, ?, ?)",
        (source, category, int(time.time() * 1000)),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, found=0, added=0, updated=0, error=None):
    conn.execute(
        "UPDATE scrape_runs SET finished_at = ?, found = ?, added = ?, "
        "updated = ?, error = ? WHERE id = ?",
        (int(time.time() * 1000), found, added, updated, error, run_id),
    )
    conn.commit()


def stats(conn):
    total = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE is_active = 1"
    ).fetchone()[0]
    by_source = conn.execute(
        "SELECT source, COUNT(*) c FROM listings WHERE is_active = 1 "
        "GROUP BY source ORDER BY c DESC"
    ).fetchall()
    return total, [dict(r) for r in by_source]
