"""Exposure rules that hold before a request reaches the pipeline."""

import importlib
from dataclasses import replace

import httpx
import pytest

from app import config as config_module
from app.config import settings
from app.main import create_app

LOCAL = replace(settings, host="127.0.0.1", api_key=None)


def test_non_loopback_host_without_api_key_is_refused():
    with pytest.raises(ValueError, match="API_KEY"):
        replace(settings, host="0.0.0.0", api_key=None)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
def test_loopback_hosts_start_without_an_api_key(host):
    assert replace(settings, host=host, api_key=None).host == host


def test_non_loopback_host_starts_with_an_api_key():
    assert replace(settings, host="0.0.0.0", api_key="token").host == "0.0.0.0"


def test_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    try:
        assert importlib.reload(config_module).Settings(api_key=None).host == "127.0.0.1"
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)


async def _request(app, method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_interactive_docs_are_hidden_when_a_key_is_configured(path):
    response = await _request(create_app(replace(LOCAL, api_key="token")), "GET", path)
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_interactive_docs_stay_available_without_a_key(path):
    assert (await _request(create_app(LOCAL), "GET", path)).status_code == 200


async def test_oversized_body_is_rejected_before_authentication():
    app = create_app(replace(LOCAL, api_key="token", max_request_bytes=1024))
    response = await _request(app, "POST", "/crawl", content=b"x" * 2048, headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert "1024" in response.json()["detail"]


async def test_body_within_the_limit_still_reaches_authentication():
    app = create_app(replace(LOCAL, api_key="token", max_request_bytes=1024))
    response = await _request(app, "POST", "/crawl", json={"url": "https://example.com"})
    assert response.status_code == 401


async def test_streamed_body_is_capped_without_a_content_length():
    app = create_app(replace(LOCAL, max_request_bytes=1024))
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"x" * 600, "more_body": True}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/crawl",
            "raw_path": b"/crawl",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        },
        receive,
        send,
    )
    assert [message["type"] for message in messages] == ["http.response.start", "http.response.body"]
    assert messages[0]["status"] == 413
