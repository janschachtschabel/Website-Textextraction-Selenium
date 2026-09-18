"""Background batch jobs: submit, poll, bounded, and honest about interrupted work."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import diskcache
import httpx
import pytest

from app.config import settings
from app.main import create_app
from app.resources import STORAGE_LAYOUT, Resources


class NoBrowser:
    async def fetch(self, *args):
        pytest.fail("These pages are static")


@pytest.fixture
def jobs_api(tmp_path, article_html):
    @asynccontextmanager
    async def start(delay=0.0, **overrides):
        async def upstream(request):
            await asyncio.sleep(delay)
            if request.url.path == "/blocked":
                return httpx.Response(429, content=b"<main>Rate limited</main>", headers={"content-type": "text/html"})
            return httpx.Response(200, content=article_html.encode(), headers={"content-type": "text/html"})

        config = replace(
            settings,
            result_cache_dir=str(tmp_path),
            conversion_workers=1,
            default_retries=0,
            host="127.0.0.1",
            api_key=None,
            **overrides,
        )
        resources = Resources(
            config, transport=httpx.MockTransport(upstream), validate=lambda url: None, browser=NoBrowser()
        )
        app = create_app(config, resources)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                yield client, resources

    return start


async def finished(client, status_url, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = (await client.get(status_url)).json()
        if body["status"] in {"done", "failed"}:
            return body
        await asyncio.sleep(0.05)
    pytest.fail("The job did not finish")


async def test_a_job_runs_in_the_background_and_reports_the_batch_result(jobs_api):
    async with jobs_api() as (client, _):
        urls = ["https://example.com/a", "https://example.com/blocked"]
        accepted = await client.post("/jobs", json={"urls": urls, "mode": "fast"})
        assert accepted.status_code == 202
        job = accepted.json()
        assert job["status"] == "queued" and job["status_url"] == f"/jobs/{job['job_id']}"
        done = await finished(client, job["status_url"])
        assert done["status"] == "done" and done["finished_at"] and done["error"] is None
        result = done["result"]
        assert (result["total"], result["succeeded"], result["failed"]) == (2, 1, 1)
        assert result["results"][1]["error"] == "Extraction blocked"


async def test_unknown_jobs_answer_404(jobs_api):
    async with jobs_api() as (client, _):
        response = await client.get("/jobs/not-a-job")
        assert response.status_code == 404 and response.json()["detail"] == "Unknown or expired job"


async def test_active_jobs_are_bounded_per_process(jobs_api):
    async with jobs_api(delay=3, max_active_jobs=1) as (client, _):
        first = await client.post("/jobs", json={"urls": ["https://example.com/one"], "mode": "fast"})
        second = await client.post("/jobs", json={"urls": ["https://example.com/two"], "mode": "fast"})
        assert first.status_code == 202
        assert second.status_code == 503 and second.json()["detail"] == "Too many active jobs"


async def test_submissions_pay_the_inbound_limit_per_url(jobs_api):
    async with jobs_api(inbound_rate_limit_rps=0.01, inbound_rate_limit_burst=2) as (client, _):
        two = await client.post("/jobs", json={"urls": ["https://example.com/a", "https://example.com/b"]})
        assert two.status_code == 202
        assert (await client.post("/jobs", json={"urls": ["https://example.com/c"]})).status_code == 429


async def test_shutdown_marks_unfinished_jobs_as_interrupted(jobs_api, tmp_path):
    async with jobs_api(delay=10) as (client, _):
        job = (await client.post("/jobs", json={"urls": ["https://example.com/slow"], "mode": "fast"})).json()
    with diskcache.Cache(str(tmp_path / f"state-{STORAGE_LAYOUT}"), disk=diskcache.JSONDisk) as state:
        record = state.get("job:" + job["job_id"])
    assert record["status"] == "failed" and record["error"] == "Interrupted: the service shut down"


async def test_a_job_past_its_deadline_without_a_result_is_reported_lost(jobs_api):
    async with jobs_api() as (client, resources):
        orphan = {
            "status": "running",
            "submitted_at": "2026-09-18T08:00:00+00:00",
            "deadline_at": time.time() - 120,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        resources.state.set("job:orphan", orphan)
        body = (await client.get("/jobs/orphan")).json()
        assert body["status"] == "failed" and body["error"] == "Job lost: the process that ran it stopped"
