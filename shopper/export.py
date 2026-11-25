from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

COLUMNS = [
    "id", "store", "brand", "title", "category", "gender", "price", "original_price",
    "discount_pct", "currency", "sizes", "colors", "in_stock", "suspicious", "lowest_seen",
    "max_price_seen", "url", "last_seen",
]


def export(conn: sqlite3.Connection, out: Path, only_deals: bool = False) -> int:
    where = "WHERE original_price IS NOT NULL" if only_deals else ""
    rows = conn.execute(
        f"SELECT {', '.join(COLUMNS)} FROM current_deals {where} ORDER BY discount_pct DESC, price ASC"
    ).fetchall()
    out = Path(out)
    if out.suffix.lower() == ".xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "deals"
        ws.append(COLUMNS)
        for r in rows:
            ws.append(list(r))
        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"
        wb.save(out)
    else:
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            w.writerows(rows)
    return len(rows)
