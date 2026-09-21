"""Embedded article bodies and the existing KMap lesson format."""

import json
from html import escape
from urllib.parse import urljoin

from bs4 import BeautifulSoup


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
    base = urljoin(url or "", base_tag["href"] if base_tag else "./")
    attachments = [a for a in node.get("attachments", []) if isinstance(a, dict)]
    targets = {a.get("file"): urljoin(base, a["href"]) for a in attachments if isinstance(a.get("href"), str)}
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


def embedded_html(soup: BeautifulSoup, url: str | None) -> str | None:
    """Use articleBody, never a generic SEO description as a full document."""
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
