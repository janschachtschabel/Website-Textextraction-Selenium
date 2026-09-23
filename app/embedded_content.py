"""Content a page carries as data: article bodies, KMap lessons and YouTube's video details."""

import json
import re
from html import escape
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .links import absolute_url


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "article", "item"):
            yield from _nodes(value.get(key))


def _attachment_sections(node, fragment, soup, url) -> str:
    """The historic payload names its files as inline:<file>; point those at the real URL
    and list whatever the body did not already use."""
    base_tag = soup.find("base", href=True)
    base = absolute_url(url or "", base_tag["href"] if base_tag else "./") or url or ""
    attachments = [a for a in node.get("attachments", []) if isinstance(a, dict)]
    targets = {
        a.get("file"): target
        for a in attachments
        if isinstance(a.get("href"), str) and (target := absolute_url(base, a["href"]))
    }
    used = set()
    for tag in fragment.select("[href], [src]"):
        for attr in ("href", "src"):
            original = tag.get(attr, "")
            if original.startswith("inline:") and original[7:] in targets:
                tag[attr] = targets[original[7:]]
                used.add(original[7:])
    sections = {}
    for item in attachments:
        name = item.get("name") or item.get("file")
        target = targets.get(item.get("file"))
        if not name or not target or item.get("file") in used:
            continue
        heading = {"explanation": "Explanations", "idea": "Ideas", "usage": "Applications"}.get(
            item.get("tag"), "Attachments"
        )
        sections.setdefault(heading, []).append(
            f'<li><a href="{escape(target, quote=True)}">{escape(str(name))}</a></li>'
        )
    return "".join(f"<h2>{heading}</h2><ul>{''.join(items)}</ul>" for heading, items in sections.items())


_YOUTUBE_PLAYER = re.compile(r"\bytInitialPlayerResponse\s*=\s*")


def youtube_video(soup: BeautifulSoup) -> str | None:
    """A YouTube watch page's title, channel and description, from the player data it embeds.

    Plain HTTP gets only the page's footer as text, and a fresh browser in the EU meets a consent
    page, but the watch page carries the video's text in ytInitialPlayerResponse.
    """
    for script in soup.find_all("script", src=False):
        text = script.string or ""
        match = _YOUTUBE_PLAYER.search(text)
        if not match:
            continue
        try:
            player, _ = json.JSONDecoder().raw_decode(text, match.end())
        except ValueError:
            return None
        details = player.get("videoDetails") if isinstance(player, dict) else None
        title = details.get("title") if isinstance(details, dict) else None
        if not isinstance(title, str) or not title.strip():
            return None
        parts = [f"<h1>{escape(title)}</h1>"]
        author = details.get("author")
        if isinstance(author, str) and author.strip():
            parts.append(f"<p>{escape(author)}</p>")
        description = details.get("shortDescription")
        if isinstance(description, str):
            parts += [f"<p>{escape(line)}</p>" for line in description.splitlines() if line.strip()]
        return "".join(parts)
    return None


def embedded_html(soup: BeautifulSoup, url: str | None) -> str | None:
    """Use articleBody, never a generic SEO description as a full document."""
    host = (urlsplit(url or "").hostname or "").lower()
    if host == "youtube.com" or host.endswith(".youtube.com"):
        video = youtube_video(soup)
        if video:
            return video
    for script in soup.select(
        'script[type="json"], script[type="application/json"], script[type="application/ld+json"]'
    ):
        raw = script.get_text().strip()
        historic = script.get("id") == "embedded-topic"
        if historic:
            raw = raw[raw.find("{") : raw.rfind("}") + 1]
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue  # Scripts need not contain a supported JSON payload.
        for node in _nodes(value):
            body = node.get("articleBody") or (node.get("description") if historic else None)
            if not isinstance(body, str) or not body.strip():
                continue
            fragment = BeautifulSoup(body, "lxml")
            title = node.get("title") or node.get("headline") or node.get("name") or node.get("topic")
            prefix = f"<h1>{escape(title)}</h1>" if isinstance(title, str) else ""
            if not historic:
                visible = soup.select_one('article, main, [role="main"]')
                if visible and len(visible.get_text(" ", strip=True)) >= 200:
                    return None
                return prefix + str(fragment)
            # Resolve first: the call rewrites the inline: references inside fragment,
            # and the operands of a return expression are evaluated left to right.
            sections = _attachment_sections(node, fragment, soup, url)
            return prefix + str(fragment) + sections
    return None
