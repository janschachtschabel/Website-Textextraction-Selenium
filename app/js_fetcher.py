"""Selenium fetch adapter. One return type, no bot-wall bypass or fake status 200."""

import asyncio
import re

from selenium.common.exceptions import WebDriverException

from .browser_readiness import driver_navigation_error, navigation_error, navigation_status, wait_for_content
from .config import settings
from .deadline import Deadline
from .egress_proxy import EgressProxy
from .results import CrawlError, FetchResult
from .selenium_driver import create_driver

CAPTURE_MATH = """
if (window.MathJax && MathJax.startup && MathJax.startup.document) {
  for (const item of MathJax.startup.document.math || []) {
    if (item.typesetRoot && item.math) item.typesetRoot.setAttribute('data-latex', item.math);
  }
}
"""


# A rejected certificate or a policy block answers the same way on every attempt.
_PERMANENT = re.compile(r"net::ERR_(CERT_[A-Z0-9_]+|BLOCKED_BY_CLIENT)")


def permanent_failure(message: str) -> bool:
    return bool(_PERMANENT.search(message))


SCREENSHOT_MAX_HEIGHT = 20000  # pixels; one page must not fill a response


def capture_screenshot(driver, full_page: bool, warnings: list) -> str:
    """The viewport, or the whole document when asked - bounded, and the response says when it is cut."""
    if not full_page:
        return driver.get_screenshot_as_base64()
    content = driver.execute_cdp_cmd("Page.getLayoutMetrics", {})["cssContentSize"]
    height = min(content["height"], SCREENSHOT_MAX_HEIGHT)
    if content["height"] > height:
        warnings.append(f"Full-page screenshot clipped at {SCREENSHOT_MAX_HEIGHT} pixels")
    return driver.execute_cdp_cmd(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": content["width"], "height": height, "scale": 1},
        },
    )["data"]


def navigation_failed(code):
    return CrawlError(f"Selenium navigation failed ({code})" if code else "Selenium navigation failed", 502)


def web_url(value):
    """False for Chrome's own pages: its blank start page (kept by a download) and error pages."""
    return value.startswith(("http:", "https:"))


def main_frame(driver, events):
    frame = driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]["frame"]
    if frame["url"].startswith("chrome-error:"):
        error = navigation_error(events, frame["id"])
        # Chrome also shows its page for an error status without a body; the server did respond.
        if error != "net::ERR_HTTP_RESPONSE_CODE_FAILURE":
            raise navigation_failed(error)
    return frame


def selenium_fetch(url, options, proxy_url, expires_at):
    deadline = Deadline.at(expires_at)
    driver = None
    try:
        driver = create_driver(options, proxy_url)
        driver.set_page_load_timeout(deadline.remaining())
        driver.set_script_timeout(min(10, deadline.remaining()))
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["file://*", "ftp://*"]})
        driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "deny"})
        if options.js_strategy == "speed" and not options.screenshot:
            driver.execute_cdp_cmd(
                "Network.setBlockedURLs",
                {
                    "urls": [
                        "file://*",
                        "ftp://*",
                        "*.png",
                        "*.jpg",
                        "*.jpeg",
                        "*.gif",
                        "*.webp",
                        "*.woff",
                        "*.woff2",
                        "*.mp4",
                        "*.mp3",
                    ]
                },
            )
        driver.get(url)
        events = driver.get_log("performance")
        frame = main_frame(driver, events)
        status, mime = navigation_status(events, frame["id"])
        settled = True
        if web_url(frame["url"]) and (status is None or status < 400):
            settled = wait_for_content(driver, options, deadline)
        events.extend(driver.get_log("performance"))
        frame = main_frame(driver, events)
        status, mime = navigation_status(events, frame["id"])
        warnings = []
        page = web_url(frame["url"])
        if page:
            driver.execute_script(CAPTURE_MATH)
            snapshot = driver.execute_script(
                "const html = document.documentElement.outerHTML; "
                "return {html: html.slice(0, arguments[0]), truncated: html.length > arguments[0]};",
                options.max_bytes,
            )
            data = snapshot["html"].encode("utf-8")
            truncated = snapshot["truncated"] or len(data) > options.max_bytes
        else:
            data, truncated = b"", False  # Chrome's own page is never content
            if status is None or status < 400:
                warnings.append("Browser displayed no page (for example a download); use mode=auto or fast for files")
        if status is None:
            warnings.append("Browser could not observe the main document HTTP status")
        if not settled:
            warnings.append("Page content did not settle within the auto-wait limit; returning the current state")
        if truncated:
            warnings.append("Rendered HTML truncated at max_bytes")
        screenshot = (
            capture_screenshot(driver, options.screenshot_full_page, warnings)
            if page and options.screenshot and not options.anonymize
            else None
        )
        final_url = driver.current_url
        return FetchResult(
            data[: options.max_bytes],
            final_url if web_url(final_url) else url,
            status,
            "text/html; charset=utf-8" if mime in {None, "text/html", "application/xhtml+xml"} else mime,
            "selenium",
            truncated,
            screenshot,
            warnings,
            settled,
        )
    except WebDriverException as exc:
        # ChromeDriver raises for other failures, e.g. "unknown error: net::ERR_TUNNEL_CONNECTION_FAILED".
        raise navigation_failed(driver_navigation_error(exc.msg)) from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except WebDriverException as exc:
                # Propagate cleanup failure so the parent terminates the worker's entire process group.
                raise CrawlError("Selenium cleanup failed", 502) from exc


class BrowserFetcher:
    def __init__(self, pool, rate_limiter, config=settings):
        self.pool, self.rate_limiter, self.config = pool, rate_limiter, config

    async def fetch(self, url, options, deadline):
        # A per-job guard prevents a caller's upstream proxy or policy counters leaking to another job.
        async with EgressProxy(protection=self.config.ssrf_protection, upstream=options.proxy) as guard:
            for attempt in range(options.retries + 1):
                await self.rate_limiter.acquire(url, options.crawl_rate_limit_rps, deadline)
                try:
                    result = await self.pool.run(
                        selenium_fetch, (url, options, guard.url, deadline.expires_at), deadline
                    )
                    if guard.blocked:
                        result.warnings.append(f"Network policy blocked {guard.blocked} browser connection(s)")
                    return result
                except CrawlError as exc:
                    if exc.status_code != 502 or attempt == options.retries or permanent_failure(str(exc)):
                        raise
                    await deadline.run(asyncio.sleep(min(2**attempt, 8)))
        raise AssertionError("Unreachable browser retry state")
