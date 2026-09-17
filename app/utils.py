from __future__ import annotations

import random
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def is_ssrf_url(url: str) -> bool:
    """Compatibility helper; actual connections additionally use the egress guard."""
    from .results import CrawlError
    from .security import resolve_target

    try:
        resolve_target(url)
        return False
    except CrawlError:
        return True


def detect_error_page(text: str, status_code: int | None, *, check_thin: bool = False) -> bool:
    """Short valid pages are useful; empty results and explicit blocks are failures."""
    from .preflight import blocked_content

    return blocked_content(text, status_code) or (check_thin and not text.strip())


def extract_links_from_html(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue
        absolute = urljoin(base_url, href)
        links.append(absolute)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    return unique


# Heuristics for link classification
SOCIAL_DOMAINS = {
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "t.me",
    "telegram.org",
    "tiktok.com",
    "mastodon.social",
    "github.com",
    "medium.com",
    "reddit.com",
    "xing.com",
    "pinterest.com",
    "snapchat.com",
    "discord.com",
    "twitch.tv",
    "vimeo.com",
}

DOWNLOAD_EXTS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".csv",
    ".txt",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".mkv",
    ".wav",
    ".epub",
    ".mobi",
}

# Non-navigable schemes whose links should be omitted from output
_SKIP_SCHEMES = frozenset({"javascript:", "data:", "blob:", "vbscript:"})

_RE_LEGAL = re.compile(
    r"/(impressum|datenschutz(erkl[äa]e?rung)?|privacy([\-_]policy)?|"
    r"agb|terms([\-_]of[\-_](service|use))?|"
    r"cookie(s|[\-_]policy|[\-_]settings|einstellungen)?|"
    r"nutzungsbedingungen|haftungsausschluss|disclaimer|imprint|"
    r"rechtliches|widerruf)($|/|\?|#)",
    re.IGNORECASE,
)
_RE_AUTH = re.compile(
    r"/(login|logout|sign[\-_]?(in|out|up)|register|signup|"
    r"account|my[\-_]?account|mein[\-_]?konto|profil(e)?|"
    r"anmeld(en|ung)|abmeld(en|ung)|registrier(en|ung)|"
    r"passwort|password[\-_]?reset)($|/|\?|#)",
    re.IGNORECASE,
)
_RE_SEARCH = re.compile(
    r"/(search|suche|recherche)($|/|\?|#)"
    r"|[?&](q|query|search|suche|s|keyword)=",
    re.IGNORECASE,
)
_RE_CONTACT = re.compile(
    r"/(contact([\-_]us?)?|kontakt|support|help|hilfe|feedback|"
    r"write[\-_]to[\-_]us|reach[\-_]us)($|/|\?|#)",
    re.IGNORECASE,
)
_NAV_TEXTS = frozenset(
    {
        "home",
        "start",
        "startseite",
        "nach oben",
        "back to top",
        "top",
        "menu",
        "menü",
        "navigation",
        "zurück",
        "back",
        "übersicht",
        "overview",
        "sitemap",
        "→",
        "←",
        "›",
        "‹",
        "»",
        "«",
        "▲",
        "▸",
        "◂",
    }
)


def _is_internal(link: str, base_url: str) -> bool:
    try:
        ah = (urlparse(link).hostname or "").lower().removeprefix("www.")
        bh = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
        return bool(ah) and ah == bh
    except Exception:
        return False


def _classify_link(absolute_url: str, raw_href: str, text: str | None) -> str:
    """Classify a link given its absolute URL, original href, and visible text."""
    # In-page fragment anchors (original href begins with #)
    if raw_href.startswith("#"):
        return "anchor"

    u = absolute_url.lower()

    # mailto / tel → contact
    if u.startswith(("mailto:", "tel:")):
        return "contact"

    # Non-navigable schemes
    if any(u.startswith(s) for s in _SKIP_SCHEMES):
        return "other"

    try:
        parsed = urlparse(u)
        host = parsed.hostname or ""
        path = parsed.path
    except Exception:
        host = ""
        path = ""

    # Social domains: exact match or any subdomain (e.g. www.twitter.com)
    if any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS):
        return "social"

    # Path/query-based classification
    if _RE_LEGAL.search(u):
        return "legal"
    if _RE_AUTH.search(u):
        return "auth"
    if _RE_SEARCH.search(u):
        return "search"
    if _RE_CONTACT.search(u):
        return "contact"

    # Download by file extension (path already stripped of query/fragment by urlparse)
    pl = path.lower()
    if any(pl.endswith(ext) for ext in DOWNLOAD_EXTS):
        return "download"

    # Nav heuristics via visible link text
    if text:
        t = " ".join(text.split()).lower()
        if t in _NAV_TEXTS:
            return "nav"

    return "content"


def extract_links_detailed_from_html(html: str, base_url: str) -> list[dict]:
    """Return list of dicts: {url, text, internal, category}.

    Uses heuristics to classify links and determines internal vs external.
    Deduplicates by absolute URL (first occurrence wins).
    Skips non-navigable hrefs (javascript:, data:, blob:, vbscript:).
    """
    soup = BeautifulSoup(html, "lxml")
    seen_urls: set[str] = set()
    items: list[dict] = []
    for tag in soup.find_all("a", href=True):
        raw_href = (tag.get("href") or "").strip()
        if not raw_href:
            continue

        # Skip non-navigable schemes before URL resolution
        rh_lower = raw_href.lower()
        if any(rh_lower.startswith(s) for s in _SKIP_SCHEMES):
            continue

        absolute = urljoin(base_url, raw_href)

        # Deduplicate by absolute URL
        if absolute in seen_urls:
            continue
        seen_urls.add(absolute)

        # Normalise link text; fall back to aria-label / title for icon-only links
        raw_text = tag.get_text(" ", strip=True)
        text: str | None = " ".join(raw_text.split()) if raw_text else None
        if not text:
            for attr in ("aria-label", "title"):
                val = (tag.get(attr) or "").strip()
                if val:
                    text = " ".join(val.split())
                    break

        category = _classify_link(absolute, raw_href, text)
        internal = _is_internal(absolute, base_url)
        items.append(
            {
                "url": absolute,
                "text": text,
                "internal": internal,
                "category": category,
            }
        )
    return items


MIME_TO_EXT = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/plain": ".txt",
    "application/json": ".json",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def guess_extension(content_type: str | None, default: str = ".bin") -> str:
    if not content_type:
        return default
    ctype = content_type.split(";")[0].strip().lower()
    return MIME_TO_EXT.get(ctype, default)


def normalize_proxy(proxy: str | None) -> str | None:
    """Return a valid proxy URL or None.

    - Treat "string" or "" or whitespace as None (OpenAPI default noise)
    - Require a scheme in {http, https, socks5, socks5h, socks4}; otherwise None
    """
    if not proxy:
        return None
    s = proxy.strip()
    if not s or s.lower() == "string":
        return None
    parsed = urlparse(s)
    if parsed.scheme.lower() in {"http", "https", "socks5", "socks5h", "socks4"}:
        return s
    return None


UA_POOL = [
    # Modern desktop Chrome variants
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    # A Firefox variant
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]


def pick_user_agent(default_ua: str | None = None) -> str:
    pool = UA_POOL.copy()
    if default_ua and default_ua not in pool:
        pool.append(default_ua)
    return random.choice(pool)
