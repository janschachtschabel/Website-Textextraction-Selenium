"""Bounded content readiness and main-frame network status observation."""

import json
import time

from selenium.common.exceptions import InvalidSelectorException
from selenium.webdriver.common.by import By

from .deadline import Deadline
from .results import CrawlError

SNAPSHOT = """
const root = document.querySelector('main, article, [role=main]') || document.body;
const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
const busy = [...document.querySelectorAll('[aria-busy=true], [role=progressbar]')].some(visible);
const text = root ? root.innerText : '';
if (window.MathJax && MathJax.startup && MathJax.startup.promise && !window.__extractMathAttached) {
  window.__extractMathAttached = true;
  Promise.resolve(MathJax.startup.promise).then(() => window.__extractMathReady = true,
                                             () => window.__extractMathReady = true);
}
return {text: text, busy: busy, ready: document.readyState !== 'loading',
        math: !window.__extractMathAttached || !!window.__extractMathReady};
"""


def navigation_status(entries, frame_id):
    status, mime = None, None
    for entry in entries:
        try:
            message = json.loads(entry["message"])["message"]
            params = message.get("params", {})
            if (
                message["method"] == "Network.responseReceived"
                and params.get("type") == "Document"
                and params.get("frameId") == frame_id
            ):
                response = params["response"]
                status, mime = int(response["status"]), response.get("mimeType")
        except (ValueError, KeyError, TypeError):
            continue  # Chrome also emits unrelated/non-network log records.
    return status, mime


def wait_for_content(driver, options, deadline: Deadline):
    if not (options.js_auto_wait or options.wait_for_selectors or options.wait_for_ms):
        return
    started = time.monotonic()
    changed = started
    previous = None
    stable_for = 0.3 if options.js_strategy == "speed" else 1.0
    while True:
        remaining = deadline.remaining()
        snapshot = driver.execute_script(SNAPSHOT)
        try:
            selected = all(
                any(el.is_displayed() for el in driver.find_elements(By.CSS_SELECTOR, selector))
                for selector in options.wait_for_selectors
            )
        except InvalidSelectorException as exc:
            raise CrawlError("Invalid content selector", 422) from exc
        text = snapshot.get("text", "")
        signature = (len(text), text[:512], text[-512:])
        now = time.monotonic()
        if signature != previous:
            previous, changed = signature, now
        minimum = now - started >= options.wait_for_ms / 1000
        content_ready = (
            bool(text.strip())
            and snapshot["ready"]
            and not snapshot["busy"]
            and snapshot["math"]
            and now - changed >= stable_for
        )
        if selected and minimum and (not options.js_auto_wait or content_ready):
            return
        time.sleep(min(0.1, remaining))
