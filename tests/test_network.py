import socket
from dataclasses import replace

import diskcache
import httpx
import pytest

from app.config import settings
from app.deadline import Deadline
from app.http_fetcher import HTTPFetcher
from app.preflight import blocked_content, needs_browser
from app.rate_limiter import RateLimiter
from app.results import ConversionResult, CrawlError, FetchResult
from app.schemas import CrawlRequest, resolve_options
from app.security import resolve_target


def options(**values):
    return resolve_options(CrawlRequest(url="https://example.com", **values))


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::ffff:127.0.0.1", "fe80::1", "100.64.0.1"]
)
def test_private_resolved_targets_are_blocked(address, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        ],
    )
    with pytest.raises(CrawlError):
        resolve_target("https://rebind.example")


async def test_http_reads_full_document_and_reports_request_limit():
    body = b"<html><main>" + b"a" * 810_000 + b"ENDMARKER</main></html>"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body, headers={"content-type": "text/html"})
    )
    fetcher = HTTPFetcher(transport=transport, validate=lambda url: None)
    full = await fetcher.fetch("https://example.com", options(max_bytes=900_000), Deadline(5))
    assert full.data == body and not full.truncated
    small = await fetcher.fetch("https://example.com", options(max_bytes=1024), Deadline(5))
    assert len(small.data) == 1024 and small.truncated
    await fetcher.close()


@pytest.mark.parametrize("retries", [0, 1])
async def test_http_timeout_is_reported_as_the_expired_deadline(retries):
    # Every HTTPX timeout is the remaining deadline; with a coarse clock it can fire before the deadline.
    def transport(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    fetcher = HTTPFetcher(transport=httpx.MockTransport(transport), validate=lambda url: None)
    with pytest.raises(CrawlError) as error:
        await fetcher.fetch("https://example.com", options(retries=retries), Deadline(5))
    assert (str(error.value), error.value.status_code) == ("Crawl deadline exceeded", 504)
    await fetcher.close()


async def test_redirect_is_checked_before_second_request():
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    def validate(url):
        if "127.0.0.1" in url:
            raise CrawlError("Target blocked", 400)

    fetcher = HTTPFetcher(transport=httpx.MockTransport(transport), validate=validate)
    with pytest.raises(CrawlError, match="blocked"):
        await fetcher.fetch("https://example.com", options(), Deadline(5))
    assert len(calls) == 1
    await fetcher.close()


async def test_retry_is_rate_limited_and_cookies_do_not_cross_requests():
    seen = []
    reserved = []

    async def acquire(url, rate, deadline):
        reserved.append(url)

    def transport(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(
            503 if len(seen) == 1 else 200,
            content=b"hello",
            headers={"set-cookie": "session=secret", "retry-after": "0"},
        )

    fetcher = HTTPFetcher(transport=httpx.MockTransport(transport), validate=lambda url: None, acquire=acquire)
    result = await fetcher.fetch("https://example.com", options(retries=1), Deadline(5))
    await fetcher.fetch("https://example.com", options(retries=0), Deadline(5))
    assert result.status_code == 200 and len(reserved) == 3
    assert seen == [None, None, None]
    await fetcher.close()


def test_fractional_rate_and_changed_override_share_reservations(tmp_path):
    with diskcache.Cache(str(tmp_path)) as store:
        limiter = RateLimiter(store, replace(settings, global_rate_limit_rps=0))
        assert limiter.reserve("example.com", 10, now=100, latest=200) == 100
        assert limiter.reserve("example.com", 0.5, now=100, latest=200) == 102
        second_worker = RateLimiter(store, settings)
        assert second_worker.reserve("example.com", 0.5, now=100, latest=200) == 104


def test_blocked_status_short_success_and_feed_link_do_not_start_browser():
    article = ConversionResult("A short useful answer.", "trafilatura", "ok")
    for status, html in [
        (429, '<div id="root"></div>'),
        (200, "<main>Short answer.</main>"),
        (200, '<link type="application/rss+xml" href="/feed"><main>Article</main>'),
    ]:
        fetched = FetchResult(html.encode(), "https://example.com", status, "text/html")
        assert not needs_browser(fetched, article)


@pytest.mark.parametrize(
    "text, blocked",
    [
        ("Just a moment...", True),
        ("# Example Shop\n\nChecking your browser before accessing example.com.", True),
        ("www.example.com\nVerifying you are human. This may take a few seconds.", True),
        ("# Physics\n\nLight changes its direction at a mirror.", False),
        ("# Physics\n\nIntroduction\n\nJust a moment of reflection explains the rays.", False),
    ],
)
def test_challenge_text_is_detected_after_a_heading_or_site_name(text, blocked):
    assert blocked_content(text, 200) is blocked


def test_empty_spa_shell_needs_browser_but_cookie_words_do_not(article_html):
    shell = FetchResult(b'<div id="root"></div><script src="app.js"></script>', "https://example.com", 200, "text/html")
    assert needs_browser(shell, ConversionResult())
    fetched = FetchResult(
        (article_html + "<p>Cookie consent: accept</p>").encode(), "https://example.com", 200, "text/html"
    )
    assert not needs_browser(fetched, ConversionResult("Useful extracted content. " * 100, "trafilatura", "ok"))
