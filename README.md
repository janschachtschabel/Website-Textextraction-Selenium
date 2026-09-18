# Website Text Extraction with Selenium

An HTTP-first FastAPI service that extracts web pages and documents into Markdown.
It uses Trafilatura for main content, MarkItDown for document conversion, and
Selenium/Chrome when JavaScript rendering is needed. No Playwright dependency.

Version 0.5 adds page metadata, an inbound rate limit and faster browser jobs; 0.4 closed
the exposure gaps found in the September 2026 audit; 0.3 corrected
extraction, privacy, network safety and resource limits. See the
[changelog](CHANGELOG.md), the [migration notes](docs/migration-0.3.md) and the
[audit remediation record](docs/audit-remediation.md).

## Install and run

Python 3.11 to 3.13 and a local writable cache directory are required. Chrome is
needed only for `mode=js` or an automatic browser fallback. Linux is the primary
production and CI platform.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
python run.py
```

`HOST` defaults to `127.0.0.1`. Any other address requires `API_KEY`; the service
refuses to start with a non-loopback host and no key. The examples below assume the
key is also exported in your shell. `/docs`, `/redoc` and `/openapi.json` document
the request and response schema and are served only while no key is configured.
`/health` is public and reports process readiness even while all workers are busy.
`/stats` uses the same Bearer authentication as the crawl endpoints. Request bodies
above `MAX_REQUEST_BYTES` (1 MiB) are answered with 413 before authentication and the
connection is closed, so a client that is still uploading can see the reset instead of
the response body.

`INBOUND_RATE_LIMIT_RPS` (off by default) limits crawl requests after authentication:
every URL, also inside a batch, costs one token from a bucket of
`INBOUND_RATE_LIMIT_BURST` (20) tokens that refills at the configured rate. Excess
requests get 429 with `Retry-After`. The bucket is per process and shared by all
clients; failed authentication never consumes tokens.

Every failed crawl is logged as one line with host, mode, upstream status, extraction
status and elapsed time - never the path or query string. `LOG_JSON=true` emits the
same fields as JSON and, like the readable sink, without exception variable values.

For PDF and Office files, install `pip install -e '.[documents]'`. For local PII
processing, install `pip install -e '.[pii]'` and then, for example,
`python -m spacy download de_core_news_lg`. Only the requested language is loaded,
on first use. Models are never downloaded while processing a request.
`requirements.txt` installs the same core dependencies from `pyproject.toml`.

The declared ranges resolve to the newest compatible releases, which is what CI
tests. For a deployment that must resolve the same way twice, install against the
verified set: `pip install -e '.[documents]' -c constraints.txt`. Refresh that file
by re-resolving without it, running both suites and `pip-audit`, then
`pip freeze --exclude-editable`.

Preinstall compatible Chrome and ChromeDriver binaries in production and set
`CHROME_BINARY` and `CHROMEDRIVER_PATH`. Otherwise Selenium Manager locates/downloads
them. Chrome sandboxing and TLS verification are enabled by default.
On Ubuntu 23.10+, prefer packaged Google Chrome at its standard installation path:
Ubuntu's AppArmor profile does not cover arbitrary downloaded Chrome binaries.
See the [Chromium sandbox documentation](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).
CI uses the runner's packaged Chrome and matching ChromeDriver, and checks that
Chrome starts with sandboxing enabled before running browser fixtures.

## Extract a page

```bash
curl http://127.0.0.1:8000/crawl \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","mode":"auto"}'
```

| Mode | Behavior |
|---|---|
| `fast` | Stream HTTP response and convert it; no browser |
| `auto` | Try HTTP and extraction first; render an empty or JS-dependent app shell |
| `js` | Render with Selenium using a fresh profile |

A short useful page is a success. A cookie/privacy phrase or RSS discovery link
alone does not trigger Selenium. HTTP errors and detected challenge pages do not
trigger attempts to bypass the block. PDF, Office and feed bodies use document
conversion rather than browser routing. With `mode=js`, Chrome downloads such
files instead of rendering them: the result has no text and a warning. Source
LaTeX/MathML is preserved when available; formulas present only as pixels cannot
be reconstructed reliably.

Important result fields:

| Field | Meaning |
|---|---|
| `request_mode` | Requested mode, such as `auto` |
| `fetch_engine` | Actual fetch path: `http` or `selenium` |
| `converter` | Actual converter, including a fallback |
| `status_code` | Upstream HTTP status; `null` if Chrome could not observe it |
| `extraction_status` | `ok`, `empty`, `unsupported`, `skipped`, `failed`, or `blocked` |
| `success` | Usable, complete (non-truncated, settled) text from a successful upstream response |
| `truncated` / `warnings` | Explicit size limits, fallbacks and other limitations |
| `cached` / `coalesced` | Shared result-cache hit / shared in-progress extraction |
| `links` / `metadata` | Classified links and page metadata, when requested |
| `elapsed_ms` | Duration of this call, including waiting |

An HTTP 200 **from this API** can contain an upstream error or an unsuccessful
extraction. Check `success`, `status_code` and `extraction_status`. Operational
failures use a sanitized API error: 400 for prohibited/invalid destinations,
422 for invalid options, 502 for fetch failures, 503 for queue/unavailable PII,
and 504 for an expired deadline. Browser navigation errors, such as an untrusted
certificate or a failed HTTPS connection, are 502 fetch failures that name Chrome's
`net::ERR_*` code. For plain HTTP targets, the network guard answers a connection
it rejects or cannot open with status 403 or 502; both fetch paths report that status.
Chrome's own pages (error pages, the blank page a download leaves) are never
returned as content or screenshot. Failed/partial results are not cached.

## Batch requests and deadlines

```bash
curl http://127.0.0.1:8000/crawl/batch \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com","https://www.python.org"],"max_concurrency":2,"timeout_ms":60000}'
```

Batch and single requests share every option and server default. A batch accepts
1–50 URLs and preserves input order. Each URL consumes the same global-per-worker
admission slot as a single request. `max_concurrency` additionally limits the batch.
Every URL's deadline starts when the batch is accepted, including time waiting
behind other batch entries. Large batches may therefore need a larger deadline
or smaller batches. The maximum is 600 seconds; nginx allows a cleanup margin.

`timeout_ms` covers admission, rate waits, redirects/retries, browser work,
conversion and anonymization. Browser and converter workers are separate bounded
process pools. On timeout/cancellation, the worker and its child processes are
terminated and partial temporary files removed before the slot is reused. OS
cleanup can add a short margin to the response deadline.

## Options and privacy

Omitted/null configurable options inherit `.env` values. Explicit `false` and `0`
remain overrides. `.env.example` lists all supported settings.

- `html_converter`: `trafilatura` (default), `markitdown`, or `bs4`. Trafilatura
  output starts with the page's first visible `<h1>`, also when it sits outside
  the main content, unless it already starts with a heading. For overview pages
  with little running text, `markitdown` keeps more, including navigation.
- `trafilatura_clean_markdown=false`: Trafilatura's raw text extraction.
- `max_bytes`: bounds decoded HTTP output and rendered HTML; truncation is explicit.
  Compressed HTTP input is decoded with a bounded output allocation. For Chrome,
  this bounds returned HTML, **not total bytes of all browser assets**.
- `js_strategy=speed`: eager navigation; blocks common image/font/media URLs unless
  a screenshot is requested. `accuracy` waits for normal page load.
- `wait_for_selectors`: wait until all CSS selectors match visible elements.
  Content stability, busy indicators (`aria-busy`, visible progress bars without a
  value) and MathJax readiness replace fixed sleeps.
  `wait_for_ms` is an optional minimum wait within the same deadline.
- `js_auto_wait=true` waits for that readiness for at most 10 s (`speed`) or
  20 s (`accuracy`) once the selectors and `wait_for_ms` are satisfied; those two
  still apply until the deadline. Pages that never settle (a permanent spinner,
  a denied download) are then returned in their current state with a warning
  and `success=false`. Leave room for page load plus this limit in `timeout_ms`;
  an expired deadline still ends the request with 504.
- `user_agent`, `headless`, `allow_insecure_ssl` and `proxy` apply to each request.
  The default user agent is `WebsiteTextExtraction/<version>` plus the project URL as
  contact; sites with a bot policy, such as Wikimedia, block crawlers without one.
- `screenshot=false` by default. A requested screenshot is available only on the
  browser path; choose `mode=js` when a screenshot is required.
- `extract_metadata=true`: `metadata` with title, description, author, publication
  date, site name, canonical URL and `<html lang>` of an HTML page, as the page declares
  them (Trafilatura). An undeclared canonical URL falls back to the final URL, an
  undeclared site name to the host. Off by default: it parses the page once more,
  about 15-60 ms per document.
- `anonymize=true`: local Presidio redacts Markdown. Missing/failed models produce
  an error with no page text, never an unredacted fallback. Links, metadata and
  screenshots are suppressed for these responses. Source URL metadata remains URL metadata;
  automated PII detection is not a guarantee that every identifier is recognized.
- Media `skip`/`none` return `extraction_status=skipped` with empty Markdown.
  `metadata` uses local `ffprobe` with a bounded runtime. See migration notes for
  the restrictions on legacy `full` transcription.

Each browser job has a new profile, preventing cross-request cookies/storage.
Starting Chrome for it costs about 0.6-0.9 s on a desktop machine; ChromeDriver is
stopped directly once the session ends, because Selenium's own shutdown polls it in
one-second steps.
HTTP requests do not retain a cookie jar. Sites requiring persistent logins or
interactive consent are outside this anonymous extraction contract.

## Network policy and capacity

Both HTTPX and Chrome use a local egress guard. Each new connection resolves and
checks its destination and then connects to a validated numeric IP. HTTP redirects
are also checked before following them. Browser subrequests go through the guard;
Chrome's implicit loopback proxy bypass and direct QUIC/UDP paths are disabled.
Only HTTP(S) URLs are accepted. Private, loopback, link-local, reserved and mapped
private addresses are denied by default. Keep `SSRF_PROTECTION=true`.

HTTP(S) upstream proxies are supported, including authentication. They must accept
CONNECT requests to numeric destinations; this prevents a second target DNS lookup
at the proxy. Private upstream proxies are also denied under the default network
policy. SOCKS and arbitrary proxy protocols are rejected explicitly. TLS remains
end-to-end. Keep network-level egress restrictions around production browser
workers as an additional boundary for untrusted web content.

Default capacity per Uvicorn process: 8 active URLs, 50 waiting URLs, 16 HTTP
connections, 2 browser workers and 2 conversion workers. Each worker process is
replaced after `WORKER_MAX_JOBS` (100) successful jobs, which bounds the memory that
lxml, MarkItDown and Chrome accumulate. No Chrome/NLP warmup runs
at startup. Both Selenium strategies share the same browser budget. Increasing
`UVICORN_WORKERS` multiplies these capacities; start with the default of 1.

Result cache, rate reservations and minute metrics are shared across processes on
the same host when `RESULT_CACHE_DIR` is shared. Do not place SQLite on a network
filesystem. Identical in-flight requests coalesce within each Uvicorn process;
this is not a distributed coalescing service. Cache keys include effective options,
media policy, model identity and a format version. `force_refresh=true` bypasses
lookup and refreshes the successful result. TTL 0 disables result caching.

Both stores hold JSON, never pickled objects, and live in `results-json-v1` and
`state-json-v1` under `RESULT_CACHE_DIR`. On POSIX the service refuses to start when
that directory is readable by anyone but its owner. Cached data is derived, so
directories from earlier versions can simply be deleted.

Rate limits count document attempts: HTTP requests, HTTP redirects/retries and
browser initial navigation/retries. Browser assets and internal browser redirects
are not counted separately. Fractional values such as 0.5 requests/s work. A
`DEFAULT_DOMAIN_RATE_LIMIT_RPS` above 0 is a ceiling: `crawl_rate_limit_rps` can only
lower it for a request, and 0 no longer disables it. Global and per-host
reservations are atomic across workers. Metrics keep 60 minute aggregates;
reported percentile latencies are histogram approximations and exclude cache hits
and coalesced requests.

## Verification and load testing

```bash
pip install -e '.[dev,documents,loadtest]'
pytest -q -m 'not selenium'
ruff check app tests helper/loadtest.py
python -m compileall -q app helper run.py
python -m build --no-isolation
RUN_SELENIUM_TESTS=1 pytest -q tests/test_selenium_integration.py
```

CI runs unit/API tests on Python 3.11, 3.12 and 3.13 plus a separate real Chrome job.
The browser fixtures in `tests/test_selenium_integration.py` cover dynamic and late
content, hidden and static progress indicators, a permanent spinner, an empty first
`<main>`, certificate error pages, downloads, upstream 404, isolated cookies,
subrequest blocking, screenshots and deadline cleanup.

For benchmarks, put URLs in `helper/test_urls.txt`, export `API_BASE` and `API_KEY`,
and run the two scenarios separately:

```bash
LOADTEST_CACHE_SCENARIO=cold python helper/loadtest.py
LOADTEST_CACHE_SCENARIO=warm python helper/loadtest.py
```

Cold requests use `force_refresh=true`; warm runs prefill the cache. Timing includes
the complete response body. Failed extraction, upstream errors, truncation,
missing requested screenshots and cache-scenario mismatches are excluded from
successful latency samples. Raw results retain the scenario. No production
throughput or speedup claim is implied by the regression tests.

[Colab notebook](Website_Textextraction_Selenium.ipynb) provides an optional demo
using the same package metadata. Public tunnel setup requires a secret API key.
The [nginx example](deploy/nginx.conf) terminates TLS, redirects plain HTTP,
allows the maximum request deadline and limits inbound requests with the zones in
[nginx-ratelimit.conf](deploy/nginx-ratelimit.conf) per client IP, which the application's
own inbound limit cannot distinguish. Both guards listen on loopback without authentication,
so run the service on a host you do not share with untrusted local users.

## License

[Apache License 2.0](LICENSE).
