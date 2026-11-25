"""Real-browser fetching for the most protected sites, with a choice of engine.

Engines (``browser_engine`` in stores.yaml, or ``--engine`` on the CLI):

* ``patchright`` (default) — a drop-in Playwright fork that removes the automation leaks
  (``Runtime.enable``, ``navigator.webdriver``, command-line flags…). Drives your installed
  Google Chrome (``channel="chrome"``) with a persistent profile.
* ``camoufox`` — a patched Firefox that spoofs the fingerprint at the C++ level, with
  human-like mouse movement. Strongest against heavy fingerprinting; slower.
* ``playwright`` — plain Playwright + Chrome; kept as a fallback.

All engines expose the same Playwright API, so the rest of the code does not care which one
runs. Instead of scraping the DOM we listen to the JSON responses the page itself requests.

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

ENGINES = ("patchright", "camoufox", "playwright")

INSTALL_HINTS = {
    "patchright": "pip install patchright && patchright install chrome",
    "camoufox": "pip install 'camoufox[geoip]' && camoufox fetch",
    "playwright": "pip install playwright && playwright install chrome",
}


class BrowserFetcher:
    def __init__(self, store: str, headless: bool = False, engine: str = "patchright",
                 channel: str | None = "chrome", min_delay: float = 3.0, max_delay: float = 8.0,
                 proxy: str | None = None):
        if engine not in ENGINES:
            raise ValueError(f"unknown browser engine {engine!r}; choose one of {ENGINES}")
        self.store = store
        self.min_delay, self.max_delay = min_delay, max_delay
        self.engine = engine
        self._closers: list[Callable[[], None]] = []
        profile = DATA_DIR / "profiles" / f"{store}-{engine}"  # profiles are engine-specific
        profile.mkdir(parents=True, exist_ok=True)
        try:
            if engine == "camoufox":
                self.ctx = self._launch_camoufox(profile, headless, proxy)
            else:
                self.ctx = self._launch_chromium(engine, profile, headless, channel, proxy)
        except RuntimeError:
            self.close_runtime()
            raise
        except Exception as e:
            # Missing browser binary, failed auto-download, bad proxy… → let the strategy chain
            # move on to the next engine/method instead of failing the whole store.
            self.close_runtime()
            raise RuntimeError(f"{engine} failed to start ({type(e).__name__}: {e}); "
                               f"install with: {INSTALL_HINTS[engine]}") from e
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        log.info("[%s] browser engine: %s", store, engine)

    # ---------- launchers ----------
    def _launch_chromium(self, engine, profile, headless, channel, proxy):
        try:
            if engine == "patchright":
                from patchright.sync_api import sync_playwright
            else:
                from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(f"{engine} is not installed: {INSTALL_HINTS[engine]}") from e
        pw = sync_playwright().start()
        self._closers.append(pw.stop)
        launch = dict(user_data_dir=str(profile), headless=headless, locale="en-US")
        if proxy:
            launch["proxy"] = {"server": proxy}
        if engine == "patchright":
            # Patchright's recommended stealth setup: real Chrome, no fixed viewport, and no
            # extra flags or custom user agent (those are fingerprints themselves).
            launch["no_viewport"] = True
        else:
            launch.update(viewport={"width": 1366, "height": 860}, timezone_id="America/New_York",
                          args=["--disable-blink-features=AutomationControlled"])
        try:
            return pw.chromium.launch_persistent_context(channel=channel, **launch)
        except Exception as e:
            log.warning("Chrome channel %r unavailable (%s), falling back to bundled Chromium", channel, e)
            return pw.chromium.launch_persistent_context(**launch)

    def _launch_camoufox(self, profile, headless, proxy):
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as e:
            raise RuntimeError(f"camoufox is not installed: {INSTALL_HINTS['camoufox']}") from e
        opts = dict(
            headless=headless,
            persistent_context=True,
            user_data_dir=str(profile),
            humanize=True,          # human-like cursor movement
            main_world_eval=True,   # allow "mw:" evaluate to read page globals
            locale="en-US",
        )
        if proxy:
            opts["proxy"] = {"server": proxy}
            opts["geoip"] = True    # match timezone/locale to the proxy's IP (needs camoufox[geoip])
        manager = Camoufox(**opts)
        ctx = manager.__enter__()
        self._closers.append(lambda: manager.__exit__(None, None, None))
        return ctx

    def close_runtime(self) -> None:
        for fn in reversed(self._closers):
            try:
                fn()
            except Exception as e:
                log.debug("browser runtime close: %s", e)
        self._closers.clear()

    def close(self) -> None:
        if self.engine != "camoufox":
            try:
                self.ctx.close()
            except Exception:
                pass
        self.close_runtime()

    def _eval_main(self, js: str, arg=None):
        """Evaluate in the page's main JS world (needed to read window.* globals).

        Patchright and Camoufox run evaluate() in an isolated world by default to stay invisible.
        """
        if self.engine == "patchright":
            return self.page.evaluate(js, arg, isolated_context=False)
        if self.engine == "camoufox":
            return self.page.evaluate("mw:" + js, arg)
        return self.page.evaluate(js, arg)

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
        # Written as an expression (IIFE) so it works with Camoufox's "mw:" main-world prefix too.
        return self._eval_main(
            """(() => {
              const out = [];
              document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                try { out.push(JSON.parse(s.textContent)); } catch (e) {}
              });
              for (const k of ['__NEXT_DATA__', '__INITIAL_STATE__', '__PRELOADED_STATE__']) {
                const el = document.getElementById(k);
                if (el) { try { out.push(JSON.parse(el.textContent)); } catch (e) {} }
                if (window[k]) { try { out.push(JSON.parse(JSON.stringify(window[k]))); } catch (e) {} }
              }
              return out;
            })()"""
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
