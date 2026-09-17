"""Selenium fetch adapter. One return type, no bot-wall bypass or fake status 200."""

import asyncio

from selenium.common.exceptions import WebDriverException

from .browser_readiness import navigation_error, navigation_status, net_error_code, wait_for_content
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


def navigation_failed(code):
    return CrawlError(f"Selenium navigation failed ({code})" if code else "Selenium navigation failed", 502)


def main_frame(driver, events):
    frame = driver.execute_cdp_cmd("Page.getFrameTree", {})["frameTree"]["frame"]
    if frame["url"].startswith("chrome-error:"):
        # Chrome shows its own page for certificate errors; that page is never content.
        raise navigation_failed(navigation_error(events, frame["id"]))
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
        # A download or an empty response leaves Chrome's initial blank page in place.
        displayed = frame["url"].startswith(("http:", "https:"))
        settled = True
        if displayed and (status is None or status < 400):
            settled = wait_for_content(driver, options, deadline)
        events.extend(driver.get_log("performance"))
        status, mime = navigation_status(events, main_frame(driver, events)["id"])
        driver.execute_script(CAPTURE_MATH)
        snapshot = driver.execute_script(
            "const html = document.documentElement.outerHTML; "
            "return {html: html.slice(0, arguments[0]), truncated: html.length > arguments[0]};",
            options.max_bytes,
        )
        data = snapshot["html"].encode("utf-8")
        warnings = []
        if not displayed:
            warnings.append("Browser displayed no page (for example a download); use mode=auto or fast for files")
        if status is None:
            warnings.append("Browser could not observe the main document HTTP status")
        if not settled:
            warnings.append("Page content did not settle within the auto-wait limit; returning the current state")
        truncated = snapshot["truncated"] or len(data) > options.max_bytes
        if truncated:
            warnings.append("Rendered HTML truncated at max_bytes")
        screenshot = driver.get_screenshot_as_base64() if options.screenshot and not options.anonymize else None
        return FetchResult(
            data[: options.max_bytes],
            driver.current_url if displayed else url,
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
        raise navigation_failed(net_error_code(exc.msg)) from exc
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
                    if exc.status_code != 502 or attempt == options.retries:
                        raise
                    await deadline.run(asyncio.sleep(min(2**attempt, 8)))
        raise AssertionError("Unreachable browser retry state")
