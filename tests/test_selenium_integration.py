"""Real browser gates, using only a locally controlled fixture origin."""

import asyncio
import os
import time
import traceback
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
            status = 404 if path == "/missing" else 200
            if path == "/dynamic":
                html = '<main id="result" aria-busy="true"></main><script>setTimeout(()=>{let e=document.querySelector("main");e.innerText="DYNAMICCONTENT ready";e.setAttribute("aria-busy","false")},700)</script>'
            elif path == "/cookie":
                html = '<main>Session page</main><script>document.cookie="private_session=one; path=/"</script>'
            elif path == "/subrequest":
                html = f'<main>Public content</main><iframe src="http://127.0.0.1:{port}/private"></iframe>'
            elif path == "/hang":
                html = '<main aria-busy="true">Loading content</main>'
            else:
                html = "<main>Page not found</main>" if status == 404 else "<main>Fixture page content</main>"
            data = ("<!doctype html><html><body>" + html + "</body></html>").encode()
            writer.write(
                f"HTTP/1.1 {status} Fixture\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
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
                url = f"http://fixture.example:{port}{path}"
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


async def test_profiles_do_not_share_cookies_and_private_subrequests_are_blocked(browser):
    fetch, requests, _ = browser
    await fetch("/cookie")
    await fetch("/inspect")
    assert all("private_session" not in head for path, head in requests if path == "/inspect")
    result = await fetch("/subrequest")
    assert result.status_code == 200
    assert "/private" not in [path for path, _ in requests]


async def test_browser_deadline_cleans_up_and_next_job_succeeds(browser):
    fetch, _, pool = browser
    await fetch("/inspect")
    started = time.monotonic()
    with pytest.raises(CrawlError, match="deadline"):
        await fetch("/hang", seconds=3)
    assert time.monotonic() - started < 6
    assert pool.stats()["started"] == 0
    recovered = await fetch("/inspect")
    assert recovered.status_code == 200
