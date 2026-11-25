"""Macy's — heavily protected (Akamai Bot Manager). Plain HTTP is usually rejected, so the
default chain is: real Chrome → capture the discovery XHR JSON (``/xapi/``) the page loads →
generic product walker. In headed mode you can pass a one-off challenge by hand; the persistent
profile keeps the cookie for later runs.
"""

from __future__ import annotations

from typing import Iterator

from shopper.models import Product
from shopper.stores.base import StoreAdapter


class Macys(StoreAdapter):
    name = "macys"

    def scrape_browser_generic(self, cat: dict, limit: int) -> Iterator[Product]:
        cat = {"xhr_match": "/xapi/", "scrolls": 14, **cat}
        yield from super().scrape_browser_generic(cat, limit)
