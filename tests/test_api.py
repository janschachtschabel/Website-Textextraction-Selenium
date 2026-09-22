import asyncio
from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.main import create_app
from app.resources import Resources


class NoBrowser:
    async def fetch(self, *args):
        pytest.fail("This HTTP-only result must not launch Selenium")


@pytest.fixture
async def api(tmp_path, article_html):
    state = {"calls": 0, "active": 0, "peak": 0}

    async def upstream(request):
        state["calls"] += 1
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0.015)
            if request.url.path == "/blocked":
                return httpx.Response(429, content=b"<main>Rate limited</main>", headers={"content-type": "text/html"})
            if request.url.path == "/empty":
                return httpx.Response(200, content=b"", headers={"content-type": "text/html"})
            if request.url.path == "/choices":
                return httpx.Response(300, content=article_html.encode(), headers={"content-type": "text/html"})
            return httpx.Response(200, content=article_html.encode(), headers={"content-type": "text/html"})
        finally:
            state["active"] -= 1

    config = replace(
        settings,
        result_cache_dir=str(tmp_path),
        max_concurrent_requests=1,
        conversion_workers=1,
        default_retries=0,
        api_key=None,
        host="127.0.0.1",
    )
    resources = Resources(
        config, transport=httpx.MockTransport(upstream), validate=lambda url: None, browser=NoBrowser()
    )
    app = create_app(config, resources)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, state, resources


async def test_http_cache_coalescing_force_refresh_and_actual_engine(api):
    client, state, _ = api
    payload = {"url": "https://example.com/article"}
    responses = await asyncio.gather(*(client.post("/crawl", json=payload) for _ in range(4)))
    assert all(r.status_code == 200 and r.json()["success"] for r in responses)
    assert state["calls"] == 1
    assert sum(r.json()["coalesced"] for r in responses) == 3
    response = (await client.post("/crawl", json=payload)).json()
    assert response["cached"] and response["fetch_engine"] == "http"
    assert response["converter"] == "trafilatura"
    refreshed = await client.post("/crawl", json={**payload, "force_refresh": True})
    assert refreshed.json()["cached"] is False and state["calls"] == 2


async def test_each_batch_url_uses_global_capacity_and_errors_are_counted(api):
    client, state, _ = api
    response = await client.post(
        "/crawl/batch",
        json={
            "urls": ["https://example.com/one", "https://example.com/blocked", "https://example.com/empty"],
            "max_concurrency": 3,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["succeeded"], body["failed"], state["peak"]) == (1, 2, 1)
    assert body["results"][1]["result"]["status_code"] == 429
    assert body["results"][1]["result"]["fetch_engine"] == "http"
    assert body["results"][2]["result"]["extraction_status"] == "empty"
    # The upstream status is the reason; "blocked" only says how the body was classified.
    assert body["results"][1]["error"] == "Upstream status 429"
    assert body["results"][2]["error"] == "Extraction empty"
    stats = (await client.get("/stats")).json()
    assert stats["requests_success"] == 1 and stats["requests_error"] == 2


async def test_failed_extraction_is_not_cached_and_health_needs_no_idle_browser(api):
    client, state, resources = api
    for _ in range(2):
        response = await client.post("/crawl", json={"url": "https://example.com/empty"})
        assert not response.json()["success"]
    assert state["calls"] == 2
    health = await client.get("/health")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert resources.browser_pool.stats()["started"] == 0


async def test_anonymization_failure_returns_no_text_or_parallel_representation(api, monkeypatch):
    # Fail even where the PII extra is installed; spawned conversion workers inherit the environment.
    monkeypatch.setenv("PRESIDIO_DE_MODEL", "model_not_installed_for_test")
    client, state, _ = api
    response = await client.post(
        "/crawl",
        json={"url": "https://example.com/article", "anonymize": True, "extract_links": True, "screenshot": True},
    )
    assert response.status_code == 503
    assert "Reflection" not in response.text
    assert "markdown" not in response.json()
    response = await client.post("/crawl", json={"url": "https://example.com/article", "anonymize": True})
    assert response.status_code == 503 and state["calls"] == 2


async def test_auth_protects_crawl_job_stats_and_metrics_routes(tmp_path):
    config = replace(settings, result_cache_dir=str(tmp_path), api_key="fixture-key", host="127.0.0.1")
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path, payload in [
                ("/crawl", {"url": "https://example.com"}),
                ("/crawl/batch", {"urls": ["https://example.com"]}),
                ("/jobs", {"urls": ["https://example.com"]}),
            ]:
                assert (await client.post(path, json=payload)).status_code == 401
            assert (await client.get("/jobs/any")).status_code == 401
            for path in ("/stats", "/metrics"):
                assert (await client.get(path)).status_code == 401
                assert (await client.get(path, headers={"Authorization": "Bearer fixture-key"})).status_code == 200
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/stats", headers={b"Authorization": b"Bearer \xff"})).status_code == 401


async def test_auto_uses_rendered_result_and_its_success_status(api):
    from app.results import FetchResult

    client, _, resources = api

    async def shell(request):
        return httpx.Response(
            200, content=b'<div id="root"></div><script src="app.js"></script>', headers={"content-type": "text/html"}
        )

    resources.http.transports = dict.fromkeys((False, True), httpx.MockTransport(shell))

    class Browser:
        async def fetch(self, url, options, deadline):
            return FetchResult(
                b"<main>Rendered lesson content.</main>", url + "/rendered", 200, "text/html", "selenium"
            )

    resources.browser = Browser()
    result = (await client.post("/crawl", json={"url": "https://example.com/shell"})).json()
    assert result["success"] and not result["error_page_detected"]
    assert result["fetch_engine"] == "selenium" and "Rendered lesson content" in result["markdown"]


async def test_unsettled_rendered_page_is_returned_but_not_a_cached_success(api):
    from app.results import FetchResult

    client, _, resources = api
    calls = []

    class Browser:
        async def fetch(self, url, options, deadline):
            calls.append(url)
            # Like a spinner that never stopped: the content may be incomplete, as with truncation.
            return FetchResult(
                b"<main>Rendered lesson content.</main>", url, 200, "text/html", "selenium", settled=False
            )

    resources.browser = Browser()
    for _ in range(2):
        result = (await client.post("/crawl", json={"url": "https://example.com/spinner", "mode": "js"})).json()
        assert not result["success"] and not result["cached"]
        assert "Rendered lesson content" in result["markdown"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload, error",
    [
        ({"urls": ["https://example.com/long"], "mode": "fast", "max_bytes": 1024}, "Extraction truncated"),
        ({"urls": ["https://example.com/spinner"], "mode": "js"}, "Extraction incomplete"),
        ({"urls": ["https://example.com/choices"], "mode": "fast"}, "Upstream status 300"),
        ({"urls": ["https://example.com/unobserved"], "mode": "js"}, "Upstream status unknown"),
    ],
)
async def test_batch_error_names_why_an_extracted_page_is_not_a_success(api, payload, error):
    from app.results import FetchResult

    client, _, resources = api

    class Browser:
        async def fetch(self, url, options, deadline):
            unobserved = url.endswith("/unobserved")  # otherwise a page that never settled
            status = None if unobserved else 200
            body = b"<main>Rendered lesson content.</main>"
            return FetchResult(body, url, status, "text/html", "selenium", settled=unobserved)

    resources.browser = Browser()
    item = (await client.post("/crawl/batch", json=payload)).json()["results"][0]
    assert item["result"]["extraction_status"] == "ok" and not item["success"]
    assert item["error"] == error


def pii_backend(text, language):
    from app.schemas import AnonymizationResult

    return "[redacted]", AnonymizationResult(entities_found=["PERSON"], entity_count=1)


async def test_only_redacted_output_is_cached_and_parallel_representations_are_suppressed(api, monkeypatch):
    client, state, _ = api
    monkeypatch.setattr("app.service.anonymize_document", pii_backend)
    payload = {
        "url": "https://example.com/pii",
        "anonymize": True,
        "extract_links": True,
        "extract_metadata": True,  # an author name is personal data
        "screenshot": True,
    }
    for cached in (False, True):
        result = (await client.post("/crawl", json=payload)).json()
        assert result["cached"] is cached and result["markdown"] == "[redacted]"
        assert result["links"] is None and result["screenshot_base64"] is None and result["metadata"] is None
        assert any("metadata" in warning for warning in result["warnings"])
        assert result["anonymization"]["entity_count"] == 1
    assert state["calls"] == 1


async def test_batch_wait_deadline_is_included_in_url_error_metrics(api):
    client, _, resources = api

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, content=b"late")

    resources.http.transports = dict.fromkeys((False, True), httpx.MockTransport(slow))
    response = await client.post(
        "/crawl/batch",
        json={"urls": ["https://example.com/a", "https://example.com/b"], "max_concurrency": 1, "timeout_ms": 1000},
    )
    assert response.json()["failed"] == 2
    assert (await client.get("/stats")).json()["requests_error"] == 2


async def test_unexpected_adapter_failure_stays_isolated_to_its_batch_item(api, article_html):
    client, _, resources = api

    def upstream(request):
        if request.url.path == "/broken":
            raise RuntimeError("sensitive upstream content")
        return httpx.Response(200, content=article_html.encode(), headers={"content-type": "text/html"})

    resources.http.transports = dict.fromkeys((False, True), httpx.MockTransport(upstream))
    response = await client.post(
        "/crawl/batch", json={"urls": ["https://example.com/broken", "https://example.com/ok"]}
    )
    assert response.status_code == 200
    assert response.json()["failed"] == 1 and response.json()["succeeded"] == 1
    assert "sensitive upstream content" not in response.text


async def test_batch_pipeline_runs_without_the_http_layer(api):
    from app.schemas import BatchCrawlRequest, resolve_options

    _, state, resources = api
    urls = ["https://example.com/a", "https://example.com/b", "https://example.com/blocked"]
    options = resolve_options(BatchCrawlRequest(urls=urls), resources.config)
    response = await resources.service.crawl_batch(urls, options, max_concurrency=2)
    assert (response.total, response.succeeded, response.failed) == (3, 2, 1)
    assert [item.url for item in response.results] == urls
    assert response.results[-1].error == "Upstream status 429"
    assert state["peak"] <= 2


async def test_both_worker_pools_use_the_configured_job_budget(api):
    _, _, resources = api
    assert resources.browser_pool.max_jobs == resources.conversion_pool.max_jobs == resources.config.worker_max_jobs


def test_worker_job_budget_must_be_positive():
    with pytest.raises(ValueError, match="worker_max_jobs"):
        replace(settings, host="127.0.0.1", worker_max_jobs=0)


async def test_metadata_is_returned_only_when_requested(api):
    client, _, _ = api
    plain = (await client.post("/crawl", json={"url": "https://example.com/plain"})).json()
    assert plain["success"] and plain["metadata"] is None
    described = (
        await client.post("/crawl", json={"url": "https://example.com/described", "extract_metadata": True})
    ).json()
    assert described["metadata"]["title"] == "Optics"
    assert described["metadata"]["canonical_url"] == "https://example.com/described"


async def test_prometheus_metrics_are_cumulative_counters_histogram_and_gauges(api):
    from prometheus_client.parser import text_string_to_metric_families

    client, _, _ = api
    await client.post("/crawl", json={"url": "https://example.com/metered"})  # extracted
    await client.post("/crawl", json={"url": "https://example.com/metered"})  # cache hit
    await client.post("/crawl", json={"url": "https://example.com/blocked"})  # upstream 429
    response = await client.get("/metrics")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    samples = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }
    assert samples[("extraction_requests_total", (("outcome", "success"),))] == 2
    assert samples[("extraction_requests_total", (("outcome", "error"),))] == 1
    assert samples[("extraction_cache_hits_total", ())] == 1
    # Latency covers fresh successful extractions only, like /stats.
    assert samples[("extraction_request_duration_seconds_count", ())] == 1
    assert samples[("extraction_request_duration_seconds_bucket", (("le", "+Inf"),))] == 1
    assert samples[("extraction_ready", ())] == 1
    assert samples[("extraction_cache_entries", ())] == 1
    assert samples[("extraction_pool_workers", (("pool", "conversion"), ("state", "limit")))] == 1
    assert ("extraction_active_requests", ()) in samples


async def test_a_forced_refresh_never_joins_another_request(api):
    """force_refresh promises an unconditional fetch, so it must not take a leader's answer."""
    client, state, resources = api
    payload = {"url": "https://example.com/article"}
    leader = asyncio.create_task(client.post("/crawl", json=payload))
    for _ in range(500):  # wait for the leader to be in flight, not for a fixed moment
        if resources.service.inflight:
            break
        await asyncio.sleep(0.002)
    assert resources.service.inflight
    forced = await client.post("/crawl", json={**payload, "force_refresh": True})
    assert (await leader).status_code == 200
    assert forced.status_code == 200
    assert forced.json()["coalesced"] is False and forced.json()["cached"] is False
    assert state["calls"] == 2
    assert not resources.service.inflight  # neither request left the other's entry behind


@pytest.mark.parametrize(
    "path, body",
    [
        ("/crawl", {"url": "https://example.com/article"}),
        ("/crawl/batch", {"urls": ["https://example.com/article"]}),
        ("/jobs", {"urls": ["https://example.com/article"]}),
    ],
)
async def test_a_deadline_above_the_operator_ceiling_is_refused_on_every_route(api, path, body):
    """resolve_options raises CrawlError; this pins that the handler turns it into a 422 the
    client can read, on all three routes that resolve options."""
    client, _, _ = api
    response = await client.post(path, json={**body, "timeout_ms": 700_000})
    assert response.status_code == 422
    assert "600" in response.json()["detail"]
