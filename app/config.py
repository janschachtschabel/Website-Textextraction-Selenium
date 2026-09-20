"""Validated server defaults; request overrides are resolved in schemas.py."""

import ipaddress
import os
from dataclasses import dataclass

from dotenv import load_dotenv

from . import __version__

load_dotenv()

# Sites with a bot policy expect a crawler to say who runs it and how to reach them.
DEFAULT_USER_AGENT = (
    f"WebsiteTextExtraction/{__version__} (+https://github.com/janschachtschabel/Website-Textextraction-Selenium)"
)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError(f"{name} must be a boolean")
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_json: bool = _bool("LOG_JSON", False)
    api_key: str | None = os.getenv("API_KEY") or None
    default_mode: str = os.getenv("DEFAULT_MODE", "auto")
    default_timeout_seconds: int = int(os.getenv("DEFAULT_TIMEOUT_SECONDS", "120"))
    default_retries: int = int(os.getenv("DEFAULT_RETRIES", "1"))
    default_headless: bool = _bool("DEFAULT_HEADLESS", True)
    default_max_bytes: int = int(os.getenv("DEFAULT_MAX_BYTES", str(10 * 1024 * 1024)))
    default_user_agent: str = os.getenv("DEFAULT_USER_AGENT") or DEFAULT_USER_AGENT  # empty: the default
    default_accept_language: str = os.getenv("DEFAULT_ACCEPT_LANGUAGE", "")
    default_js_auto_wait: bool = _bool("DEFAULT_JS_AUTO_WAIT", True)
    default_js_strategy: str = os.getenv("DEFAULT_JS_STRATEGY", "speed")
    selenium_max_pool_size: int = int(os.getenv("SELENIUM_MAX_POOL_SIZE", "2"))
    selenium_no_sandbox: bool = _bool("SELENIUM_NO_SANDBOX", False)
    chrome_binary: str | None = os.getenv("CHROME_BINARY") or None
    chromedriver_path: str | None = os.getenv("CHROMEDRIVER_PATH") or None
    conversion_workers: int = int(os.getenv("CONVERSION_WORKERS", "2"))
    worker_max_jobs: int = int(os.getenv("WORKER_MAX_JOBS", "100"))
    http_max_connections: int = int(os.getenv("HTTP_MAX_CONNECTIONS", "16"))
    max_concurrent_requests: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "8"))
    max_queue_size: int = int(os.getenv("MAX_QUEUE_SIZE", "50"))
    queue_timeout_seconds: int = int(os.getenv("QUEUE_TIMEOUT_SECONDS", "60"))
    max_active_jobs: int = int(os.getenv("MAX_ACTIVE_JOBS", "10"))
    job_result_ttl: int = int(os.getenv("JOB_RESULT_TTL", "3600"))
    media_conversion_policy: str = os.getenv("MEDIA_CONVERSION_POLICY", "skip").lower()
    allow_insecure_ssl: bool = _bool("ALLOW_INSECURE_SSL", False)
    ssrf_protection: bool = _bool("SSRF_PROTECTION", True)
    respect_robots_txt: bool = _bool("RESPECT_ROBOTS_TXT", False)
    html_converter: str = os.getenv("HTML_CONVERTER", "trafilatura").lower()
    trafilatura_clean_markdown: bool = _bool("TRAFILATURA_CLEAN_MARKDOWN", True)
    result_cache_ttl: int = int(os.getenv("RESULT_CACHE_TTL", "300"))
    result_cache_max_size: int = int(os.getenv("RESULT_CACHE_MAX_SIZE", "200"))
    result_cache_dir: str = os.getenv("RESULT_CACHE_DIR", "")
    revalidation_ttl: int = int(os.getenv("REVALIDATION_TTL", "86400"))
    global_rate_limit_rps: float = float(os.getenv("GLOBAL_RATE_LIMIT_RPS", "0"))
    default_domain_rate_limit_rps: float = float(os.getenv("DEFAULT_DOMAIN_RATE_LIMIT_RPS", "0"))
    presidio_de_model: str = os.getenv("PRESIDIO_DE_MODEL", "de_core_news_lg")
    presidio_en_model: str = os.getenv("PRESIDIO_EN_MODEL", "en_core_web_lg")
    uvicorn_workers: int = int(os.getenv("UVICORN_WORKERS", "1"))
    max_request_bytes: int = int(os.getenv("MAX_REQUEST_BYTES", str(1024 * 1024)))
    inbound_rate_limit_rps: float = float(os.getenv("INBOUND_RATE_LIMIT_RPS", "0"))
    inbound_rate_limit_burst: int = int(os.getenv("INBOUND_RATE_LIMIT_BURST", "20"))

    def __post_init__(self):
        for name in (
            "selenium_max_pool_size",
            "conversion_workers",
            "worker_max_jobs",
            "max_active_jobs",
            "job_result_ttl",
            "http_max_connections",
            "max_concurrent_requests",
            "queue_timeout_seconds",
            "uvicorn_workers",
            "result_cache_max_size",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if not 1 <= self.default_timeout_seconds <= 600:
            raise ValueError("DEFAULT_TIMEOUT_SECONDS must be 1..600")
        if not 0 <= self.default_retries <= 10 or not 1024 <= self.default_max_bytes <= 100 * 1024 * 1024:
            raise ValueError("Invalid default retry or byte limit")
        if (
            min(
                self.max_queue_size,
                self.result_cache_ttl,
                self.revalidation_ttl,
                self.global_rate_limit_rps,
                self.default_domain_rate_limit_rps,
            )
            < 0
        ):
            raise ValueError("Queue size, TTL and rates cannot be negative")
        for name, choices in {
            "default_mode": {"auto", "fast", "js"},
            "default_js_strategy": {"accuracy", "speed"},
            "html_converter": {"trafilatura", "markitdown", "bs4"},
            "media_conversion_policy": {"skip", "none", "metadata", "full"},
        }.items():
            if getattr(self, name) not in choices:
                raise ValueError(f"Invalid {name}")
        if not 1024 <= self.max_request_bytes <= 100 * 1024 * 1024:
            raise ValueError("MAX_REQUEST_BYTES must be 1024..104857600")
        # resolve_options hands these to every request; an invalid value would be a 500 per crawl.
        if not 1 <= len(self.default_user_agent) <= 512 or any(
            ord(c) < 32 or ord(c) > 126 for c in self.default_user_agent
        ):
            raise ValueError("DEFAULT_USER_AGENT must be printable ASCII, 1 to 512 characters")
        if len(self.default_accept_language) > 256 or any(
            ord(c) < 32 or ord(c) > 126 for c in self.default_accept_language
        ):
            raise ValueError("DEFAULT_ACCEPT_LANGUAGE must be printable ASCII, at most 256 characters")
        if self.inbound_rate_limit_rps < 0 or self.inbound_rate_limit_burst < 1:
            raise ValueError("INBOUND_RATE_LIMIT_RPS must be >= 0 and INBOUND_RATE_LIMIT_BURST >= 1")
        # An unauthenticated service on a reachable address is a crawling proxy for anyone who finds it.
        if not self.api_key and not _is_loopback(self.host):
            raise ValueError("Set API_KEY before binding HOST to a non-loopback address")


settings = Settings()
