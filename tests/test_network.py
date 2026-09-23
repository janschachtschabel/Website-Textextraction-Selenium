import socket
import time
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
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::ffff:127.0.0.1", "fe80::1", "fec0::1", "100.64.0.1"]
)
def test_private_resolved_targets_are_blocked(address, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        ],
    )
    with pytest.raises(CrawlError, match="blocked by network policy"):
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


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:8767/stats",
        "http://169.254.169.254/latest/meta-data/",
        "http://internal.example/admin",
        "http://mixed.example/admin",
        "http://[::ffff:10.0.0.5]/",
    ],
)
async def test_default_policy_blocks_private_redirect_targets_before_requesting_them(dns, location):
    dns.update(
        {
            "public.example": "93.184.216.34",
            "internal.example": "10.0.0.5",
            "mixed.example": ["93.184.216.34", "10.0.0.8"],
        }
    )
    calls = []

    def transport(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    fetcher = HTTPFetcher(transport=httpx.MockTransport(transport))
    with pytest.raises(CrawlError, match="Target blocked by network policy") as blocked:
        await fetcher.fetch("https://public.example/redirect", options(), Deadline(5))
    assert blocked.value.status_code == 400
    assert calls == ["https://public.example/redirect"]
    await fetcher.close()


async def test_default_policy_follows_public_redirects(dns):
    dns.update({"public.example": "93.184.216.34", "moved.example": "93.184.216.35"})
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "https://moved.example/article"})
        return httpx.Response(200, content=b"<main>Moved</main>", headers={"content-type": "text/html"})

    fetcher = HTTPFetcher(transport=httpx.MockTransport(transport))
    result = await fetcher.fetch("https://public.example/old", options(), Deadline(5))
    assert (result.status_code, result.final_url) == (200, "https://moved.example/article")
    assert calls == ["https://public.example/old", "https://moved.example/article"]
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


def test_challenge_check_stays_fast_on_whitespace_only_text():
    started = time.perf_counter()
    for _ in range(5):
        assert not blocked_content(" \n" * 499, 200)
    assert (time.perf_counter() - started) / 5 < 0.01


def test_empty_spa_shell_needs_browser_but_cookie_words_do_not(article_html):
    shell = FetchResult(b'<div id="root"></div><script src="app.js"></script>', "https://example.com", 200, "text/html")
    assert needs_browser(shell, ConversionResult())
    fetched = FetchResult(
        (article_html + "<p>Cookie consent: accept</p>").encode(), "https://example.com", 200, "text/html"
    )
    assert not needs_browser(fetched, ConversionResult("Useful extracted content. " * 100, "trafilatura", "ok"))


async def test_requests_accept_html_and_send_a_language_only_when_set():
    seen = []

    def upstream(request):
        seen.append(request.headers)
        return httpx.Response(200, content=b"<p>x</p>", headers={"content-type": "text/html"})

    fetcher = HTTPFetcher(transport=httpx.MockTransport(upstream), validate=lambda url: None)
    try:
        for language in ("", "de,en;q=0.8"):
            options = resolve_options(CrawlRequest(url="https://example.com", accept_language=language))
            await fetcher.fetch("https://example.com", options, Deadline(5))
    finally:
        await fetcher.close()
    assert all(headers["accept"].startswith("text/html,") for headers in seen)
    assert "accept-language" not in seen[0]
    assert seen[1]["accept-language"] == "de,en;q=0.8"


async def test_validators_are_only_sent_to_the_url_that_issued_them():
    seen = []

    def upstream(request):
        seen.append((request.url.path, request.headers.get("if-none-match")))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://other.example/lesson"})
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=b"<p>x</p>", headers={"content-type": "text/html", "etag": '"v1"'})

    fetcher = HTTPFetcher(transport=httpx.MockTransport(upstream), validate=lambda url: None)
    try:
        first = await fetcher.fetch("https://example.com/old", options(), Deadline(5))
        assert first.validators == {"etag": '"v1"', "url": "https://other.example/lesson"}
        again = await fetcher.fetch("https://example.com/old", options(), Deadline(5), first.validators)
    finally:
        await fetcher.close()
    assert again.status_code == 304
    # The redirecting origin never sees the other site's ETag.
    assert seen[-2:] == [("/old", None), ("/lesson", '"v1"')]


def test_a_long_extraction_answers_routing_without_parsing_the_page_again(monkeypatch, article_html):
    from app import preflight

    monkeypatch.setattr(preflight, "BeautifulSoup", lambda *args: pytest.fail("The page was parsed a second time"))
    fetched = FetchResult(
        (article_html + '<div id="root"></div><script src="app.js"></script>').encode(),
        "https://example.com",
        200,
        "text/html",
    )
    extracted = ConversionResult("Reflection changes the direction of light. " * 40, "trafilatura", "ok")
    assert needs_browser(fetched, extracted) is False


def test_image_alt_text_is_not_visible_content_for_that_shortcut():
    shell = FetchResult(b'<div id="root"></div><script src="app.js"></script>', "https://example.com", 200, "text/html")
    alt_only = ConversionResult(
        "![" + "Beschreibung eines Bildes " * 60 + "](https://example.com/bild.png)", "bs4", "ok"
    )
    assert needs_browser(shell, alt_only) is True
