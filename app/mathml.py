"""Presentation MathML to LaTeX, for pages that publish no TeX annotation.

Flattening MathML to its text loses exactly what the markup carries: a radical, a
fraction bar and an exponent all become adjacent tokens, so ``a 1 2 + a 2 2`` is all a
reader gets back of the length of a vector. Serlo publishes presentation MathML only.

Everything a page controls - element text and the fence attributes - is escaped, because
the result is embedded in ``$$...$$`` inside Markdown. Unicode letters that LaTeX has no
command for, Greek and accented characters among them, are passed through; both KaTeX and
MathJax accept them in math mode.
"""

_BACKSLASH = chr(92)

# Page text must not turn into LaTeX commands or grouping. Letter commands carry an empty
# group, or they would swallow the character that follows them.
_ESCAPED = {
    _BACKSLASH: r"\backslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "^": r"\hat{}",
    "~": r"\sim{}",
}

_OPERATORS = {
    "−": "-",
    "⋅": r"\cdot",
    "×": r"\times",
    "÷": r"\div",
    "≤": r"\leq",
    "≥": r"\geq",
    "≠": r"\neq",
    "≈": r"\approx",
    "≡": r"\equiv",
    "±": r"\pm",
    "∓": r"\mp",
    "∞": r"\infty",
    "∑": r"\sum",
    "∏": r"\prod",
    "∫": r"\int",
    "√": r"\surd",
    "∈": r"\in",
    "∉": r"\notin",
    "⊂": r"\subset",
    "⊆": r"\subseteq",
    "∪": r"\cup",
    "∩": r"\cap",
    "→": r"\to",
    "⇒": r"\Rightarrow",
    "↔": r"\leftrightarrow",
    "⇔": r"\Leftrightarrow",
    "∀": r"\forall",
    "∃": r"\exists",
    "∥": r"\parallel",
    "⊥": r"\perp",
    "∠": r"\angle",
    # A degree sign must never map to ^{...}: inside an msup that is a second superscript.
    "°": r"\circ",
    "∘": r"\circ",
    "…": r"\ldots",
    "⋯": r"\cdots",
    "′": "'",
}

_LETTERS = {
    "ℂ": r"\mathbb{C}",
    "ℕ": r"\mathbb{N}",
    "ℙ": r"\mathbb{P}",
    "ℚ": r"\mathbb{Q}",
    "ℝ": r"\mathbb{R}",
    "ℤ": r"\mathbb{Z}",
}

# The over-element of an accent names the accent; everything else is a generic overset.
_ACCENTS = {
    "→": r"\vec",  # rightwards arrow
    chr(0x20D7): r"\vec",  # combining arrow above
    "¯": r"\overline",  # macron
    "‾": r"\overline",  # overline
    "^": r"\hat",
    "ˆ": r"\hat",  # modifier circumflex accent
    "~": r"\tilde",
    "˜": r"\tilde",  # small tilde
    "˙": r"\dot",  # dot above
    "·": r"\dot",  # middle dot
}

_LEAVES = {"mi", "mn", "mo", "mtext", "ms"}
_IGNORED = {"mspace", "mphantom", "annotation", "annotation-xml"}
MAX_DEPTH = 64  # a page can nest without limit; deeper than any real formula, its text is enough


def to_latex(element) -> str:
    """LaTeX for a presentation MathML element, empty when it carries nothing convertible."""
    return _convert(element)


def _elements(node):
    return [child for child in node.children if getattr(child, "name", None)]


def _join(parts) -> str:
    return " ".join(part for part in parts if part)


def _arguments(node, depth: int) -> list[str]:
    return [_convert(child, depth + 1) for child in _elements(node)]


def _leaf(node, name) -> str:
    text = node.get_text().strip()
    if not text:
        return ""
    if name == "mo":
        return _OPERATORS.get(text, _escape(text))
    if name == "mtext":
        return r"\text{" + _escape(text) + "}"
    return _LETTERS.get(text, _escape(text))


def _escape(text: str) -> str:
    return "".join(_ESCAPED.get(character, character) for character in text)


def _group(latex: str) -> str:
    return "{" + latex + "}"


def _over(node, depth) -> str:
    parts = _arguments(node, depth)
    if len(parts) != 2:
        return _join(parts)
    accent = _ACCENTS.get(_elements(node)[1].get_text().strip())
    if accent:
        return accent + _group(parts[0])
    return r"\overset" + _group(parts[1]) + _group(parts[0])


def _under(node, depth) -> str:
    parts = _arguments(node, depth)
    if len(parts) != 2:
        return _join(parts)
    return r"\underset" + _group(parts[1]) + _group(parts[0])


def _root(node, depth) -> str:
    parts = _arguments(node, depth)
    if len(parts) != 2:
        return _join(parts)
    index = _group(parts[1]) if "]" in parts[1] else parts[1]  # a bracket would close the argument
    return r"\sqrt[" + index + "]" + _group(parts[0])


def _scripts(marks: str, count: int):
    """msup/msub/msubsup/munderover all read as a base plus one or two scripts."""

    def convert(node, depth) -> str:
        parts = _arguments(node, depth)
        if len(parts) != count + 1:
            return _join(parts)
        return parts[0] + "".join(mark + _group(part) for mark, part in zip(marks, parts[1:], strict=True))

    return convert


def _fraction(node, depth) -> str:
    parts = _arguments(node, depth)
    if len(parts) != 2:
        return _join(parts)
    return r"\frac" + _group(parts[0]) + _group(parts[1])


def _table(node, depth) -> str:
    rows = [row for row in _arguments(node, depth) if row]
    if not rows:
        return ""
    return r"\begin{matrix} " + (" " + r"\cr" + " ").join(rows) + r" \end{matrix}"


def _fenced(node, depth) -> str:
    # The fences are attributes of a page-controlled element, so they are page text too.
    return _escape(node.get("open", "(")) + _join(_arguments(node, depth)) + _escape(node.get("close", ")"))


_HANDLERS = {
    "msqrt": lambda node, depth: r"\sqrt" + _group(_join(_arguments(node, depth))),
    "mroot": _root,
    "msup": _scripts("^", 1),
    "msub": _scripts("_", 1),
    "msubsup": _scripts("_^", 2),
    "munderover": _scripts("_^", 2),
    "mover": _over,
    "munder": _under,
    "mfrac": _fraction,
    "mtable": _table,
    "mtr": lambda node, depth: " & ".join(cell for cell in _arguments(node, depth) if cell),
    "mfenced": _fenced,
}


def _convert(node, depth: int = 0) -> str:
    name = (node.name or "").lower()
    if name in _IGNORED:
        return ""
    if name in _LEAVES:
        return _leaf(node, name)
    if depth >= MAX_DEPTH:
        return _escape(node.get_text(" ", strip=True))
    handler = _HANDLERS.get(name)
    if handler:
        return handler(node, depth)
    return _join(_arguments(node, depth))  # math, mrow, mstyle, semantics and unknown wrappers
