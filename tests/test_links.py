"""The links field is public output; its classification needs to stay put."""

import pytest

from app.utils import extract_links_detailed_from_html

BASE = "https://www.example.com/lessons/optics"


def extract(body: str, base: str = BASE):
    return extract_links_detailed_from_html(f"<html><body>{body}</body></html>", base)


@pytest.mark.parametrize(
    "href, text, category",
    [
        ("/lessons/refraction", "Refraction", "content"),
        ("#figure-2", "Figure 2", "anchor"),
        ("https://twitter.com/example", "Follow us", "social"),
        ("https://www.youtube.com/watch?v=abc", "Video", "social"),
        ("/impressum", "Impressum", "legal"),
        ("/datenschutzerklaerung", "Datenschutz", "legal"),
        ("/login", "Anmelden", "auth"),
        ("/suche?q=optik", "Suche", "search"),
        ("/?q=optik", "Suche", "search"),
        ("/kontakt", "Kontakt", "contact"),
        ("mailto:info@example.com", "Mail", "contact"),
        ("tel:+4930123456", "Anrufen", "contact"),
        ("/files/worksheet.pdf", "Arbeitsblatt", "download"),
        ("/files/archive.ZIP", "Archiv", "download"),
        ("/", "Startseite", "nav"),
        ("/uebersicht", "Übersicht", "nav"),
        ("/lessons/refraction?ref=nav", "Brechung", "content"),
    ],
)
def test_links_are_classified_by_target_and_text(href, text, category):
    (link,) = extract(f'<a href="{href}">{text}</a>')
    assert link["category"] == category
    assert link["text"] == text


def test_relative_targets_are_resolved_against_the_final_url():
    (link,) = extract('<a href="../physics/optics">Optik</a>')
    assert link["url"] == "https://www.example.com/physics/optics"


@pytest.mark.parametrize(
    "href, internal",
    [
        ("https://example.com/other", True),
        ("https://www.example.com/other", True),
        ("/other", True),
        ("https://other.example.com/page", False),
        ("https://example.org/page", False),
    ],
)
def test_internal_links_ignore_a_leading_www(href, internal):
    (link,) = extract(f'<a href="{href}">Weiter</a>')
    assert link["internal"] is internal


def test_repeated_targets_appear_once_with_the_first_text():
    links = extract('<a href="/a">First</a><a href="/a">Second</a><a href="/b">Other</a>')
    assert [(link["url"], link["text"]) for link in links] == [
        ("https://www.example.com/a", "First"),
        ("https://www.example.com/b", "Other"),
    ]


@pytest.mark.parametrize("href", ["javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,x", "blob:abc"])
def test_non_navigable_schemes_are_dropped(href):
    assert extract(f'<a href="{href}">Klick</a>') == []


def test_empty_and_missing_targets_are_dropped():
    assert extract('<a href="">Leer</a><a>Ohne</a>') == []


@pytest.mark.parametrize(
    "markup, text",
    [
        ('<a href="/a" aria-label="Zur Startseite"><img src="/logo.png"></a>', "Zur Startseite"),
        ('<a href="/a" title="Zum Kurs"><img src="/logo.png"></a>', "Zum Kurs"),
        ('<a href="/a"><img src="/logo.png"></a>', None),
        ('<a href="/a">  Viel   Text\n  </a>', "Viel Text"),
    ],
)
def test_link_text_falls_back_to_accessible_names(markup, text):
    (link,) = extract(markup)
    assert link["text"] == text
