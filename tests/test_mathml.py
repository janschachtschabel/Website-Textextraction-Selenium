"""Presentation MathML carries the whole formula; flattening it to text loses the operators."""

import pytest
from bs4 import BeautifulSoup

from app.mathml import to_latex


def math(markup: str):
    return BeautifulSoup(f"<math>{markup}</math>", "lxml").find("math")


@pytest.mark.parametrize(
    "markup, latex",
    [
        ("<mi>a</mi>", "a"),
        ("<mn>42</mn>", "42"),
        ("<mrow><mi>a</mi><mo>+</mo><mi>b</mi></mrow>", "a + b"),
        ("<mover><mi>a</mi><mo>→</mo></mover>", r"\vec{a}"),
        ("<mover><mi>x</mi><mo>¯</mo></mover>", r"\overline{x}"),
        ("<mover><mi>x</mi><mi>n</mi></mover>", r"\overset{n}{x}"),
        ("<munder><mi>x</mi><mi>n</mi></munder>", r"\underset{n}{x}"),
        ("<msqrt><mi>x</mi></msqrt>", r"\sqrt{x}"),
        ("<mroot><mi>x</mi><mn>3</mn></mroot>", r"\sqrt[3]{x}"),
        ("<msup><mi>a</mi><mn>2</mn></msup>", "a^{2}"),
        ("<msub><mi>a</mi><mn>1</mn></msub>", "a_{1}"),
        ("<msubsup><mi>a</mi><mn>1</mn><mn>2</mn></msubsup>", "a_{1}^{2}"),
        ("<munderover><mo>∑</mo><mn>0</mn><mi>n</mi></munderover>", r"\sum_{0}^{n}"),
        ("<mfrac><mn>1</mn><mn>2</mn></mfrac>", r"\frac{1}{2}"),
        ("<mtext>und</mtext>", r"\text{und}"),
        ("<mo>⋅</mo>", r"\cdot"),
        ("<mo>−</mo>", "-"),
        ("<mo>=</mo>", "="),
        ("<mspace width='1em'/>", ""),
        ("<mphantom><mi>x</mi></mphantom>", ""),
        ("<mstyle><mi>x</mi></mstyle>", "x"),
    ],
)
def test_each_presentation_element_becomes_its_latex(markup, latex):
    assert to_latex(math(markup)) == latex


def test_the_length_of_a_vector_keeps_its_radical_and_exponents():
    """The Serlo article that the flattening turned into 'a 1 2 + a 2 2'."""
    markup = (
        "<mrow><mi>|</mi><mover><mi>a</mi><mo stretchy='false'>→</mo></mover><mi>|</mi><mo>=</mo></mrow>"
        "<mrow><msqrt><mrow><msubsup><mi>a</mi><mn>1</mn><mn>2</mn></msubsup><mo>+</mo>"
        "<msubsup><mi>a</mi><mn>2</mn><mn>2</mn></msubsup></mrow></msqrt></mrow>"
    )
    assert to_latex(math(markup)) == r"| \vec{a} | = \sqrt{a_{1}^{2} + a_{2}^{2}}"


def test_a_column_vector_keeps_its_rows():
    markup = (
        "<mrow><mo fence='true' form='prefix'>(</mo>"
        "<mtable><mtr><mtd><mn>2</mn></mtd></mtr><mtr><mtd><mn>3</mn></mtd></mtr></mtable>"
        "<mo fence='true' form='postfix'>)</mo></mrow>"
    )
    assert to_latex(math(markup)) == r"( \begin{matrix} 2 \cr 3 \end{matrix} )"


def test_a_table_row_separates_its_cells():
    markup = "<mtable><mtr><mtd><mn>1</mn></mtd><mtd><mn>2</mn></mtd></mtr></mtable>"
    assert to_latex(math(markup)) == r"\begin{matrix} 1 & 2 \end{matrix}"


@pytest.mark.parametrize("character, escaped", [("%", r"\%"), ("#", r"\#"), ("&", r"\&"), ("$", r"\$")])
def test_latex_control_characters_in_page_text_are_escaped(character, escaped):
    assert to_latex(math(f"<mtext>{character}</mtext>")) == r"\text{" + escaped + "}"


def test_an_unknown_element_still_yields_the_content_it_wraps():
    assert to_latex(math("<menclose notation='box'><mi>x</mi></menclose>")) == "x"


def test_a_formula_with_nothing_to_convert_is_empty():
    assert to_latex(math("")) == ""
