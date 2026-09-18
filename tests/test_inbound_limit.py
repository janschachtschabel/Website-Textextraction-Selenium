"""Inbound requests are limited after authentication; each crawled URL costs one token."""

from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.inbound_limit import InboundLimit
from app.main import create_app
from app.resources import Resources


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_a_burst_is_admitted_then_the_client_is_told_how_long_to_wait():
    clock = Clock()
    limit = InboundLimit(rate=1, burst=3, clock=clock)
    assert [limit.take(1) for _ in range(3)] == [0, 0, 0]
    assert limit.take(1) == pytest.approx(1.0)
    clock.now += 2
    assert limit.take(1) == 0


def test_a_batch_larger_than_the_burst_runs_once_and_pays_off_its_excess():
    clock = Clock()
    limit = InboundLimit(rate=2, burst=3, clock=clock)
    assert limit.take(10) == 0  # a full bucket admits it
    assert limit.take(1) == pytest.approx((1 - (3 - 10)) / 2)


def test_a_rejected_request_does_not_consume_tokens():
    clock = Clock()
    limit = InboundLimit(rate=1, burst=1, clock=clock)
    assert limit.take(1) == 0
    assert limit.take(1) > 0
    clock.now += 1
    assert limit.take(1) == 0


@pytest.fixture
async def limited_api(tmp_path, article_html):
    def upstream(request):
        return httpx.Response(200, content=article_html.encode(), headers={"content-type": "text/html"})

    config = replace(
        settings,
        result_cache_dir=str(tmp_path),
        result_cache_ttl=0,
        conversion_workers=1,
        default_retries=0,
        host="127.0.0.1",
        api_key="token",
        inbound_rate_limit_rps=0.01,
        inbound_rate_limit_burst=2,
    )
    resources = Resources(config, transport=httpx.MockTransport(upstream), validate=lambda url: None)
    app = create_app(config, resources)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_requests_beyond_the_burst_get_429_with_retry_after(limited_api):
    client = limited_api
    auth = {"Authorization": "Bearer token"}
    for _ in range(3):  # failed authentication never touches the bucket
        assert (await client.post("/crawl", json={"url": "https://example.com/a"})).status_code == 401
    batch = await client.post(
        "/crawl/batch", json={"urls": ["https://example.com/a", "https://example.com/b"]}, headers=auth
    )
    assert batch.status_code == 200
    limited = await client.post("/crawl", json={"url": "https://example.com/c"}, headers=auth)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert (await client.get("/health")).status_code == 200


def test_inbound_limit_settings_are_validated():
    with pytest.raises(ValueError, match="INBOUND"):
        replace(settings, host="127.0.0.1", inbound_rate_limit_rps=-1)
    with pytest.raises(ValueError, match="INBOUND"):
        replace(settings, host="127.0.0.1", inbound_rate_limit_burst=0)
