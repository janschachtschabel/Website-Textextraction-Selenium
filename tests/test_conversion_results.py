import json
import time

import pytest
from bs4 import BeautifulSoup

from app.converter import convert_document
from app.html_converter import document_links


def test_empty_and_unknown_binary_are_not_successful():
    assert convert_document(b"", "text/html").status == "empty"
    result = convert_document(b"\x00\xff\x01", "application/octet-stream")
    assert result.status == "unsupported"
    assert result.markdown == ""


def test_embedded_summary_does_not_replace_article(article_html):
    summary = '<script type="application/ld+json">' + json.dumps({"description": "SUMMARY " * 300}) + "</script>"
    result = convert_document((summary + article_html).encode(), "text/html")
    assert "Reflection" in result.markdown
    assert "SUMMARY" not in result.markdown


def test_kmap_inline_attachment_uses_final_origin():
    payload = {
        "title": "Lesson",
        "description": '<p>Diagram <a href="inline:diagram.pdf">Download</a></p>',
        "attachments": [{"file": "diagram.pdf", "href": "files/diagram.pdf", "name": "Diagram"}],
    }
    html = '<base href="/app/"><script id="embedded-topic" type="json">' + json.dumps(payload) + "</script>"
    result = convert_document(html.encode(), "text/html", "https://school.example/topic/42")
    assert "https://school.example/app/files/diagram.pdf" in result.markdown


# A worksheet post as kindOERgarten and Planet-N publish them: the material is the list of files.
WORKSHEET_POST = """<html lang="de"><head><title>Äpfel zählen</title></head><body>
<header><nav><a href="/">Start</a> <a href="/programm.pdf">Jahresprogramm</a></nav></header>
<article><h1>Äpfel zählen: 1 bis 3</h1><div class="entry-content">
<p>Auch mit Äpfeln lässt sich das Zählen üben. Hier für die Zahlen bis 3.</p>
<ul>
<li><a href="/uploads/aepfel-sw.pdf">Arbeitsblatt: Äpfel am Apfelbaum zählen – schwarz-weiß</a></li>
<li><a href="/uploads/aepfel-bunt.PDF">Arbeitsblatt: Äpfel am Apfelbaum zählen – bunt</a></li>
<li><a href="/uploads/loesungen.pdf"><img src="/icon.png" alt=""></a></li>
</ul>
<p>Quelle: Kita-Archiv<span style="display: none"><a href="/archiv/aepfel-2016.pdf">@1</a></span></p>
<p hidden><a href="/uploads/entwurf.pdf">Entwurf</a></p>
</div>
<div class="related"><h3>Ähnliche Beiträge</h3>
<p><a href="/birnen/">Birnen zählen bis 5</a> In "Bildungsbereich"</p></div>
</article>
<aside><a href="/flyer.pdf">Flyer</a></aside>
<footer><a href="/datenschutz.pdf">Datenschutz</a></footer>
</body></html>"""


def test_document_links_in_the_content_survive_the_extraction():
    result = convert_document(WORKSHEET_POST.encode(), "text/html; charset=utf-8", "https://kita.example/2017/aepfel/")
    text, files = result.markdown.rsplit("\n\n---\n\n", 1)
    worksheet = "- [Arbeitsblatt: Äpfel am Apfelbaum zählen – {}](https://kita.example/uploads/{})"
    assert files.splitlines() == [
        worksheet.format("schwarz-weiß", "aepfel-sw.pdf"),
        worksheet.format("bunt", "aepfel-bunt.PDF"),
        "- [loesungen.pdf](https://kita.example/uploads/loesungen.pdf)",
    ]
    assert "Auch mit Äpfeln" in text


def test_a_document_link_the_text_keeps_is_not_repeated(article_html):
    handout = '<p>Das <a href="/files/handout.pdf">Handout</a> fasst den Versuch zusammen und nennt das Material.</p>'
    html = article_html.replace("</main>", handout + "</main>")
    result = convert_document(html.encode(), "text/html", "https://school.example/optics")
    assert result.markdown.count("https://school.example/files/handout.pdf") == 1
    assert "\n---\n" not in result.markdown  # kept by the text, not added after it


@pytest.mark.parametrize(
    ("context", "counted"),
    [
        ("<p>{}</p>", True),
        ("<nav>{}</nav>", False),
        ("<header>{}</header>", False),
        ("<footer>{}</footer>", False),
        ("<aside>{}</aside>", False),
        ("<main><aside>{}</aside></main>", False),
        ('<div role="navigation">{}</div>', False),
        ('<div role="banner">{}</div>', False),
        ('<div role="contentinfo">{}</div>', False),
        ('<div role="complementary">{}</div>', False),
        ("<p hidden>{}</p>", False),
        ('<p aria-hidden="true">{}</p>', False),
        ('<p style="display:none">{}</p>', False),
        ('<p style="visibility: hidden">{}</p>', False),
        # An article's or section's own header, footer and aside belong to its content.
        ("<article><header>{}</header></article>", True),
        ("<article><footer>{}</footer></article>", True),
        ("<section><footer>{}</footer></section>", True),
        ("<main><header>{}</header></main>", True),
        ("<article><aside>{}</aside></article>", True),
        ('<div hidden="until-found">{}</div>', True),
    ],
)
def test_a_file_counts_where_the_page_shows_its_content(context, counted):
    html = "<body><article><p>Text</p></article>" + context.format('<a href="https://s.example/a.pdf">A</a>')
    assert (document_links(BeautifulSoup(html + "</body>", "lxml")) == [("A", "https://s.example/a.pdf")]) is counted


def test_a_file_link_without_text_is_labelled_by_aria_label_title_or_file_name():
    html = (
        '<a href="https://s.example/a.pdf" aria-label="Lösungen"><img src="i.png" alt=""></a>'
        '<a href="https://s.example/b.pdf" title="Arbeitsblatt\n\n  bunt"></a>'
        '<a href="https://s.example/c%20d.pdf"> </a>'
    )
    assert document_links(BeautifulSoup(html, "lxml")) == [
        ("Lösungen", "https://s.example/a.pdf"),
        ("Arbeitsblatt bunt", "https://s.example/b.pdf"),
        ("c d.pdf", "https://s.example/c%20d.pdf"),
    ]


def test_a_file_is_what_links_reports_as_a_download():
    html = (
        '<a href=" https://s.example/blatt.pdf ">Blatt</a>'  # browsers ignore the spaces
        '<a href="https://s.example/blatt.pdf/">Seite</a>'  # a page named like a file
        '<a href="https://s.example/get?file=blatt.pdf">Abruf</a>'
        '<a href="mailto:post@s.example?subject=blatt.pdf">Post</a>'
    )
    assert document_links(BeautifulSoup(html, "lxml")) == [("Blatt", "https://s.example/blatt.pdf")]


def test_a_malformed_link_does_not_cost_the_page_its_text(article_html):
    links = '<p><a href="http://[URL]">Vorlage</a> und <a href="/files/blatt.pdf">Blatt</a></p>'
    html = article_html.replace("</main>", links + "</main>")
    result = convert_document(html.encode(), "text/html", "https://s.example/a")
    assert result.status == "ok" and "Reflection" in result.markdown
    assert "https://s.example/files/blatt.pdf" in result.markdown


def test_a_malformed_base_leaves_references_relative_to_the_page(article_html):
    link = '<p><a href="blatt.pdf">Blatt</a></p>'
    html = '<base href="http://[URL]/">' + article_html.replace("</main>", link + "</main>")
    result = convert_document(html.encode(), "text/html", "https://s.example/lessons/a")
    assert "https://s.example/lessons/blatt.pdf" in result.markdown


def test_a_malformed_attachment_does_not_cost_the_lesson_its_text():
    payload = {
        "title": "Lesson",
        "description": '<p>The diagram shows the rays. <a href="inline:diagram.pdf">Download</a></p>',
        "attachments": [{"file": "diagram.pdf", "href": "http://[URL]/diagram.pdf", "name": "Diagram"}],
    }
    html = '<script id="embedded-topic" type="json">' + json.dumps(payload) + "</script>"
    result = convert_document(html.encode(), "text/html", "https://school.example/topic/42")
    assert "The diagram shows the rays." in result.markdown


def test_a_youtube_watch_page_yields_its_title_channel_and_description(youtube_watch_page):
    html = youtube_watch_page("Strukturen erkennen.\nMuster finden.")
    result = convert_document(html.encode(), "text/html; charset=utf-8", "https://www.youtube.com/watch?v=VhCv6MlgWlE")
    assert result.markdown.startswith("# Mathematik ist überall")
    assert "Mathe Kanal" in result.markdown and "Muster finden." in result.markdown
    assert "Urheberrecht" not in result.markdown


def test_media_skip_is_explicit():
    result = convert_document(b"media", "video/mp4", media_conversion_policy="skip")
    assert result.status == "skipped"
    assert result.markdown == ""


def test_legacy_full_media_cannot_invoke_remote_transcription(monkeypatch):
    from app import converter

    called = []
    monkeypatch.setattr(converter, "markitdown_stream", lambda *args: called.append(args) or "remote transcript")
    result = converter.convert_document(b"audio", "audio/mpeg", media_conversion_policy="full")
    assert result.status == "unsupported"
    assert called == []


def test_markitdown_cannot_dispatch_a_source_url_to_remote_site_converters(monkeypatch):
    from types import SimpleNamespace

    from app import html_converter

    seen = []

    class Converter:
        def convert_stream(self, stream, *, stream_info):
            seen.append(stream_info.url)
            return SimpleNamespace(text_content="local text")

    monkeypatch.setattr(html_converter._local, "markitdown", Converter(), raising=False)
    html_converter.markitdown_stream(b"<p>local</p>", "text/html", ".html", "https://www.youtube.com/watch?v=123")
    assert seen == [None]


@pytest.mark.parametrize("mode, routed", [("auto", True), ("fast", False), ("js", False)])
def test_browser_routing_is_computed_only_for_auto_mode(monkeypatch, mode, routed):
    from app import preflight
    from app.config import settings
    from app.results import FetchResult
    from app.schemas import CrawlRequest, resolve_options
    from app.worker_tasks import prepare_document

    checks = []
    monkeypatch.setattr(preflight, "needs_browser", lambda fetched, converted: bool(checks.append(mode)) or True)
    fetched = FetchResult(
        b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>",
        "https://example.com/app",
        200,
        "text/html",
    )
    options = resolve_options(CrawlRequest(url="https://example.com/app", mode=mode), settings)
    _, use_browser, *_ = prepare_document(fetched, options, time.monotonic() + 30)
    assert bool(checks) is routed
    assert use_browser is routed


def test_failed_metadata_extraction_leaves_the_text_intact(monkeypatch, article_html):
    from app import page_metadata
    from app.config import settings
    from app.results import FetchResult
    from app.schemas import CrawlRequest, resolve_options
    from app.worker_tasks import prepare_document

    def unparseable_date(tree, default_url=None):
        raise ValueError("date out of range")

    monkeypatch.setattr(page_metadata, "extract_metadata", unparseable_date)
    fetched = FetchResult(article_html.encode(), "https://example.com/a", 200, "text/html")
    options = resolve_options(CrawlRequest(url="https://example.com/a", mode="fast", extract_metadata=True), settings)
    converted, _, _, metadata = prepare_document(fetched, options, time.monotonic() + 30)
    assert converted.status == "ok" and "Reflection" in converted.markdown
    assert metadata is None
    assert "Page metadata unavailable (ValueError)" in converted.warnings
