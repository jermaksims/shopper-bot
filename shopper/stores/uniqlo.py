"""Uniqlo US — uses the JSON commerce API that uniqlo.com's own frontend calls.

Endpoint (as seen in DevTools → Network on a category page):
    /us/api/commerce/v5/en/products?path=<category ids>&flagCodes=discount&offset=0&limit=36

If Uniqlo changes the API version or parameters, open a sale page, filter Network by
"products?", and update ``api_path`` / params in config/stores.yaml — the parser is tolerant.
"""

from __future__ import annotations

from typing import Iterator

from shopper.models import Product, to_cents
from shopper.stores.base import StoreAdapter

GENDERS = {"MEN": "men", "WOMEN": "women", "KIDS": "kids", "BABY": "baby", "UNISEX": "unisex"}


def _price(block) -> int | None:
    if not block:
        return None
    if isinstance(block, dict):
        return to_cents(block.get("value"))
    return to_cents(block)


def parse_item(item: dict, base_url: str) -> dict | None:
    pid = item.get("productId") or item.get("id")
    name = item.get("name")
    if not pid or not name:
        return None
    prices = item.get("prices") or {}
    base = _price(prices.get("base"))
    promo = _price(prices.get("promo"))
    price = promo or base
    if not price:
        return None
    images = item.get("images") or {}
    img = None
    main = images.get("main")
    if isinstance(main, dict) and main:
        first = next(iter(main.values()))
        img = first.get("image") if isinstance(first, dict) else first
    elif isinstance(main, list) and main:
        img = main[0].get("url") if isinstance(main[0], dict) else main[0]
    gender_raw = (item.get("genderCategory") or item.get("genderName") or "").upper()
    price_group = item.get("priceGroup") or "00"
    rating = item.get("rating") or {}
    return {
        "title": name,
        "url": f"{base_url}/us/en/products/{pid}/{price_group}",
        "price": price,
        "original_price": base if promo and base and base > promo else None,
        "currency": ((prices.get("base") or {}).get("currency") or {}).get("code", "USD")
        if isinstance(prices.get("base"), dict) else "USD",
        "gender": GENDERS.get(gender_raw, gender_raw.lower() or None),
        "colors": [c.get("name") for c in item.get("colors") or [] if isinstance(c, dict) and c.get("name")],
        "sizes": [s.get("name") for s in item.get("sizes") or [] if isinstance(s, dict) and s.get("name")],
        "image_url": img,
        "sku": str(pid),
        "in_stock": not item.get("soldOut", False),
        "rating": rating.get("average"),
    }


class Uniqlo(StoreAdapter):
    name = "uniqlo"
    brand = "Uniqlo"

    def strategies(self):
        return {"api": self.scrape_api, **super().strategies()}

    def scrape_api(self, cat: dict, limit: int) -> Iterator[Product]:
        self.http.warmup()
        api = self.cfg.get("api_path", "/us/api/commerce/v5/en/products")
        page_size = 36
        offset, n = 0, 0
        while n < limit:
            params = {"offset": offset, "limit": page_size, "httpFailure": "true"}
            params.update(cat.get("params", {"flagCodes": "discount"}))
            data = self.http.get_json(api, params=params, referer=cat.get("url", self.base_url + "/"))
            result = data.get("result") or {}
            items = result.get("items") or []
            if not items:
                return
            for it in items:
                d = parse_item(it, self.base_url)
                if d and (p := self.to_product(d, cat)):
                    n += 1
                    yield p
                    if n >= limit:
                        return
            total = (result.get("pagination") or {}).get("total", 0)
            offset += len(items)
            if total and offset >= total:
                return
