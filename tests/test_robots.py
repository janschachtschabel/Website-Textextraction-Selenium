"""Opt-in robots.txt (RFC 9309) for the requested URL."""

from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.main import create_app
from app.resources import Resources

AGENT = "WebsiteTextExtraction/0.6 (+https://example.org/contact)"
ROBOTS = {
    "rules.example": (200, "User-agent: *\nDisallow: /private\n"),
    "missing.example": (404, ""),
    "broken.example": (503, ""),
    "agent.example": (200, "User-agent: WebsiteTextExtraction\nDisallow: /members\n\nUser-agent: *\nDisallow: /\n"),
}


class NoBrowser:
    async def fetch(self, *args):
        pytest.fail("These pages are static")


@pytest.fixture
async def robots_api(tmp_path, article_html):
    fetched = []

    def upstream(request):
        if request.url.path == "/robots.txt":
            fetched.append(request.url.host)
            if request.url.host == "down.example":
                raise httpx.ConnectError("refused")
            status, body = ROBOTS[request.url.host]
            return httpx.Response(status, content=body.encode(), headers={"content-type": "text/plain"})
        return httpx.Response(200, content=article_html.encode(), headers={"content-type": "text/html"})

    config = replace(
        settings,
        result_cache_dir=str(tmp_path),
        conversion_workers=1,
        default_retries=0,
        default_user_agent=AGENT,
        host="127.0.0.1",
        api_key=None,
        respect_robots_txt=True,
    )
    resources = Resources(
        config, transport=httpx.MockTransport(upstream), validate=lambda url: None, browser=NoBrowser()
    )
    app = create_app(config, resources)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, fetched


async def crawl(client, url, **options):
    return await client.post("/crawl", json={"url": url, "mode": "fast", **options})


async def test_disallowed_paths_are_refused_and_others_extracted(robots_api):
    client, fetched = robots_api
    refused = await crawl(client, "https://rules.example/private/page")
    assert refused.status_code == 403 and refused.json()["detail"] == "Disallowed by robots.txt"
    allowed = await crawl(client, "https://rules.example/lessons/optics")
    assert allowed.status_code == 200 and allowed.json()["success"]
    assert fetched == ["rules.example"]  # one robots.txt per origin, reused


@pytest.mark.parametrize(
    "url, status",
    [
        ("https://missing.example/page", 200),  # 4xx: unavailable, no restrictions
        ("https://broken.example/page", 403),  # 5xx: unreachable, complete disallow
        ("https://down.example/page", 403),  # connection failed: unreachable
        ("https://agent.example/lessons", 200),  # our group applies, not the catch-all
        ("https://agent.example/members/list", 403),
    ],
)
async def test_unavailable_unreachable_and_agent_specific_rules(robots_api, url, status):
    client, _ = robots_api
    assert (await crawl(client, url)).status_code == status


async def test_a_request_can_skip_the_check(robots_api):
    client, fetched = robots_api
    response = await crawl(client, "https://rules.example/private/page", respect_robots_txt=False)
    assert response.status_code == 200 and fetched == []


def test_robots_check_is_off_by_default():
    from app.schemas import CrawlRequest, resolve_options

    assert resolve_options(CrawlRequest(url="https://example.com"), settings).respect_robots_txt is False


async def test_robots_entries_of_different_transports_do_not_mix(robots_api):
    """What comes back depends on the transport, and every later request trusts the answer."""
    client, fetched = robots_api
    assert (await crawl(client, "https://rules.example/lessons/optics")).status_code == 200
    assert (await crawl(client, "https://rules.example/lessons/optics", allow_insecure_ssl=True)).status_code == 200
    assert fetched == ["rules.example", "rules.example"]


def test_a_caller_supplied_proxy_gets_its_own_robots_entry():
    from app.robots import cache_key
    from app.schemas import CrawlOptions

    plain = CrawlOptions()
    assert cache_key("https://rules.example", plain) == cache_key("https://rules.example", CrawlOptions())
    assert cache_key("https://rules.example", plain) != cache_key("https://other.example", plain)
    for transport in (CrawlOptions(proxy="http://proxy.example:8080"), CrawlOptions(allow_insecure_ssl=True)):
        assert cache_key("https://rules.example", plain) != cache_key("https://rules.example", transport)
