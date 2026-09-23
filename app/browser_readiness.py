"""Bounded content readiness and main-frame network status observation."""

import json
import re
import time

from selenium.common.exceptions import InvalidSelectorException
from selenium.webdriver.common.by import By

from .deadline import Deadline
from .preflight import challenge_text
from .results import CrawlError

# Pages can keep a spinner forever or render no text at all (e.g. a denied download). Auto-wait is
# best effort: it stops this long after the explicit waits are met instead of waiting for the deadline.
AUTO_WAIT_LIMIT_SECONDS = {"speed": 10.0, "accuracy": 20.0}
# A bot challenge such as Cloudflare's reloads into the page once it lets the browser through,
# within seconds (3.9 s on leifiphysik.de); one that has not by this limit will not.
CHALLENGE_LIMIT_SECONDS = 10.0

_NET_ERROR = re.compile(r"net::ERR_[A-Z0-9_]+")

SNAPSHOT = """
// Pages may contain an empty <main> before the real one: use the first candidate with text. If all
// candidates are still empty, the first one is awaited; only pages without candidates use the body.
let text = null;
for (const el of document.querySelectorAll('main, article, [role=main]')) {
  const candidate = el.innerText || '';
  if (candidate.trim()) { text = candidate; break; }
  if (text === null) text = candidate;
}
if (text === null) text = (document.body && document.body.innerText) || '';
const rendered = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
// A busy region counts while rendered, even while empty. Only indeterminate progressbars are loading
// indicators: a value (aria-valuenow) marks static progress such as skill bars, and aria-hidden or a
// missing size marks a hidden loader.
const spinning = el => {
  const box = el.getBoundingClientRect();
  return rendered(el) && !el.matches('[aria-hidden=true], [aria-valuenow]') && box.width > 0 && box.height > 0;
};
const busy = [...document.querySelectorAll('[aria-busy=true]')].some(rendered)
  || [...document.querySelectorAll('[role=progressbar]')].some(spinning);
if (window.MathJax && MathJax.startup && MathJax.startup.promise && !window.__extractMathAttached) {
  window.__extractMathAttached = true;
  Promise.resolve(MathJax.startup.promise).then(() => window.__extractMathReady = true,
                                             () => window.__extractMathReady = true);
}
return {text: text, busy: busy, ready: document.readyState !== 'loading',
        math: !window.__extractMathAttached || !!window.__extractMathReady};
"""


def _message(entry):
    message = json.loads(entry["message"])["message"]
    return message["method"], message.get("params", {})


def navigation_status(entries, frame_id):
    status, mime = None, None
    for entry in entries:
        try:
            method, params = _message(entry)
            if (
                method == "Network.responseReceived"
                and params.get("type") == "Document"
                and params.get("frameId") == frame_id
            ):
                response = params["response"]
                status, mime = int(response["status"]), response.get("mimeType")
        except (ValueError, KeyError, TypeError):
            continue  # Chrome also emits unrelated/non-network log records.
    return status, mime


def navigation_error(entries, frame_id):
    """Chrome's network error code for the main document, e.g. net::ERR_CERT_DATE_INVALID."""
    documents, error = set(), None
    for entry in entries:
        try:
            method, params = _message(entry)
            if (
                method == "Network.requestWillBeSent"
                and params.get("type") == "Document"
                and params.get("frameId") == frame_id
            ):
                documents.add(params["requestId"])
            elif method == "Network.loadingFailed" and params.get("requestId") in documents:
                error = params.get("errorText")
        except (ValueError, KeyError, TypeError):
            continue
    return error if isinstance(error, str) and _NET_ERROR.fullmatch(error) else None


def driver_navigation_error(message):
    """Code from ChromeDriver's "unknown error: net::ERR_..." navigation message.

    Other WebDriver messages can quote page-controlled text (script errors, alerts); they never match.
    """
    prefix, _, code = (message or "").partition("\n")[0].strip().partition("unknown error: ")
    return code if not prefix and _NET_ERROR.fullmatch(code) else None


def wait_for_challenge(driver, options, deadline: Deadline) -> None:
    """Let a bot challenge the page shows finish, as any browser does, instead of reading it.

    Whether the site then lets the browser in stays its decision; the wait ends once the title
    no longer names a challenge or after CHALLENGE_LIMIT_SECONDS.
    """
    if not options.js_auto_wait:
        return
    limit = time.monotonic() + CHALLENGE_LIMIT_SECONDS
    while challenge_text(driver.title) and time.monotonic() < limit:
        time.sleep(min(0.1, deadline.remaining()))


def wait_for_content(driver, options, deadline: Deadline) -> bool:
    """Wait for selectors, the minimum wait and (optionally) settled content.

    Returns False when auto-wait gave up on content that never settled. Its limit starts once the
    selectors and the minimum wait are satisfied.
    """
    if not (options.js_auto_wait or options.wait_for_selectors or options.wait_for_ms):
        return True
    started = time.monotonic()
    changed = started
    previous = None
    explicit_since = None
    stable_for = 0.3 if options.js_strategy == "speed" else 1.0
    auto_wait_limit = AUTO_WAIT_LIMIT_SECONDS[options.js_strategy]
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
        if not (selected and minimum):
            explicit_since = None
        else:
            if explicit_since is None:
                explicit_since = now
            if content_ready or not options.js_auto_wait:
                return True
            if now - explicit_since >= auto_wait_limit:
                return False
        time.sleep(min(0.1, remaining))
