"""Read-only query tools exposed to Claude. Each returns a JSON string."""

from __future__ import annotations

import json
import re
import sqlite3

MAX_ROWS = 50

DEAL_COLS = (
    "id, store, brand, title, category, gender, price, original_price, discount_pct, "
    "sizes, in_stock, suspicious, lowest_seen, url"
)


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _words(q: str) -> list[str]:
    return [w for w in re.split(r"\W+", q.lower()) if len(w) > 1]


def search_deals(
    conn: sqlite3.Connection,
    query: str | None = None,
    brand: str | None = None,
    store: str | None = None,
    category: str | None = None,
    gender: str | None = None,
    size: str | None = None,
    max_price: float | None = None,
    min_discount: float | None = None,
    in_stock: bool = True,
    exclude_suspicious: bool = False,
    sort: str = "discount",
    limit: int = 20,
) -> str:
    where, args = [], []
    for w in _words(query or ""):
        where.append("(LOWER(title) LIKE ? OR LOWER(COALESCE(category,'')) LIKE ?)")
        args += [f"%{w}%", f"%{w}%"]
    for col, val in (("brand", brand), ("store", store), ("category", category)):
        if val:
            where.append(f"LOWER(COALESCE({col},'')) LIKE ?")
            args.append(f"%{val.lower()}%")
    if gender:
        where.append("(LOWER(COALESCE(gender,'')) = ? OR gender IS NULL OR gender = 'unisex')")
        args.append(gender.lower())
    if size:
        where.append("(',' || UPPER(COALESCE(sizes,'')) || ',') LIKE ?")
        args.append(f"%,{size.upper()},%")
    if max_price is not None:
        where.append("price <= ?")
        args.append(max_price)
    if min_discount is not None:
        where.append("discount_pct >= ?")
        args.append(min_discount)
    if in_stock:
        where.append("in_stock = 1")
    if exclude_suspicious:
        where.append("suspicious = 0")
    order = {
        "discount": "discount_pct DESC, price ASC",
        "price": "price ASC, discount_pct DESC",
        "savings": "(COALESCE(original_price, price) - price) DESC",
    }.get(sort, "discount_pct DESC")
    sql = f"SELECT {DEAL_COLS} FROM current_deals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ?"
    args.append(max(1, min(int(limit), MAX_ROWS)))
    rows = _rows(conn.execute(sql, args))
    return json.dumps({"count": len(rows), "results": rows}, ensure_ascii=False)


def get_price_history(conn: sqlite3.Connection, product_id: int) -> str:
    prod = conn.execute(f"SELECT {DEAL_COLS} FROM current_deals WHERE id = ?", (product_id,)).fetchone()
    if not prod:
        return json.dumps({"error": f"product {product_id} not found"})
    hist = _rows(conn.execute(
        "SELECT price/100.0 AS price, original_price/100.0 AS original_price, scraped_at "
        "FROM price_history WHERE product_id = ? ORDER BY scraped_at", (product_id,)))
    return json.dumps({"product": dict(prod), "history": hist}, ensure_ascii=False)


def compare_similar(conn: sqlite3.Connection, query: str, limit: int = 15) -> str:
    """Cheapest matching item per store, to compare the same kind of garment across shops."""
    words = _words(query)
    if not words:
        return json.dumps({"error": "empty query"})
    cond = " AND ".join("LOWER(title) LIKE ?" for _ in words)
    args = [f"%{w}%" for w in words]
    rows = _rows(conn.execute(
        f"""SELECT {DEAL_COLS} FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY store ORDER BY price) AS rn
                FROM current_deals WHERE in_stock = 1 AND {cond})
            WHERE rn <= 3 ORDER BY price LIMIT ?""",
        args + [max(1, min(int(limit), MAX_ROWS))],
    ))
    return json.dumps({"count": len(rows), "results": rows}, ensure_ascii=False)


_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b", re.I)


def run_sql(conn: sqlite3.Connection, query: str) -> str:
    q = query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I) or ";" in q or _FORBIDDEN.search(q):
        return json.dumps({"error": "only a single read-only SELECT statement is allowed"})
    try:
        cur = conn.execute(q)
        rows = [dict(r) for r in cur.fetchmany(MAX_ROWS)]
    except sqlite3.Error as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"count": len(rows), "results": rows, "truncated_to": MAX_ROWS}, ensure_ascii=False)


def db_overview(conn: sqlite3.Connection) -> str:
    stores = _rows(conn.execute(
        "SELECT store, COUNT(*) AS products, SUM(original_price IS NOT NULL) AS on_sale, "
        "ROUND(AVG(discount_pct),1) AS avg_discount, MAX(last_seen) AS last_seen "
        "FROM current_deals GROUP BY store"))
    cats = _rows(conn.execute(
        "SELECT category, COUNT(*) AS n FROM current_deals GROUP BY category ORDER BY n DESC LIMIT 30"))
    return json.dumps({"stores": stores, "top_categories": cats}, ensure_ascii=False)


SCHEMA_DOC = """View current_deals columns: id, store, brand, title, category, gender, price (USD),
original_price (USD or NULL), discount_pct (0-100), currency, sizes (comma separated), colors,
in_stock (0/1), url, image_url, last_seen, n_obs, max_price_seen, min_price_seen,
suspicious (1 = claimed original price never observed), lowest_seen (1 = lowest price we've seen).
Table price_history(product_id, price cents, original_price cents, scraped_at)."""

TOOL_DEFS = [
    {
        "name": "search_deals",
        "description": "Search scraped clothing products/deals. Words in `query` must all appear in the title or category (English, e.g. 'wool sweater'). Returns up to `limit` rows with price, original_price, discount_pct, url.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords in English, e.g. 'oxford shirt'"},
                "brand": {"type": "string"},
                "store": {"type": "string", "description": "uniqlo, ralphlauren, tommy, macys, ..."},
                "category": {"type": "string"},
                "gender": {"type": "string", "enum": ["men", "women", "kids", "baby", "unisex"]},
                "size": {"type": "string", "description": "Exact size label, e.g. 'M' or '32'"},
                "max_price": {"type": "number", "description": "USD"},
                "min_discount": {"type": "number", "description": "Percent, 0-100"},
                "in_stock": {"type": "boolean"},
                "exclude_suspicious": {"type": "boolean"},
                "sort": {"type": "string", "enum": ["discount", "price", "savings"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_price_history",
        "description": "Price history of one product (by id from search results). Use it to judge whether a discount is real.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer"}},
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "compare_similar",
        "description": "Compare the cheapest in-stock matches for the same kind of item across stores (up to 3 per store).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_sql",
        "description": "Run one read-only SQLite SELECT for questions the other tools can't answer. " + SCHEMA_DOC,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "db_overview",
        "description": "Which stores and categories are in the database, how many items are on sale, and when data was last scraped.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

HANDLERS = {
    "search_deals": search_deals,
    "get_price_history": get_price_history,
    "compare_similar": compare_similar,
    "run_sql": run_sql,
    "db_overview": db_overview,
}


def call_tool(conn: sqlite3.Connection, name: str, args: dict) -> str:
    fn = HANDLERS.get(name)
    if fn is None:
        raise ValueError(f"unknown tool {name}")
    return fn(conn, **args)
