import json
import time

import pytest

from app import browser_readiness
from app.browser_readiness import navigation_status
from app.config import settings
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
    options = resolve_options(CrawlRequest(url="https://example.com/missing", mode="js"))
    result = js_fetcher.selenium_fetch(
        "https://example.com/missing", options, "http://127.0.0.1:1234", Deadline(5).expires_at
    )
    assert (result.status_code, result.data, result.warnings) == (404, b"", [])
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
