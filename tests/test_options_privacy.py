from dataclasses import replace

import pytest
from pydantic import ValidationError

from app import anonymizer
from app.config import settings
from app.result_cache import make_cache_key
from app.results import CrawlError
from app.schemas import BatchCrawlRequest, CrawlRequest, resolve_options


def test_single_and_batch_resolve_same_server_defaults():
    custom = replace(
        settings,
        default_mode="fast",
        default_timeout_seconds=17,
        default_retries=3,
        default_max_bytes=9000,
        default_js_strategy="accuracy",
    )
    single = resolve_options(CrawlRequest(url="https://example.com"), custom)
    batch = resolve_options(BatchCrawlRequest(urls=["https://example.com"]), custom)
    assert single == batch
    assert (single.mode, single.timeout_ms, single.retries, single.max_bytes) == ("fast", 17000, 3, 9000)
    assert single.js_strategy == "accuracy"
    assert single.screenshot is False


def test_explicit_false_overrides_global_insecure_ssl():
    custom = replace(settings, allow_insecure_ssl=True)
    assert (
        resolve_options(CrawlRequest(url="https://example.com", allow_insecure_ssl=False), custom).allow_insecure_ssl
        is False
    )


@pytest.mark.parametrize("proxy", ["socks5://host:123", "http://", "file:///etc/passwd"])
def test_unsupported_proxy_is_rejected(proxy):
    with pytest.raises(ValidationError):
        CrawlRequest(url="https://example.com", proxy=proxy)


def test_effective_media_policy_and_model_identity_change_cache_key():
    req = CrawlRequest(url="https://example.com/video")
    one = resolve_options(req, replace(settings, media_conversion_policy="skip"))
    two = resolve_options(req, replace(settings, media_conversion_policy="metadata"))
    assert make_cache_key(str(req.url), one, settings) != make_cache_key(str(req.url), two, settings)
    assert make_cache_key(str(req.url), one, settings) != make_cache_key(
        str(req.url), one, replace(settings, presidio_de_model="different")
    )


def test_unavailable_pii_engine_never_returns_original_text(monkeypatch):
    anonymizer._engines.clear()

    def unavailable(name):
        raise ImportError("optional dependency unavailable")

    monkeypatch.setattr(anonymizer, "import_module", unavailable)
    with pytest.raises(CrawlError, match="Anonymization unavailable") as error:
        anonymizer.anonymize("Contact alice@example.com", "de")
    assert error.value.status_code == 503
    assert "alice@example.com" not in str(error.value)


@pytest.mark.parametrize("requested, effective", [(None, 2.0), (0, 2.0), (5, 2.0), (0.5, 0.5)])
def test_operator_domain_rate_limit_is_a_ceiling(requested, effective):
    custom = replace(settings, default_domain_rate_limit_rps=2)
    options = resolve_options(CrawlRequest(url="https://example.com", crawl_rate_limit_rps=requested), custom)
    assert options.crawl_rate_limit_rps == effective


@pytest.mark.parametrize("requested, effective", [(None, 0.0), (7, 7.0)])
def test_clients_choose_the_rate_when_the_operator_sets_none(requested, effective):
    custom = replace(settings, default_domain_rate_limit_rps=0)
    options = resolve_options(CrawlRequest(url="https://example.com", crawl_rate_limit_rps=requested), custom)
    assert options.crawl_rate_limit_rps == effective
