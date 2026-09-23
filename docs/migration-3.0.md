# Migrating to 3.0

`POST /crawl/batch` is unchanged. Only clients of `POST /jobs` are affected, and only where
they read `result` from `GET /jobs/{job_id}`.

## What changed

- `GET /jobs/{job_id}` no longer carries `result`. It reports `status`, `progress`, `error`
  and a `results_url`.
- `POST /jobs` answers with a `results_url` beside the `status_url`.
- `GET /jobs/{job_id}/results?offset=0&limit=20` pages through the URLs a job has finished,
  while it runs and after. `limit` is at most 100.
- A job id is only what the service hands out, `[A-Za-z0-9_-]` up to 64 characters; any
  other answers 422 instead of 404.

## Reading the results

Page through `results_url` instead of reading `result.results`, passing each page's
`next_offset` on. An empty page from a job that has ended means there is nothing more; from
one that is still running, that nothing new has finished yet. A job reported lost is the
exception - that verdict comes from the clock, and a stalled process can still add rows -
so read it once more before resubmitting what is missing:

```python
import time

import httpx


def results(base, job_id, key):
    """Yield each finished URL of a job as soon as it is stored."""
    offset, headers = 0, {"Authorization": f"Bearer {key}"}
    while True:
        params = {"offset": offset, "limit": 100}
        page = httpx.get(f"{base}/jobs/{job_id}/results", params=params, headers=headers).json()
        yield from page["results"]
        offset = page["next_offset"]
        if not page["results"]:
            if page["status"] in ("done", "failed"):
                return
            time.sleep(2)
```

Rows come in the order the URLs finished, not the order they were submitted in. Each
carries `position`, its index in the request, and otherwise the fields an entry of
`result.results` had. Where the client used the summary:

| Before | Now |
|---|---|
| `result.total` | `progress.total` |
| `result.succeeded` | `progress.succeeded` |
| `result.failed` | `progress.done - progress.succeeded` |
| `result.elapsed_ms` | `finished_at - submitted_at`, to the second |
| `result.results[i]` | the row whose `position` is `i` |

## Before upgrading

- Collect the results of jobs that have finished. The new service has no field to return
  them in; they would expire within `JOB_RESULT_TTL` anyway.
- Unfinished jobs end with the restart, as with any upgrade.
- A panel that deploys the compose file from `main`, as the
  [README](../README.md#deploy-from-a-panel-without-a-checkout) describes, gets 3.0 with its
  next update. Until the client reads `results_url`, point the panel at the file of the
  `v2.3.1` tag instead. It differs from the 3.0 file only in naming the image `:2.3.1`:

  ```
  https://raw.githubusercontent.com/janschachtschabel/Website-Textextraction-Selenium/v2.3.1/docker-compose.yml
  ```

- A compose file that names `latest` gets 3.0 with the next pull; name `:2.3.1` there
  instead.

## What it buys

- Results as they finish, instead of all of them after the last URL.
- A job holds at most `max_concurrency` results in memory. Before 3.0 it held every one:
  measured at 2000 URLs, 22 to 165 MB per job, depending on the pages.
- A restart no longer takes the finished URLs with it. Their rows stay readable as long as
  the job's record does, and their positions say which URLs to submit again.
