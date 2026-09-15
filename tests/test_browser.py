import json

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
            return {"frameTree": {"frame": {"id": "main"}}}

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
