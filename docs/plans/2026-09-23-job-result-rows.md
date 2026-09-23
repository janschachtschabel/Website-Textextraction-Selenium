# Job results as rows

## Goal

Store each URL's result as its own row the moment it finishes, so that a job's results can
be read page by page while it runs, a job holds `max_concurrency` results in memory instead
of all of them, and a restart no longer discards the URLs that were already done.

## Context

`POST /jobs` (0.6.0) keeps every result in memory and writes them as one JSON record when
the batch ends; `GET /jobs/{id}` returns that record. 2.3.0 raised the limits to thousands
of URLs per job and added `progress` counts. Measured on 2026-09-22 against the real schema
classes, 2000 results hold 22 MB at 10 KB of Markdown a page, 63 MB at 30 KB and 165 MB at
80 KB - per job and per worker process, until the job ends, and `MAX_ACTIVE_JOBS`
multiplies it. A container restart after 1800 of 2000 URLs loses all 1800.
`docs/settings.md` lists exactly these as what raising the limits does not solve.

The hook already exists: since 2.3.0, `crawl_batch` holds each finished item in `counted()`
to report progress - and then keeps it until the end.

## Decisions

Taken by the user on 2026-09-23, in the clarification round of this plan:

1. **A finished job reports counts, not results.** `GET /jobs/{id}` carries `progress` and a
   `results_url`; the results are read from `GET /jobs/{id}/results`, while the job runs and
   after. The `result` field goes. This breaks existing clients: **3.0.0**, with a migration
   note.
2. **After a restart, the rows stay readable.** The job is still reported `failed`
   ("Interrupted: the service shut down") or lost, as today. Every row names its `position`
   in the submitted list, so a client resubmits exactly the missing URLs as a new job. The
   service does not resume jobs itself.

Engineering decisions made here, each argued below: rows are numbered in the order URLs
finish; they are written one at a time; each is delivered while its URL still holds its
concurrency slot; a row that cannot be stored fails the job; a page holds 20 rows by
default and 100 at most; `/crawl/batch` does not change.

## Scope

In scope:

- `CrawlService.stream_batch`: deliver each item as it finishes instead of collecting it
- `JobRunner` writes one row per URL and reads pages of them
- `GET /jobs/{job_id}/results?offset=&limit=`
- `results_url` on `JobAccepted` and `JobStatus`; `JobStatus.result` removed
- `job_id` path parameters restricted to the alphabet the service hands out
- README, `docs/settings.md`, `docs/migration-3.0.md`, CHANGELOG, version 3.0.0

Out of scope:

- server-side resume of an interrupted job (decision 2)
- any change to `POST /crawl/batch`
- faster detection of a lost job; it stays at deadline plus 30 seconds
- deleting a job's rows on request, or filtering them (failures only, and the like)
- a storage layout bump: the record key is unchanged and older records stay readable

## Approaches considered

**A - one state-store key per row (chosen).** `job:{id}:row:{n}` in the existing diskcache
state store, `n` counting URLs as they finish. Pages are read by key, without scanning.
Reuses the store's expiry, its multi-process safety and its permission checks. Measured on
2026-09-23: writing a 10-80 KB row takes 0.3 ms at the median and under 0.7 ms at the 95th
percentile; reading a page of 100 takes 8-23 ms. Cost: one key per URL, 2000 for a large
job, in a store without size eviction - the same bytes the single record holds today.

**B - a JSON Lines file per job.** Append each result to a file beside the stores and page
by byte offset. A natural append-only stream, but a second storage mechanism with its own
expiry sweeper, its own cross-process append safety on Windows and Linux, and its own
permission checks - everything the state store already gets right. Rejected: more new code
for no capability A lacks.

**C - flush the growing result list periodically.** Keep one record and rewrite it with all
results so far every few seconds. The smallest diff, but every flush rewrites everything
written before it - 63 MB rewritten again and again for a 2000-URL job - and memory stays
proportional to the list. Rejected: it misses two of the three goals.

**Row numbering.** Numbered by input position, the stored rows would have gaps until every
URL is done, and a reader could not tell "not yet" from "never". Numbered by completion,
the stored rows form a gap-free prefix at every moment: `offset` becomes a cursor, and a
client asks for "everything after row k" without ever skipping one. The input position
travels inside the row.

## Global constraints

From the repository's conventions (CHANGELOG preamble, CI workflow, pyproject, the previous
plan) and this session's rules:

- Python 3.11-3.14. `asyncio.TaskGroup` and `ExceptionGroup` exist from 3.11.
- `ruff check` and `ruff format --check` on `app tests helper/loadtest.py run.py`, with
  mccabe `max-complexity = 10`. A nested function counts toward its parent's complexity.
- Tests run in `.venv` with `HOST=127.0.0.1 PYTHONIOENCODING=utf-8`.
- No new dependencies. Stores use `diskcache.JSONDisk`, never pickle.
- "Versions describe the request/response contract": removing `result` makes this 3.0.0.
- Every new test is red-proved by breaking the feature at its root, not only by watching it
  fail before the code exists. Where a task's tests pass on arrival because an earlier task
  delivered the behaviour, the red-proof is the only proof and is named in the task.
- English in code, comments and docs. Conventional Commits, as the log uses them (no `!`
  marker; a release ends with `docs: release X.Y.Z`). Stage explicit paths; never
  force-push `main`.

## Architecture

### Files

| File | Change | Responsibility afterwards |
|---|---|---|
| `app/service.py` | `stream_batch` added; `_batch_item` delivers when given a callback; `crawl_batch` loses `on_progress` | the per-URL pipeline and both ways of running a list of URLs |
| `app/jobs.py` | `_run` writes rows; `rows()` reads a page; `_keep()` shared by record and rows; no `result` stored | job records and their result rows |
| `app/schemas.py` | `JOB_RESULTS_EXAMPLE`, `JobResultRow`, `JobResults`; `results_url` on `JobAccepted` and `JobStatus`; `JobStatus.result` removed | the request/response contract |
| `app/main.py` | `JobId` path type; `GET /jobs/{job_id}/results`; route texts | the HTTP surface |
| `tests/test_api.py` | three `stream_batch` tests | |
| `tests/test_jobs.py` | fixture gains `hold`; helpers `store_rows`, `all_rows`, `watch_rows`, `rows_when`; tests below | |
| `README.md`, `docs/settings.md`, `docs/migration-3.0.md` (new), `docs/README.md`, `CHANGELOG.md`, `app/__init__.py`, `docker-compose.yml` | documents and version | |

`app/main.py` has 352 lines and grows by about 30; `app/schemas.py` has 420 and grows by
about 25. Neither is split here. Each has one reason to change - the HTTP surface, the
contract - and main.py's routes are already grouped by concern since A21; every addition
here lands in the group it belongs to. Both are the next split candidates, noted rather
than done inside a feature change.

### Data model

State store (`state-{STORAGE_LAYOUT}`, layout unchanged):

| Key | Value | Expires |
|---|---|---|
| `job:{id}` | `status`, `progress{done,succeeded,total}`, `submitted_at`, `deadline_at`, `finished_at`, `error` - no `result` | unfinished: at `deadline_at + JOB_RESULT_TTL`; finished: `JOB_RESULT_TTL` after the last save |
| `job:{id}:row:{n}` | `{"position": int, "url", "success", "result", "error"}` - a `BatchCrawlItemResult` plus its position | at `deadline_at + JOB_RESULT_TTL`, the same instant as the unfinished record |

`n` runs 0, 1, 2, ... in the order URLs finish. An `id` is `secrets.token_urlsafe(16)`,
alphabet `[A-Za-z0-9_-]`, so a row key can never equal another job's record key. A
client-supplied id is held to that alphabet (Task 2), so no request can address a row as if
it were a record.

### Data flow

Writing, per URL:

1. `stream_batch` starts one task per URL in an `asyncio.TaskGroup`; each waits for one of
   `max_concurrency` slots.
2. `_batch_item` crawls, builds the item, and - still holding its slot - awaits
   `deliver(item)`.
3. The runner's `deliver(position, item)` takes an `asyncio.Lock`, writes row
   `n = progress.done`, then sets `progress.done = n + 1`.
4. The throttled record save, at most every `PROGRESS_EVERY_SECONDS`, persists `progress`,
   which never runs ahead of the rows.
5. The item is dropped: `_batch_item` returns `None` and frees the slot.
6. When every URL is delivered, the runner sets `status = "done"` and saves the record. A
   row write that raises propagates instead: the TaskGroup cancels the URLs not yet
   finished, `stream_batch` re-raises the original error, and the job ends `failed` with
   `Job failed (<type>)`.

Reading, `GET /jobs/{id}/results?offset&limit`:

1. Read the record through `status()`, so a lost job reads as `failed`. 404 if absent.
2. Then read rows `offset, offset+1, ...`, at most `limit`, stopping at the first missing
   one, in one thread-pool hop.
3. Answer `status`, `progress`, `results` and `next_offset = offset + len(results)`.

The guarantees a client relies on, and why:

- **The record is read before the rows.** A job becomes `done` or `failed` only after its
  last row is written, so a page that reports the job ended carries complete rows, and an
  empty page from it means the end. Read the other way round, a job that finished between
  the two reads would end a client's loop one page early.
- **A page ends at the first missing row, not at `progress.done`.** The saved count trails
  the rows by up to `PROGRESS_EVERY_SECONDS`, and after a crash it stays behind for good.
- **Delivery happens inside the slot.** With the store stalled - SQLite's busy timeout is 60
  seconds - a delivery outside the slot would let crawling go on while finished items queued
  behind the lock: at fast mode's ~16 URLs a second, about a thousand results in memory.
  Inside the slot, a slow store slows the batch instead, and at most `max_concurrency`
  results exist at any time.
- **Rows are written one at a time.** The lock makes the stored rows a gap-free prefix at
  every moment. It costs nothing measurable: at under 0.7 ms a write, it admits over 1400
  rows a second against at most ~16 URLs a second crawled.

### Interfaces

```python
# app/service.py
class CrawlService:
    async def crawl_batch(self, urls, options, max_concurrency) -> BatchCrawlResponse: ...
    async def stream_batch(self, urls, options, max_concurrency, deliver) -> None: ...
    #   deliver: async (position: int, item: BatchCrawlItemResult) -> None
    async def _batch_item(self, url, options, expires_at, semaphore, started, deliver=None): ...
    #   returns the item, or None once deliver - bound to its position - has taken it

# app/jobs.py
def _row_key(job_id: str, seq: int) -> str: ...
class JobRunner:
    async def rows(self, job_id: str, offset: int, limit: int) -> tuple[dict | None, list[dict]]: ...
    async def _write_row(self, job_id: str, seq: int, row: dict, keep: float) -> None: ...
    def _keep(self, record: dict) -> float: ...

# app/main.py
JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
# GET /jobs/{job_id}/results?offset=0&limit=20 -> JobResults   (offset >= 0, 1 <= limit <= 100)

# app/schemas.py
class JobResultRow(BatchCrawlItemResult): ...  # adds position: int
class JobResults(BaseModel): ...  # job_id, status, progress, results: list[JobResultRow], next_offset: int
class JobAccepted(BaseModel): ...  # adds results_url: str
class JobStatus(BaseModel): ...  # adds results_url: str, loses result
```

### Dependencies

None added. `asyncio.TaskGroup`, `ExceptionGroup`, `functools.partial` and
`typing.Annotated` are standard library. `fastapi.Query` and `Path(pattern=...)` are in the
installed FastAPI: verified on 0.141.1, two routes sharing one `Annotated` alias both answer
422 to an id with a colon.

## Non-functional

- **Performance.** One row write per URL replaces one record write of 22-165 MB at the end
  of a large job. Figures above.
- **Memory.** At most `max_concurrency` results per job exist at a time - at most 10, by
  the request model - whatever the length of the list. Proven by Task 4's test and measured
  end to end in the verification plan.
- **Disk.** Unchanged in total: the rows hold what the single record held, in the same
  store, until they expire. The store has no size eviction, as today.
- **Security.** The results route takes the same key as every job route. `job_id` is held to
  the token alphabet on both job routes (Task 2). A page carries at most 100 results of up
  to the caller's own `max_bytes` each - the caller's budget, by the reasoning that closed
  B19. `offset` needs no upper bound: past the last row a page costs one lookup.
- **Observability.** A row that cannot be written ends the job with `Job failed (<type>)`
  in the log and the record, like any job failure today. A failing progress save still logs
  a warning and does not fail the job.

## Risks

| Risk | Mitigation |
|---|---|
| Clients that read `result` break | 3.0.0 with `docs/migration-3.0.md`; publishing waits until the client is migrated or the panel pins `2.3.1` (see Release) |
| Results of jobs that finished shortly before the upgrade become unreachable | Migration note: collect them before upgrading. They expire within `JOB_RESULT_TTL` anyway |
| Cancelling pending URLs interrupts browser work | It is the path an expiring deadline already takes, and B01/B06 made it clean up. Task 1's stop test runs it |
| The gc-based memory test counts nothing on some runtime and passes vacuously | Its red-proof retains every item and must see the count rise. If it does not rise, the test is broken, and that is a finding |
| A reader implemented rows-first would end one page early | `rows()` reads the record first and says why in its docstring; review item |
| Rows of a job that finishes early outlive its record until the original deadline plus the TTL | Fixed in review: both ends of a job touch every row to the record's expiry (see Implementation notes) |

## Open questions

None. The two questions that change the contract were answered on 2026-09-23.

## Tasks

Nine tasks in three phases. The order follows two constraints: the id check (Task 2) must
exist before any row does, because a row key is an id plus a suffix; and the tests that
read `result` today can only move to the rows route once it exists (Task 3 before Task 4).

Verification commands used throughout:

```bash
HOST=127.0.0.1 PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check app tests helper/loadtest.py run.py
.venv/Scripts/python.exe -m ruff format --check app tests helper/loadtest.py run.py
```

### Phase 1 - the service delivers instead of collecting

**Step 0: invoke `/better-coding-workflow`** (skills unload; reload before coding).

#### Task 1: `stream_batch` hands each item over inside its slot

**Files:**
- Modify: `app/service.py` - imports, `_batch_item`, new `stream_batch` after `crawl_batch`
- Test: `tests/test_api.py`, after `test_batch_pipeline_runs_without_the_http_layer`

**What:** A second way to run a list of URLs. Each finished item goes to a callback with its
position, while the URL still holds its slot, and nothing is collected. A failing callback
fails the batch with the original error and cancels what has not started.

**Steps:**

- [ ] Write the failing tests:

```python
async def test_a_streamed_batch_delivers_every_url_once_with_its_position(api):
    from app.schemas import BatchCrawlRequest, resolve_options

    _, _, resources = api
    urls = ["https://example.com/a", "https://example.com/blocked", "https://example.com/a"]
    options = resolve_options(BatchCrawlRequest(urls=urls), resources.config)
    delivered = []

    async def deliver(position, item):
        delivered.append((position, item.url, item.success))

    assert await resources.service.stream_batch(urls, options, 2, deliver) is None
    assert sorted(delivered) == [(0, urls[0], True), (1, urls[1], False), (2, urls[2], True)]


async def test_a_streamed_batch_delivers_inside_its_concurrency_slot(api):
    """A slow result store has to hold the batch back. Delivered outside the slot, the next
    URL would start while this one waits, and finished results would pile up in memory
    behind a stalled store - the case streaming exists to prevent."""
    from app.schemas import BatchCrawlRequest, resolve_options

    _, state, resources = api
    urls = [f"https://example.com/{n}" for n in range(3)]
    options = resolve_options(BatchCrawlRequest(urls=urls), resources.config)
    started = []

    async def deliver(position, item):
        before = state["calls"]
        await asyncio.sleep(0.1)
        started.append(state["calls"] - before)

    await resources.service.stream_batch(urls, options, 1, deliver)
    assert started == [0, 0, 0], f"crawls started while a result was being delivered: {started}"


async def test_a_failing_delivery_fails_the_batch_and_stops_the_rest(api):
    """A result with nowhere to go makes crawling on pointless. The caller must be able to
    name what went wrong, so the original error comes out, not an ExceptionGroup."""
    from app.schemas import BatchCrawlRequest, resolve_options

    _, state, resources = api
    urls = [f"https://example.com/{n}" for n in range(8)]
    options = resolve_options(BatchCrawlRequest(urls=urls), resources.config)

    async def deliver(position, item):
        raise OSError("result store unavailable")

    with pytest.raises(OSError, match="result store unavailable"):
        await resources.service.stream_batch(urls, options, 1, deliver)
    await asyncio.sleep(0.5)  # time enough for URLs nobody cancelled to be crawled anyway
    assert state["calls"] <= 2, f"{state['calls']} of 8 URLs were crawled after the first result failed"
```

- [ ] Run `pytest tests/test_api.py -q -k streamed_batch or failing_delivery`. Expected:
  three failures, `AttributeError: 'CrawlService' object has no attribute 'stream_batch'`.
- [ ] Implement. Imports in `app/service.py` become:

```python
import asyncio
import time
from functools import partial
from urllib.parse import urlsplit
```

`_batch_item` becomes:

```python
    async def _batch_item(self, url, options, expires_at, semaphore, started, deliver=None):
        deadline = Deadline.at(expires_at)
        acquired = False
        try:
            try:
                await deadline.run(semaphore.acquire())
                acquired = True
                result = await self.crawl(url, options, deadline)
                item = BatchCrawlItemResult(
                    url=url,
                    success=result.success,
                    result=result,
                    error=None if result.success else failure_reason(result),
                )
            except CrawlError as exc:
                if not acquired:
                    # crawl() records the attempt itself; a queue failure never reaches it.
                    await self._record(time.monotonic() - started, False)
                item = BatchCrawlItemResult(url=url, success=False, error=str(exc))
            if deliver is None:
                return item
            # Still inside the slot, so a slow result store holds the batch back instead of
            # letting finished results pile up in memory behind it.
            await deliver(item)
            return None
        finally:
            if acquired:
                semaphore.release()
```

`stream_batch`, after `crawl_batch`:

```python
    async def stream_batch(self, urls, options, max_concurrency, deliver) -> None:
        """Crawl like crawl_batch, but hand each item to deliver(position, item) as it
        finishes instead of collecting it, so a job holds max_concurrency results at a time
        rather than all of them. A delivery that fails fails the batch and cancels the URLs
        still pending: their results would have nowhere to go."""
        started = time.monotonic()
        # All deadlines start at batch admission, including time behind max_concurrency.
        expires_at = started + options.timeout_ms / 1000
        semaphore = asyncio.Semaphore(max_concurrency)
        try:
            async with asyncio.TaskGroup() as group:
                for position, url in enumerate(urls):
                    handoff = partial(deliver, position)
                    group.create_task(self._batch_item(url, options, expires_at, semaphore, started, handoff))
        except ExceptionGroup as failed:
            raise failed.exceptions[0] from failed
```

- [ ] Run the three tests: expected `3 passed`. Run the whole suite and ruff: green, and
  `_batch_item` stays under the complexity gate (5 expected).
- [ ] Red-proofs, each reverted afterwards:
  - deliver after the slot is released (move `await deliver(item)` behind the `finally`):
    the slot test fails with `[1, 1, 0]` or similar;
  - replace the TaskGroup with `asyncio.gather`: the stop test fails with `8 of 8 URLs`;
  - re-raise the group unchanged: `pytest.raises(OSError)` fails on `ExceptionGroup`.
- [ ] Commit `app/service.py tests/test_api.py`:
  `feat: stream a batch to a callback instead of collecting it`

**Verification:** `pytest tests/test_api.py -q` green; ruff clean.
**Rollback:** `git revert` the commit. Nothing calls `stream_batch` yet.

### Phase 2 - jobs store rows

**Step 0: invoke `/better-coding-workflow`.**

#### Task 2: a job id is only what the service hands out

**Files:**
- Modify: `app/main.py` - import `Annotated`; `JobId` before `_job_routes`; `job_status`'s parameter
- Test: `tests/test_jobs.py`

**What:** `GET /jobs/{job_id}` takes only `[A-Za-z0-9_-]{1,64}`. Today `/jobs/abc:row:0`
answers a harmless 404; once rows exist, it would read a row as a record and fail with 500.

**Steps:**

- [ ] Write the failing test (Task 3 adds the second path to it):

```python
@pytest.mark.parametrize("path", ["/jobs/abc:row:0"])
async def test_a_job_id_is_only_what_the_service_hands_out(jobs_api, path):
    """The id becomes part of a store key. One with a colon could name a key that is not a
    job record - a result row, once rows exist - so it is refused at the door."""
    async with jobs_api() as (client, _):
        assert (await client.get(path)).status_code == 422
```

- [ ] Run it. Expected: `assert 404 == 422`.
- [ ] Implement. `from typing import Annotated` joins the standard-library imports after
  `from contextlib import asynccontextmanager`. Before `def _job_routes`:

```python
# The id becomes part of a state-store key, so it may only be what token_urlsafe produces:
# with a colon it could address a result row as if it were a job record.
JobId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
```

  and `async def job_status(job_id: str = Path(max_length=64)):` becomes
  `async def job_status(job_id: JobId):`.
- [ ] Run it: passes. `test_unknown_jobs_answer_404` (`/jobs/not-a-job`) still passes.
- [ ] Commit `app/main.py tests/test_jobs.py`:
  `fix: hold job ids to the alphabet the service hands out`

**Rollback:** `git revert`.

#### Task 3: read a job's rows page by page

**Files:**
- Modify: `app/jobs.py` - `_row_key` after `_now`; `JobRunner.rows` after `status`
- Modify: `app/schemas.py` - `JOB_RESULTS_EXAMPLE` after `JOB_STATUS_EXAMPLE`; `JobResultRow`, `JobResults` after `JobStatus`
- Modify: `app/main.py` - import `Query` and `JobResults`; route after `job_status`
- Test: `tests/test_jobs.py`

**What:** The read half, tested against rows placed in the store the way the runner will
write them. Nothing writes rows yet.

**Steps:**

- [ ] Write the failing tests. Imports: `from app.jobs import KEY, _row_key`. Add
  `"/jobs/abc:row:0/results"` to the id test's parameters.

```python
def store_rows(resources, job_id, count, done=None):
    """A running job's record and its first count rows, as the runner leaves them: numbered
    as they finished, each naming its URL's place in the request."""
    resources.state.set(
        KEY + job_id,
        {
            "status": "running",
            "progress": {"done": count if done is None else done, "succeeded": 0, "total": 5},
            "submitted_at": "2026-09-23T08:00:00+00:00",
            "deadline_at": time.time() + 600,
            "finished_at": None,
            "error": None,
        },
    )
    for seq in range(count):
        position = 4 - seq
        row = {"position": position, "url": f"https://example.com/{position}", "success": False, "error": "x"}
        resources.state.set(_row_key(job_id, seq), row)


async def test_results_are_paged_in_the_order_they_finished(jobs_api):
    async with jobs_api() as (client, resources):
        store_rows(resources, "paged", 5)
        first = (await client.get("/jobs/paged/results?limit=2")).json()
        rest = (await client.get(f"/jobs/paged/results?offset={first['next_offset']}&limit=10")).json()
    assert [row["position"] for row in first["results"]] == [4, 3] and first["next_offset"] == 2
    assert [row["position"] for row in rest["results"]] == [2, 1, 0] and rest["next_offset"] == 5
    assert rest["status"] == "running" and rest["progress"]["total"] == 5


async def test_an_empty_page_keeps_the_offset_so_the_next_poll_resumes_there(jobs_api):
    async with jobs_api() as (client, resources):
        store_rows(resources, "waiting", 2)
        page = (await client.get("/jobs/waiting/results?offset=2")).json()
    assert page["results"] == [] and page["next_offset"] == 2


async def test_a_page_reads_every_row_written_although_the_saved_count_trails(jobs_api):
    """The record's count is saved at most every PROGRESS_EVERY_SECONDS, and after a crash
    it stays behind the rows for good. A page bounded by it would lose the difference."""
    async with jobs_api() as (client, resources):
        store_rows(resources, "trailing", 3, done=1)
        page = (await client.get("/jobs/trailing/results")).json()
    assert len(page["results"]) == 3


async def test_results_of_an_unknown_job_answer_404(jobs_api):
    async with jobs_api() as (client, _):
        response = await client.get("/jobs/not-a-job/results")
    assert response.status_code == 404 and response.json()["detail"] == "Unknown or expired job"


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
async def test_a_page_outside_its_bounds_is_refused(jobs_api, query):
    async with jobs_api() as (client, resources):
        store_rows(resources, "bounded", 1)
        assert (await client.get(f"/jobs/bounded/results?{query}")).status_code == 422
```

- [ ] Run them. Expected: failures on `404 != 200` and `KeyError` - the route does not exist.
- [ ] Implement. `app/jobs.py`, after `_now`:

```python
def _row_key(job_id: str, seq: int) -> str:
    """Rows are numbered from 0 in the order their URLs finished."""
    return f"{KEY}{job_id}:row:{seq}"
```

  and in `JobRunner`, after `status`:

```python
    async def rows(self, job_id: str, offset: int, limit: int) -> tuple[dict | None, list[dict]]:
        """The job's record, then up to limit of its rows from offset on.

        The record is read first. A job is marked done or failed only after its last row is
        written, so the rows read after a record that says so are complete, and an empty
        page then means the end. The page stops at the first row that does not exist rather
        than at the record's count: that is saved at most every PROGRESS_EVERY_SECONDS, and
        after a crash it trails the rows for good."""
        record = await self.status(job_id)
        if record is None:
            return None, []
        state = self.resources.state

        def read():
            page = []
            for seq in range(offset, offset + limit):
                row = state.get(_row_key(job_id, seq))
                if row is None:
                    break
                page.append(row)
            return page

        return record, await self.resources.io(read)
```

  `app/schemas.py`, after `JOB_STATUS_EXAMPLE`:

```python
JOB_RESULTS_EXAMPLE = {
    "job_id": JOB_EXAMPLE["job_id"],
    "status": "running",
    "progress": {"done": 1, "succeeded": 1, "total": 2},
    "results": [{"position": 0, **ITEM_EXAMPLE}],
    "next_offset": 1,
}
```

  and after `JobStatus`:

```python
class JobResultRow(BatchCrawlItemResult):
    model_config = ConfigDict(json_schema_extra={"examples": [{"position": 0, **ITEM_EXAMPLE}]})
    position: int = Field(description="Index of this URL in the list the job was given")


class JobResults(BaseModel):
    """URLs a job has finished, in the order they finished - which is not the order they
    were submitted in; each row's position says which URL of the request it was."""

    model_config = ConfigDict(json_schema_extra={"examples": [JOB_RESULTS_EXAMPLE]})
    job_id: str = Field(description="Identifier of the job")
    status: Literal["queued", "running", "done", "failed"] = Field(description="Where the job stands")
    progress: JobProgress | None = Field(None, description="How many URLs are done")
    results: list[JobResultRow] = Field(description="Rows from offset on, at most limit of them")
    next_offset: int = Field(
        description="The offset for the next page. An empty page from a job that is no longer "
        "queued or running means there is nothing more to read"
    )
```

  `app/main.py`: `Query` joins the `fastapi` import, `JobResults` the `.schemas` import.
  In `_job_routes`, after `job_status`:

```python
    @application.get(
        "/jobs/{job_id}/results",
        response_model=JobResults,
        dependencies=[Security(check_auth)],
        summary="Read a job's results as they finish",
    )
    async def job_results(
        job_id: JobId,
        offset: int = Query(0, ge=0, description="Rows already read: the previous page's next_offset"),
        limit: int = Query(20, ge=1, le=100, description="Rows in this page"),
    ):
        """Page through the URLs a job has finished, while it runs and after.

        Rows come in the order the URLs finished; `position` names the URL of the request.
        Start at `offset=0` and pass each page's `next_offset` on. An empty page from a job
        that is still running means nothing new yet; from one that has ended, nothing more.
        A page holds at most `limit` results of up to `max_bytes` each. Answers 404 when
        the id is unknown or its record has expired."""
        record, rows = await application.state.resources.jobs.rows(job_id, offset, limit)
        if record is None:
            raise HTTPException(404, "Unknown or expired job")
        return JobResults(
            job_id=job_id,
            status=record["status"],
            progress=record.get("progress"),
            results=rows,
            next_offset=offset + len(rows),
        )
```

- [ ] Run them: pass. Suite and ruff green.
- [ ] Red-proof: bound the loop by `record["progress"]["done"]` instead of the first missing
  row. `test_a_page_reads_every_row_written_although_the_saved_count_trails` must fail with
  `1 != 3`. Revert.
- [ ] Commit `app/jobs.py app/schemas.py app/main.py tests/test_jobs.py`:
  `feat: read a job's result rows page by page`

**Rollback:** `git revert`; the route is new and unused by anything else.

#### Task 4: a job writes a row per URL and keeps none of them

**Files:**
- Modify: `app/jobs.py` - `submit`'s record, `_run`, new `_write_row` and `_keep`, `_save`
- Test: `tests/test_jobs.py` - helpers `all_rows` and `watch_rows`; three tests move off `result`; three new tests

**What:** `_run` switches from `crawl_batch` to `stream_batch`. Each item becomes row
`progress.done` under a lock, then the count moves on; the record no longer carries
`result`. A row that cannot be written fails the job.

**Steps:**

- [ ] Write the tests. Imports: `import gc`, `from app.schemas import BatchCrawlItemResult`.

```python
async def all_rows(client, job_id):
    """Every row of a job that has ended, paged the way a client reads them."""
    rows, offset = [], 0
    while True:
        page = (await client.get(f"/jobs/{job_id}/results?offset={offset}&limit=100")).json()
        if not page["results"]:
            return rows
        rows += page["results"]
        offset = page["next_offset"]


def watch_rows(monkeypatch, jobs, observe):
    """Call observe(seq, row) as each row is written, then write it."""
    original = jobs._write_row

    async def spy(job_id, seq, row, keep):
        observe(seq, row)
        await original(job_id, seq, row, keep)

    monkeypatch.setattr(jobs, "_write_row", spy)


async def test_each_url_becomes_a_row_numbered_as_it_finishes(jobs_api, monkeypatch):
    """Rows count up without gaps in the order URLs finish, and each names its URL's place
    in the request - the only way to tell two identical URLs apart."""
    async with jobs_api() as (client, resources):
        seen = []
        watch_rows(monkeypatch, resources.jobs, lambda seq, row: seen.append((seq, row["position"])))
        urls = [f"https://example.com/{n % 3}" for n in range(6)]
        job = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 3})).json()
        await finished(client, job["status_url"])
    assert [seq for seq, _ in seen] == list(range(6)), seen
    assert sorted(position for _, position in seen) == list(range(6)), seen


async def test_a_job_holds_no_more_results_than_it_crawls_at_once(jobs_api, monkeypatch):
    """What the rows are for: memory follows max_concurrency, not the length of the list.
    Counted directly - result objects alive as each row is written - rather than through
    allocator statistics, which measure far more than this. Counted against a baseline
    taken just before the job, so a result some earlier test still holds does not count."""

    def results_alive():
        return sum(type(obj) is BatchCrawlItemResult for obj in gc.get_objects())

    async with jobs_api() as (client, resources):
        gc.collect()
        before = results_alive()
        alive = []
        watch_rows(monkeypatch, resources.jobs, lambda seq, row: alive.append(results_alive() - before))
        urls = [f"https://example.com/{n}" for n in range(12)]
        job = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 2})).json()
        await finished(client, job["status_url"])
    assert len(alive) == 12 and max(alive) <= 2, f"results alive as each row was written: {alive}"


async def test_a_result_that_cannot_be_stored_fails_the_job(jobs_api, monkeypatch):
    """Unlike progress, a row is the job's output. Losing one quietly would report a job
    done that is missing results."""
    async with jobs_api() as (client, resources):

        async def refuse(job_id, seq, row, keep):
            raise OSError("disk full")

        monkeypatch.setattr(resources.jobs, "_write_row", refuse)
        urls = [f"https://example.com/{n}" for n in range(4)]
        job = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 1})).json()
        body = await finished(client, job["status_url"])
    assert body["status"] == "failed" and body["error"] == "Job failed (OSError)"
```

  The three tests that read `result` move to the rows. The first is renamed, because what
  it proves changes with the contract:

```python
async def test_a_job_runs_in_the_background_and_reports_every_url(jobs_api):
    async with jobs_api() as (client, _):
        urls = ["https://example.com/a", "https://example.com/blocked"]
        accepted = await client.post("/jobs", json={"urls": urls, "mode": "fast"})
        assert accepted.status_code == 202
        job = accepted.json()
        assert job["status"] == "queued" and job["status_url"] == f"/jobs/{job['job_id']}"
        done = await finished(client, job["status_url"])
        assert done["status"] == "done" and done["finished_at"] and done["error"] is None
        assert done["progress"] == {"done": 2, "succeeded": 1, "total": 2}
        rows = {row["position"]: row for row in await all_rows(client, job["job_id"])}
    assert sorted(rows) == [0, 1] and rows[0]["success"]
    assert rows[1]["error"] == "Upstream status 429"
```

  In `test_progress_counts_successes_apart_from_failures`, the `body["result"]` line becomes:

```python
        rows = await all_rows(client, accepted["job_id"])
        assert sorted(row["success"] for row in rows) == [False, True, True]
```

  In `test_a_failing_progress_write_does_not_fail_the_job`, `assert body["result"]["succeeded"] == 3`
  becomes `assert len(await all_rows(client, accepted["job_id"])) == 3`. The orphan record
  in `test_a_job_past_its_deadline_without_a_result_is_reported_lost` loses its
  `"result": None`, matching the new record.

- [ ] Run `pytest tests/test_jobs.py -q`. Expected: the three new tests fail with
  `AttributeError: ... has no attribute '_write_row'`; the three moved tests fail on an
  empty row list - nothing writes rows yet.
- [ ] Implement in `app/jobs.py`. In `submit`, the record loses `"result": None`. `_run`,
  `_write_row`, `_keep` and `_save` become:

```python
    async def _run(self, job_id, record, urls, options, max_concurrency):
        record["status"] = "running"
        await self._save(job_id, record)
        written = time.monotonic()
        order = asyncio.Lock()

        async def deliver(position, item):
            nonlocal written
            async with order:  # numbered as they finish, so the stored rows are a gap-free prefix
                done = record["progress"]["done"]
                row = {"position": position, **item.model_dump(mode="json")}
                await self._write_row(job_id, done, row, self._keep(record))
                record["progress"] = {
                    "done": done + 1,
                    "succeeded": record["progress"]["succeeded"] + item.success,
                    "total": len(urls),
                }
            if time.monotonic() - written < PROGRESS_EVERY_SECONDS:
                return
            written = time.monotonic()
            try:
                await self._save(job_id, record)
            except Exception as exc:
                # Progress is incidental; the rows are the job. A busy state store must not
                # turn a batch that succeeded into a failure - the rule B08 set for metrics.
                logger.warning("Job progress not recorded ({})", type(exc).__name__)

        try:
            await self.resources.service.stream_batch(urls, options, max_concurrency, deliver)
            record["status"] = "done"
        except Exception as exc:
            logger.error("Job failed ({})", type(exc).__name__)
            record.update(status="failed", error=f"Job failed ({type(exc).__name__})")
        record["finished_at"] = _now()
        await self._save(job_id, record)

    async def _write_row(self, job_id, seq, row, keep):
        """Not caught: a result that cannot be stored fails the job."""
        await self.resources.io(self.resources.state.set, _row_key(job_id, seq), row, expire=keep)

    def _keep(self, record):
        """Seconds a record or row stays. Unfinished work outlives its deadline long enough to
        be reported as lost; a row written while its job runs expires with the record."""
        keep = self.config.job_result_ttl
        if record["status"] in UNFINISHED:
            keep += max(0, record["deadline_at"] - time.time())
        return keep

    async def _save(self, job_id, record):
        await self.resources.io(self.resources.state.set, KEY + job_id, record, expire=self._keep(record))
```

- [ ] Run: all of `tests/test_jobs.py` passes, including the progress and throttle tests
  from 2.3.x unchanged. Suite and ruff green; `_run` stays under the gate (4 expected).
- [ ] Red-proofs, each reverted:
  - retain every item (`kept.append(item)` in a wrapper around `deliver` in
    `stream_batch`): the memory test must fail with the count rising towards 12. If the
    count stays at 0-2, the gc count sees nothing and the test is broken - stop and fix it;
  - drop the lock and add `await asyncio.sleep(0.01)` between reading `done` and writing,
    as a slow store would: the numbering test fails on repeated numbers;
  - re-raise the ExceptionGroup unchanged in `stream_batch`: the store test fails with
    `Job failed (ExceptionGroup)`.
- [ ] Commit `app/jobs.py tests/test_jobs.py`:
  `feat: a job stores each result as it finishes and keeps none in memory`

**Rollback:** `git revert`; Task 3's route then reads no rows, and `JobStatus.result`
(still present until Task 6) is filled again.

#### Task 5: `crawl_batch` collects, and only collects

**Files:** Modify `app/service.py`.

**What:** `on_progress` and `counted` have no caller after Task 4. `crawl_batch` returns to
the shape it had before 2.3.0. Behaviour-preserving: `test_batch_pipeline_runs_without_the_http_layer`
and the `/crawl/batch` route tests cover it before and after.

**Steps:**

- [ ] Confirm there is no caller: `grep -rn on_progress app tests` finds only `crawl_batch`.
- [ ] Replace `crawl_batch` with:

```python
    async def crawl_batch(self, urls, options, max_concurrency) -> BatchCrawlResponse:
        started = time.monotonic()
        # All deadlines start at batch admission, including time behind max_concurrency.
        expires_at = started + options.timeout_ms / 1000
        semaphore = asyncio.Semaphore(max_concurrency)
        results = await asyncio.gather(
            *(self._batch_item(url, options, expires_at, semaphore, started) for url in urls)
        )
        succeeded = sum(item.success for item in results)
        return BatchCrawlResponse(
            total=len(results),
            succeeded=succeeded,
            failed=len(results) - succeeded,
            results=results,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
```

- [ ] Suite and ruff green.
- [ ] Commit `app/service.py`: `refactor: drop the progress callback crawl_batch no longer needs`

**Rollback:** `git revert`.

#### Task 6: the job record points to its results instead of carrying them

**Files:**
- Modify: `app/schemas.py` - `JOB_EXAMPLE`, `JOB_STATUS_EXAMPLE`, `JobAccepted`, `JobStatus`
- Modify: `app/main.py` - `submit_job` and `job_status` routes; their docstrings; `DESCRIPTION`
- Test: `tests/test_jobs.py`

**What:** The contract change of decision 1. `result` leaves `JobStatus`; `results_url`
joins both job answers.

**Steps:**

- [ ] Write the failing test:

```python
async def test_a_job_points_to_its_results_from_the_start(jobs_api):
    async with jobs_api() as (client, _):
        job = (await client.post("/jobs", json={"urls": ["https://example.com/a"]})).json()
        body = await finished(client, job["status_url"])
    assert job["results_url"] == f"/jobs/{job['job_id']}/results"
    assert body["results_url"] == job["results_url"] and "result" not in body
```

- [ ] Run it. Expected: `KeyError: 'results_url'`.
- [ ] Implement. `app/schemas.py`:

```python
JOB_EXAMPLE = {
    "job_id": "6l2fSU_R43ujmWd46UmHQA",
    "status": "queued",
    "status_url": "/jobs/6l2fSU_R43ujmWd46UmHQA",
    "results_url": "/jobs/6l2fSU_R43ujmWd46UmHQA/results",
}
```

  In `JOB_STATUS_EXAMPLE`, `"result": BATCH_ANSWER_EXAMPLE,` becomes
  `"results_url": JOB_EXAMPLE["results_url"],`. `JobAccepted` gains

```python
    results_url: str = Field(description="Path to read the results from, as they finish")
```

  and `JobStatus` loses its `result` field and gains

```python
    results_url: str = Field(description="Path to read the results from, while the job runs and after")
```

  `app/main.py`, in `submit_job`:

```python
        return JobAccepted(
            job_id=job_id, status="queued", status_url=f"/jobs/{job_id}", results_url=f"/jobs/{job_id}/results"
        )
```

  and in `job_status`:

```python
        fields = {name: record.get(name) for name in JobStatus.model_fields if name not in {"job_id", "results_url"}}
        return JobStatus(job_id=job_id, results_url=f"/jobs/{job_id}/results", **fields)
```

  Texts. `job_status`'s docstring becomes:

```
        """Report how far a job has got, and where its results are.

        The results themselves are read from `results_url`, while the job runs and after.
        Answers 404 when the id is unknown or its record has expired."""
```

  `submit_job`'s last sentence, "Poll `status_url`; the record stays readable for an hour
  after the job finishes.", becomes "Poll `status_url` for progress and read the results
  from `results_url` as they finish; both stay readable for `JOB_RESULT_TTL` after the job
  ends." In `DESCRIPTION`, "is polled at `GET /jobs/{job_id}`, which reports how many URLs
  are finished while it runs" becomes "is polled at `GET /jobs/{job_id}`; its results
  arrive page by page at `GET /jobs/{job_id}/results` as the URLs finish".
- [ ] Run: passes. Suite green - including
  `test_the_published_schema_names_the_configured_limit_not_a_constant`, which reads the
  `POST /jobs` texts and must still find no "50" in them.
- [ ] Commit `app/schemas.py app/main.py tests/test_jobs.py`:
  `feat: a job record points to its results instead of carrying them`

**Rollback:** `git revert`.

#### Task 7: rows outlive the process that wrote them

**Files:** Test `tests/test_jobs.py` - the `jobs_api` fixture gains `hold`; helper `rows_when`; two tests.

**What:** Decision 2, proven. No application code is expected to change: Tasks 3 and 4
deliver the behaviour. So the red-proof is the only proof, and it is mandatory.

**Steps:**

- [ ] In the fixture, `start(delay=0.0, **overrides)` becomes `start(delay=0.0, hold=None, **overrides)`,
  and the upstream's first lines become:

```python
        async def upstream(request):
            await asyncio.sleep(delay)
            if hold is not None and request.url.path == "/held":
                await hold.wait()
```

- [ ] Write the tests:

```python
async def rows_when(client, job_id, count, seconds=10):
    """Poll a running job's rows until count of them exist."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rows = (await client.get(f"/jobs/{job_id}/results?limit=100")).json()["results"]
        if len(rows) >= count:
            return rows
        await asyncio.sleep(0.05)
    pytest.fail(f"fewer than {count} rows appeared")


async def test_rows_written_before_a_shutdown_stay_readable(jobs_api):
    """A restart still ends the job, but no longer takes the finished URLs with it. Their
    positions tell a client exactly which URLs to submit again."""
    urls = ["https://example.com/a", "https://example.com/b", "https://example.com/held"]
    async with jobs_api(hold=asyncio.Event()) as (client, _):
        job = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 1})).json()
        await rows_when(client, job["job_id"], 2)
    async with jobs_api() as (client, _):  # the next process, on the same store
        body = (await client.get(job["status_url"])).json()
        rows = await all_rows(client, job["job_id"])
    assert body["status"] == "failed" and body["error"] == "Interrupted: the service shut down"
    assert sorted(row["position"] for row in rows) == [0, 1]


async def test_a_reader_sees_every_finished_row_before_the_count_is_saved(jobs_api, monkeypatch):
    """The saved count trails the rows by up to PROGRESS_EVERY_SECONDS. Here it is held at 0
    on purpose; the rows must be readable regardless."""
    monkeypatch.setattr("app.jobs.PROGRESS_EVERY_SECONDS", 3600.0)
    hold = asyncio.Event()
    urls = ["https://example.com/a", "https://example.com/b", "https://example.com/held"]
    async with jobs_api(hold=hold) as (client, _):
        job = (await client.post("/jobs", json={"urls": urls, "max_concurrency": 1})).json()
        rows = await rows_when(client, job["job_id"], 2)
        status = (await client.get(job["status_url"])).json()
        hold.set()
    assert status["progress"]["done"] == 0, "the throttle was meant to hold the saved count back"
    assert sorted(row["position"] for row in rows) == [0, 1]
```

- [ ] Run: both pass.
- [ ] Red-proof: bound `rows()` by `record["progress"]["done"]`. Both tests must fail - the
  first finds no rows after the restart (its record was last saved with `done: 0`), the
  second times out in `rows_when`. Revert.
- [ ] Commit `tests/test_jobs.py`: `test: rows outlive the process that wrote them`

**Rollback:** `git revert`.

### Phase 3 - documents and version

**Step 0: invoke `/better-coding-workflow`.**

#### Task 8: document reading results page by page

**Files:** `README.md` ("Background jobs"), `docs/settings.md` (bulk section, `JOB_RESULT_TTL`
row), `docs/migration-3.0.md` (new), `docs/README.md` ("The rest").

**What each must say:**

README, "Background jobs":
- `POST /jobs` answers 202 with `job_id`, `status_url` and `results_url`.
- `status_url` reports `status` and `progress`; it no longer carries results.
- `results_url` pages through finished URLs as they finish: `offset` from 0, `limit` 20 by
  default and 100 at most, each page's `next_offset` passed on. Rows come in finishing
  order; `position` is the URL's index in the request. An empty page from a job that is
  still running means nothing new yet; from one that has ended, nothing more.
- A job holds at most `max_concurrency` results in memory, whatever the list's length.
- After a restart the job reads `failed` ("Interrupted: the service shut down") or lost, as
  before; the rows it wrote stay readable until its deadline plus `JOB_RESULT_TTL`. Submit
  the URLs whose positions are missing as a new job.
- The removed sentence "Per-URL results arrive with the finished batch, not before." and the
  words "`done` with the batch `result`" go.
- Commands, exactly:

```bash
curl "http://127.0.0.1:8000/jobs/<job_id>/results?offset=0&limit=100" -H "Authorization: Bearer $API_KEY"
```

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

`docs/settings.md`, "Crawling in bulk": the list of "three things this does not do"
becomes what is true now - results are stored per URL as they finish and read at
`results_url`; a job holds at most `max_concurrency` results in memory, where before 3.0.0
it held all of them (the measured 22/63/165 MB stays as the reason); the one limit left is
that a restart still ends an unfinished job, whose finished rows stay readable, with
`position` saying which URLs to submit again. The `JOB_RESULT_TTL` row reads: "Seconds a
finished job's record and its result rows stay readable."

`docs/migration-3.0.md`, following `docs/migration-0.3.md`'s form:
- who is affected: clients that read `result` from `GET /jobs/{job_id}`; `/crawl/batch`
  callers are not;
- what changed: `result` removed; `results_url` on `POST /jobs` and `GET /jobs/{job_id}`;
  `GET /jobs/{job_id}/results?offset&limit`;
- how to migrate: the Python loop above; map rows back with `position`, since they come in
  finishing order; `result.total` is `progress.total`, `result.succeeded` is
  `progress.succeeded`, `result.failed` is `progress.done - progress.succeeded`,
  `result.elapsed_ms` is `finished_at - submitted_at` to the second;
- before upgrading: collect the results of finished jobs - the new service has no field to
  return them in; unfinished jobs end with the restart anyway;
- a panel that updates to `latest`: pin `jschachtschabel/website-textextraction:2.3.1` in
  its compose file until the client is migrated.

`docs/README.md`, "The rest": one line linking `migration-3.0.md`, described as "reading job
results page by page instead of from one record".

**Verification:** every relative link resolves (the link check from the 2.3.1 round);
`pytest tests/test_packaging.py -q` green - no setting was added, so the settings guards
hold unchanged.
**Commit:** `README.md docs/settings.md docs/migration-3.0.md docs/README.md`:
`docs: job results are read page by page`

#### Task 9: release 3.0.0 in the repository

**Files:** `app/__init__.py` (`"3.0.0"`), `docker-compose.yml` (image `:3.0.0`), `CHANGELOG.md`.

**What:** The version and the entry. Nothing is tagged or published here - see Release.

The CHANGELOG entry, `## 3.0.0 - <release date>`, carries:
- `### Changed - a job's results are read as they finish` - breaking: `result` removed,
  `results_url` and the results route added; why, with the measured 22/63/165 MB and the
  restart that lost all 1800 of 2000; the two decisions and their reasons; a pointer to
  `docs/migration-3.0.md`.
- `### Fixed - a job id is only what the service hands out` - the 422, and the 500 it
  prevents once rows exist.
- that a result which cannot be stored fails the job, where a progress write that fails
  still does not; and that `/crawl/batch` is unchanged.

**Verification:** full suite (`test_the_compose_image_is_the_one_the_workflow_publishes_at_this_version`
binds the compose tag to the version); ruff; link check.
**Commit:** `app/__init__.py docker-compose.yml CHANGELOG.md`: `docs: release 3.0.0`

## Verification plan

| Requirement | Verified by | Success | Failure looks like |
|---|---|---|---|
| Results readable while the job runs | `test_a_reader_sees_every_finished_row_before_the_count_is_saved` | rows 0 and 1 read while URL 2 is held and the saved count is 0 | `rows_when` times out |
| Memory bounded by `max_concurrency` | `test_a_job_holds_no_more_results_than_it_crawls_at_once` | at most 2 results alive at each of 12 writes | the count grows with the list |
| Finished rows survive a restart | `test_rows_written_before_a_shutdown_stay_readable` | the next process reads positions 0 and 1 of an interrupted job | no rows |
| Rows are gap-free and name their position | `test_each_url_becomes_a_row_numbered_as_it_finishes` | numbers 0-5, positions 0-5 | repeated or missing numbers |
| Pages behave as documented | Task 3's five tests | order, cursor, empty page, 404, bounds | |
| No id addresses a row as a record | `test_a_job_id_is_only_what_the_service_hands_out` | 422 on both routes | 404, or 500 once rows exist |
| A result that cannot be stored fails the job | `test_a_result_that_cannot_be_stored_fails_the_job` | `failed`, `Job failed (OSError)` | `done`, or `ExceptionGroup` |
| A failing delivery stops the batch | `test_a_failing_delivery_fails_the_batch_and_stops_the_rest` | at most 2 of 8 crawled | 8 of 8 |
| `/crawl/batch` unchanged | `test_batch_pipeline_runs_without_the_http_layer` and the route tests | green before and after | |
| Contract documented and consistent | packaging guards, schema-text test, link check | green | |

Regression: the full suite (377 tests on 2026-09-23 before this work) passes, with only the
three tests named in Task 4 changed, each because the contract they assert changed by
decision.

**End to end, the one check a unit test cannot make** - container memory under a real
2000-URL job, old image against new:

1. Build and look inside, since `docker build | tail` hides a failed build:
   `docker build -t website-textextraction:rows-probe .` then
   `docker run --rm website-textextraction:rows-probe python -c "import app; print(app.__version__)"` - expect `3.0.0`.
2. An upstream on the host: a directory holding `article.html` with about 30 KB of paragraph
   text, served by `python -m http.server 8765 --directory <dir>`.
3. `docker run -d --name wte-rows-probe -p 127.0.0.1:8199:8000 -e API_KEY=probe -e SSRF_PROTECTION=false -e MAX_URLS_PER_REQUEST=2000 -e MAX_TIMEOUT_SECONDS=7200 --add-host host.docker.internal:host-gateway website-textextraction:rows-probe`.
   `SSRF_PROTECTION=false` exists only because this upstream is on a private address; the
   probe listens on loopback.
4. Submit 2000 URLs `http://host.docker.internal:8765/article.html?n=<i>` with `mode: fast`
   and `max_concurrency: 10`; sample `docker stats --no-stream --format "{{.MemUsage}}" wte-rows-probe`
   every 2 seconds until the job ends, reading rows meanwhile.
5. Repeat with `jschachtschabel/website-textextraction:2.3.1`, reading `result` at the end.
6. Expected: 2.3.1's memory climbs with the job; the new image's stays flat within noise, and
   its rows are readable from the first seconds.
7. Clean up only what the probe started: `docker rm -f wte-rows-probe`, and the http.server
   by its listening PID.

Then `/better-coding-review` over the whole series before anything is published.

## Release

Not a task: it needs the user.

1. The user's panel deploys the compose file from `main`, which names a fixed image tag.
   Once `main` names `:3.0.0`, the next click on Update delivers the breaking change;
   publishing also moves `latest` for anyone who pulls that.
2. Before tagging or merging: the user confirms the client reads `results_url`, or points
   the panel at the compose file of the `v2.3.1` tag, as `docs/migration-3.0.md` shows.
3. Then tag the branch head `v3.0.0`; the workflow builds, smoke-tests, checks the
   namespace and pushes. Fast-forward `main` only once `:3.0.0` is on Docker Hub, so the
   file a panel fetches never names an image that does not exist yet. Afterwards, as for
   2.3.1: the tags on Docker Hub, the version inside the pulled image, and one real job
   read through `results_url`.

## Known limitations after this plan

- A lost job is still detected only at its deadline plus 30 seconds.
- After a crash, the record's `progress` can trail its rows; the rows are complete.
- No server-side resume - decided.

## Implementation notes

Carried out on 2026-09-23 on the branch `feature/job-result-rows`, so that `main` stayed
releasable at 2.3.1 while the contract changed. Where the work departed from the text
above:

- **Task 3.** The first red was an `ImportError` at collection - the tests import
  `_row_key` - rather than the per-test failures listed. The reason was the right one: the
  read half did not exist.
- **Tasks 3 and 4.** `rows` and `_run` measure 5 on the complexity gate, one above the
  estimate; the gate is 10.
- **Task 6.** One text the plan did not list: `JobAccepted.status_url` was described as the
  "Path to poll for the result", which the change made false. It now names status and
  progress.
- **Task 7.** `test_rows_written_before_a_shutdown_stay_readable` waits for its rows at the
  store (`rows_stored`), not through the results route. Written as planned, it waited
  through the route, and the red-proof failed it before the restart - proving nothing the
  second test did not. Waiting at the store, the same break fails it where it should:
  after the restart, `[] == [0, 1]`.
- **Task 8.** The plan said an interrupted job's rows stay readable until its deadline plus
  `JOB_RESULT_TTL`. A graceful shutdown saves the record as finished, so it expires
  `JOB_RESULT_TTL` after the shutdown, and the route answers 404 from then on. The
  documents say what holds in every case: the rows stay readable as long as the record does.
- **Review.** A review with fresh context found nothing critical or major and six minor
  points, all fixed on the branch. Two properties had no test: the results route's key
  check, and reading the record before the rows - swapping the two reads left all 120 job
  tests green. Rows expired on the deadline's schedule rather than the record's: hours of
  disk after an early end, and the first rows gone before the record after a late one.
  Both ends of a job now touch every row to the record's expiry. Three texts said more
  than the code backs: the progress count after a shutdown, the verdict "lost", and
  `max_bytes` for screenshots. The single storage thread that record ordering depends on
  is now commented. The review's one open question - whether the numbering test sees a
  missing lock without the delay the red-proof had added - was settled by experiment:
  lock removed, no delay, 20 of 20 runs failed.
- **End to end.** One 2000-URL job in fast mode against a local upstream, 29.7 KB of
  Markdown a page, in the real images: 3.0.0 served its first row after 6.4 seconds and all
  2000 through the documented loop. Container memory grew by 136 MiB against 175 MiB for
  2.3.1 - not flat, as the verification plan expected. The difference is smaller than the
  ~60 MB of Markdown 2.3.1 holds by design, so the container figure is dominated by what
  both versions share. diskcache maps each store file with up to 64 MiB and caches up to
  32 MiB per connection, which fits the size but was not measured. The bound on result
  objects rests on the gc test; the container-level claim is open.
- **Release.** The plan assumed the user's panel pulls `latest`. It deploys the compose
  file from `main`, the route the README describes, and that file names a fixed tag. The
  migration note therefore shows the `v2.3.1` file to point such a panel at, and the
  release tags first and moves `main` once the image exists.
