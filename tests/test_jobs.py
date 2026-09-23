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


def saves(monkeypatch, jobs):
    """Every record the runner writes, as (status, progress). A poll cannot see writes the
    throttle swallowed, so anything about how often it writes is asserted from here."""
    original = jobs._save
    written = []

    async def spy(job_id, record):
        written.append((record["status"], dict(record["progress"])))
        await original(job_id, record)

    monkeypatch.setattr(jobs, "_save", spy)
    return written


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
        assert result["results"][1]["error"] == "Upstream status 429"


async def test_unknown_jobs_answer_404(jobs_api):
    async with jobs_api() as (client, _):
        response = await client.get("/jobs/not-a-job")
        assert response.status_code == 404 and response.json()["detail"] == "Unknown or expired job"


@pytest.mark.parametrize("path", ["/jobs/abc:row:0"])
async def test_a_job_id_is_only_what_the_service_hands_out(jobs_api, path):
    """The id becomes part of a store key. One with a colon could name a key that is not a
    job record - a result row, once rows exist - so it is refused at the door."""
    async with jobs_api() as (client, _):
        assert (await client.get(path)).status_code == 422


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


async def test_a_running_job_writes_its_progress_before_it_finishes(jobs_api, monkeypatch):
    """Without this a 2000-URL job answers "running" for an hour and then everything at
    once, which is indistinguishable from a job that is stuck.

    Asserted on what was written, not on what a poll happened to catch: racing polls
    against a job makes the test timing-dependent, and an earlier version of it passed
    even with the throttle disabled entirely, because the save at the end supplied the
    only progress it ever saw.
    """
    monkeypatch.setattr("app.jobs.PROGRESS_EVERY_SECONDS", 0.0)
    async with jobs_api() as (client, resources):
        written = saves(monkeypatch, resources.jobs)
        urls = [f"https://example.com/{n}" for n in range(6)]
        accepted = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 2})).json()
        await finished(client, accepted["status_url"])

    mid_run = [progress["done"] for status, progress in written if status == "running"]
    assert [done for done in mid_run if 0 < done < 6], (
        f"nothing was written while the job ran, so a poll could only ever see 0 or 6: {written}"
    )
    assert mid_run == sorted(mid_run), f"progress must not go backwards, saw {mid_run}"
    assert written[-1][1]["done"] == 6, f"the last write must count every URL, saw {written[-1]}"


async def test_the_throttle_keeps_a_large_job_from_writing_once_per_url(jobs_api, monkeypatch):
    """The counterpart to the test above: that one proves progress is written at all, this
    one proves the throttle decides how often. Without it a 2000-URL batch in fast mode is
    2000 writes to a store shared across processes, through the same thread pool the
    crawling uses. A swallowed write must still not cost the final count."""
    monkeypatch.setattr("app.jobs.PROGRESS_EVERY_SECONDS", 3600.0)
    async with jobs_api() as (client, resources):
        written = saves(monkeypatch, resources.jobs)
        urls = [f"https://example.com/{n}" for n in range(8)]
        accepted = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 2})).json()
        body = await finished(client, accepted["status_url"])

    running = [progress for status, progress in written if status == "running"]
    assert len(running) == 1, f"the throttle let {len(running)} writes through, close to one per URL: {written}"
    assert body["progress"] == {"done": 8, "succeeded": 8, "total": 8}, "a throttled write must not lose the count"


async def test_progress_counts_successes_apart_from_failures(jobs_api):
    async with jobs_api() as (client, _):
        urls = ["https://example.com/one", "https://example.com/blocked", "https://example.com/two"]
        accepted = (await client.post("/jobs", json={"urls": urls})).json()
        body = await finished(client, accepted["status_url"])
        assert body["progress"] == {"done": 3, "succeeded": 2, "total": 3}
        assert body["result"]["succeeded"] == 2 and body["result"]["failed"] == 1


async def test_a_queued_job_already_names_its_size(jobs_api):
    async with jobs_api(delay=0.2) as (client, _):
        urls = [f"https://example.com/{n}" for n in range(4)]
        accepted = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 1})).json()
        body = (await client.get(accepted["status_url"])).json()
        assert body["progress"]["total"] == 4


async def test_a_failing_progress_write_does_not_fail_the_job(jobs_api, monkeypatch):
    """Progress is incidental; the crawling is the job. A busy state store must not turn a
    batch that succeeded into a failure - the same rule B08 established for metrics."""
    monkeypatch.setattr("app.jobs.PROGRESS_EVERY_SECONDS", 0.0)
    async with jobs_api(delay=0.05) as (client, resources):
        original = resources.jobs._save

        async def refuse(job_id, record):
            if record["status"] == "running" and record["progress"]["done"]:
                raise RuntimeError("state store busy")
            await original(job_id, record)

        monkeypatch.setattr(resources.jobs, "_save", refuse)
        urls = [f"https://example.com/{n}" for n in range(3)]
        accepted = (await client.post("/jobs", json={"urls": urls})).json()
        body = await finished(client, accepted["status_url"])
        assert body["status"] == "done", body.get("error")
        assert body["result"]["succeeded"] == 3
