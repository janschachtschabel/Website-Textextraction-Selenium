from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return int(val)
    except Exception:
        return default


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _get_int("PORT", 8000)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_json: bool = _get_bool("LOG_JSON", False)
    # Optional Bearer-Token for API authentication (empty = auth disabled)
    api_key: str | None = os.getenv("API_KEY") or None

    # Presidio PII anonymization (spacy models)
    presidio_de_model: str = os.getenv("PRESIDIO_DE_MODEL", "de_core_news_lg")
    presidio_en_model: str = os.getenv("PRESIDIO_EN_MODEL", "en_core_web_lg")

    # Crawl defaults
    default_mode: str = os.getenv("DEFAULT_MODE", "auto")
    default_timeout_seconds: int = _get_int("DEFAULT_TIMEOUT_SECONDS", 120)
    default_retries: int = _get_int("DEFAULT_RETRIES", 1)
    default_headless: bool = _get_bool("DEFAULT_HEADLESS", True)
    default_stealth: bool = _get_bool("DEFAULT_STEALTH", True)
    default_max_bytes: int = _get_int("DEFAULT_MAX_BYTES", 10 * 1024 * 1024)
    default_user_agent: str = os.getenv(
        "DEFAULT_USER_AGENT",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        ),
    )

    # Selenium settings
    selenium_pool_size: int = _get_int("SELENIUM_POOL_SIZE", 2)
    selenium_max_pool_size: int = _get_int("SELENIUM_MAX_POOL_SIZE", 8)  # Dynamic scaling limit
    selenium_scale_threshold: float = float(os.getenv("SELENIUM_SCALE_THRESHOLD", "0.8"))  # Scale at 80% usage
    default_js_auto_wait: bool = _get_bool("DEFAULT_JS_AUTO_WAIT", True)
    # JS strategy: accuracy|speed
    default_js_strategy: str = os.getenv("DEFAULT_JS_STRATEGY", "speed")
    
    # Request queuing and capacity
    max_queue_size: int = _get_int("MAX_QUEUE_SIZE", 50)  # Maximum queued requests
    queue_timeout_seconds: int = _get_int("QUEUE_TIMEOUT_SECONDS", 60)  # Max wait in queue

    # Media handling
    media_conversion_policy: str = (os.getenv("MEDIA_CONVERSION_POLICY", "skip").strip().lower())
    # Security: allow disabling SSL verification globally (use with care)
    allow_insecure_ssl: bool = _get_bool("ALLOW_INSECURE_SSL", False)
    # SSRF protection: block requests to private/loopback IPs (recommended: true)
    ssrf_protection: bool = _get_bool("SSRF_PROTECTION", True)
    # HTML converter selection: trafilatura|markitdown|bs4
    html_converter: str = os.getenv("HTML_CONVERTER", "trafilatura").strip().lower()
    # Trafilatura mode: cleaned main content (true) vs raw html2txt (false)
    trafilatura_clean_markdown: bool = _get_bool("TRAFILATURA_CLEAN_MARKDOWN", True)
    # Result cache: TTL in seconds (0 = disabled)
    result_cache_ttl: int = _get_int("RESULT_CACHE_TTL", 300)
    # Max cache size in MB (used by diskcache as size_limit; 0 = unlimited)
    result_cache_max_size: int = _get_int("RESULT_CACHE_MAX_SIZE", 200)
    # Directory for diskcache storage; empty = system temp dir
    result_cache_dir: str = os.getenv("RESULT_CACHE_DIR", "")

    # Rate limiting (0 = disabled)
    # Global cap: max total crawl requests per second across all domains
    global_rate_limit_rps: float = float(os.getenv("GLOBAL_RATE_LIMIT_RPS", "0"))
    # Per-domain default: max requests per second to any single domain
    default_domain_rate_limit_rps: float = float(os.getenv("DEFAULT_DOMAIN_RATE_LIMIT_RPS", "0"))

    # Uvicorn worker processes (multiprocessing; 1 = single-process dev mode)
    # Note: each worker has its own in-memory cache and metrics — use Redis for shared state
    uvicorn_workers: int = _get_int("UVICORN_WORKERS", 4)


settings = Settings()
