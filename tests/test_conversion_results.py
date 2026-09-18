import json
import time

import pytest

from app.converter import convert_document


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
