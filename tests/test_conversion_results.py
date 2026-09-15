import json

from app.converter import convert_document


def test_empty_and_unknown_binary_are_not_successful():
    assert convert_document(b'', 'text/html').status == 'empty'
    result = convert_document(b'\x00\xff\x01', 'application/octet-stream')
    assert result.status == 'unsupported'
    assert result.markdown == ''


def test_embedded_summary_does_not_replace_article(article_html):
    summary = '<script type="application/ld+json">' + json.dumps({'description': 'SUMMARY ' * 300}) + '</script>'
    result = convert_document((summary + article_html).encode(), 'text/html')
    assert 'Reflection' in result.markdown
    assert 'SUMMARY' not in result.markdown


def test_kmap_inline_attachment_uses_final_origin():
    payload = {'title': 'Lesson', 'description': '<p>Diagram <a href="inline:diagram.pdf">Download</a></p>',
               'attachments': [{'file': 'diagram.pdf', 'href': 'files/diagram.pdf', 'name': 'Diagram'}]}
    html = '<base href="/app/"><script id="embedded-topic" type="json">' + json.dumps(payload) + '</script>'
    result = convert_document(html.encode(), 'text/html', 'https://school.example/topic/42')
    assert 'https://school.example/app/files/diagram.pdf' in result.markdown


def test_media_skip_is_explicit():
    result = convert_document(b'media', 'video/mp4', media_conversion_policy='skip')
    assert result.status == 'skipped'
    assert result.markdown == ''
