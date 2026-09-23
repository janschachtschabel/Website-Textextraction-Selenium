import asyncio
import json
import time
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from app import browser_readiness
from app.browser_readiness import navigation_status
from app.config import settings
from app.deadline import Deadline
from app.js_fetcher import BrowserFetcher
from app.results import CrawlError, FetchResult
from app.schemas import CrawlRequest, resolve_options
from app.selenium_driver import build_options


def test_browser_respects_request_options_and_has_no_security_bypass():
    request = resolve_options(
        CrawlRequest(
            url="https://example.com",
            allow_insecure_ssl=False,
            user_agent="IntegrationTest/1",
            headless=True,
            js_strategy="speed",
        )
    )
    chrome = build_options(request, "http://127.0.0.1:1234", settings)
    assert chrome.accept_insecure_certs is False
    assert chrome.page_load_strategy == "eager"
    assert "--proxy-server=http://127.0.0.1:1234" in chrome.arguments
    assert "--proxy-bypass-list=<-loopback>" in chrome.arguments
    assert "--user-agent=IntegrationTest/1" in chrome.arguments
    assert "--disable-web-security" not in chrome.arguments
    assert "--no-sandbox" not in chrome.arguments


async def test_browser_result_ending_on_a_prohibited_address_is_rejected(dns):
    dns["public.example"] = "93.184.216.34"
    subrequest = b"CONNECT 10.0.0.1:443 HTTP/1.1\r\nHost: 10.0.0.1:443\r\n\r\n"

    class Pool:
        def __init__(self, final_url, blocked_request=None):
            self.final_url, self.blocked_request = final_url, blocked_request

        async def run(self, function, args, deadline):
            if self.blocked_request:
                # A browser request through this job's guard.
                reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(args[2]).port)
                writer.write(self.blocked_request)
                await writer.drain()
                assert (await reader.read()).startswith(b"HTTP/1.1 403")
                writer.close()
                await writer.wait_closed()
            return FetchResult(b"<main>Page</main>", self.final_url, 200, "text/html", "selenium")

    class Rate:
        async def acquire(self, *args):
            pass

    def fetch(pool):
        options = resolve_options(CrawlRequest(url="https://public.example/", mode="js"))
        fetcher = BrowserFetcher(pool, Rate(), replace(settings, ssrf_protection=True))
        return fetcher.fetch("https://public.example/", options, Deadline(5))

    kept = await fetch(Pool("https://public.example/article", subrequest))
    assert kept.warnings == ["Network policy blocked 1 browser connection(s)"]
    assert (await fetch(Pool("about:blank"))).final_url == "about:blank"
    # A script navigation that the guard blocked, and a page that bypassed the guard.
    for pool in (Pool("https://[::1]/", subrequest), Pool("http://127.0.0.1:8767/stats")):
        with pytest.raises(CrawlError, match="Target blocked by network policy") as blocked:
            await fetch(pool)
        assert blocked.value.status_code == 400


def test_main_document_status_is_not_overwritten_by_iframe_or_assets():
    def event(status, frame, kind):
        return {
            "message": json.dumps(
                {
                    "message": {
                        "method": "Network.responseReceived",
                        "params": {
                            "type": kind,
                            "frameId": frame,
                            "response": {"status": status, "mimeType": "text/html"},
                        },
                    }
                }
            )
        }

    entries = [event(404, "main", "Document"), event(200, "iframe", "Document"), event(200, "main", "Image")]
    assert navigation_status(entries, "main") == (404, "text/html")
    assert navigation_status([], "main") == (None, None)


@pytest.mark.parametrize(
    "snapshot",
    [
        {"text": "Visible content", "busy": True, "ready": True, "math": True},  # spinner that never stops
        {"text": "", "busy": False, "ready": True, "math": True},  # nothing rendered, e.g. a download
    ],
)
def test_auto_wait_gives_up_on_pages_that_never_settle(monkeypatch, snapshot):
    from app.deadline import Deadline

    class Driver:
        def execute_script(self, script, *args):
            return snapshot

        def find_elements(self, *args):
            return []

    monkeypatch.setattr(browser_readiness, "AUTO_WAIT_LIMIT_SECONDS", {"speed": 0.3, "accuracy": 0.3})
    options = resolve_options(CrawlRequest(url="https://example.com", js_strategy="speed", js_auto_wait=True))
    started = time.monotonic()
    assert browser_readiness.wait_for_content(Driver(), options, Deadline(3)) is False
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("explicit", [{"wait_for_selectors": ["#results"]}, {"wait_for_ms": 600}])
def test_auto_wait_limit_starts_after_explicit_waits(monkeypatch, explicit):
    from app.deadline import Deadline

    started = time.monotonic()

    class Element:
        def is_displayed(self):
            return True

    class Driver:
        def execute_script(self, script, *args):
            # The page keeps changing until 0.6 s, later than the auto-wait limit, then stays.
            elapsed = time.monotonic() - started
            self.late = elapsed >= 0.6
            text = "Loaded results" if self.late else f"Loading {int(elapsed * 20)}"
            return {"text": text, "busy": False, "ready": True, "math": True}

        def find_elements(self, *args):
            return [Element()] if self.late else []

    monkeypatch.setattr(browser_readiness, "AUTO_WAIT_LIMIT_SECONDS", {"speed": 0.3, "accuracy": 0.3})
    options = resolve_options(
        CrawlRequest(url="https://example.com", js_strategy="speed", js_auto_wait=True, **explicit)
    )
    assert browser_readiness.wait_for_content(Driver(), options, Deadline(5)) is True


def test_rendered_html_is_bounded_before_webdriver_transfers_it(monkeypatch):
    from app import js_fetcher
    from app.deadline import Deadline

    class Driver:
        current_url = "https://example.com/"

        @property
        def page_source(self):
            raise AssertionError("Do not transfer the entire unbounded document")

        def set_page_load_timeout(self, value):
            pass

        def set_script_timeout(self, value):
            pass

        def get(self, url):
            pass

        def execute_cdp_cmd(self, method, params):
            return {"frameTree": {"frame": {"id": "main", "url": "https://example.com/"}}}

        def get_log(self, kind):
            return [
                {
                    "message": json.dumps(
                        {
                            "message": {
                                "method": "Network.responseReceived",
                                "params": {
                                    "type": "Document",
                                    "frameId": "main",
                                    "response": {"status": 200, "mimeType": "text/html"},
                                },
                            }
                        }
                    )
                }
            ]

        def execute_script(self, script, *args):
            if args:
                assert args == (1024,)
                return {"html": "x" * 1024, "truncated": True}

        def quit(self):
            pass

    monkeypatch.setattr(js_fetcher, "create_driver", lambda *args: Driver())
    options = resolve_options(CrawlRequest(url="https://example.com", max_bytes=1024, js_auto_wait=False))
    result = js_fetcher.selenium_fetch("https://example.com", options, "http://127.0.0.1:1234", Deadline(5).expires_at)
    assert result.truncated and len(result.data) == 1024


def log_entry(method, **params):
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


class NavigationDriver:
    """Fake Chrome whose main frame ends at `frame_url` after navigation."""

    def __init__(self, frame_url, entries):
        self.frame_url, self.entries = frame_url, entries
        self.current_url = frame_url

    def set_page_load_timeout(self, value):
        pass

    def set_script_timeout(self, value):
        pass

    def get(self, url):
        pass

    def execute_cdp_cmd(self, method, params):
        return {"frameTree": {"frame": {"id": "main", "url": self.frame_url}}}

    def get_log(self, kind):
        entries, self.entries = self.entries, []
        return entries

    def execute_script(self, script, *args):
        if args:
            return {"html": "<html><head></head><body></body></html>", "truncated": False}
        return {"text": "Displayed text", "busy": False, "ready": True, "math": True}

    def get_screenshot_as_base64(self):
        return "iVBORw0KGgo="

    def quit(self):
        pass


@pytest.mark.parametrize(
    "error_text, message",
    [
        ("net::ERR_CERT_AUTHORITY_INVALID", "Selenium navigation failed (net::ERR_CERT_AUTHORITY_INVALID)"),
        ("unexpected <b>text</b>", "Selenium navigation failed"),
    ],
)
def test_chrome_error_page_is_a_navigation_failure_not_content(monkeypatch, error_text, message):
    from app import js_fetcher
    from app.deadline import Deadline
    from app.results import CrawlError

    entries = [
        log_entry("Network.requestWillBeSent", requestId="1", frameId="main", type="Document"),
        log_entry("Network.loadingFailed", requestId="7", type="Document", errorText="net::ERR_ABORTED"),
        log_entry("Network.loadingFailed", requestId="1", type="Document", errorText=error_text),
    ]
    driver = NavigationDriver("chrome-error://chromewebdata/", entries)
    monkeypatch.setattr(js_fetcher, "create_driver", lambda *args: driver)
    options = resolve_options(CrawlRequest(url="https://example.com", mode="js"))
    with pytest.raises(CrawlError) as error:
        js_fetcher.selenium_fetch("https://example.com", options, "http://127.0.0.1:1234", Deadline(5).expires_at)
    assert (str(error.value), error.value.status_code) == (message, 502)


def test_chrome_page_for_an_empty_error_response_keeps_the_status_without_its_text(monkeypatch):
    from app import js_fetcher
    from app.deadline import Deadline

    entries = [
        log_entry("Network.requestWillBeSent", requestId="1", frameId="main", type="Document"),
        log_entry(
            "Network.responseReceived",
            requestId="1",
            frameId="main",
            type="Document",
            response={"status": 404, "mimeType": "text/html"},
        ),
        log_entry(
            "Network.loadingFailed", requestId="1", type="Document", errorText="net::ERR_HTTP_RESPONSE_CODE_FAILURE"
        ),
    ]
    driver = NavigationDriver("chrome-error://chromewebdata/", entries)
    driver.current_url = "https://example.com/missing"
    monkeypatch.setattr(js_fetcher, "create_driver", lambda *args: driver)
    options = resolve_options(CrawlRequest(url="https://example.com/missing", mode="js", screenshot=True))
    result = js_fetcher.selenium_fetch(
        "https://example.com/missing", options, "http://127.0.0.1:1234", Deadline(5).expires_at
    )
    assert (result.status_code, result.data, result.warnings) == (404, b"", [])
    assert result.screenshot_base64 is None  # a picture of Chrome's error page is not content either
    assert result.final_url == "https://example.com/missing"


def test_chromedriver_navigation_error_names_the_network_error(monkeypatch):
    from selenium.common.exceptions import WebDriverException

    from app import js_fetcher
    from app.deadline import Deadline
    from app.results import CrawlError

    class RejectedTunnel(NavigationDriver):
        def get(self, url):
            raise WebDriverException("unknown error: net::ERR_TUNNEL_CONNECTION_FAILED\n  (Session info: chrome=1)")

    monkeypatch.setattr(js_fetcher, "create_driver", lambda *args: RejectedTunnel("data:,", []))
    options = resolve_options(CrawlRequest(url="https://example.com", mode="js"))
    with pytest.raises(CrawlError) as error:
        js_fetcher.selenium_fetch("https://example.com", options, "http://127.0.0.1:1234", Deadline(5).expires_at)
    assert str(error.value) == "Selenium navigation failed (net::ERR_TUNNEL_CONNECTION_FAILED)"


def test_page_script_error_text_never_becomes_a_network_error_code(monkeypatch):
    from selenium.common.exceptions import JavascriptException

    from app import js_fetcher
    from app.deadline import Deadline
    from app.results import CrawlError

    class ThrowingPage(NavigationDriver):
        def execute_script(self, script, *args):
            raise JavascriptException("javascript error: net::ERR_WRITTEN_BY_THE_PAGE")

    entries = [
        log_entry(
            "Network.responseReceived",
            frameId="main",
            type="Document",
            response={"status": 200, "mimeType": "text/html"},
        )
    ]
    monkeypatch.setattr(js_fetcher, "create_driver", lambda *args: ThrowingPage("https://example.com/", entries))
    options = resolve_options(CrawlRequest(url="https://example.com", mode="js", js_auto_wait=True))
    with pytest.raises(CrawlError) as error:
        js_fetcher.selenium_fetch("https://example.com", options, "http://127.0.0.1:1234", Deadline(5).expires_at)
    assert str(error.value) == "Selenium navigation failed"


def test_download_is_reported_without_waiting_for_a_page(monkeypatch):
    from app import js_fetcher
    from app.deadline import Deadline

    entries = [
        log_entry(
            "Network.responseReceived",
            frameId="main",
            type="Document",
            response={"status": 200, "mimeType": "application/pdf"},
        )
    ]
    # Chrome denies the download and keeps its initial blank page.
    monkeypatch.setattr(js_fetcher, "create_driver", lambda *args: NavigationDriver("data:,", entries))
    monkeypatch.setattr(js_fetcher, "wait_for_content", lambda *args: pytest.fail("No page to wait for"))
    options = resolve_options(CrawlRequest(url="https://example.com/a.pdf", mode="js", js_auto_wait=True))
    result = js_fetcher.selenium_fetch(
        "https://example.com/a.pdf", options, "http://127.0.0.1:1234", Deadline(5).expires_at
    )
    assert any("download" in warning for warning in result.warnings)
    assert result.final_url == "https://example.com/a.pdf"


class CountingPool:
    def __init__(self, error):
        self.error, self.attempts = error, 0

    async def run(self, function, args, deadline):
        self.attempts += 1
        raise self.error


async def browser_attempts(error, retries=2):
    from app.deadline import Deadline
    from app.js_fetcher import BrowserFetcher

    class NoRateLimit:
        async def acquire(self, *args):
            return None

    pool = CountingPool(error)
    fetcher = BrowserFetcher(pool, NoRateLimit(), settings)
    options = resolve_options(CrawlRequest(url="https://example.com", retries=retries))
    with pytest.raises(type(error)):
        await fetcher.fetch("https://example.com", options, Deadline(10))
    return pool.attempts


@pytest.mark.parametrize(
    "code", ["net::ERR_CERT_AUTHORITY_INVALID", "net::ERR_CERT_DATE_INVALID", "net::ERR_BLOCKED_BY_CLIENT"]
)
async def test_deterministic_browser_failures_are_not_retried(code):
    from app.js_fetcher import navigation_failed

    assert await browser_attempts(navigation_failed(code)) == 1


@pytest.mark.parametrize("code", ["net::ERR_CONNECTION_RESET", "net::ERR_TUNNEL_CONNECTION_FAILED", None])
async def test_transient_browser_failures_still_use_every_attempt(code):
    from app.js_fetcher import navigation_failed

    assert await browser_attempts(navigation_failed(code)) == 3


class ChromeDriverProcess:
    def __init__(self):
        self.returncode, self.calls = None, []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        self.returncode = 0

    def wait(self, timeout):
        self.calls.append("wait")


class ChromeDriverService:
    def __init__(self):
        self.process = ChromeDriverProcess()
        self.stopped_after_exit = None

    def stop(self):
        self.stopped_after_exit = self.process.poll() is not None


class QuittingDriver:
    def __init__(self):
        self.service = ChromeDriverService()


def test_quit_ends_the_session_then_stops_chromedriver_without_polling(monkeypatch):
    from selenium.webdriver.remote.webdriver import WebDriver

    from app.selenium_driver import Chrome

    ended = []
    monkeypatch.setattr(WebDriver, "quit", lambda self: ended.append("session"))
    driver = QuittingDriver()
    Chrome.quit(driver)
    assert ended == ["session"]
    assert driver.service.process.calls == ["terminate", "wait"]
    assert driver.service.stopped_after_exit is True  # Service.stop() then skips its shutdown polling


def test_quit_stops_chromedriver_even_when_the_session_cannot_be_ended(monkeypatch):
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.remote.webdriver import WebDriver

    from app.selenium_driver import Chrome

    def lost_session(self):
        raise WebDriverException("chrome not reachable")

    monkeypatch.setattr(WebDriver, "quit", lost_session)
    driver = QuittingDriver()
    with pytest.raises(WebDriverException):
        Chrome.quit(driver)  # the caller turns this into a worker restart
    assert driver.service.process.calls == ["terminate", "wait"]
    assert driver.service.stopped_after_exit is True


def test_browser_gets_the_language_list_without_q_values():
    chosen = resolve_options(CrawlRequest(url="https://example.com", accept_language="de-DE, en;q=0.8"))
    assert "--accept-lang=de-DE,en" in build_options(chosen, "http://127.0.0.1:1234", settings).arguments
    unset = resolve_options(CrawlRequest(url="https://example.com", accept_language=""))
    arguments = build_options(unset, "http://127.0.0.1:1234", settings).arguments
    assert not any(argument.startswith("--accept-lang") for argument in arguments)


def test_browser_status_reports_configured_paths_and_whether_they_exist(tmp_path):
    from dataclasses import replace

    from app.selenium_driver import browser_status

    present = tmp_path / "chrome"
    present.write_text("binary", encoding="utf-8")
    configured = replace(settings, chrome_binary=str(present), chromedriver_path=str(tmp_path / "gone"))
    assert browser_status(configured) == {
        "chrome_binary": str(present),
        "chrome_binary_exists": True,
        "chromedriver_path": str(tmp_path / "gone"),
        "chromedriver_exists": False,
    }
    # Unset paths are resolved by Selenium Manager at first use, so their presence is unknown.
    unset = replace(settings, chrome_binary=None, chromedriver_path=None)
    assert browser_status(unset) == {
        "chrome_binary": None,
        "chrome_binary_exists": None,
        "chromedriver_path": None,
        "chromedriver_exists": None,
    }


class LayoutDriver:
    def __init__(self, height, width=1280):
        self.height, self.width, self.calls = height, width, []

    def execute_cdp_cmd(self, command, params):
        self.calls.append((command, params))
        if command == "Page.getLayoutMetrics":
            return {"cssContentSize": {"width": self.width, "height": self.height}}
        return {"data": "FULLSHOT"}

    def get_screenshot_as_base64(self):
        return "VIEWPORTSHOT"


def test_a_viewport_screenshot_needs_no_layout_query():
    from app.js_fetcher import capture_screenshot

    driver, warnings = LayoutDriver(500), []
    assert capture_screenshot(driver, False, warnings) == "VIEWPORTSHOT"
    assert driver.calls == [] and warnings == []


def test_a_full_page_screenshot_captures_the_whole_content():
    from app.js_fetcher import capture_screenshot

    driver, warnings = LayoutDriver(3000), []
    assert capture_screenshot(driver, True, warnings) == "FULLSHOT"
    command, params = driver.calls[-1]
    assert command == "Page.captureScreenshot" and params["captureBeyondViewport"] is True
    assert params["clip"] == {"x": 0, "y": 0, "width": 1280, "height": 3000, "scale": 1} and warnings == []


def test_a_very_long_page_is_clipped_and_the_response_says_so():
    from app.js_fetcher import SCREENSHOT_MAX_HEIGHT, capture_screenshot

    driver, warnings = LayoutDriver(SCREENSHOT_MAX_HEIGHT + 1000), []
    capture_screenshot(driver, True, warnings)
    assert driver.calls[-1][1]["clip"]["height"] == SCREENSHOT_MAX_HEIGHT
    assert any("clipped" in warning for warning in warnings)


def test_a_very_wide_page_is_clipped_too():
    """The layout size is the page's to declare; both dimensions are bounded."""
    from app.js_fetcher import SCREENSHOT_MAX_WIDTH, capture_screenshot

    driver, warnings = LayoutDriver(500, width=SCREENSHOT_MAX_WIDTH + 100_000), []
    capture_screenshot(driver, True, warnings)
    clip = driver.calls[-1][1]["clip"]
    assert clip["width"] == SCREENSHOT_MAX_WIDTH and clip["height"] == 500
    assert any("clipped" in warning for warning in warnings)
