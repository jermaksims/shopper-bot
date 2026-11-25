"""HTTP fetching that looks like a real Chrome browser.

Key points:
* curl_cffi with ``impersonate="chrome"`` reproduces Chrome's TLS/HTTP2 fingerprint. Plain
  ``requests`` has a Python fingerprint (JA3) that Akamai/Cloudflare reject instantly.
* One stable, matching User-Agent per session (curl_cffi sets it to match the impersonated build).
* Cookies persist between runs, like a returning visitor.
* Slow, jittered pacing per domain; exponential backoff when a block is detected.
"""

from __future__ import annotations

import json
import logging
import random
import time
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urlencode, urlparse

from curl_cffi import requests as cffi

from shopper import DATA_DIR
from shopper.fetch.blocks import Blocked, detect_block
from shopper.fetch.cache import ResponseCache

log = logging.getLogger(__name__)

NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

XHR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}


class HttpFetcher:
    def __init__(
        self,
        store: str,
        base_url: str,
        min_delay: float = 2.0,
        max_delay: float = 6.0,
        impersonate: str = "chrome",
        max_retries: int = 3,
        cache_ttl: int = 0,
        proxy: str | None = None,
    ):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.min_delay, self.max_delay = min_delay, max_delay
        self.max_retries = max_retries
        self.cache = ResponseCache(store, cache_ttl)
        self.session = cffi.Session(impersonate=impersonate, timeout=30)
        if proxy:  # optional, e.g. your own SOCKS/HTTP proxy; nothing paid is required
            self.session.proxies = {"http": proxy, "https": proxy}
        self.cookie_file = DATA_DIR / "cookies" / f"{store}.json"
        self._last_request = 0.0
        self._warmed = False
        self._load_cookies()

    # ---------- cookies ----------
    def _load_cookies(self) -> None:
        if self.cookie_file.exists():
            for c in json.loads(self.cookie_file.read_text()):
                self.session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))

    def save_cookies(self) -> None:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        jar = self.session.cookies.jar
        data = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in jar]
        self.cookie_file.write_text(json.dumps(data))

    def import_cookies(self, cookies: list[dict]) -> None:
        """Cookie hand-off from a real browser session (see BrowserFetcher.export_cookies)."""
        for c in cookies:
            self.session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
        self.save_cookies()

    # ---------- pacing ----------
    def _pace(self) -> None:
        wait = random.uniform(self.min_delay, self.max_delay) - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def warmup(self) -> None:
        """Visit the home page first, like a person would, to pick up session cookies."""
        if self._warmed:
            return
        self._warmed = True
        try:
            self.get(self.base_url + "/", xhr=False, use_cache=False)
        except Blocked:
            raise
        except Exception as e:  # network hiccup on warmup is not fatal
            log.warning("[%s] warmup failed: %s", self.store, e)

    # ---------- requests ----------
    def get(
        self,
        url: str,
        params: dict | None = None,
        xhr: bool = True,
        headers: dict | None = None,
        use_cache: bool = True,
        referer: str | None = None,
    ) -> str:
        if not url.startswith("http"):
            url = self.base_url + url
        full = url + ("?" + urlencode(params, doseq=True) if params else "")
        if use_cache and (hit := self.cache.get(full)) is not None:
            return hit

        h = dict(XHR_HEADERS if xhr else NAV_HEADERS)
        if xhr:
            h["Referer"] = referer or self.base_url + "/"
            h["Origin"] = f"{urlparse(self.base_url).scheme}://{urlparse(self.base_url).netloc}"
        elif referer:
            h["Referer"] = referer
            h["Sec-Fetch-Site"] = "same-origin"
        if headers:
            h.update(headers)

        last_reason = ""
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                r = self.session.get(full, headers=h)
            except Exception as e:  # connection reset etc.
                last_reason = f"network error: {e}"
                self._backoff(attempt)
                continue
            reason = detect_block(r.status_code, r.text, r.headers.get("content-type", ""))
            if reason is None and r.status_code < 400:
                self.save_cookies()
                self.cache.put(full, r.text)
                return r.text
            if r.status_code == 404:
                raise FileNotFoundError(full)
            last_reason = reason or f"http {r.status_code}"
            log.warning("[%s] %s on %s (attempt %d)", self.store, last_reason, full, attempt + 1)
            self._backoff(attempt)
        raise Blocked(last_reason, full)

    def get_json(self, url: str, params: dict | None = None, **kw):
        return json.loads(self.get(url, params=params, xhr=True, **kw))

    def _backoff(self, attempt: int) -> None:
        # 10s, 30s, 90s ... plus jitter. Being patient is the cheapest anti-block measure there is.
        time.sleep(10 * (3 ** attempt) + random.uniform(0, 5))
