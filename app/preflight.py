from __future__ import annotations

import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .http_fetcher import _make_client, get_http_client


async def preflight(
    url: str,
    timeout_seconds: int,
    user_agent: str,
    allow_insecure_ssl: bool | None = None,
) -> dict[str, Any]:
    """Lightweight HTTP probe to choose a crawl strategy.

    Returns a dict with keys:
    - status: int
    - final_url: str
    - content_type: Optional[str]
    - content_bytes: bytes (may be empty if not text-like)
    - html_text: Optional[str]
    - features: dict
    - strategy: str (HTTP_ONLY | JS_LIGHT | JS_LIGHT_CONSENT | HTTP_THEN_JS | PDF | RSS | YOUTUBE | BLOCKED)
    """
    req_headers = {"User-Agent": user_agent}
    timeout = httpx.Timeout(timeout_seconds)
    insecure = allow_insecure_ssl if allow_insecure_ssl is not None else settings.allow_insecure_ssl

    # HTML analysis only needs the first chunk; cap larger downloads to avoid
    # loading multi-MB pages entirely into memory for a probe request.
    _HTML_PREVIEW_BYTES = 512 * 1024   # 512 KB – sufficient for all feature checks
    _BINARY_MAX_BYTES   = settings.default_max_bytes  # PDF/RSS reused as final content

    async def _stream(client: httpx.AsyncClient):
        async with client.stream("GET", url, headers=req_headers, timeout=timeout) as r:
            status = r.status_code
            final_url = str(r.url)
            ctype = (r.headers.get("content-type") or "").lower()
            is_binary = (
                ctype.startswith("application/pdf")
                or "application/rss" in ctype
                or "application/atom+xml" in ctype
                or final_url.lower().endswith(".pdf")
            )
            cap = _BINARY_MAX_BYTES if is_binary else _HTML_PREVIEW_BYTES
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                if chunk:
                    buf.extend(chunk)
                    if len(buf) >= cap:
                        break
            return r, status, final_url, ctype, bytes(buf[:cap])

    if insecure:
        async with _make_client(verify=False) as tmp_client:
            r, status, final_url, ctype, raw_bytes = await _stream(tmp_client)
    else:
        r, status, final_url, ctype, raw_bytes = await _stream(get_http_client())

    orig_ctype = r.headers.get("content-type")  # preserve original casing

    # Hard blocks: server explicitly refuses or rate-limits → no point running Selenium
    if status in (403, 407, 429) or status >= 500:
        return {
            "status": status,
            "final_url": final_url,
            "content_type": orig_ctype,
            "content_bytes": raw_bytes,
            "html_text": None,
            "features": {"blocked_status": status},
            "strategy": "BLOCKED",
        }

    # Quick binary types
    if ctype.startswith("application/pdf") or final_url.lower().endswith(".pdf"):
        return {
            "status": status,
            "final_url": final_url,
            "content_type": orig_ctype,
            "content_bytes": raw_bytes,
            "html_text": None,
            "features": {},
            "strategy": "PDF",
        }

    # RSS/Atom
    if "application/rss" in ctype or "application/atom+xml" in ctype:
        return {
            "status": status,
            "final_url": final_url,
            "content_type": orig_ctype,
            "content_bytes": raw_bytes,
            "html_text": None,
            "features": {},
            "strategy": "RSS",
        }

    text = raw_bytes.decode("utf-8", errors="ignore")
    # Prefer XML parser for XML content-types to avoid warnings
    soup = BeautifulSoup(text, "xml") if ("xml" in ctype and "html" not in ctype) else BeautifulSoup(text, "lxml")

    # Features
    # Strip <script>/<style> before measuring visible text length so that
    # inline JSON blobs (window.__INITIAL_STATE__, __next_data__, etc.) in
    # SPAs don't inflate the count and falsely trigger HTTP_ONLY strategy.
    _soup_text = soup.__copy__()
    for _tag in _soup_text.find_all(["script", "style", "noscript"]):
        _tag.decompose()
    text_len = len(_soup_text.get_text(" ", strip=True))
    has_main = bool(soup.select_one("main, article, #content, #main-content, [role=main], #app, #__next, #root"))
    html_lower = text.lower()
    spa_mark = any(k in html_lower for k in ("__next_data__", "window.__nuxt__", "ng-version", "__apollo_state__"))
    js_required = re.search(r"(enable javascript|activate javascript|ohne javascript)", html_lower, re.I) is not None
    consent = re.search(r"(consent|cookie|datenschutz).*?(accept|zustimmen|einverstanden)", html_lower, re.I) is not None
    bot_wall = re.search(
        r"(captcha"
        r"|just a moment"
        r"|attention required"
        r"|checking your browser"
        r"|checking if the site connection"
        r"|please verify you are"
        r"|ray id.{0,40}cloudflare"
        r"|cloudflare.{0,40}ray id"
        r"|enable cookies.*cloudflare"
        r")",
        html_lower,
        re.I,
    ) is not None
    rss_link = bool(soup.select("link[type='application/rss+xml'], link[type='application/atom+xml']"))

    # YouTube quick path
    you = ("youtube.com/watch" in final_url.lower()) or ("youtu.be/" in final_url.lower())

    # Strategy selection
    if bot_wall:
        strat = "BLOCKED"
    elif you:
        strat = "YOUTUBE"
    elif rss_link:
        strat = "RSS"
    elif text_len >= 800 and (has_main or not spa_mark) and not js_required and not consent:
        strat = "HTTP_ONLY"
    elif spa_mark or (has_main and text_len < 500) or js_required or consent:
        strat = "JS_LIGHT_CONSENT" if consent else "JS_LIGHT"
    else:
        strat = "HTTP_THEN_JS"

    return {
        "status": status,
        "final_url": final_url,
        "content_type": orig_ctype,
        "content_bytes": raw_bytes,
        "html_text": text,
        "features": {
            "text_len": text_len,
            "has_main": has_main,
            "spa_mark": spa_mark,
            "js_required": js_required,
            "consent": consent,
            "bot_wall": bot_wall,
            "rss_link": rss_link,
            "youtube": you,
        },
        "strategy": strat,
    }
