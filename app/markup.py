"""Conservative Markdown postprocessing and source equation preservation."""

import re

from bs4 import BeautifulSoup, UnicodeDammit

from .mathml import to_latex


def decode_text(data: bytes, content_type: str | None = None) -> str:
    # UnicodeDammit decodes strictly and then guesses: one bad byte, or a character cut at
    # max_bytes, would turn a whole UTF-8 page into mojibake. Settle UTF-8 first. Bytes in the
    # ASCII range keep their declared charset (ISO-2022-JP, UTF-16 without BOM).
    if not data.isascii():
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                return exc.object[: exc.start].decode("utf-8")
        text = data.decode("utf-8-sig", errors="replace")
        invalid = text.count("\ufffd")
        non_ascii = len(text) - len(text.encode("ascii", errors="ignore"))
        if non_ascii - invalid > invalid:
            return text  # UTF-8 with a few stray bytes
    match = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', content_type or "", re.I)
    encoding = [match.group(1)] if match else []
    return UnicodeDammit(data, known_definite_encodings=encoding, is_html=True).unicode_markup or ""


def enhance_table_structure(text: str) -> str:
    lines = text.split("\n")
    result = []
    fence = None
    in_table = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^(`{3,}|~{3,})", stripped):
            marker = stripped[0]
            fence = None if fence == marker else (fence or marker)
        row = not fence and stripped.startswith("|") and stripped.endswith("|")
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        result.append(line)
        if row and not in_table and next_line.startswith("|") and next_line.endswith("|"):
            cells = re.split(r"(?<!\\)\|", stripped)[1:-1]
            next_cells = re.split(r"(?<!\\)\|", next_line)[1:-1]
            separator = all(re.fullmatch(r"\s*:?-{3,}:?\s*", c) for c in next_cells)
            if len(cells) == len(next_cells) and not separator:
                result.append("|" + "|".join(" --- " for _ in cells) + "|")
        in_table = row
    return "\n".join(result)


_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def _presentation(equation) -> str:
    """MathML carries the structure of the formula; other markup holds rendered glyphs only."""
    if equation.name == "math":
        return to_latex(equation) or equation.get_text(" ", strip=True)
    return equation.get_text(" ", strip=True)


def _reveal(replacement):
    """Unhide the wrappers that exist only to keep this formula from sighted readers.

    Wikipedia ships its MathML in a ``display: none`` span next to a rendered image, and
    extractors drop hidden content - so the substituted LaTeX would never reach the output.
    A wrapper holding anything besides the formula keeps its styling: one more element, even
    a text-free one such as a tracking pixel, ends the walk.
    """
    formula = replacement.string
    for parent in replacement.parents:
        children = [child for child in parent.children if getattr(child, "name", None)]
        if parent.name in {"body", "html", "[document]"} or len(children) != 1:
            return
        if parent.get_text(" ", strip=True) != formula:
            return
        if _HIDDEN.search(parent.get("style", "")):
            del parent["style"]
        for attribute in ("aria-hidden", "hidden"):
            parent.attrs.pop(attribute, None)


def prepare_html(data: bytes, content_type: str | None) -> BeautifulSoup:
    soup = BeautifulSoup(decode_text(data, content_type), "lxml")
    for equation in soup.select('script[type^="math/tex"], math, mjx-container'):
        if not equation.parent:
            continue
        annotation = equation.select_one('annotation[encoding="application/x-tex"]')
        source = (
            equation.get("data-tex")
            or equation.get("data-latex")
            or (annotation.get_text() if annotation else _presentation(equation))
        )
        if source:
            replacement = soup.new_tag("span")
            replacement.string = "$$" + source + "$$"
            equation.replace_with(replacement)
            _reveal(replacement)
    for tag in soup.select("[data-tex], [data-latex]"):
        source = tag.get("data-tex") or tag.get("data-latex")
        tag.clear()
        tag.append("$$" + str(source) + "$$")
    return soup
