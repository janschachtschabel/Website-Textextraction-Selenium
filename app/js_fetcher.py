from __future__ import annotations

import os
import queue
import threading
import time

from loguru import logger
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from starlette.concurrency import run_in_threadpool
from webdriver_manager.chrome import ChromeDriverManager

# Global driver pool - configurable via environment
from .config import settings
from .utils import detect_error_page, pick_user_agent

# Cache the ChromeDriver binary path after the first install to avoid repeated
# network/disk lookups on every driver creation.
_chromedriver_path: str | None = None
_chromedriver_lock = threading.Lock()

# Dynamic pool management
_driver_pools: dict[str, queue.Queue] = {
    'normal': queue.Queue(),
    'eager': queue.Queue(),
}
_pool_sizes: dict[str, int] = {
    'normal': settings.selenium_pool_size,
    'eager': settings.selenium_pool_size,
}
_pool_usage: dict[str, int] = {'normal': 0, 'eager': 0}  # Track active drivers
_pool_initialized: dict[str, bool] = {'normal': False, 'eager': False}
_pool_lock = threading.Lock()
_scaling_lock = threading.Lock()


def _create_driver(
    proxy: str | None = None,
    user_agent: str | None = None,
    page_load_strategy: str = 'normal',
    allow_insecure_ssl: bool = False,
) -> webdriver.Chrome:
    """Create a new Chrome driver with enhanced stability and anti-detection options.

    page_load_strategy: 'normal' | 'eager'
    """
    options = Options()
    _chrome_bin = os.environ.get("CHROME_BINARY")
    if _chrome_bin:
        options.binary_location = _chrome_bin
    options.add_argument("--headless=new")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    # Enhanced stability flags for problematic sites
    options.add_argument("--disable-logging")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees,VizDisplayCompositor")
    options.add_argument("--disable-ipc-flooding-protection")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-background-networking")
    options.add_argument("--remote-debugging-port=0")  # Disable DevTools
    
    # Additional anti-detection and stability flags
    options.add_argument("--disable-web-security")
    options.add_argument("--disable-hang-monitor")
    options.add_argument("--disable-prompt-on-repost")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--disable-component-extensions-with-background-pages")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-translate")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-permissions-api")
    
    # Memory and performance optimizations
    options.add_argument("--memory-pressure-off")
    options.add_argument("--max_old_space_size=4096")
    options.add_argument("--aggressive-cache-discard")
    
    # Enhanced user agent handling
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    else:
        # Default realistic user agent
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # SSL certificate verification
    if allow_insecure_ssl or (settings.allow_insecure_ssl):
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--ignore-ssl-errors")
        options.add_argument("--ignore-certificate-errors-spki-list")

    # Enhanced proxy handling
    if proxy and proxy.strip() and proxy.strip().lower() != "string":
        options.add_argument(f"--proxy-server={proxy}")
        options.add_argument("--ignore-ssl-errors-on-proxy")
        if not (allow_insecure_ssl or settings.allow_insecure_ssl):
            # Only add these when not already set globally above
            options.add_argument("--ignore-certificate-errors-spki-list")
            options.add_argument("--ignore-certificate-errors")
    
    # Enhanced stealth settings
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins-discovery")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-images")  # Faster loading
    
    # Window size for consistent rendering
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--start-maximized")
    
    # Page load strategy
    try:
        if page_load_strategy in ('eager', 'normal'):
            options.page_load_strategy = page_load_strategy
    except Exception:
        pass

    global _chromedriver_path
    if _chromedriver_path is None:
        with _chromedriver_lock:
            if _chromedriver_path is None:
                _chromedriver_path = ChromeDriverManager().install()
    service = Service(_chromedriver_path)
    driver = webdriver.Chrome(service=service, options=options)
    # Mark driver with its strategy for returning to the right pool
    try:
        driver._strategy_key = 'eager' if page_load_strategy == 'eager' else 'normal'
    except Exception:
        pass
    
    # Enhanced anti-detection script – injected via CDP so it runs on EVERY page load,
    # not just the blank page at driver creation time.
    stealth_script = """
    (() => {
      try {
        // navigator.webdriver
        try {
          const desc = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')
                     || Object.getOwnPropertyDescriptor((navigator || {}).__proto__ || {}, 'webdriver');
          if ((desc && desc.configurable) || !('webdriver' in navigator)) {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          }
        } catch (e) {}
        
        // plugins
        try {
          if (!navigator.plugins || navigator.plugins.length === 0) {
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
          }
        } catch (e) {}
        
        // languages
        try {
          if (!navigator.languages || navigator.languages.length === 0) {
            Object.defineProperty(navigator, 'languages', { get: () => ['de-DE','de','en-US','en'] });
          }
        } catch (e) {}
        
        // permissions
        try {
          const originalQuery = navigator.permissions && navigator.permissions.query;
          if (originalQuery) {
            navigator.permissions.query = (parameters) => (
              parameters && parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
            );
          }
        } catch (e) {}
        
        // chrome runtime - do not redefine if present/non-configurable
        try {
          const hasChrome = 'chrome' in window;
          if (!hasChrome) {
            Object.defineProperty(window, 'chrome', {
              value: { runtime: {} },
              configurable: true
            });
          } else if (window.chrome && typeof window.chrome === 'object') {
            window.chrome.runtime = window.chrome.runtime || {};
          }
        } catch (e) {}
      } catch (e) {}
    })();
    """
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": stealth_script})
    except Exception:
        # Fallback: run once on current page if CDP injection fails
        try:
            driver.execute_script(stealth_script)
        except Exception:
            pass
    
    return driver


def _initialize_pool(strategy_key: str):
    """Initialize the driver pool for the given strategy on first use.

    The lock is only held long enough to check/set the initialized flag.
    Actual driver creation happens outside the lock to avoid blocking other
    threads for N * 30 s (one driver-creation per slot at startup).
    """
    with _pool_lock:
        if _pool_initialized.get(strategy_key, False):
            return
        # Mark as initialized before releasing the lock so concurrent callers
        # don't also try to create the initial drivers.
        _pool_initialized[strategy_key] = True
        initial_size = _pool_sizes[strategy_key]

    page_load_strategy = 'eager' if strategy_key == 'eager' else 'normal'
    for _ in range(initial_size):
        try:
            driver = _create_driver(page_load_strategy=page_load_strategy)
            _driver_pools[strategy_key].put(driver)
        except Exception as e:
            logger.warning(f"Failed to create initial driver for {strategy_key} pool: {e}")


def _pick_strategy_key(js_strategy: str) -> str:
    return 'eager' if js_strategy == 'speed' else 'normal'


def _get_driver(js_strategy: str, timeout_seconds: int = 30) -> webdriver.Chrome:
    """Get a driver from the appropriate pool with dynamic scaling."""
    key = _pick_strategy_key(js_strategy)
    if not _pool_initialized.get(key, False):
        _initialize_pool(key)
    
    # Check if we should scale up the pool
    _maybe_scale_pool(key)
    
    # Track usage for scaling decisions
    with _scaling_lock:
        _pool_usage[key] += 1

    # Use timeout to prevent indefinite blocking if pool is exhausted
    try:
        driver = _driver_pools[key].get(timeout=timeout_seconds)
        return driver
    except queue.Empty:
        # Try scaling up one more time if we hit capacity
        if _try_emergency_scale(key):
            try:
                driver = _driver_pools[key].get(timeout=5)  # Short retry
                return driver
            except queue.Empty:
                pass
        # Decrement usage counter since we failed to acquire a driver
        with _scaling_lock:
            _pool_usage[key] = max(0, _pool_usage[key] - 1)
        raise TimeoutException(f"No available drivers in {key} pool after {timeout_seconds}s. Pool exhausted at size {_pool_sizes[key]}.") from None


def _return_driver(driver: webdriver.Chrome):
    """Return a driver to the appropriate pool with health check and usage tracking."""
    key = getattr(driver, "_strategy_key", 'normal')
    
    try:
        # Track usage decrease
        with _scaling_lock:
            _pool_usage[key] = max(0, _pool_usage[key] - 1)
        
        # Basic health check - if driver is broken, don't return it
        try:
            _ = driver.current_url  # Simple check to see if driver is responsive
        except Exception:
            # Driver is broken, create a new one to replace it
            try:
                driver.quit()
            except Exception:
                pass
            # Create replacement driver
            page_load_strategy = 'eager' if key == 'eager' else 'normal'
            replacement = _create_driver(page_load_strategy=page_load_strategy)
            _driver_pools[key].put(replacement)
            return
        
        # Driver is healthy, return to pool
        _driver_pools[key].put(driver)
        
        # Check if we should scale down (after returning driver)
        _maybe_scale_down(key)
        
    except Exception:
        # If anything fails, try to put in normal pool as fallback
        try:
            _driver_pools['normal'].put(driver)
        except Exception:
            # If even that fails, the driver is lost - create a replacement
            try:
                replacement = _create_driver(page_load_strategy='normal')
                _driver_pools['normal'].put(replacement)
            except Exception:
                pass  # Give up gracefully


def _maybe_scale_pool(key: str):
    """Check if pool should be scaled up based on usage."""
    should_scale = False
    page_load_strategy = 'eager' if key == 'eager' else 'normal'
    with _scaling_lock:
        current_size = _pool_sizes[key]
        current_usage = _pool_usage[key]
        available = _driver_pools[key].qsize()
        usage_ratio = current_usage / max(current_size, 1)
        if (usage_ratio >= settings.selenium_scale_threshold and
                available <= 1 and
                current_size < settings.selenium_max_pool_size):
            # Reserve the slot before releasing the lock so two threads don't
            # both decide to create a driver at the same time.
            _pool_sizes[key] += 1
            should_scale = True

    if should_scale:
        try:
            new_driver = _create_driver(page_load_strategy=page_load_strategy)
            _driver_pools[key].put(new_driver)
            logger.info(f"Scaled up {key} pool to {_pool_sizes[key]} drivers (usage: {current_usage})")
        except Exception as e:
            # Roll back the reserved slot on failure
            with _scaling_lock:
                _pool_sizes[key] = max(settings.selenium_pool_size, _pool_sizes[key] - 1)
            logger.warning(f"Failed to scale up {key} pool: {e}")


def _try_emergency_scale(key: str) -> bool:
    """Emergency scaling when pool is completely exhausted."""
    page_load_strategy = 'eager' if key == 'eager' else 'normal'
    with _scaling_lock:
        if _pool_sizes[key] >= settings.selenium_max_pool_size:
            return False
        _pool_sizes[key] += 1  # Reserve slot before releasing lock
    try:
        new_driver = _create_driver(page_load_strategy=page_load_strategy)
        _driver_pools[key].put(new_driver)
        logger.info(f"Emergency scaled {key} pool to {_pool_sizes[key]} drivers")
        return True
    except Exception as e:
        with _scaling_lock:
            _pool_sizes[key] = max(settings.selenium_pool_size, _pool_sizes[key] - 1)
        logger.warning(f"Emergency scaling failed for {key} pool: {e}")
        return False


def _maybe_scale_down(key: str):
    """Check if pool should be scaled down when usage is low."""
    idle_driver = None
    with _scaling_lock:
        current_size = _pool_sizes[key]
        current_usage = _pool_usage[key]
        available = _driver_pools[key].qsize()
        min_size = settings.selenium_pool_size
        if (current_size > min_size and
                available > current_size * 0.7 and
                current_usage < current_size * 0.3):
            try:
                idle_driver = _driver_pools[key].get_nowait()
                _pool_sizes[key] -= 1
            except queue.Empty:
                pass

    if idle_driver is not None:
        try:
            idle_driver.quit()
            logger.info(f"Scaled down {key} pool to {_pool_sizes[key]} drivers")
        except Exception:
            pass


def get_pool_stats() -> dict:
    """Get current pool statistics for monitoring."""
    with _scaling_lock:
        return {
            'normal': {
                'size': _pool_sizes['normal'],
                'usage': _pool_usage['normal'],
                'available': _driver_pools['normal'].qsize(),
            },
            'eager': {
                'size': _pool_sizes['eager'], 
                'usage': _pool_usage['eager'],
                'available': _driver_pools['eager'].qsize(),
            }
        }


def _try_click_cookie_banners(driver: webdriver.Chrome):
    """Try to click common cookie acceptance buttons and handle overlays."""
    selectors = [
        "button:contains('Accept')",
        "button:contains('Akzeptieren')",
        "button:contains('Alle akzeptieren')",
        "button:contains('Agree')",
        "button:contains('Zustimmen')",
        "button:contains('OK')",
        "#onetrust-accept-btn-handler",
        "button[aria-label*='Accept']",
        "button[id*='accept']",
        "button[class*='accept']",
        "button[class*='cookie']",
        "[data-testid*='accept']",
        ".cookie-accept",
        ".accept-cookies",
        "#cookieAccept",
        "#accept-cookies",
    ]
    
    for selector in selectors:
        try:
            # Use CSS selector for most, XPath for text content
            if ":contains(" in selector:
                text = selector.split(":contains('")[1].split("')")[0]
                element = driver.find_element(By.XPATH, f"//button[contains(text(), '{text}')]")
            else:
                element = driver.find_element(By.CSS_SELECTOR, selector)
            
            if element.is_displayed():
                # Scroll element into view
                driver.execute_script("arguments[0].scrollIntoView(true);", element)
                time.sleep(0.2)
                
                # Try regular click first
                try:
                    element.click()
                except Exception:
                    # Fallback to JavaScript click
                    driver.execute_script("arguments[0].click();", element)
                
                time.sleep(1)  # Wait for banner to disappear
                break
        except Exception:
            continue


def _try_click_cookie_banners_fast(driver: webdriver.Chrome, max_seconds: float = 1.5) -> bool:
    """Fast cookie banner scan using find_elements with implicit wait disabled.

    Scans a compact set of robust selectors and exits on first successful click.
    """
    try:
        # Temporarily disable implicit waits
        prev = driver.timeouts.implicit_wait
    except Exception:
        prev = 0
    try:
        driver.implicitly_wait(0)
        # Quick pre-check: is there any consent/cookie overlay at all?
        try:
            has_overlay = driver.execute_script(
                """
                return !!document.querySelector(
                  "[id*='consent'],[class*='consent'],[id*='cookie'],[class*='cookie']"
                );
                """
            )
            if not has_overlay:
                return False
        except Exception:
            pass

        selectors = [
            "#onetrust-accept-btn-handler",
            "button[aria-label*='Accept']",
            "button[aria-label*='accept']",
            "button[aria-label*='Zustimmen']",
            "button[id*='accept']",
            "button[class*='accept']",
            "[data-testid*='accept']",
            ".cookie-accept",
            ".accept-cookies",
            "#accept-cookies",
        ]
        deadline = time.time() + max_seconds
        while time.time() < deadline:
            for sel in selectors:
                try:
                    elems = driver.find_elements(By.CSS_SELECTOR, sel)
                except Exception:
                    elems = []
                if elems:
                    el = elems[0]
                    try:
                        driver.execute_script("arguments[0].scrollIntoView(true);", el)
                        time.sleep(0.05)
                        try:
                            el.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.2)
                        return True
                    except Exception:
                        continue
            time.sleep(0.12)
        return False
    finally:
        try:
            driver.implicitly_wait(prev)
        except Exception:
            pass


def _any_loader_visible(driver: webdriver.Chrome) -> bool:
    """Quick check for common loading indicators being visible."""
    selectors = [
        '.loading', '.spinner', '.loader', '[data-loading]',
        '.loading-spinner', '.loading-overlay', '.preloader',
        '[aria-label*="loading"]', '[aria-label*="Loading"]'
    ]
    try:
        for sel in selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _has_overlay_or_body_lock(driver: webdriver.Chrome) -> bool:
    """Detects common consent/modal overlays or body scroll lock.

    Uses a combination of CSS selectors and viewport coverage heuristics.
    """
    try:
        return driver.execute_script(
            """
            try {
              const doc = document;
              const body = doc.body;
              const de = doc.documentElement;
              const gv = (el, prop) => el ? getComputedStyle(el)[prop] : '';
              const overflowLocked = ['hidden','clip'].includes(gv(body,'overflow')) || ['hidden','clip'].includes(gv(de,'overflow'));

              // Common CMP/overlay selectors
              const sel = "[id*='consent'],[class*='consent'],[id*='cookie'],[class*='cookie'],[class*='overlay'],[class*='backdrop'],[class*='modal'],[aria-modal='true'],.qc-cmp-ui,.qc-cmp2-container,.qc-cmp3-container,.sp_veil,#sp_message_container_*,#sp_message_iframe_*,.ReactModal__Overlay,.cc-window,.osano-cm-dialog";
              const overlay = doc.querySelector(sel);
              if (overlay && (overlay.offsetWidth * overlay.offsetHeight) > 0) return true;

              // Heuristic: any fixed element covering >50% viewport
              const vw = window.innerWidth || 0; const vh = window.innerHeight || 0;
              const area = vw * vh;
              if (area > 0) {
                const nodes = Array.from(doc.querySelectorAll('*'));
                for (const el of nodes) {
                  try {
                    const cs = getComputedStyle(el);
                    if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
                    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
                    const r = el.getBoundingClientRect();
                    const cover = Math.max(0, Math.min(vw, r.right) - Math.max(0, r.left)) * Math.max(0, Math.min(vh, r.bottom) - Math.max(0, r.top));
                    if (cover / area > 0.5) return true;
                  } catch(e) { /* ignore */ }
                }
              }
              return !!overflowLocked;
            } catch (e) { return false; }
            """
        )
    except Exception:
        return False



def _wait_for_mathjax(driver: webdriver.Chrome, timeout_ms: int = 5000) -> bool:
    """If MathJax is present, wait for typesetPromise to complete to ensure formulas render."""
    try:
        return driver.execute_async_script(
            """
            const done = arguments[0];
            try {
              if (window.MathJax && MathJax.typesetPromise) {
                MathJax.typesetPromise().then(() => done(true)).catch(() => done(false));
              } else {
                done(true);
              }
            } catch (e) { done(true); }
            """
        )
    except Exception:
        return False


def _attempt_with_temp_driver(
    url: str,
    timeout_seconds: int,
    proxy: str | None,
    max_bytes: int,
    js_strategy: str = "accuracy",
    budget_left: float | None = None,
    allow_insecure_ssl: bool | None = None,
) -> tuple[int, str, bytes, str | None] | None:
    """One-shot retry using a fresh driver with a rotated user agent.

    Lightweight and strictly budget-bounded to avoid long stalls.
    """
    ua = pick_user_agent(settings.default_user_agent)
    eff_ssl = allow_insecure_ssl if allow_insecure_ssl is not None else settings.allow_insecure_ssl
    temp = _create_driver(proxy=proxy, user_agent=ua, allow_insecure_ssl=eff_ssl)
    try:
        # Bound by remaining budget if provided
        effective_to = min(timeout_seconds, int(max(1.0, budget_left))) if budget_left else timeout_seconds
        temp.set_page_load_timeout(effective_to)
        if js_strategy == "accuracy":
            temp.implicitly_wait(5)
        else:
            temp.implicitly_wait(0)

        temp.get(url)

        # Strategy-aware basic readiness
        try:
            if js_strategy == "accuracy":
                WebDriverWait(temp, min(10, effective_to)).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            else:
                WebDriverWait(temp, min(6, effective_to)).until(
                    lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
                )
        except Exception:
            pass

        # Fast cookie scan for non-accuracy
        try:
            if js_strategy == "accuracy":
                _try_click_cookie_banners(temp)
            else:
                _try_click_cookie_banners_fast(temp, max(0.2, min(0.4, (budget_left or 1.0))))
        except Exception:
            pass

        # MathJax: only if present and with a small cap for non-accuracy
        try:
            has_mj = temp.execute_script("return !!(window.MathJax && MathJax.typesetPromise);")
        except Exception:
            has_mj = False
        if has_mj:
            if js_strategy == "accuracy":
                _wait_for_mathjax(temp)
            else:
                _wait_for_mathjax(temp, timeout_ms=700)

        # Minimal settle
        time.sleep(0.2 if js_strategy != "accuracy" else 0.8)

        content = temp.page_source
        final_url = temp.current_url
        content_bytes = content.encode("utf-8")[:max_bytes]
        return 200, final_url, content_bytes, "text/html; charset=utf-8"
    except Exception:
        return None
    finally:
        try:
            temp.quit()
        except Exception:
            pass


def _detect_error_pages(content: str) -> bool:
    """Detect if content indicates an error page. Delegates to shared utils implementation."""
    return detect_error_page(content, status_code=None)


class TimeBudget:
    """Global time budget helper to cap total JS runtime.

    Based on a deadline (now + seconds). Use .left() to get remaining seconds and
    .slice(desired, floor) to obtain a safe timeout bounded by the budget.
    """
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + max(0.0, float(seconds))

    def left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def ok(self) -> bool:
        return self.left() > 0.0

    def slice(self, desired: float, floor: float = 0.3) -> float:
        """Return a timeout not exceeding desired nor the remaining budget.

        Ensures a minimal positive floor to avoid zero-second waits when possible.
        """
        remaining = self.left()
        if remaining <= 0.0:
            return 0.0
        return max(0.0 if desired <= 0 else min(desired, remaining), 0.0 if floor <= 0 else min(floor, remaining))


def _selenium_fetch(
    url: str,
    timeout_seconds: int,
    retries: int,
    proxy: str | None,
    user_agent: str,
    max_bytes: int,
    wait_for_selectors: list[str] | None = None,
    wait_for_ms: int | None = None,
    js_strategy: str = "accuracy",
    allow_insecure_ssl: bool | None = None,
    take_screenshot: bool = False,
) -> tuple[int, str, bytes, str | None, bytes | None]:
    """Fetch URL using Selenium with enhanced SPA and error handling."""
    def _sync_fetch():
        driver = None

        def _snap() -> bytes | None:
            """Capture viewport screenshot from the current driver state."""
            if not take_screenshot or driver is None:
                return None
            try:
                return driver.get_screenshot_as_png()
            except Exception:
                return None
        try:
            # Get driver with timeout to prevent indefinite blocking
            driver = _get_driver(js_strategy, timeout_seconds=min(timeout_seconds, 30))
        except Exception as e:
            raise WebDriverException(f"Failed to acquire driver from pool: {e}") from e
        
        try:
            budget = TimeBudget(timeout_seconds)
    
            try:
                # Set timeouts
                # Cap navigation timeout for speed to avoid long stalls; accuracy uses full budget
                nav_cap = 8 if js_strategy == "speed" else timeout_seconds
                driver.set_page_load_timeout(max(1, int(min(nav_cap, budget.left()))))
                # Strategy-based implicit wait: both use 0 for explicit control
                driver.implicitly_wait(0)
                
                last_exc = None
                did_early_accuracy = False
                max_attempts = (min(retries, 1) + 1) if js_strategy == "speed" else (retries + 1)
                for attempt in range(max_attempts):
                    if not budget.ok():
                        break
                    try:
                        # Block heavy resources for both modes to accelerate load
                        blocked_applied = False
                        try:
                            driver.execute_cdp_cmd('Network.enable', {})
                            driver.execute_cdp_cmd('Network.setBlockedURLs', {
                                'urls': [
                                    '*doubleclick*', '*googlesyndication*', '*googletagmanager*',
                                    '*facebook.com/tr*', '*google-analytics*', '*googleadservices*',
                                    '*adsystem*', '*amazon-adsystem*', '*googletag*'
                                ]
                            })
                            blocked_applied = True
                        except Exception:
                            blocked_applied = False
                        # Navigate to URL
                        driver.get(url)
                        
                        # Wait for basic page load (strategy-aware)
                        if js_strategy == "accuracy":
                            WebDriverWait(driver, max(0.5, min(6.0, budget.left()))).until(
                                lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
                            )
                        else:
                            # For speed accept DOMContentLoaded ('interactive') to start earlier (tighter cap)
                            WebDriverWait(driver, max(0.5, min(5.0, budget.left()))).until(
                                lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
                            )
                        
                        # Try to click cookie banners early
                        try:
                            if js_strategy == "accuracy":
                                if budget.ok():
                                    _try_click_cookie_banners(driver)
                            else:
                                # Fast scan with small budget (speed only)
                                if budget.ok():
                                    _try_click_cookie_banners_fast(
                                        driver,
                                        max(0.1, min(0.25, budget.left()))
                                    )
                        except Exception:
                            pass

                        # Speed-only CMP escalation: if overlay/body-lock persists, grant a one-off larger cookie budget
                        if js_strategy == "speed" and budget.ok():
                            try:
                                if _has_overlay_or_body_lock(driver):
                                    _try_click_cookie_banners_fast(
                                        driver,
                                        max(0.6, min(1.0, budget.left()))
                                    )
                            except Exception:
                                pass
                        
                        # Speed mode: skip all SPA detection and waits - extract immediately after basic load
                        if js_strategy == "speed":
                            # Minimal wait for basic content, then extract
                            time.sleep(min(1.0, budget.left()))
                            try:
                                content = driver.page_source
                            except Exception:
                                content = ""
                            final_url = driver.current_url
                            content_bytes = content.encode("utf-8")[:max_bytes]
                            return 200, final_url, content_bytes, "text/html; charset=utf-8", _snap()
                        
                        # Accuracy mode: use speed-like approach with longer settle
                        if js_strategy == "accuracy" and budget.ok():
                            # Longer settle for accuracy but then DIRECT extraction like speed mode
                            time.sleep(min(2.0, budget.left()))
                            try:
                                content = driver.page_source
                            except Exception:
                                content = ""
                            final_url = driver.current_url
                            content_bytes = content.encode("utf-8")[:max_bytes]
                            return 200, final_url, content_bytes, "text/html; charset=utf-8", _snap()
                        
                        # Get page source and URL
                        try:
                            content = driver.page_source
                        except Exception:
                            continue
                        final_url = driver.current_url
                        
                        # Check for error pages
                        if _detect_error_pages(content):
                            # Try to extract actual HTTP status from browser without blocking the UI thread for long.
                            try:
                                status_code = None
                                if js_strategy == "accuracy":
                                    # Keep existing behavior in accuracy
                                    status_code = driver.execute_script(
                                        """
                                        var xhr = new XMLHttpRequest();
                                        xhr.open('HEAD', window.location.href, false);
                                        xhr.send();
                                        return xhr.status;
                                        """
                                    )
                                else:
                                    # Speed: async HEAD with short timeout to avoid stalls (e.g., Cloudflare challenges)
                                    status_code = driver.execute_async_script(
                                        """
                                        const done = arguments[0];
                                        const to = arguments[1] || 1200;
                                        try {
                                          const xhr = new XMLHttpRequest();
                                          xhr.open('HEAD', window.location.href, true);
                                          xhr.timeout = to;
                                          xhr.onreadystatechange = function(){ if (xhr.readyState === 4) done(xhr.status); };
                                          xhr.ontimeout = function(){ done(0); };
                                          xhr.onerror = function(){ done(0); };
                                          xhr.send();
                                        } catch(e) { done(0); }
                                        """,
                                        int(min(1500, max(200, budget.left() * 1000)))
                                    )
                                if isinstance(status_code, int) and status_code >= 400:
                                    content_bytes = content.encode("utf-8")[:max_bytes]
                                    return status_code, final_url, content_bytes, "text/html; charset=utf-8", _snap()
                            except Exception:
                                pass
                            # If still looks like an error (but HTTP may be 200), try a one-off UA-rotated attempt
                            alt = None
                            if js_strategy != "speed" and budget.left() > 3.0:
                                alt = _attempt_with_temp_driver(url, timeout_seconds, proxy, max_bytes, js_strategy, budget.left(), allow_insecure_ssl)
                            if alt:
                                return (*alt, None)
                        
                        # If page content is suspiciously short, attempt UA-rotated retry once
                        if len(content) < 1200 and js_strategy != "speed" and budget.left() > 3.0:
                            alt = _attempt_with_temp_driver(url, timeout_seconds, proxy, max_bytes, js_strategy, budget.left(), allow_insecure_ssl)
                            if alt:
                                return (*alt, None)
                        
                        # Enforce max_bytes
                        content_bytes = content.encode("utf-8")[:max_bytes]
                        
                        return 200, final_url, content_bytes, "text/html; charset=utf-8", _snap()
                        
                    except Exception as e:
                        last_exc = e
                        # Early fallback: first renderer timeout in speed mode -> one-shot accuracy attempt
                        try:
                            if (
                                not did_early_accuracy
                                and js_strategy == "speed"
                                and budget.left() > 2.0
                            ):
                                msg = (str(e) or "").lower()
                                if "timed out receiving message from renderer" in msg:
                                    alt = _attempt_with_temp_driver(
                                        url,
                                        timeout_seconds=timeout_seconds,
                                        proxy=proxy,
                                        allow_insecure_ssl=allow_insecure_ssl,
                                        max_bytes=max_bytes,
                                        js_strategy="accuracy",
                                        budget_left=budget.left(),
                                    )
                                    did_early_accuracy = True
                                    if alt:
                                        return alt
                        except Exception:
                            pass
                        # Exponential backoff with cap, but respect budget
                        if not budget.ok():
                            break
                        backoff = min(2 ** attempt, 5)
                        time.sleep(min(backoff, budget.left()))
                    finally:
                        # Restore CDP blocked URLs so pooled driver does not carry
                        # stale network-blocking state into future requests.
                        if blocked_applied:
                            try:
                                driver.execute_cdp_cmd('Network.setBlockedURLs', {'urls': []})
                            except Exception:
                                pass
                # If we get here, retries exhausted
                if last_exc:
                    # Fallback: if speed mode failed with renderer/timeout issues, try one-shot accuracy attempt
                    try:
                        if js_strategy == "speed" and budget.left() > 2.0:
                            alt = _attempt_with_temp_driver(
                                url,
                                timeout_seconds=timeout_seconds,
                                proxy=proxy,
                                allow_insecure_ssl=allow_insecure_ssl,
                                max_bytes=max_bytes,
                                js_strategy="accuracy",
                                budget_left=budget.left(),
                            )
                            if alt:
                                return (*alt, None)
                    except Exception:
                        pass
                    raise last_exc
                raise RuntimeError("Unknown JS fetch error")
                
            finally:
                _return_driver(driver)
        except Exception as e:
            raise WebDriverException(f"Failed to fetch URL: {e}") from e

    return _sync_fetch()


async def fetch_with_playwright(
    url: str,
    timeout_seconds: int = 30,
    retries: int = 1,
    proxy: str | None = None,
    user_agent: str | None = None,
    max_bytes: int = 50 * 1024 * 1024,
    headless: bool = True,
    stealth: bool = True,
    wait_for_selectors: list[str] | None = None,
    wait_for_ms: int | None = None,
    js_strategy: str = "accuracy",
    allow_insecure_ssl: bool | None = None,
    take_screenshot: bool = False,
) -> tuple[int, str, bytes, str | None, bytes | None]:
    """
    Selenium-based fetching with driver pool (renamed for compatibility).
    Returns: (status_code, final_url, content_bytes, content_type, screenshot_png | None)
    """
    return await run_in_threadpool(
        _selenium_fetch,
        url, timeout_seconds, retries, proxy, user_agent, max_bytes,
        wait_for_selectors, wait_for_ms, js_strategy, allow_insecure_ssl,
        take_screenshot,
    )


def cleanup_drivers():
    """Clean up all drivers in pools."""
    with _pool_lock:
        for pool in _driver_pools.values():
            while not pool.empty():
                try:
                    driver = pool.get_nowait()
                    driver.quit()
                except Exception:
                    pass
        # Reset pool state
        _pool_initialized['normal'] = False
        _pool_initialized['eager'] = False
        _pool_sizes['normal'] = settings.selenium_pool_size
        _pool_sizes['eager'] = settings.selenium_pool_size
        _pool_usage['normal'] = 0
        _pool_usage['eager'] = 0
