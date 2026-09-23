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


def test_auto_renders_with_accuracy_unless_the_request_names_a_strategy():
    speed_by_default = replace(settings, default_mode="auto", default_js_strategy="speed")

    def strategy(**request):
        return resolve_options(CrawlRequest(url="https://example.com", **request), speed_by_default).js_strategy

    assert strategy() == "accuracy"
    assert strategy(mode="auto") == "accuracy"
    assert strategy(mode="auto", js_strategy="speed") == "speed"
    assert strategy(mode="js") == "speed"
    assert strategy(mode="fast") == "speed"


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


def test_accept_language_inherits_the_server_default_and_can_be_overridden():
    custom = replace(settings, default_accept_language="de,en;q=0.8")
    assert resolve_options(CrawlRequest(url="https://example.com"), custom).accept_language == "de,en;q=0.8"
    assert (
        resolve_options(CrawlRequest(url="https://example.com", accept_language="fr"), custom).accept_language == "fr"
    )
    unset = replace(settings, default_accept_language="")
    assert resolve_options(CrawlRequest(url="https://example.com"), unset).accept_language == ""


@pytest.mark.parametrize(
    "value, problem", [("de-DE,en;q=0.8", None), ("de\r\nX-Injected: 1", "printable ASCII"), ("d" * 257, "256")]
)
def test_accept_language_must_be_a_short_printable_header_value(value, problem):
    if problem is None:
        assert CrawlRequest(url="https://example.com", accept_language=value).accept_language == value
        return
    with pytest.raises(ValidationError, match=problem):
        CrawlRequest(url="https://example.com", accept_language=value)


def test_a_full_page_screenshot_requires_a_screenshot():
    assert CrawlRequest(url="https://example.com", screenshot=True, screenshot_full_page=True).screenshot_full_page
    with pytest.raises(ValidationError, match="screenshot"):
        CrawlRequest(url="https://example.com", screenshot_full_page=True)
