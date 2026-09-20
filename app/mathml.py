"""Presentation MathML to LaTeX, for pages that publish no TeX annotation.

Flattening MathML to its text loses exactly what the markup carries: a radical, a
fraction bar and an exponent all become adjacent tokens, so ``a 1 2 + a 2 2`` is all a
reader gets back of the length of a vector. Serlo publishes presentation MathML only.

Unicode letters that LaTeX has no command for - Greek, script and accented characters -
are passed through; both KaTeX and MathJax accept them in math mode.
"""

_BACKSLASH = chr(92)

# Page-controlled text must not turn into LaTeX commands or grouping.
_ESCAPED = {
    _BACKSLASH: r"\backslash",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "^": r"\hat{}",
    "~": r"\sim",
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
    "°": r"^{\circ}",
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
    "→": r"\vec",
    "⃗": r"\vec",
    "¯": r"\overline",
    "‾": r"\overline",
    "^": r"\hat",
    "ˆ": r"\hat",
    "~": r"\tilde",
    "˜": r"\tilde",
    "˙": r"\dot",
    "·": r"\dot",
}

_LEAVES = {"mi", "mn", "mo", "mtext", "ms"}
_IGNORED = {"mspace", "mphantom", "annotation", "annotation-xml"}


def to_latex(element) -> str:
    """LaTeX for a presentation MathML element, empty when it carries nothing convertible."""
    return _convert(element)


def _elements(node):
    return [child for child in node.children if getattr(child, "name", None)]


def _join(parts) -> str:
    return " ".join(part for part in parts if part)


def _arguments(node) -> list[str]:
    return [_convert(child) for child in _elements(node)]


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


def _over(node) -> str:
    parts = _arguments(node)
    if len(parts) != 2:
        return _join(parts)
    accent = _ACCENTS.get(_elements(node)[1].get_text().strip())
    if accent:
        return accent + _group(parts[0])
    return r"\overset" + _group(parts[1]) + _group(parts[0])


def _under(node) -> str:
    parts = _arguments(node)
    if len(parts) != 2:
        return _join(parts)
    return r"\underset" + _group(parts[1]) + _group(parts[0])


def _root(node) -> str:
    parts = _arguments(node)
    if len(parts) != 2:
        return _join(parts)
    return r"\sqrt[" + parts[1] + "]" + _group(parts[0])


def _scripts(command: str, count: int):
    """msup/msub/msubsup/munderover all read as base plus one or two scripts."""

    def convert(node) -> str:
        parts = _arguments(node)
        if len(parts) != count + 1:
            return _join(parts)
        return parts[0] + "".join(mark + _group(part) for mark, part in zip(command, parts[1:], strict=True))

    return convert


def _fraction(node) -> str:
    parts = _arguments(node)
    if len(parts) != 2:
        return _join(parts)
    return r"\frac" + _group(parts[0]) + _group(parts[1])


def _table(node) -> str:
    rows = [row for row in _arguments(node) if row]
    if not rows:
        return ""
    return r"\begin{matrix} " + (" " + r"\cr" + " ").join(rows) + r" \end{matrix}"


def _fenced(node) -> str:
    return node.get("open", "(") + _join(_arguments(node)) + node.get("close", ")")


_HANDLERS = {
    "msqrt": lambda node: r"\sqrt" + _group(_join(_arguments(node))),
    "mroot": _root,
    "msup": _scripts("^", 1),
    "msub": _scripts("_", 1),
    "msubsup": _scripts("_^", 2),
    "munderover": _scripts("_^", 2),
    "mover": _over,
    "munder": _under,
    "mfrac": _fraction,
    "mtable": _table,
    "mtr": lambda node: " & ".join(cell for cell in _arguments(node) if cell),
    "mfenced": _fenced,
}


def _convert(node) -> str:
    name = (node.name or "").lower()
    if name in _IGNORED:
        return ""
    if name in _LEAVES:
        return _leaf(node, name)
    handler = _HANDLERS.get(name)
    if handler:
        return handler(node)
    return _join(_arguments(node))  # math, mrow, mstyle, semantics and unknown wrappers
