"""Classified outbound links for the response; runs inside conversion workers."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

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


def absolute_url(base: str, href: str) -> str | None:
    """href resolved against base; None when no URL parser accepts it - a template placeholder
    such as http://[URL] - so that one such link does not cost a page its conversion."""
    try:
        return urljoin(base, href)
    except ValueError:
        return None


def _is_internal(link: str, base_url: str) -> bool:
    try:
        ah = (urlparse(link).hostname or "").lower().removeprefix("www.")
        bh = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
        return bool(ah) and ah == bh
    except Exception:
        return False


# Checked in this order; the first pattern that matches names the category.
_URL_PATTERNS = ((_RE_LEGAL, "legal"), (_RE_AUTH, "auth"), (_RE_SEARCH, "search"), (_RE_CONTACT, "contact"))


def _scheme_category(url: str, raw_href: str) -> str | None:
    """The categories that follow from the scheme alone, before anything is parsed."""
    if raw_href.startswith("#"):  # an in-page fragment, which the absolute URL no longer shows
        return "anchor"
    if url.startswith(("mailto:", "tel:")):
        return "contact"
    if any(url.startswith(scheme) for scheme in _SKIP_SCHEMES):
        return "other"
    return None


def _host_and_path(url: str) -> tuple[str, str]:
    try:
        parsed = urlparse(url)
        return parsed.hostname or "", parsed.path
    except Exception:
        return "", ""


def _classify_link(absolute_url: str, raw_href: str, text: str | None) -> str:
    """Classify a link given its absolute URL, original href, and visible text."""
    url = absolute_url.lower()
    by_scheme = _scheme_category(url, raw_href)
    if by_scheme:
        return by_scheme
    host, path = _host_and_path(url)
    # Social domains: exact match or any subdomain (e.g. www.twitter.com)
    if any(host == domain or host.endswith("." + domain) for domain in SOCIAL_DOMAINS):
        return "social"
    for pattern, category in _URL_PATTERNS:
        if pattern.search(url):
            return category
    # Download by file extension; urlparse already stripped the query and fragment.
    if any(path.endswith(extension) for extension in DOWNLOAD_EXTS):
        return "download"
    if text and " ".join(text.split()).lower() in _NAV_TEXTS:
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

        absolute = absolute_url(base_url, raw_href)
        if absolute is None:
            continue

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
