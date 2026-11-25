from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


def to_cents(value) -> int | None:
    """Convert '$1,299.50', 29.9 or '29.90 USD' to integer cents.

    Numbers are treated as dollars. Returns None for empty/unparseable input.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value) * 100)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not m:
        return None
    return round(float(m.group(0).replace(",", "")) * 100)


class Product(BaseModel):
    store: str
    url: str
    title: str
    brand: str | None = None
    category: str | None = None
    gender: str | None = None
    price: int  # current price, cents
    original_price: int | None = None  # list/"was" price, cents
    currency: str = "USD"
    sizes: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    in_stock: bool = True
    image_url: str | None = None
    sku: str | None = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("original_price")
    @classmethod
    def _drop_bogus_original(cls, v, info):
        price = info.data.get("price")
        if v is not None and price is not None and v <= price:
            return None  # not a markdown
        return v

    @property
    def discount_pct(self) -> float:
        if not self.original_price:
            return 0.0
        return round(100 * (1 - self.price / self.original_price), 1)
