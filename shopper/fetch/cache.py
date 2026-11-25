"""Tiny on-disk response cache so parser development does not re-hit the sites."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from shopper import DATA_DIR


class ResponseCache:
    def __init__(self, store: str, ttl_seconds: int = 3600, root: Path | None = None):
        self.dir = (root or DATA_DIR / "cache") / store
        self.ttl = ttl_seconds

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def get(self, key: str) -> str | None:
        if self.ttl <= 0:
            return None
        p = self._path(key)
        if not p.exists() or time.time() - p.stat().st_mtime > self.ttl:
            return None
        return json.loads(p.read_text())["body"]

    def put(self, key: str, body: str) -> None:
        if self.ttl <= 0:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps({"key": key, "body": body}))
