"""SQLite storage: products, price history, scrape runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from shopper import DATA_DIR
from shopper.models import Product

DEFAULT_DB = DATA_DIR / "shopper.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY,
    store          TEXT NOT NULL,
    url            TEXT NOT NULL,
    sku            TEXT,
    title          TEXT NOT NULL,
    brand          TEXT,
    category       TEXT,
    gender         TEXT,
    currency       TEXT NOT NULL DEFAULT 'USD',
    sizes          TEXT,           -- comma separated
    colors         TEXT,           -- comma separated
    image_url      TEXT,
    in_stock       INTEGER NOT NULL DEFAULT 1,
    price          INTEGER NOT NULL,   -- cents
    original_price INTEGER,            -- cents, NULL if not on sale
    discount_pct   REAL NOT NULL DEFAULT 0,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    UNIQUE (store, url)
);
CREATE INDEX IF NOT EXISTS idx_products_discount ON products(discount_pct DESC);
CREATE INDEX IF NOT EXISTS idx_products_price ON products(price);

CREATE TABLE IF NOT EXISTS price_history (
    product_id     INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price          INTEGER NOT NULL,
    original_price INTEGER,
    scraped_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_product ON price_history(product_id, scraped_at);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY,
    store       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | blocked | error
    items       INTEGER NOT NULL DEFAULT 0,
    method      TEXT,
    error       TEXT
);

-- Deals with a sanity check: a markdown is "suspicious" when we have tracked the item
-- for a while and never actually saw it selling near its claimed original price.
DROP VIEW IF EXISTS current_deals;
CREATE VIEW current_deals AS
SELECT
    p.id, p.store, p.brand, p.title, p.category, p.gender,
    p.price / 100.0                    AS price,
    p.original_price / 100.0           AS original_price,
    p.discount_pct,
    p.currency, p.sizes, p.colors, p.in_stock, p.url, p.image_url, p.last_seen,
    h.n_obs,
    h.max_seen / 100.0                 AS max_price_seen,
    h.min_seen / 100.0                 AS min_price_seen,
    CASE WHEN p.original_price IS NOT NULL AND h.n_obs >= 3
              AND h.max_seen < p.original_price * 0.9 THEN 1 ELSE 0 END AS suspicious,
    CASE WHEN p.price <= h.min_seen THEN 1 ELSE 0 END AS lowest_seen
FROM products p
JOIN (
    SELECT product_id, COUNT(*) AS n_obs, MAX(price) AS max_seen, MIN(price) AS min_seen
    FROM price_history GROUP BY product_id
) h ON h.product_id = p.id;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str = DEFAULT_DB, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_product(conn: sqlite3.Connection, p: Product) -> int:
    ts = p.scraped_at.isoformat(timespec="seconds")
    row = conn.execute(
        """
        INSERT INTO products (store, url, sku, title, brand, category, gender, currency, sizes,
                              colors, image_url, in_stock, price, original_price, discount_pct,
                              first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (store, url) DO UPDATE SET
            sku = COALESCE(excluded.sku, sku), title = excluded.title,
            brand = COALESCE(excluded.brand, brand),
            category = COALESCE(excluded.category, category),
            gender = COALESCE(excluded.gender, gender),
            currency = excluded.currency, sizes = excluded.sizes, colors = excluded.colors,
            image_url = COALESCE(excluded.image_url, image_url), in_stock = excluded.in_stock,
            price = excluded.price, original_price = excluded.original_price,
            discount_pct = excluded.discount_pct, last_seen = excluded.last_seen
        RETURNING id
        """,
        (
            p.store, p.url, p.sku, p.title, p.brand, p.category, p.gender, p.currency,
            ",".join(p.sizes), ",".join(p.colors), p.image_url, int(p.in_stock), p.price,
            p.original_price, p.discount_pct, ts, ts,
        ),
    ).fetchone()
    pid = row[0]
    last = conn.execute(
        "SELECT price, original_price FROM price_history WHERE product_id = ? "
        "ORDER BY scraped_at DESC LIMIT 1",
        (pid,),
    ).fetchone()
    # Only record a new observation when the price changed or on a new day; keeps history compact.
    if last is None or (last[0], last[1]) != (p.price, p.original_price) or _new_day(conn, pid, ts):
        conn.execute(
            "INSERT INTO price_history (product_id, price, original_price, scraped_at) VALUES (?,?,?,?)",
            (pid, p.price, p.original_price, ts),
        )
    return pid


def _new_day(conn: sqlite3.Connection, pid: int, ts: str) -> bool:
    last_ts = conn.execute(
        "SELECT MAX(scraped_at) FROM price_history WHERE product_id = ?", (pid,)
    ).fetchone()[0]
    return last_ts is None or last_ts[:10] != ts[:10]


def start_run(conn: sqlite3.Connection, store: str) -> int:
    cur = conn.execute("INSERT INTO scrape_runs (store, started_at) VALUES (?, ?)", (store, _now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str, items: int, method: str | None, error: str | None = None):
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, status=?, items=?, method=?, error=? WHERE id=?",
        (_now(), status, items, method, error, run_id),
    )
    conn.commit()
