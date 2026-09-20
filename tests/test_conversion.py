import json
import os

import pytest

from app.converter import bytes_to_markdown, enhance_table_structure


def test_selected_trafilatura_extracts_main_content(article_html):
    markdown = bytes_to_markdown(
        article_html.encode(), "text/html", url="https://example.com/optics", html_converter="trafilatura"
    )
    assert "Reflection" in markdown
    assert "NAVIGATIONTOKEN" not in markdown


PARAGRAPHS = "<p>Light travels through transparent materials and changes direction at a mirror.</p>" * 8


def trafilatura_markdown(body):
    html = "<html><body>" + body.replace("{}", PARAGRAPHS) + "</body></html>"
    return bytes_to_markdown(html.encode(), "text/html", html_converter="trafilatura")


@pytest.mark.parametrize(
    "layout, heading",
    [
        ("<h1>{}</h1><div>{{}}</div>", "Optics and light"),
        ("<header><h1>{}</h1></header><main>{{}}</main>", "Optics and light"),
        ("<header><h1>{}</h1></header><main>{{}}</main>", "<b>Ohm</b>sche Gesetze"),
        ("<header><h1>{}</h1></header><main>{{}}</main>", "Das Molekül H<sub>2</sub>O"),
        ("<header><h1>{}</h1></header><main>{{}}</main>", "Intro to<br>optics"),
    ],
)
def test_trafilatura_output_gets_the_page_heading_it_dropped(layout, heading):
    markdown = trafilatura_markdown(layout.format(heading))
    expected = {"<b>Ohm</b>sche": "Ohmsche", "H<sub>2</sub>O": "H2O", "<br>": " "}
    for markup, text in expected.items():
        heading = heading.replace(markup, text)
    assert markdown.startswith(f"# {heading}\n\n")


@pytest.mark.parametrize(
    "heading",
    ["Optics and light", "Intro to <em>Optics</em>", '<a href="/optics">Optics</a> and light', "H<sub>2</sub>O"],
)
def test_trafilatura_page_heading_it_kept_is_not_repeated(heading):
    markdown = trafilatura_markdown(f"<article><h1>{heading}</h1>{{}}</article>")
    assert sum(line.startswith("# ") for line in markdown.splitlines()) == 1


@pytest.mark.parametrize(
    "body",
    [
        '<header><h1 class="sr-only">Hauptnavigation</h1></header><main><h2>Optics</h2>{}</main>',
        "<header><h1 hidden>Hauptnavigation</h1></header><main><h2>Optics</h2>{}</main>",
        '<header><h1 aria-hidden="TRUE">Hauptnavigation</h1></header><main><h2>Optics</h2>{}</main>',
        "<header><h1>Hauptnavigation</h1></header><article><h1>Optics</h1>{}</article>",
        "<h1>" + "Hauptnavigation " * 20 + "</h1><div>{}</div>",  # an unclosed heading swallowed the page
    ],
)
def test_hidden_logo_or_overlong_h1_does_not_become_the_heading(body):
    assert not trafilatura_markdown(body).startswith("# Hauptnavigation")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux descriptor inspection")
def test_repeated_html_conversion_closes_descriptors(article_html, monkeypatch):
    import tempfile

    original = tempfile.mkstemp
    opened = []

    def record_file(*args, **kwargs):
        fd, path = original(*args, **kwargs)
        opened.append(fd)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", record_file)
    before = len(os.listdir("/proc/self/fd"))
    try:
        for _ in range(5):
            assert bytes_to_markdown(article_html.encode(), "text/html", html_converter="bs4")
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize(
    "markdown",
    [
        "```python\nx = a | b | c\n```",
        "The expressions |x| and |y| are absolute values.",
        "| A | B |\n| --- | --- |\n| 1 | 2 |",
    ],
)
def test_postprocessing_does_not_corrupt_code_or_valid_markdown(markdown):
    assert enhance_table_structure(markdown) == markdown


def test_table_separator_has_the_same_number_of_columns():
    output = enhance_table_structure("| A | B |\n| 1 | 2 |")
    assert output.splitlines()[1].count("|") == 3


def test_article_body_takes_precedence_over_metadata_description():
    payload = {
        "@type": "Article",
        "headline": "Optics",
        "description": "SHORTMETADATA " * 80,
        "articleBody": "COMPLETEARTICLE " * 100,
    }
    html = '<html><script type="application/ld+json">' + json.dumps(payload) + "</script></html>"
    markdown = bytes_to_markdown(html.encode(), "text/html", html_converter="trafilatura")
    assert "COMPLETEARTICLE" in markdown
    assert "SHORTMETADATA" not in markdown


def test_source_math_is_retained_in_markdown(article_html):
    html = article_html.replace("</main>", '<p>Equation: <script type="math/tex">x^2+y^2=z^2</script></p></main>')
    assert "x^2+y^2=z^2" in bytes_to_markdown(html.encode(), "text/html", html_converter="trafilatura")


def test_declared_legacy_encoding_is_respected():
    data = "<html><p>Grün und größer: Wärmeübertragung.</p></html>".encode("cp1252")
    markdown = bytes_to_markdown(data, "text/html; charset=windows-1252", html_converter="bs4")
    assert "Grün" in markdown and "Wärmeübertragung" in markdown


@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "text/html"])
def test_utf8_body_cut_inside_a_character_stays_readable(content_type):
    data = "<html><p>Grün und größer: Wärmeübertragung.</p></html>".encode()
    cut = data[: data.rindex("ü".encode()) + 1]  # max_bytes ended inside the last "ü"
    markdown = bytes_to_markdown(cut, content_type, html_converter="bs4")
    assert "Grün und größer: Wärme" in markdown


def test_utf8_page_with_a_stray_byte_keeps_its_umlauts():
    data = "<html><p>Grün und größer: Wärmeübertragung.</p></html>".encode().replace(b" und ", b" \x92 ")
    markdown = bytes_to_markdown(data, "text/html; charset=utf-8", html_converter="bs4")
    assert "Grün" in markdown and "Wärmeübertragung" in markdown


@pytest.mark.parametrize(
    "text, encoding, content_type, head",
    [
        ("日本語のテキスト", "iso-2022-jp", "text/html; charset=iso-2022-jp", ""),
        ("日本語のテキスト", "iso-2022-jp", "text/html", '<meta charset="iso-2022-jp">'),
        ("Привет мир", "utf-16-le", "text/html; charset=utf-16le", ""),
    ],
)
def test_declared_charset_of_ascii_range_bytes_is_respected(text, encoding, content_type, head):
    # These bytes are also valid UTF-8, so only the declaration tells how to read them.
    data = f"<html>{head}<p>{text}</p></html>".encode(encoding)
    assert data.isascii()
    assert text in bytes_to_markdown(data, content_type, html_converter="bs4")


@pytest.mark.parametrize("content_type", ["text/html; charset=utf-8", "text/html"])
def test_legacy_page_without_or_with_wrong_charset_keeps_its_umlauts(content_type):
    data = "<html><p>Grün und größer: Wärmeübertragung.</p></html>".encode("cp1252")
    markdown = bytes_to_markdown(data, content_type, html_converter="bs4")
    assert "Grün" in markdown and "Wärmeübertragung" in markdown


def test_rendered_math_prefers_preserved_latex_over_svg_glyph_text():
    html = '<main><p>Equation <mjx-container data-latex="x^2+y^2=z^2"><svg><text>x2+y2</text></svg></mjx-container></p></main>'
    assert "x^2+y^2=z^2" in bytes_to_markdown(html.encode(), "text/html", html_converter="bs4")


MATHML_LENGTH = (
    "<math><mrow><mi>|</mi><mover><mi>a</mi><mo>→</mo></mover><mi>|</mi><mo>=</mo></mrow>"
    "<mrow><msqrt><mrow><msubsup><mi>a</mi><mn>1</mn><mn>2</mn></msubsup><mo>+</mo>"
    "<msubsup><mi>a</mi><mn>2</mn><mn>2</mn></msubsup></mrow></msqrt></mrow></math>"
)


def test_presentation_mathml_becomes_latex_instead_of_flattened_text():
    html = f"<html><body><p>Es gilt {MATHML_LENGTH}.</p></body></html>"
    markdown = bytes_to_markdown(html.encode(), "text/html", html_converter="bs4")
    assert r"$$| \vec{a} | = \sqrt{a_{1}^{2} + a_{2}^{2}}$$" in markdown
    assert "a 1 2" not in markdown


def test_a_tex_annotation_still_wins_over_the_presentation_markup():
    html = (
        "<html><body><p>Es gilt <math><semantics><mrow><msup><mi>a</mi><mn>2</mn></msup></mrow>"
        '<annotation encoding="application/x-tex">a^{2}</annotation></semantics></math>.</p></body></html>'
    )
    assert "$$a^{2}$$" in bytes_to_markdown(html.encode(), "text/html", html_converter="bs4")


def test_a_hidden_accessibility_wrapper_does_not_swallow_the_formula():
    """Wikipedia hides the MathML copy from sighted readers; trafilatura drops hidden content."""
    formula = (
        '<span class="mwe-math-element"><span class="mwe-math-mathml-inline" style="display: none;">'
        '<math><semantics><annotation encoding="application/x-tex">a^{2}+b^{2}=c^{2}</annotation>'
        "</semantics></math></span>"
        '<img alt="a^{2}+b^{2}=c^{2}" aria-hidden="true" src="/formula.svg"></span>'
    )
    markdown = trafilatura_markdown(f"<main>{{}}<p>Der Satz lautet {formula}.</p></main>")
    assert "$$a^{2}+b^{2}=c^{2}$$" in markdown
