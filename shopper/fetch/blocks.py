"""Recognise anti-bot block / challenge pages so we can back off instead of parsing garbage."""

from __future__ import annotations

import re


class Blocked(Exception):
    """The site served a block or challenge page instead of content."""

    def __init__(self, reason: str, url: str = "", status: int | None = None):
        super().__init__(f"{reason} (status={status}) {url}")
        self.reason = reason
        self.url = url
        self.status = status


# (vendor, regex) — checked against the first chunk of the body
_MARKERS: list[tuple[str, re.Pattern]] = [
    ("akamai", re.compile(r"Access Denied.{0,400}(edgesuite|Reference\s*#)", re.I | re.S)),
    ("akamai", re.compile(r"_abck|bm-verify|akamai-bot", re.I)),
    ("perimeterx", re.compile(r"px-captcha|_pxCaptcha|Press\s*&(amp;)?\s*Hold|perimeterx", re.I)),
    ("cloudflare", re.compile(r"cf-chl-|Just a moment\.\.\.|cf_chl_opt|challenge-platform", re.I)),
    ("datadome", re.compile(r"datadome|captcha-delivery\.com", re.I)),
    ("incapsula", re.compile(r"_Incapsula_Resource|Incapsula incident", re.I)),
    ("captcha", re.compile(r"g-recaptcha|h-captcha|hcaptcha\.com|are you a (human|robot)", re.I)),
]

BLOCK_STATUSES = {401, 403, 405, 429, 503}


def detect_block(status: int, body: str, content_type: str = "") -> str | None:
    """Return a reason string if the response looks like a block, else None."""
    head = body[:20000] if body else ""
    is_json = "json" in content_type.lower()
    # JSON bodies with a 200 are almost always real data; vendor scripts (e.g. Akamai's
    # _abck cookie script) are present on many normal HTML pages, so only treat markers as a
    # block when the page is small or the status is already bad.
    if status == 200 and is_json:
        return None
    for vendor, rx in _MARKERS:
        if rx.search(head):
            if status in BLOCK_STATUSES or len(body) < 30000 or vendor in ("perimeterx", "captcha", "datadome"):
                return f"{vendor} challenge"
    if status == 429:
        return "rate limited (429)"
    if status in BLOCK_STATUSES:
        return f"http {status}"
    return None
