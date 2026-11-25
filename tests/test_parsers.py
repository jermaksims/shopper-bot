import json
from pathlib import Path

from shopper.fetch.blocks import detect_block
from shopper.models import Product, to_cents
from shopper.parse.products import extract_embedded, walk_products
from shopper.stores.sfcc import parse_tiles
from shopper.stores.uniqlo import parse_item

FIX = Path(__file__).parent / "fixtures"


def test_to_cents():
    assert to_cents("$1,299.50") == 129950
    assert to_cents(29.9) == 2990
    assert to_cents("29.90 USD") == 2990
    assert to_cents(None) is None and to_cents("n/a") is None


def test_product_discount_and_bogus_original():
    p = Product(store="x", url="u", title="t", price=2990, original_price=4990)
    assert p.discount_pct == 40.1
    q = Product(store="x", url="u", title="t", price=2990, original_price=2990)
    assert q.original_price is None and q.discount_pct == 0


def test_uniqlo_items():
    data = json.loads((FIX / "uniqlo_products.json").read_text())
    items = [parse_item(i, "https://www.uniqlo.com") for i in data["result"]["items"]]
    sweater, tee = items
    assert sweater["price"] == 2990 and sweater["original_price"] == 4990
    assert sweater["gender"] == "women"
    assert sweater["sizes"] == ["S", "M"] and "BLACK" in sweater["colors"]
    assert sweater["url"] == "https://www.uniqlo.com/us/en/products/E465185-000/00"
    assert sweater["image_url"].endswith(".jpg")
    assert tee["price"] == 1490 and tee["original_price"] is None and tee["gender"] == "men"


def test_sfcc_tiles():
    tiles = parse_tiles((FIX / "sfcc_grid.html").read_text(), "https://www.ralphlauren.com")
    assert len(tiles) == 2
    a, b = tiles
    assert a["title"] == "Cable-Knit Cotton Sweater"
    assert a["price"] == 8880 and a["original_price"] == 14800
    assert a["url"].startswith("https://www.ralphlauren.com/men/clothing/")
    assert b["price"] == 11000 and b["original_price"] is None


def test_jsonld_and_next_data():
    html = (FIX / "jsonld_category.html").read_text()
    blobs = extract_embedded(html)
    prods = {p["title"]: p for b in blobs for p in walk_products(b, "https://usa.tommy.com")}
    chino = prods["Flex Slim Fit Chino"]
    assert chino["price"] == 3999 and chino["original_price"] is None
    assert chino["brand"] == "Tommy Hilfiger" and chino["in_stock"]
    hoodie = prods["Sold Out Hoodie"]
    assert hoodie["url"] == "https://usa.tommy.com/en/hoodie-456" and not hoodie["in_stock"]
    puffer = prods["Quilted Puffer Jacket"]
    assert puffer["price"] == 11900 and puffer["original_price"] == 29900


def test_macys_xhr_walker():
    data = json.loads((FIX / "macys_xapi.json").read_text())
    prods = {p["title"]: p for p in walk_products(data, "https://www.macys.com")}
    merino = prods["Club Room Men's Merino Sweater"]
    assert merino["price"] == 2999 and merino["original_price"] == 7500
    assert merino["url"] == "https://www.macys.com/shop/product/club-room-merino?ID=12345"
    assert merino["brand"] == "Club Room" and merino["sku"] == "12345"
    jeans = prods["Levi's 511 Slim Jeans"]
    assert jeans["price"] == 3999 and jeans["original_price"] == 6950


def test_block_detection():
    assert detect_block(403, (FIX / "akamai_block.html").read_text()) == "akamai challenge"
    assert detect_block(200, "<html>Just a moment...</html>") == "cloudflare challenge"
    assert detect_block(429, "") == "rate limited (429)"
    assert detect_block(200, '{"items": []}', "application/json") is None
    big_normal_page = "<html>" + "x" * 50000 + "<script>_abck</script></html>"
    assert detect_block(200, big_normal_page) is None
