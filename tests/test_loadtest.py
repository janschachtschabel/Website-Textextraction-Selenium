import asyncio
import json

import pytest

from helper import loadtest


@pytest.mark.parametrize(
    "patch",
    [
        {"success": False},
        {"status_code": 429},
        {"error_page_detected": True},
        {"markdown": ""},
        {"truncated": True},
        {"screenshot_base64": None},
    ],
)
def test_benchmark_rejects_false_success_and_missing_screenshot(patch):
    valid = {
        "success": True,
        "status_code": 200,
        "extraction_status": "ok",
        "markdown": "Useful text",
        "error_page_detected": False,
        "truncated": False,
        "screenshot_base64": "iVBORw0KGgo=",
    }
    assert not loadtest.response_success({**valid, **patch}, screenshot=True)


async def test_latency_includes_body_and_cold_payload_bypasses_cache(monkeypatch):
    clock = [0]
    sent = []
    monkeypatch.setattr(loadtest.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(loadtest, "CACHE_SCENARIO", "cold")

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def text(self):
            clock[0] = 5
            return json.dumps(
                {
                    "success": True,
                    "status_code": 200,
                    "extraction_status": "ok",
                    "markdown": "Useful content",
                    "cached": False,
                }
            )

    class Session:
        def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return Response()

    result = await loadtest.fetch(Session(), asyncio.Semaphore(1), "https://example.com", "fast", "trafilatura", 1)
    assert result.success and result.response_time == 5
    assert sent[0]["force_refresh"] is True
