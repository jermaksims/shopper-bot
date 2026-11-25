"""Store registry. ``adapter: generic`` in stores.yaml works for any site exposing JSON-LD,
embedded state or product XHRs; dedicated adapters exist where a cleaner API is known."""

from __future__ import annotations

from pathlib import Path

import yaml

from shopper.stores.base import StoreAdapter
from shopper.stores.macys import Macys
from shopper.stores.sfcc import RalphLauren, SFCCStore, TommyHilfiger
from shopper.stores.uniqlo import Uniqlo

ADAPTERS: dict[str, type[StoreAdapter]] = {
    "uniqlo": Uniqlo,
    "ralphlauren": RalphLauren,
    "tommy": TommyHilfiger,
    "macys": Macys,
    "sfcc": SFCCStore,
    "generic": StoreAdapter,
}

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "stores.yaml"


def load_config(path: Path | None = None) -> dict[str, dict]:
    p = path or CONFIG_PATH
    if not p.exists():
        p = Path.cwd() / "config" / "stores.yaml"
    return yaml.safe_load(p.read_text())["stores"]


def build(name: str, cfg: dict) -> StoreAdapter:
    cls = ADAPTERS[cfg.get("adapter", name)]
    adapter = cls(cfg)
    adapter.name = name
    if cfg.get("brand"):
        adapter.brand = cfg["brand"]
    return adapter
