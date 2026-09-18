"""Descriptive metadata comes from what the page declares, never from arbitrary markup."""

from app.page_metadata import page_metadata

FINAL = "https://physik.example/final"
DECLARED = """<html lang="de"><head>
<title>Lichtbrechung | Physikportal</title>
<meta name="description" content="Wie Licht an Grenzflaechen abgelenkt wird.">
<meta name="author" content="Erika Mustermann">
<meta property="og:site_name" content="Physikportal">
<meta property="article:published_time" content="2026-03-14T09:00:00+01:00">
<link rel="canonical" href="https://physik.example/lichtbrechung">
</head><body><article><h1>Lichtbrechung</h1><p>Licht wird gebrochen.</p></article></body></html>"""


def test_declared_metadata_is_returned():
    assert page_metadata(DECLARED, FINAL) == {
        "title": "Lichtbrechung",
        "description": "Wie Licht an Grenzflaechen abgelenkt wird.",
        "author": "Erika Mustermann",
        "date": "2026-03-14",
        "site_name": "Physikportal",
        "canonical_url": "https://physik.example/lichtbrechung",
        "language": "de",
    }


def test_undeclared_address_and_site_fall_back_to_the_final_url():
    metadata = page_metadata("<html><head><title>Nur Titel</title></head><body><p>x</p></body></html>", FINAL)
    assert metadata["title"] == "Nur Titel"
    assert metadata["canonical_url"] == FINAL and metadata["site_name"] == "physik.example"
    assert metadata["author"] is None and metadata["date"] is None and metadata["language"] is None


def test_script_urls_never_become_the_canonical_url():
    page = '<html><head><link rel="canonical" href="javascript:alert(1)"></head><body><p>x</p></body></html>'
    assert page_metadata(page, FINAL)["canonical_url"] == FINAL


def test_page_controlled_text_is_bounded():
    page = "<html><head><title>" + "A" * 5000 + "</title></head><body><p>x</p></body></html>"
    assert len(page_metadata(page, FINAL)["title"]) == 1000


def test_empty_documents_have_no_metadata():
    assert page_metadata("", FINAL) is None
