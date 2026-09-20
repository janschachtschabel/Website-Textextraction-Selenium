"""Failures must be visible in the log without leaking request data."""

from dataclasses import replace

import httpx
import pytest
from loguru import logger

from app.config import settings
from app.logging_setup import setup_logging
from app.main import create_app
from app.resources import Resources


class NoBrowser:
    async def fetch(self, *args):
        pytest.fail("This HTTP-only result must not launch Selenium")


@pytest.fixture
async def api(tmp_path, article_html):
    async def upstream(request):
        if request.url.path == "/missing":
            return httpx.Response(404, content=b"<main>Not found</main>", headers={"content-type": "text/html"})
        if request.url.path == "/unreachable":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, content=article_html.encode(), headers={"content-type": "text/html"})

    config = replace(
        settings,
        result_cache_dir=str(tmp_path),
        conversion_workers=1,
        default_retries=0,
        api_key=None,
        host="127.0.0.1",
    )
    resources = Resources(
        config, transport=httpx.MockTransport(upstream), validate=lambda url: None, browser=NoBrowser()
    )
    app = create_app(config, resources)
    async with app.router.lifespan_context(app):
        records = []
        sink = logger.add(lambda message: records.append(message.record), level="WARNING")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                yield client, records
        finally:
            logger.remove(sink)


async def test_unsuccessful_extraction_is_logged_with_host_and_status(api):
    client, records = api
    response = await client.post("/crawl", json={"url": "https://example.com/missing?token=secret"})
    assert response.status_code == 200 and not response.json()["success"]
    assert len(records) == 1
    extra = records[0]["extra"]
    assert extra["host"] == "example.com"
    assert extra["status"] == 404
    assert extra["mode"] == "auto"
    assert extra["extraction"] == "blocked"
    assert extra["elapsed_ms"] >= 0
    assert "secret" not in records[0]["message"] and "/missing" not in records[0]["message"]


async def test_crawl_error_is_logged_with_its_status(api):
    client, records = api
    response = await client.post("/crawl", json={"url": "https://example.com/unreachable"})
    assert response.status_code == 502
    assert len(records) == 1
    assert records[0]["extra"]["status"] == 502
    assert "HTTP download failed" in records[0]["message"]


async def test_successful_crawl_stays_silent(api):
    client, records = api
    response = await client.post("/crawl", json={"url": "https://example.com/article"})
    assert response.json()["success"]
    assert records == []


def _raise_for(value):
    raise RuntimeError("boom")


def test_json_logs_omit_local_variable_values(capsys):
    # loguru annotates each frame with the values on its source line when diagnose is on.
    setup_logging("INFO", True)
    try:
        token = "s3cr3t-value"
        try:
            _raise_for(token)
        except RuntimeError:
            logger.exception("Extraction failed")
        logger.complete()
        output = capsys.readouterr().out
    finally:
        logger.remove()
    assert "Extraction failed" in output and "RuntimeError" in output
    assert "s3cr3t-value" not in output


async def test_a_missing_chrome_binary_is_reported_at_startup_and_in_health(tmp_path, monkeypatch):
    records = []
    # The startup warning is logged before a test could attach a sink, so keep the app's own setup out.
    monkeypatch.setattr("app.main.setup_logging", lambda *args: None)
    sink = logger.add(lambda message: records.append(message.record), level="WARNING")
    config = replace(
        settings,
        result_cache_dir=str(tmp_path),
        host="127.0.0.1",
        api_key=None,
        chrome_binary=str(tmp_path / "no-chrome-here"),
    )
    resources = Resources(
        config, transport=httpx.MockTransport(lambda request: httpx.Response(200)), validate=lambda url: None
    )
    app = create_app(config, resources)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                health = (await client.get("/health")).json()
    finally:
        logger.remove(sink)
    assert health["browser"]["chrome_binary_exists"] is False
    assert any("no-chrome-here" in record["message"] for record in records)


async def test_failures_and_responses_carry_the_request_id_the_client_sent(api):
    client, records = api
    response = await client.post(
        "/crawl", json={"url": "https://example.com/unreachable"}, headers={"X-Request-ID": "support-case-42"}
    )
    assert response.status_code == 502
    assert response.headers["x-request-id"] == "support-case-42"
    assert records[0]["extra"]["request_id"] == "support-case-42"


async def test_a_generated_id_replaces_one_that_cannot_be_logged(api):
    client, _ = api
    sent = "broken id\r\nX-Injected: 1"
    response = await client.post("/crawl", json={"url": "https://example.com/article"}, headers={"X-Request-ID": sent})
    returned = response.headers["x-request-id"]
    assert response.status_code == 200 and returned != sent
    assert len(returned) == 16 and all(character in "0123456789abcdef" for character in returned)
