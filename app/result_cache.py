"""Versioned result identity based on resolved behavior, including PII models."""

import hashlib
import json

from .config import Settings
from .schemas import CrawlOptions

CACHE_VERSION = "extraction-v5"


def make_cache_key(url: str, options: CrawlOptions, config: Settings) -> str:
    payload = {
        "version": CACHE_VERSION,
        "url": url,
        "options": options.model_dump(exclude={"force_refresh"}),
        "pii_models": [config.presidio_de_model, config.presidio_en_model],
        "network_policy": config.ssrf_protection,
    }
    return CACHE_VERSION + ":" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
