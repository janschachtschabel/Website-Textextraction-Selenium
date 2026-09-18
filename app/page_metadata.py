"""Descriptive page metadata for the response; runs inside conversion workers."""

from trafilatura import extract_metadata, load_html

MAX_FIELD_LENGTH = 1000  # page-controlled text; keeps metadata from carrying a second document


def page_metadata(html: str, url: str) -> dict | None:
    """What the page declares about itself, or None when the input is not HTML.

    Undeclared canonical URLs fall back to ``url`` and undeclared site names to its host,
    following trafilatura; its URL validation keeps script URLs out of ``canonical_url``.
    """
    tree = load_html(html)
    if tree is None:
        return None
    language = tree.get("lang") or None  # read before trafilatura walks the tree
    document = extract_metadata(tree, default_url=url)
    values = {
        "title": document.title,
        "description": document.description,
        "author": document.author,
        "date": document.date,
        "site_name": document.sitename,
        "canonical_url": document.url,
        "language": language,
    }
    return {name: value[:MAX_FIELD_LENGTH] if value else None for name, value in values.items()}
