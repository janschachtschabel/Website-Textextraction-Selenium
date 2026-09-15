"""Selenium driver configuration. Each job receives a fresh browser profile."""

import os

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from .config import Settings, settings
from .schemas import CrawlOptions


def build_options(request: CrawlOptions, proxy_url: str, config: Settings = settings) -> Options:
    options = Options()
    options.page_load_strategy = "eager" if request.js_strategy == "speed" else "normal"
    options.accept_insecure_certs = request.allow_insecure_ssl
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if config.chrome_binary:
        options.binary_location = config.chrome_binary
    if request.headless:
        options.add_argument("--headless=new")
    if config.selenium_no_sandbox:
        options.add_argument("--no-sandbox")  # Explicit opt-in for environments that cannot provide a sandbox.
    for argument in [
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-extensions",
        "--disable-quic",
        "--dns-prefetch-disable",
        "--no-first-run",
        "--window-size=1920,1080",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--proxy-bypass-list=<-loopback>",
        f"--proxy-server={proxy_url}",
        f"--user-agent={request.user_agent}",
    ]:
        options.add_argument(argument)
    return options


def create_driver(request: CrawlOptions, proxy_url: str):
    service = Service(executable_path=settings.chromedriver_path, log_output=os.devnull)
    return webdriver.Chrome(service=service, options=build_options(request, proxy_url))
