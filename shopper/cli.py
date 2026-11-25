from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from shopper import db as dbm
from shopper.fetch import Blocked
from shopper.stores import build, load_config

app = typer.Typer(help="Black Friday clothing deal scraper + Claude chat", no_args_is_help=True)
console = Console()


def _scrape_one(name: str, cfg: dict, db_path: Path, limit: Optional[int]) -> tuple[str, str, int, str]:
    conn = dbm.connect(db_path)
    run_id = dbm.start_run(conn, name)
    adapter = build(name, cfg)
    n, status, err = 0, "ok", ""
    try:
        for p in adapter.scrape(limit=limit):
            dbm.upsert_product(conn, p)
            n += 1
            if n % 50 == 0:
                conn.commit()
                console.print(f"[dim]{name}: {n} products…[/dim]")
        if getattr(adapter, "errors", None):
            status, err = "partial", " | ".join(adapter.errors)
    except Blocked as e:
        status, err = "blocked", str(e)
    except Exception as e:  # keep other stores running
        logging.exception("store %s failed", name)
        status, err = "error", f"{type(e).__name__}: {e}"
    finally:
        conn.commit()
        adapter.close()
        dbm.finish_run(conn, run_id, status, n, adapter.used_method, err or None)
        conn.close()
    return name, status, n, err


@app.command()
def scrape(
    store: list[str] = typer.Option(None, "--store", "-s", help="Store(s) to scrape; default: all enabled"),
    limit: Optional[int] = typer.Option(None, help="Max products per store (useful for testing)"),
    workers: int = typer.Option(1, help="Stores scraped in parallel (each store itself stays sequential)"),
    engine: list[str] = typer.Option(None, "--engine", "-e",
                                     help="Browser engine(s) for the `browser` method, in order: "
                                          "patchright, camoufox, playwright (overrides stores.yaml)"),
    db: Path = typer.Option(dbm.DEFAULT_DB, help="SQLite database path"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Collect products and prices from the configured stores."""
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(message)s")
    cfg = load_config()
    names = store or [n for n, c in cfg.items() if c.get("enabled", True)]
    unknown = [n for n in names if n not in cfg]
    if unknown:
        raise typer.BadParameter(f"unknown store(s): {', '.join(unknown)}; known: {', '.join(cfg)}")
    if engine:
        from shopper.fetch.browser import ENGINES

        bad = [e for e in engine if e not in ENGINES]
        if bad:
            raise typer.BadParameter(f"unknown engine(s): {', '.join(bad)}; choose from {', '.join(ENGINES)}")
        for n in names:
            cfg[n] = {**cfg[n], "browser_engines": engine}
    dbm.connect(db).close()  # create schema
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(lambda n: _scrape_one(n, cfg[n], db, limit), names))
    t = Table(title="Scrape results")
    for col in ("store", "status", "products", "details"):
        t.add_column(col)
    colors = {"ok": "green", "partial": "yellow", "blocked": "red", "error": "red"}
    for name, status, n, err in results:
        t.add_row(name, f"[{colors[status]}]{status}[/]", str(n), err[:120])
    console.print(t)


@app.command()
def stats(db: Path = typer.Option(dbm.DEFAULT_DB)):
    """Products per store, how many are discounted, and the last scrape status."""
    conn = dbm.connect(db)
    t = Table(title="Database")
    for col in ("store", "products", "on sale", "avg disc %", "max disc %", "last run", "status", "method"):
        t.add_column(col)
    rows = conn.execute("""
        SELECT s.store, s.n, s.on_sale, s.avg_d, s.max_d, r.started_at, r.status, r.method
        FROM (SELECT store, COUNT(*) n, SUM(original_price IS NOT NULL) on_sale,
                     ROUND(AVG(CASE WHEN original_price IS NOT NULL THEN discount_pct END),1) avg_d,
                     MAX(discount_pct) max_d FROM products GROUP BY store) s
        LEFT JOIN scrape_runs r ON r.id = (SELECT MAX(id) FROM scrape_runs WHERE store = s.store)
    """).fetchall()
    for r in rows:
        t.add_row(*[("" if v is None else str(v)) for v in r])
    console.print(t)
    blocked = conn.execute(
        "SELECT store, status, error FROM scrape_runs WHERE id IN (SELECT MAX(id) FROM scrape_runs GROUP BY store) "
        "AND status != 'ok'").fetchall()
    for b in blocked:
        console.print(f"[yellow]{b['store']}: {b['status']} — {b['error']}[/yellow]")


@app.command()
def export(
    out: Path = typer.Argument(Path("deals.csv"), help=".csv or .xlsx"),
    only_deals: bool = typer.Option(False, "--only-deals", help="Only discounted items"),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Export the current deals table to CSV or XLSX."""
    from shopper.export import export as do_export

    n = do_export(dbm.connect(db), out, only_deals)
    console.print(f"Exported {n} rows to {out}")


@app.command()
def top(
    n: int = typer.Option(20),
    max_price: Optional[float] = typer.Option(None),
    gender: Optional[str] = typer.Option(None),
    db: Path = typer.Option(dbm.DEFAULT_DB),
):
    """Quick look at the biggest discounts without the LLM."""
    import json

    from shopper.chat.tools import search_deals

    res = json.loads(search_deals(dbm.connect(db), max_price=max_price, gender=gender,
                                  min_discount=1, limit=n))
    t = Table()
    for col in ("store", "title", "price", "was", "disc %", "url"):
        t.add_column(col, overflow="fold")
    for r in res["results"]:
        flag = " ⚠" if r["suspicious"] else ""
        t.add_row(r["store"], r["title"], f"${r['price']:.2f}", f"${r['original_price']:.2f}",
                  f"{r['discount_pct']}{flag}", r["url"])
    console.print(t)


@app.command()
def chat(db: Path = typer.Option(dbm.DEFAULT_DB)):
    """Chat with Claude about the scraped deals (needs ANTHROPIC_API_KEY)."""
    from shopper.chat.agent import run_chat

    if not Path(db).exists():
        raise typer.BadParameter(f"{db} not found — run `shopper scrape` first")
    run_chat(dbm.connect(db, readonly=True), console)


if __name__ == "__main__":
    app()
