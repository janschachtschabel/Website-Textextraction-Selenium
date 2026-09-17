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
