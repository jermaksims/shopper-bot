"""Salesforce Commerce Cloud (Demandware) stores — Ralph Lauren, Tommy Hilfiger and many others.

SFCC category pages load more products through a grid endpoint that returns product tiles as
HTML, e.g. ``.../Search-UpdateGrid?cgid=<category>&start=0&sz=48``. Tiles carry structured data:
``data-pid``, the product link, and prices in ``<span class="value" content="59.99">``.

Per-store details (site id, cgid, selectors) live in config/stores.yaml. To find them on a new
SFCC site: open a sale category, click "Load more", and look in DevTools → Network for a request
containing ``Search-UpdateGrid`` or ``Search-ShowAjax``.
"""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

from shopper.models import Product, to_cents
from shopper.parse.products import extract_embedded
from shopper.stores.base import StoreAdapter

DEFAULT_SELECTORS = {
    "tile": "[data-pid].product, .product-tile[data-pid], div[data-pid]",
    "name": ".pdp-link a, .product-name a, .product-tile__name, .tile-name a, a.name-link, [itemprop=name]",
    "link": ".pdp-link a, .product-name a, a.name-link, a.product-tile__link, a[href*='.html'], a[href]",
    "sale": ".price .sales .value, .price-sales, .sale-price, .product-sales-price, [class*=sale] .value, .price .value",
    "list": ".price .strike-through .value, .price .list .value, .price-standard, .strike-through, del .value, del",
    "image": "img.tile-image, img",
    "brand": ".product-brand, .brand, .product-tile__brand",
}


def _price_of(node: Node | None) -> int | None:
    if node is None:
        return None
    content = node.attributes.get("content")
    return to_cents(content) if content else to_cents(node.text(strip=True))


def _pick(node: Node, selectors: str) -> Node | None:
    """First match by *selector priority* (css_first on a comma list returns document order)."""
    for s in selectors.split(","):
        found = node.css_first(s.strip())
        if found is not None:
            return found
    return None


def parse_tiles(html: str, base_url: str, selectors: dict | None = None) -> list[dict]:
    sel = {**DEFAULT_SELECTORS, **(selectors or {})}
    tree = HTMLParser(html)
    out, seen = [], set()
    for tile in tree.css(sel["tile"]):
        pid = tile.attributes.get("data-pid")
        if not pid or pid in seen:
            continue
        name_node = _pick(tile, sel["name"])
        link_node = _pick(tile, sel["link"])
        if not name_node or not link_node:
            continue
        href = link_node.attributes.get("href")
        if not href:
            continue
        lst_node = _pick(tile, sel["list"])
        sale_node = _pick(tile, sel["sale"])
        sale = _price_of(sale_node)
        lst = _price_of(lst_node)
        if sale_node is not None and lst_node is not None and sale_node.mem_id == lst_node.mem_id:
            lst = None  # generic fallback selector hit the list price itself
        if not sale:
            sale, lst = lst, None
        if not sale:
            continue
        seen.add(pid)
        img = _pick(tile, sel["image"])
        brand = _pick(tile, sel["brand"])
        out.append({
            "title": re.sub(r"\s+", " ", name_node.text(strip=True)),
            "url": urljoin(base_url + "/", href),
            "price": sale,
            "original_price": lst if lst and lst > sale else None,
            "sku": pid,
            "image_url": img and (img.attributes.get("src") or img.attributes.get("data-src")),
            "brand": brand.text(strip=True) if brand else None,
        })
    return out


class SFCCStore(StoreAdapter):
    """Configurable SFCC adapter; subclasses only set name/brand."""

    def strategies(self):
        return {"api": self.scrape_grid, **super().strategies()}

    def scrape_grid(self, cat: dict, limit: int) -> Iterator[Product]:
        grid = self.cfg.get("grid_path")
        if not grid or "cgid" not in cat:
            return  # nothing configured → let the chain fall through to http/browser
        self.http.warmup()
        # Open the category page first (real navigation), then page through the grid XHR.
        self.http.get(cat["url"], xhr=False, referer=self.base_url + "/")
        page_size = self.cfg.get("page_size", 48)
        start, n = 0, 0
        while n < limit:
            params = {"cgid": cat["cgid"], "start": start, "sz": page_size, **cat.get("params", {})}
            html = self.http.get(grid, params=params, referer=cat["url"])
            tiles = parse_tiles(html, self.base_url, self.cfg.get("selectors"))
            if not tiles:
                # Some sites return JSON-LD inside the grid instead of tiles
                blobs = extract_embedded(html)
                got = list(self._from_blobs(blobs, cat))
                if not got:
                    return
                for p in got:
                    n += 1
                    yield p
                return
            for d in tiles:
                if (p := self.to_product(d, cat)):
                    n += 1
                    yield p
                    if n >= limit:
                        return
            if len(tiles) < page_size:
                return
            start += page_size

    def scrape_http_generic(self, cat: dict, limit: int) -> Iterator[Product]:
        """Category HTML: try tiles first, then embedded JSON."""
        self.http.warmup()
        html = self.http.get(cat["url"], xhr=False, referer=self.base_url + "/")
        tiles = parse_tiles(html, self.base_url, self.cfg.get("selectors"))
        products = [p for d in tiles if (p := self.to_product(d, cat))] or list(
            self._from_blobs(extract_embedded(html), cat)
        )
        yield from products[:limit]

    def scrape_browser_generic(self, cat: dict, limit: int) -> Iterator[Product]:
        """Real Chrome: scroll the category so the grid loads, then parse the rendered tiles."""
        with self.browser() as b:
            b.goto(cat["url"], wait_captcha_seconds=0 if self.headless else 60)
            for _ in range(cat.get("scrolls", 12)):
                b.page.mouse.wheel(0, 1200)
                b.page.wait_for_timeout(900)
                more = b.page.query_selector("button.more, .show-more button, [data-url*='UpdateGrid']")
                if more and more.is_visible():
                    more.click()
                    b._human_pause(0.5)
            html = b.page.content()
        tiles = parse_tiles(html, self.base_url, self.cfg.get("selectors"))
        products = [p for d in tiles if (p := self.to_product(d, cat))] or list(
            self._from_blobs(extract_embedded(html), cat)
        )
        yield from products[:limit]


class RalphLauren(SFCCStore):
    name = "ralphlauren"
    brand = "Ralph Lauren"


class TommyHilfiger(SFCCStore):
    name = "tommy"
    brand = "Tommy Hilfiger"
