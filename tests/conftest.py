import pytest


@pytest.fixture
def article_html():
    paragraphs = ''.join(
        '<p>Light travels through transparent materials. Reflection changes its '
        'direction at a mirror. Students compare their observations and explain '
        'the relationship between the incident and reflected rays.</p>'
        for _ in range(8)
    )
    return ('<html><head><title>Optics</title></head><body><nav>NAVIGATIONTOKEN</nav>'
            '<main><h1>Optics</h1>' + paragraphs + '</main></body></html>')
