"""Real browser gates, using only a locally controlled fixture origin."""

import asyncio
import os
import ssl
import time
import traceback
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest

from app.deadline import Deadline
from app.egress_proxy import EgressProxy
from app.js_fetcher import selenium_fetch
from app.results import CrawlError
from app.schemas import CrawlRequest, resolve_options
from app.security import Target
from app.workers import WorkerPool

pytestmark = [
    pytest.mark.selenium,
    pytest.mark.skipif(os.getenv("RUN_SELENIUM_TESTS") != "1", reason="Opt-in real Chrome integration"),
]


def fixture_fetch(*args):
    """Expose chained browser errors for fixture diagnostics, never API responses."""
    try:
        return selenium_fetch(*args)
    except CrawlError:
        traceback.print_exc()
        raise


@pytest.fixture
async def browser(monkeypatch):
    requests = []
    handlers = set()

    async def origin(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            path = head.split(" ")[1]
            requests.append((path, head))
            status = 404 if path in {"/missing", "/empty-404"} else 200
            content_type = "text/html; charset=utf-8"
            if path == "/dynamic":
                html = '<main id="result" aria-busy="true"></main><script>setTimeout(()=>{let e=document.querySelector("main");e.innerText="DYNAMIC"+"CONTENT ready";e.setAttribute("aria-busy","false")},700)</script>'
            elif path == "/cookie":
                html = '<main>Session page</main><script>document.cookie="private_session=one; path=/"</script>'
            elif path == "/subrequest":
                html = f'<main>Public content</main><iframe src="http://127.0.0.1:{port}/private"></iframe>'
            elif path == "/hang":
                html = '<main aria-busy="true">Loading content</main>'
            elif path == "/hidden-loader":
                html = '<div role="progressbar" aria-hidden="true" style="height:0"><div></div></div><main>Loaded page</main>'
            elif path == "/skill-bars":
                html = '<main>Skills page</main><div role="progressbar" aria-valuenow="75" aria-valuemin="0" aria-valuemax="100" style="width:75%;height:20px"></div>'
            elif path == "/two-mains":
                html = '<main style="height:0"></main><main>SECONDMAIN content</main>'
            elif path == "/spinner":
                html = '<main>PERMANENTSPINNER page</main><div role="progressbar" style="width:40px;height:40px"></div>'
            elif path == "/late-main":
                html = '<header>Site header navigation</header><main></main><script>setTimeout(()=>{document.querySelector("main").innerText="LATE"+"CONTENT arrived"},1500)</script>'
            elif path == "/busy-list":
                html = '<h1>Results</h1><div id="list" aria-busy="true"></div><script>setTimeout(()=>{const e=document.getElementById("list");e.innerText="LATE"+"CONTENT arrived";e.removeAttribute("aria-busy")},1500)</script>'
            elif path == "/modal-spinner":
                html = '<div aria-hidden="true"><main>Page behind a dialog</main><div role="progressbar" style="width:40px;height:40px"></div></div><div role="dialog">Consent</div><script>setTimeout(()=>{document.querySelector("[role=progressbar]").remove();document.querySelector("main").innerText="LATE"+"CONTENT arrived"},1500)</script>'
            elif path == "/download":
                html, content_type = "<main>Lesson file</main>", "application/octet-stream"  # Chrome downloads it
            else:
                html = "<main>Page not found</main>" if status == 404 else "<main>Fixture page content</main>"
            data = b"" if path == "/empty-404" else ("<!doctype html><html><body>" + html + "</body></html>").encode()
            writer.write(
                f"HTTP/1.1 {status} Fixture\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, OSError):
            return
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    def fixture_target(url, protection=True):
        parsed = urlsplit(url)
        if parsed.hostname == "fixture.example" and parsed.port == port:
            return Target("fixture.example", port, ("127.0.0.1",), "http")
        raise CrawlError("Non-fixture destination blocked", 400)

    monkeypatch.setattr("app.egress_proxy.resolve_target", fixture_target)
    pool = WorkerPool(1)
    try:
        async with EgressProxy() as guard:

            async def fetch(path, *, seconds=30, **kwargs):
                url = path if "://" in path else f"http://fixture.example:{port}{path}"
                options = resolve_options(CrawlRequest(url=url, mode="js", **kwargs))
                deadline = Deadline(seconds)
                return await pool.run(fixture_fetch, (url, options, guard.url, deadline.expires_at), deadline)

            yield fetch, requests, pool
    finally:
        await pool.close()
        server.close()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*list(handlers), return_exceptions=True)
        await server.wait_closed()


async def test_dynamic_content_screenshot_and_real_404(browser):
    fetch, _, _ = browser
    rendered = await fetch("/dynamic", screenshot=True, wait_for_selectors=["#result"])
    assert b"DYNAMICCONTENT ready" in rendered.data and rendered.status_code == 200
    assert rendered.screenshot_base64.startswith("iVBOR")
    missing = await fetch("/missing")
    assert missing.status_code == 404 and missing.engine == "selenium"
    # Chrome shows its own page for an error status without a body; keep the status, drop its text.
    empty = await fetch("/empty-404")
    assert (empty.status_code, empty.data) == (404, b"")


async def test_profiles_do_not_share_cookies_and_private_subrequests_are_blocked(browser):
    fetch, requests, _ = browser
    await fetch("/cookie")
    await fetch("/inspect")
    assert all("private_session" not in head for path, head in requests if path == "/inspect")
    result = await fetch("/subrequest")
    assert result.status_code == 200
    assert "/private" not in [path for path, _ in requests]


@pytest.mark.parametrize("path", ["/hidden-loader", "/skill-bars", "/two-mains"])
async def test_hidden_or_static_progress_and_empty_first_main_do_not_delay_readiness(browser, path):
    fetch, _, _ = browser
    # The deadline is the assertion: an auto-wait triggered here would run into its
    # ten second limit and fail the fetch instead of returning settled content.
    result = await fetch(path, seconds=8, js_strategy="speed", js_auto_wait=True)
    assert result.status_code == 200 and not result.warnings


@pytest.mark.parametrize("path", ["/late-main", "/busy-list", "/modal-spinner"])
async def test_content_that_arrives_late_is_still_awaited(browser, path):
    fetch, _, _ = browser
    result = await fetch(path, seconds=20, js_strategy="speed", js_auto_wait=True)
    assert b"LATECONTENT arrived" in result.data and result.settled


def self_signed_server_context(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.example")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.example")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


async def test_certificate_error_page_is_a_failure_not_content(browser, monkeypatch, tmp_path):
    fetch, _, _ = browser

    async def page(reader, writer):
        writer.close()

    server = await asyncio.start_server(page, "127.0.0.1", 0, ssl=self_signed_server_context(tmp_path))
    port = server.sockets[0].getsockname()[1]

    def tls_target(url, protection=True):
        if urlsplit(url).port == port:
            return Target("fixture.example", port, ("127.0.0.1",), "https")
        raise CrawlError("Non-fixture destination blocked", 400)

    monkeypatch.setattr("app.egress_proxy.resolve_target", tls_target)
    try:
        with pytest.raises(CrawlError) as error:
            await fetch(f"https://fixture.example:{port}/", seconds=20)
        assert str(error.value) == "Selenium navigation failed (net::ERR_CERT_AUTHORITY_INVALID)"
    finally:
        server.close()
        await server.wait_closed()


async def test_rejected_tunnel_is_named_and_download_does_not_wait(browser):
    fetch, _, _ = browser
    with pytest.raises(CrawlError) as error:
        await fetch("https://blocked.example/", seconds=15)
    assert str(error.value).startswith("Selenium navigation failed (net::ERR_")
    started = time.monotonic()
    result = await fetch("/download", seconds=30, js_strategy="speed", js_auto_wait=True)
    assert time.monotonic() - started < 15  # a download must not wait for a page that never loads
    assert any("download" in warning for warning in result.warnings)


async def test_permanent_spinner_does_not_use_up_the_deadline(browser):
    fetch, _, _ = browser
    started = time.monotonic()
    result = await fetch("/spinner", seconds=30, js_strategy="speed", js_auto_wait=True)
    assert time.monotonic() - started < 25  # the auto-wait limit ends the wait, not the deadline
    assert b"PERMANENTSPINNER" in result.data and not result.settled
    assert any("settle" in warning for warning in result.warnings)


async def test_browser_deadline_cleans_up_and_next_job_succeeds(browser):
    fetch, _, pool = browser
    await fetch("/inspect")
    started = time.monotonic()
    with pytest.raises(CrawlError, match="deadline"):
        await fetch("/hang", seconds=3)
    assert time.monotonic() - started < 10  # cleanup follows the deadline instead of hanging
    assert pool.stats()["started"] == 0
    recovered = await fetch("/inspect")
    assert recovered.status_code == 200
