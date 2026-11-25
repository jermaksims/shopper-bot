"""Site-agnostic product extraction.

* ``extract_embedded(html)`` — JSON-LD blocks and embedded app state (``__NEXT_DATA__`` etc.)
* ``walk_products(obj)`` — find product-like dicts anywhere inside arbitrary JSON (XHR responses,
  embedded state, JSON-LD) using key-name heuristics for name / url / sale price / list price.

This is what lets a new store work without a dedicated adapter, and it is the fallback for the
dedicated adapters when a site changes its markup.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from shopper.models import to_cents

NAME_KEYS = ("name", "productName", "displayName", "title", "product_name")
URL_KEYS = ("url", "pdpUrl", "productUrl", "productURL", "seoUrl", "canonicalUrl", "link", "href", "pdp_url")
IMAGE_KEYS = ("image", "imageUrl", "imageURL", "img", "thumbnail", "images", "primaryImage")
BRAND_KEYS = ("brand", "brandName", "manufacturer")
ID_KEYS = ("sku", "productId", "productID", "id", "pid", "styleId")

SALE_WORDS = ("sale", "promo", "final", "current", "now", "offer", "discount", "special", "selling", "lowprice")
LIST_WORDS = ("list", "regular", "original", "base", "was", "strike", "compare", "msrp", "retail", "full")

_STATE_IDS = ("__NEXT_DATA__", "__NUXT_DATA__", "__INITIAL_STATE__", "__PRELOADED_STATE__", "__APOLLO_STATE__")


def extract_embedded(html: str) -> list[Any]:
    tree = HTMLParser(html)
    out: list[Any] = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            out.append(json.loads(node.text(strip=True)))
        except (json.JSONDecodeError, TypeError):
            continue
    for sid in _STATE_IDS:
        node = tree.css_first(f"script#{sid}")
        if node:
            try:
                out.append(json.loads(node.text(strip=True)))
            except (json.JSONDecodeError, TypeError):
                pass
    # window.__INITIAL_STATE__ = {...};
    for node in tree.css("script"):
        txt = node.text() or ""
        m = re.search(r"window\.(__[A-Z_]+__)\s*=\s*(\{.*?\})\s*;?\s*(?:</|$|window\.)", txt, re.S)
        if m:
            try:
                out.append(json.loads(m.group(2)))
            except json.JSONDecodeError:
                pass
    return out


def _first(d: dict, keys) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _shallow(d: dict, keys) -> Any:
    """Look for keys on the dict itself, then one level down (e.g. product.detail.name)."""
    v = _first(d, keys)
    if v is not None:
        return v
    for child in d.values():
        if isinstance(child, dict) and child.get("@type") != "Brand":
            v = _first(child, keys)
            if v is not None:
                return v
    return None


def _as_text(v) -> str | None:
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        return _as_text(_first(v, ("name", "value", "text", "url", "src")))
    if isinstance(v, list) and v:
        return _as_text(v[0])
    return None


def _numbers(obj: Any, path: str = "", depth: int = 0) -> Iterator[tuple[str, float]]:
    """Yield (lowercased key path, number) for price-ish leaves up to a small depth."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        # {"type": "sale", "value": 29.99} / {"label": "Reg.", ...}: fold the label into the path
        tag = next((obj[t] for t in ("type", "label", "priceType") if isinstance(obj.get(t), str)), None)
        if tag:
            path = f"{path}.{re.sub(r'[^a-z]', '', tag.lower())}"
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in ("variants", "skus", "children", "items", "products", "reviews", "rating", "ratings"):
                continue
            yield from _numbers(v, f"{path}.{kl}", depth + 1)
    elif isinstance(obj, list):
        for v in obj[:3]:
            yield from _numbers(v, path, depth)  # lists do not count toward depth
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "price" in path or "offer" in path or "amount" in path or "value" in path:
            yield path, float(obj)
    elif isinstance(obj, str) and re.fullmatch(r"\s*\$?\s*\d[\d,]*(\.\d{1,2})?\s*", obj):
        if "price" in path or "offer" in path or "amount" in path:
            yield path, float(obj.replace("$", "").replace(",", ""))


def extract_prices(d: dict) -> tuple[int | None, int | None]:
    """Return (current_cents, original_cents) guessed from a product-like dict."""
    sale: list[float] = []
    lst: list[float] = []
    plain: list[float] = []
    for path, val in _numbers({k: v for k, v in d.items() if k not in NAME_KEYS}):
        if val <= 0 or val > 100000:
            continue
        last = path.rsplit(".", 2)[-2:]
        joined = ".".join(last) if "price" in path or "offer" in path else path
        if any(w in joined for w in LIST_WORDS) and not any(w in last[-1] for w in SALE_WORDS):
            lst.append(val)
        elif any(w in joined for w in SALE_WORDS):
            sale.append(val)
        elif "price" in path or "offer" in path:
            plain.append(val)
    current = min(sale) if sale else (min(plain) if plain else None)
    original = max(lst) if lst else (max(plain) if sale and plain else None)
    if current is None and original is not None:
        current, original = original, None
    cur_c, orig_c = to_cents(current), to_cents(original)
    if cur_c is not None and orig_c is not None and orig_c <= cur_c:
        orig_c = None
    return cur_c, orig_c


def walk_products(obj: Any, base_url: str = "", _depth: int = 0) -> Iterator[dict]:
    """Yield normalised product dicts found anywhere in ``obj``."""
    if _depth > 25:
        return
    if isinstance(obj, list):
        for v in obj:
            yield from walk_products(v, base_url, _depth + 1)
        return
    if not isinstance(obj, dict):
        return

    name = _as_text(_shallow(obj, NAME_KEYS))
    url = _as_text(_shallow(obj, URL_KEYS))
    typ = str(obj.get("@type", "")).lower()
    if name and (url or typ == "product"):
        price, original = extract_prices(obj)
        if price:
            availability = json.dumps(obj)[:20000]
            yield {
                "title": name,
                "url": urljoin(base_url, url) if url else None,
                "price": price,
                "original_price": original,
                "brand": _as_text(_shallow(obj, BRAND_KEYS)),
                "image_url": _as_text(_shallow(obj, IMAGE_KEYS)),
                "sku": _id_text(_shallow(obj, ID_KEYS)),
                "in_stock": "OutOfStock" not in availability,
                "currency": _currency(obj),
            }
            return  # don't descend into variants of a product we already captured
    for v in obj.values():
        if isinstance(v, (dict, list)):
            yield from walk_products(v, base_url, _depth + 1)


def _id_text(v) -> str | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(int(v))
    return _as_text(v)


def _currency(d: dict) -> str:
    blob = json.dumps(d)[:5000]
    m = re.search(r'"(?:priceCurrency|currency|currencyCode)"\s*:\s*"([A-Z]{3})"', blob)
    return m.group(1) if m else "USD"
