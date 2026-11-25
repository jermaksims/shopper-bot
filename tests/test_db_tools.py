import json
from datetime import datetime, timedelta, timezone

import pytest

from shopper import db as dbm
from shopper.chat import tools
from shopper.export import export
from shopper.models import Product
from shopper.stores.base import StoreAdapter


def _p(**kw):
    base = dict(store="uniqlo", url="https://x/1", title="Merino Wool Sweater", price=2990,
                original_price=4990, gender="men", sizes=["S", "M"], category="sale")
    base.update(kw)
    return Product(**base)


@pytest.fixture
def conn(tmp_path):
    c = dbm.connect(tmp_path / "t.db")
    yield c
    c.close()


def test_upsert_and_history(conn):
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    pid = dbm.upsert_product(conn, _p(price=4990, original_price=None, scraped_at=t0))
    # same price same day -> no new history row
    dbm.upsert_product(conn, _p(price=4990, original_price=None, scraped_at=t0 + timedelta(hours=1)))
    # price drop -> new row, same product id
    assert dbm.upsert_product(conn, _p(scraped_at=t0 + timedelta(days=20))) == pid
    hist = conn.execute("SELECT price FROM price_history WHERE product_id=?", (pid,)).fetchall()
    assert [h[0] for h in hist] == [4990, 2990]
    row = conn.execute("SELECT * FROM current_deals WHERE id=?", (pid,)).fetchone()
    assert row["price"] == 29.9 and row["discount_pct"] == 40.1
    assert row["suspicious"] == 0 and row["lowest_seen"] == 1


def test_suspicious_markup(conn):
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    # always sold at ~30, but now claims "was $100"
    for d in range(3):
        dbm.upsert_product(conn, _p(url="https://x/2", price=3000, original_price=None, scraped_at=t0 + timedelta(days=d)))
    pid = dbm.upsert_product(conn, _p(url="https://x/2", price=2900, original_price=10000, scraped_at=t0 + timedelta(days=5)))
    assert conn.execute("SELECT suspicious FROM current_deals WHERE id=?", (pid,)).fetchone()[0] == 1


def _seed(conn):
    dbm.upsert_product(conn, _p())
    dbm.upsert_product(conn, _p(store="ralphlauren", url="https://y/1", title="Cable Knit Sweater",
                                price=8880, original_price=14800, sizes=["L"]))
    dbm.upsert_product(conn, _p(store="tommy", url="https://z/1", title="Flex Chino", price=3999,
                                original_price=None, gender="men", sizes=[]))
    dbm.upsert_product(conn, _p(store="macys", url="https://m/1", title="Women Sweater Dress",
                                gender="women", price=1999, original_price=5999, in_stock=False))
    conn.commit()


def test_search_deals(conn):
    _seed(conn)
    res = json.loads(tools.search_deals(conn, query="sweater", gender="men", max_price=100))
    titles = [r["title"] for r in res["results"]]
    assert titles == ["Merino Wool Sweater", "Cable Knit Sweater"]  # out-of-stock dress excluded
    res = json.loads(tools.search_deals(conn, query="sweater", size="m"))
    assert [r["title"] for r in res["results"]] == ["Merino Wool Sweater"]
    res = json.loads(tools.search_deals(conn, sort="price", in_stock=False, limit=1))
    assert res["results"][0]["store"] == "macys"


def test_compare_and_history_tools(conn):
    _seed(conn)
    res = json.loads(tools.compare_similar(conn, "sweater"))
    assert {r["store"] for r in res["results"]} == {"uniqlo", "ralphlauren"}
    pid = res["results"][0]["id"]
    hist = json.loads(tools.get_price_history(conn, pid))
    assert hist["history"] and hist["product"]["id"] == pid
    assert "error" in json.loads(tools.get_price_history(conn, 9999))


def test_run_sql_is_read_only(conn):
    _seed(conn)
    ok = json.loads(tools.run_sql(conn, "SELECT store, COUNT(*) n FROM current_deals GROUP BY store"))
    assert ok["count"] == 4
    for bad in ["DELETE FROM products", "SELECT 1; DROP TABLE products",
                "WITH x AS (SELECT 1) DELETE FROM products", "PRAGMA table_info(products)"]:
        assert "error" in json.loads(tools.run_sql(conn, bad))
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 4


def test_readonly_connection(tmp_path):
    path = tmp_path / "r.db"
    dbm.connect(path).close()
    ro = dbm.connect(path, readonly=True)
    with pytest.raises(Exception):
        ro.execute("DELETE FROM products")


def test_call_tool_dispatch(conn):
    _seed(conn)
    out = json.loads(tools.call_tool(conn, "db_overview", {}))
    assert len(out["stores"]) == 4
    with pytest.raises(ValueError):
        tools.call_tool(conn, "nope", {})


def test_export_csv_and_xlsx(conn, tmp_path):
    _seed(conn)
    assert export(conn, tmp_path / "d.csv") == 4
    assert export(conn, tmp_path / "d.xlsx", only_deals=True) == 3
    assert (tmp_path / "d.csv").read_text().startswith("id,store,brand")


class FakeAdapter(StoreAdapter):
    """Exercises the strategy chain: first method blocked, second succeeds."""

    def strategies(self):
        from shopper.fetch import Blocked

        def blocked(cat, limit):
            raise Blocked("akamai challenge")
            yield  # pragma: no cover

        def ok(cat, limit):
            for i in range(5):
                yield self.to_product({"url": f"https://f/{cat['name']}/{i}", "title": f"T{i}",
                                       "price": 1000 + i, "original_price": 2000}, cat)

        return {"api": blocked, "browser": ok}


def test_strategy_chain_escalates():
    a = FakeAdapter({"base_url": "https://f", "methods": ["api", "browser"],
                     "categories": [{"name": "a", "url": "u", "gender": "men"}, {"name": "b", "url": "u"}]})
    prods = list(a.scrape(limit=7))
    assert len(prods) == 7 and a.used_method == "browser:patchright"
    assert prods[0].gender == "men" and prods[0].category == "a"


class EngineAdapter(StoreAdapter):
    """`browser` expands into one attempt per engine; patchright blocked -> camoufox used."""

    def strategies(self):
        from shopper.fetch import Blocked

        def browser(cat, limit):
            self.tried.append(self.engine)
            if self.engine == "patchright":
                raise Blocked("akamai challenge")
            yield self.to_product({"url": "https://e/1", "title": "T", "price": 500}, cat)

        return {"browser": browser}


def test_browser_engine_escalation():
    a = EngineAdapter({"base_url": "https://e", "methods": ["browser"],
                       "browser_engines": ["patchright", "camoufox"],
                       "categories": [{"name": "a", "url": "u"}]})
    a.tried = []
    assert len(list(a.scrape())) == 1
    assert a.tried == ["patchright", "camoufox"] and a.used_method == "browser:camoufox"


def test_single_engine_string_config():
    a = StoreAdapter({"base_url": "https://e", "browser_engine": "camoufox"})
    assert a.browser_engines == ["camoufox"] and a._expanded_methods() == ["http", "browser:camoufox"]


def test_unknown_engine_rejected():
    from shopper.fetch.browser import BrowserFetcher

    with pytest.raises(ValueError):
        BrowserFetcher("x", engine="chromium-stealth-9000")
