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


def test_default_user_agent_names_the_version_and_a_contact_url(monkeypatch):
    from app import __version__

    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("DEFAULT_USER_AGENT", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    try:
        agent = importlib.reload(config_module).Settings(api_key=None).default_user_agent
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)
    # Sites with a bot policy (Wikimedia answers 403 otherwise) expect a way to reach the operator.
    assert agent == (
        f"WebsiteTextExtraction/{__version__} (+https://github.com/janschachtschabel/Website-Textextraction-Selenium)"
    )


def test_an_empty_user_agent_setting_falls_back_to_the_default(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.setenv("DEFAULT_USER_AGENT", "")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    try:
        module = importlib.reload(config_module)
        agent = module.Settings(api_key=None).default_user_agent
        expected = module.DEFAULT_USER_AGENT
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)
    assert agent == expected


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


@pytest.mark.parametrize("key, advertised", [(None, "/docs"), ("token", None)])
async def test_root_advertises_the_docs_only_while_they_exist(key, advertised):
    response = await _request(create_app(replace(LOCAL, api_key=key)), "GET", "/")
    assert response.json()["docs"] == advertised


def _upload_scope():
    return {
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
    }


async def test_the_rest_of_an_oversized_body_is_read_before_the_error_is_sent():
    app = create_app(replace(LOCAL, max_request_bytes=1024))
    remaining = [{"type": "http.request", "body": b"x" * 600, "more_body": True} for _ in range(4)]
    remaining.append({"type": "http.request", "body": b"x" * 10, "more_body": False})
    messages = []

    async def receive():
        return remaining.pop(0) if remaining else {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(_upload_scope(), receive, send)
    assert remaining == []  # the client could finish sending, so it can read the answer
    assert messages[0]["status"] == 413


async def test_a_declared_oversized_length_also_reads_the_upload_first():
    app = create_app(replace(LOCAL, max_request_bytes=1024))
    scope = _upload_scope()
    scope["headers"] = [*scope["headers"], (b"content-length", b"2048")]
    remaining = [
        {"type": "http.request", "body": b"x" * 1024, "more_body": True},
        {"type": "http.request", "body": b"x" * 1024, "more_body": False},
    ]
    messages = []

    async def receive():
        return remaining.pop(0) if remaining else {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    assert remaining == []  # the declared length is the common case: it must not close early either
    assert messages[0]["status"] == 413


async def test_reading_the_rest_of_a_body_stays_bounded():
    from app.body_limit import DRAIN_BYTES

    app = create_app(replace(LOCAL, max_request_bytes=1024))
    reads = 0
    messages = []

    async def receive():  # a client that never stops sending
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 65536, "more_body": True}

    async def send(message):
        messages.append(message)

    await app(_upload_scope(), receive, send)
    assert reads <= DRAIN_BYTES // 65536 + 2
    assert messages[0]["status"] == 413
