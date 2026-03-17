from __future__ import annotations

import ipaddress
import random
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]
_LOOPBACK_NAMES = {"localhost", "ip6-localhost", "ip6-loopback", "loopback"}


def is_ssrf_url(url: str) -> bool:
    """Return True if *url* targets a private/internal address (SSRF risk).

    Blocks loopback hostnames and any host that resolves to a private IP
    literal.  Hostnames that require DNS resolution are allowed through
    (DNS-rebinding protection requires an additional resolver check which
    is outside the scope of this function).
    """
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_NETS)
    except ValueError:
        pass  # hostname – not an IP literal, allow through
    return False


# Precise multi-word phrases that reliably signal error/bot-wall pages.
# Single-word terms like "error", "fehler", "404", "not found" were removed
# because they produce massive false-positives on legitimate content pages
# (e.g. a tutorial *about* 404 errors, or any page containing the word "error").
_ERROR_PATTERNS = re.compile(
    r"("
    # English – HTTP error pages
    r"page not found"
    r"|this page (could not be found|does not exist|is no longer available)"
    r"|404 (–|-|error|page)"
    r"|(403|401|500|502|503|504)\s+(forbidden|error|bad gateway|service unavailable|gateway timeout)"
    r"|access denied"
    r"|temporarily unavailable"
    r"|we('re| are) currently under maintenance"
    r"|bad gateway"
    r"|gateway timeout"
    r"|service unavailable"
    # Bot / captcha walls
    r"|just a moment\.\.\."
    r"|checking your browser"
    r"|verifying you are human"
    r"|attention required"
    r"|enable javascript to continue"
    r"|please (enable|turn on) javascript"
    # German – HTTP error pages
    r"|seite (wurde )?nicht gefunden"
    r"|diese seite (existiert nicht|ist nicht mehr verfügbar)"
    r"|404[ -](fehler|seite)"
    r"|(403|401|500|502|503|504)\s+(verboten|fehler|nicht erreichbar)"
    r"|zugriff verweigert"
    r"|vorübergehend nicht verfügbar"
    r"|wir (arbeiten derzeit|sind gerade) an (der|einem) wartung"
    r"|javascript (wird benötigt|ist deaktiviert|ist erforderlich)"
    r"|bitte (aktivieren|einschalten) sie javascript"
    r")",
    re.IGNORECASE,
)


def detect_error_page(
    text: str,
    status_code: int | None,
    *,
    check_thin: bool = False,
) -> bool:
    """Return True when *text* looks like an error or empty page.

    Args:
        text:         HTML or Markdown to inspect.
        status_code:  HTTP status of the response (>= 400 → always True).
        check_thin:   When True, also flag pages whose converted Markdown has
                      fewer than 50 words, or fewer than 150 words AND no
                      discernible structure (headings / lists / paragraphs).
                      Pass check_thin=True only for the final Markdown, not
                      for raw HTML (which is naturally verbose).
    """
    if status_code and status_code >= 400:
        return True
    if _ERROR_PATTERNS.search(text):
        return True
    if check_thin and text:
        words = len(text.split())
        if words < 50:
            return True
        if words < 150:
            has_structure = bool(
                re.search(r"^#{1,6} ", text, re.MULTILINE)
                or re.search(r"^\s*[-*] ", text, re.MULTILINE)
                or text.count("\n\n") >= 2
            )
            if not has_structure:
                return True
    return False


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
    "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
    "youtube.com", "t.me", "telegram.org", "tiktok.com", "mastodon.social",
    "github.com", "medium.com", "reddit.com", "xing.com", "pinterest.com",
    "snapchat.com", "discord.com", "twitch.tv", "vimeo.com",
}

DOWNLOAD_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".csv", ".txt", ".rtf", ".odt", ".ods", ".odp",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav",
    ".epub", ".mobi",
}

# Non-navigable schemes whose links should be omitted from output
_SKIP_SCHEMES = frozenset({"javascript:", "data:", "blob:", "vbscript:"})

_RE_LEGAL = re.compile(
    r"/(impressum|datenschutz(erkl[äa]rung)?|privacy([\-_]policy)?|"
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
_NAV_TEXTS = frozenset({
    "home", "start", "startseite", "nach oben", "back to top", "top",
    "menu", "menü", "navigation", "zurück", "back",
    "übersicht", "overview", "sitemap",
    "→", "←", "›", "‹", "»", "«", "▲", "▸", "◂",
})


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
        items.append({
            "url": absolute,
            "text": text,
            "internal": internal,
            "category": category,
        })
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
