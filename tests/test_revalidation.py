"""After the fresh cache entry expires, an unchanged upstream document costs a 304, not a new extraction."""

from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.main import create_app
from app.resources import Resources
from app.result_cache import make_cache_key
from app.schemas import CrawlRequest, resolve_options

URL = "https://example.com/lesson"


class NoBrowser:
    async def fetch(self, *args):
        pytest.fail("These pages are static")


@pytest.fixture
async def revalidating_api(tmp_path, article_html):
    upstream = {"etag": '"v1"', "last_modified": "Wed, 16 Sep 2026 08:00:00 GMT", "body": article_html, "seen": []}

    def respond(request):
        upstream["seen"].append(dict(request.headers))
        etag, modified = upstream["etag"], upstream["last_modified"]
        if (etag and request.headers.get("if-none-match") == etag) or (
            not etag and request.headers.get("if-modified-since") == modified
        ):
            # A 304 carries no body but keeps the headers of the representation it stands
            # for, Content-Encoding among them. Wikimedia, Fastly and Cloudflare all do
            # this; a bare 304 is the unrealistic case, and testing only that one hid a bug
            # that broke every repeat crawl for a day.
            return httpx.Response(304, headers={"content-encoding": "gzip"})
        headers = {"content-type": "text/html", "last-modified": modified}
        if etag:
            headers["etag"] = etag
        return httpx.Response(200, content=upstream["body"].encode(), headers=headers)

    config = replace(
        settings,
        result_cache_dir=str(tmp_path),
        result_cache_ttl=300,
        revalidation_ttl=3600,
        conversion_workers=1,
        default_retries=0,
        host="127.0.0.1",
        api_key=None,
    )
    resources = Resources(
        config, transport=httpx.MockTransport(respond), validate=lambda url: None, browser=NoBrowser()
    )
    app = create_app(config, resources)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

            async def crawl(**options):
                return (await client.post("/crawl", json={"url": URL, "mode": "fast", **options})).json()

            def expire_fresh_entry(**options):
                key = make_cache_key(
                    URL, resolve_options(CrawlRequest(url=URL, mode="fast", **options), config), config
                )
                assert resources.cache.delete(key)

            yield crawl, expire_fresh_entry, upstream


async def test_unchanged_document_is_revalidated_with_its_etag(revalidating_api):
    crawl, expire_fresh_entry, upstream = revalidating_api
    first = await crawl()
    expire_fresh_entry()
    second = await crawl()
    assert upstream["seen"][-1]["if-none-match"] == '"v1"'
    assert second["revalidated"] and second["cached"] and second["success"]
    assert second["markdown"] == first["markdown"] and not first["revalidated"]
    third = await crawl()  # the revalidated result is fresh again
    assert third["cached"] and not third["revalidated"] and len(upstream["seen"]) == 2


async def test_changed_document_is_extracted_again(revalidating_api):
    crawl, expire_fresh_entry, upstream = revalidating_api
    await crawl()
    expire_fresh_entry()
    upstream["etag"], upstream["body"] = '"v2"', "<html><body><main><h1>Neu</h1>" + "<p>Ganz neuer Text.</p>" * 20
    changed = await crawl()
    assert not changed["revalidated"] and not changed["cached"] and "Ganz neuer Text" in changed["markdown"]


async def test_last_modified_alone_is_enough(revalidating_api):
    crawl, expire_fresh_entry, upstream = revalidating_api
    upstream["etag"] = None
    await crawl()
    expire_fresh_entry()
    again = await crawl()
    assert upstream["seen"][-1]["if-modified-since"] == upstream["last_modified"]
    assert again["revalidated"]


async def test_force_refresh_fetches_unconditionally(revalidating_api):
    crawl, _, upstream = revalidating_api
    await crawl()
    refreshed = await crawl(force_refresh=True)
    assert "if-none-match" not in upstream["seen"][-1] and not refreshed["revalidated"]
