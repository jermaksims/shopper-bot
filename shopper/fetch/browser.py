"""Real-browser fetching with Playwright for the most protected sites.

We drive an actual installed Chrome (``channel="chrome"``) with a persistent profile, so the
site sees an ordinary returning visitor: real JS engine, real fingerprint, cookies kept between
runs. Instead of scraping the DOM we listen to the JSON responses the page itself requests
(product grids are almost always loaded via XHR), which is both cleaner and more stable.

If a CAPTCHA appears we do NOT try to solve it: in headed mode you can solve it yourself once
(the profile then keeps the cookie); otherwise the store is marked blocked for this run.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Callable

from shopper import DATA_DIR
from shopper.fetch.blocks import Blocked, detect_block

log = logging.getLogger(__name__)


class BrowserFetcher:
    def __init__(self, store: str, headless: bool = False, channel: str | None = "chrome",
                 min_delay: float = 3.0, max_delay: float = 8.0):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # optional dependency
            raise RuntimeError(
                "Playwright is not installed: pip install -e '.[browser]' && playwright install chrome"
            ) from e
        self.store = store
        self.min_delay, self.max_delay = min_delay, max_delay
        self._pw = sync_playwright().start()
        profile = DATA_DIR / "profiles" / store
        profile.mkdir(parents=True, exist_ok=True)
        launch = dict(
            user_data_dir=str(profile),
            headless=headless,
            viewport={"width": 1366, "height": 860},
            locale="en-US",
            timezone_id="America/New_York",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(channel=channel, **launch)
        except Exception:
            log.warning("Chrome channel %r unavailable, falling back to bundled Chromium", channel)
            self.ctx = self._pw.chromium.launch_persistent_context(**launch)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def close(self) -> None:
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _human_pause(self, scale: float = 1.0) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay) * scale)

    def _check_block(self, status: int) -> None:
        html = self.page.content()
        reason = detect_block(status, html, "text/html")
        if reason:
            raise Blocked(reason, self.page.url, status)

    def goto(self, url: str, wait_captcha_seconds: int = 0) -> str:
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._human_pause(0.5)
        status = resp.status if resp else 200
        try:
            self._check_block(status)
        except Blocked:
            if wait_captcha_seconds:
                log.warning("[%s] challenge page — solve it in the browser window (%ss)…",
                            self.store, wait_captcha_seconds)
                self.page.wait_for_timeout(wait_captcha_seconds * 1000)
                self._check_block(200)
            else:
                raise
        return self.page.content()

    def capture_json(
        self,
        url: str,
        match: Callable[[str], bool],
        scrolls: int = 8,
        next_selector: str | None = None,
        max_pages: int = 1,
        wait_captcha_seconds: int = 0,
    ) -> list[tuple[str, object]]:
        """Open ``url``, scroll like a person, and collect JSON bodies of responses whose URL matches."""
        captured: list[tuple[str, object]] = []

        def on_response(resp):
            try:
                if match(resp.url) and "json" in (resp.headers.get("content-type") or ""):
                    captured.append((resp.url, resp.json()))
            except Exception:
                pass

        self.page.on("response", on_response)
        try:
            self.goto(url, wait_captcha_seconds)
            for page_no in range(max_pages):
                for _ in range(scrolls):
                    self.page.mouse.wheel(0, random.randint(600, 1400))
                    self.page.wait_for_timeout(random.randint(600, 1600))
                if next_selector and page_no + 1 < max_pages:
                    nxt = self.page.query_selector(next_selector)
                    if not nxt:
                        break
                    nxt.click()
                    self._human_pause()
        finally:
            self.page.remove_listener("response", on_response)
        return captured

    def embedded_json(self) -> list[object]:
        """Return JSON-LD blocks and common embedded state objects from the current page."""
        return self.page.evaluate(
            """() => {
              const out = [];
              document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                try { out.push(JSON.parse(s.textContent)); } catch (e) {}
              });
              for (const k of ['__NEXT_DATA__', '__INITIAL_STATE__', '__PRELOADED_STATE__']) {
                const el = document.getElementById(k);
                if (el) { try { out.push(JSON.parse(el.textContent)); } catch (e) {} }
                if (window[k]) out.push(window[k]);
              }
              return JSON.parse(JSON.stringify(out));
            }"""
        )

    def export_cookies(self) -> list[dict]:
        """Hand cookies to HttpFetcher.import_cookies for fast follow-up XHR requests."""
        return [
            {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"]}
            for c in self.ctx.cookies()
        ]

    def fetch_json_in_page(self, url: str) -> object:
        """Run fetch() from inside the page: same origin, same cookies, same fingerprint."""
        self._human_pause(0.6)
        text = self.page.evaluate(
            "async (u) => { const r = await fetch(u, {credentials: 'include', "
            "headers: {'Accept': 'application/json'}}); return await r.text(); }",
            url,
        )
        return json.loads(text)
