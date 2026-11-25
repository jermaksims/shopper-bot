"""Store adapter base: a chain of fetch strategies, cheapest first, escalating on blocks."""

from __future__ import annotations

import logging
from typing import Callable, Iterator

from shopper.fetch import Blocked, HttpFetcher
from shopper.models import Product
from shopper.parse.products import extract_embedded, walk_products

log = logging.getLogger(__name__)


class StoreAdapter:
    name: str = "base"
    brand: str | None = None  # default brand for single-brand stores

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url: str = cfg["base_url"].rstrip("/")
        self.categories: list[dict] = cfg.get("categories", [])
        self.methods: list[str] = cfg.get("methods", ["http", "browser"])
        self.headless: bool = cfg.get("headless", False)
        self._http: HttpFetcher | None = None
        self.used_method: str | None = None

    # ----- fetchers -----
    @property
    def http(self) -> HttpFetcher:
        if self._http is None:
            self._http = HttpFetcher(
                self.name,
                self.base_url,
                min_delay=self.cfg.get("min_delay", 2.0),
                max_delay=self.cfg.get("max_delay", 6.0),
                cache_ttl=self.cfg.get("cache_ttl", 0),
                proxy=self.cfg.get("proxy"),
            )
        return self._http

    def browser(self):
        from shopper.fetch.browser import BrowserFetcher

        return BrowserFetcher(
            self.name,
            headless=self.headless,
            min_delay=self.cfg.get("min_delay", 3.0),
            max_delay=self.cfg.get("max_delay", 8.0),
        )

    # ----- strategy chain -----
    def strategies(self) -> dict[str, Callable[[dict, int], Iterator[Product]]]:
        """Map of method name -> generator(category, limit). Subclasses extend this."""
        return {"http": self.scrape_http_generic, "browser": self.scrape_browser_generic}

    def scrape(self, limit: int | None = None) -> Iterator[Product]:
        """Yield products from every category. Per-category failures are collected in
        ``self.errors``; Blocked is raised only if nothing at all could be collected."""
        available = self.strategies()
        seen: set[str] = set()
        remaining = limit
        self.errors: list[str] = []
        for cat in self.categories:
            errors: list[str] = []
            for method in self.methods:
                fn = available.get(method)
                if fn is None:
                    continue
                try:
                    got = 0
                    for p in fn(cat, remaining or 10**9):
                        if p.url in seen:
                            continue
                        seen.add(p.url)
                        got += 1
                        yield p
                        if remaining is not None:
                            remaining -= 1
                            if remaining <= 0:
                                self.used_method = method
                                return
                    if got:
                        self.used_method = method
                        break
                    errors.append(f"{method}: no products found")
                except Blocked as e:
                    log.warning("[%s] %s blocked on %s: %s — escalating", self.name, method, cat.get("name"), e.reason)
                    errors.append(f"{method}: {e.reason}")
                except RuntimeError as e:  # e.g. Playwright not installed
                    errors.append(f"{method}: {e}")
            else:
                if errors:
                    self.errors.append(f"{cat.get('name', cat.get('url'))}: " + "; ".join(errors))
        if not seen and self.errors:
            raise Blocked(" | ".join(self.errors))

    def close(self) -> None:
        if self._http:
            self._http.save_cookies()

    # ----- helpers -----
    def to_product(self, d: dict, cat: dict) -> Product | None:
        if not d.get("url") or not d.get("price"):
            return None
        return Product(
            store=self.name,
            url=d["url"].split("#")[0],
            title=d["title"],
            brand=d.get("brand") or self.brand,
            category=d.get("category") or cat.get("category") or cat.get("name"),
            gender=d.get("gender") or cat.get("gender"),
            price=d["price"],
            original_price=d.get("original_price"),
            currency=d.get("currency") or "USD",
            sizes=d.get("sizes") or [],
            colors=d.get("colors") or [],
            in_stock=d.get("in_stock", True),
            image_url=d.get("image_url"),
            sku=d.get("sku"),
        )

    def _from_blobs(self, blobs, cat) -> Iterator[Product]:
        for blob in blobs:
            for d in walk_products(blob, self.base_url):
                if (p := self.to_product(d, cat)) is not None:
                    yield p

    # ----- generic strategies (work on most sites without a dedicated parser) -----
    def scrape_http_generic(self, cat: dict, limit: int) -> Iterator[Product]:
        self.http.warmup()
        n = 0
        for page_url in self._page_urls(cat):
            html = self.http.get(page_url, xhr=False, referer=self.base_url + "/")
            found = 0
            for p in self._from_blobs(extract_embedded(html), cat):
                found += 1
                n += 1
                yield p
                if n >= limit:
                    return
            if not found:
                break

    def scrape_browser_generic(self, cat: dict, limit: int) -> Iterator[Product]:
        match = cat.get("xhr_match")
        with self.browser() as b:
            captured = b.capture_json(
                cat["url"],
                match=(lambda u: match in u) if match else (lambda u: "api" in u or "search" in u),
                scrolls=cat.get("scrolls", 10),
                wait_captcha_seconds=0 if self.headless else 60,
            )
            blobs = [body for _, body in captured] + b.embedded_json()
            n = 0
            for p in self._from_blobs(blobs, cat):
                n += 1
                yield p
                if n >= limit:
                    return

    def _page_urls(self, cat: dict) -> Iterator[str]:
        """Category URL, then pagination via ``page_param`` if configured."""
        yield cat["url"]
        param = cat.get("page_param")
        if not param:
            return
        step = cat.get("page_step", 1)
        start = cat.get("page_start", 2 if step == 1 else step)
        sep = "&" if "?" in cat["url"] else "?"
        for i in range(cat.get("max_pages", 20) - 1):
            yield f"{cat['url']}{sep}{param}={start + i * step}"
