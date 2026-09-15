# Website Text Extraction with Selenium

An HTTP-first FastAPI service that extracts web pages and documents into Markdown.
It uses Trafilatura for main content, MarkItDown for document conversion, and
Selenium/Chrome when JavaScript rendering is needed. No Playwright dependency.

Version 0.3 corrects extraction, privacy, network safety and resource limits.
See [migration notes](docs/migration-0.3.md) and the
[audit remediation record](docs/audit-remediation.md).

## Install and run

Python 3.11 or 3.12 and a local writable cache directory are required. Chrome is
needed only for `mode=js` or an automatic browser fallback. Linux is the primary
production and CI platform.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
python run.py
```

Set `API_KEY` in `.env` before exposing the service. The examples below assume it
is also exported in your shell. `/docs` documents the request and response schema;
`/health` is public and reports process readiness even while all workers are busy.
`/stats` uses the same Bearer authentication as the crawl endpoints.

For PDF and Office files, install `pip install -e '.[documents]'`. For local PII
processing, install `pip install -e '.[pii]'` and then, for example,
`python -m spacy download de_core_news_lg`. Only the requested language is loaded,
on first use. Models are never downloaded while processing a request.
`requirements.txt` installs the same core dependencies from `pyproject.toml`.

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
conversion rather than browser routing. Source LaTeX/MathML is preserved when
available; formulas present only as pixels cannot be reconstructed reliably.

Important result fields:

| Field | Meaning |
|---|---|
| `request_mode` | Requested mode, such as `auto` |
| `fetch_engine` | Actual fetch path: `http` or `selenium` |
| `converter` | Actual converter, including a fallback |
| `status_code` | Upstream HTTP status; `null` if Chrome could not observe it |
| `extraction_status` | `ok`, `empty`, `unsupported`, `skipped`, `failed`, or `blocked` |
| `success` | Usable, non-truncated text from a successful upstream response |
| `truncated` / `warnings` | Explicit size limits, fallbacks and other limitations |
| `cached` / `coalesced` | Shared result-cache hit / shared in-progress extraction |
| `elapsed_ms` | Duration of this call, including waiting |

An HTTP 200 **from this API** can contain an upstream error or an unsuccessful
extraction. Check `success`, `status_code` and `extraction_status`. Operational
failures use a sanitized API error: 400 for prohibited/invalid destinations,
422 for invalid options, 502 for fetch failures, 503 for queue/unavailable PII,
and 504 for an expired deadline. Failed/partial results are not cached.

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

- `html_converter`: `trafilatura` (default), `markitdown`, or `bs4`.
- `trafilatura_clean_markdown=false`: Trafilatura's raw text extraction.
- `max_bytes`: bounds decoded HTTP output and rendered HTML; truncation is explicit.
  Compressed HTTP input is decoded with a bounded output allocation. For Chrome,
  this bounds returned HTML, **not total bytes of all browser assets**.
- `js_strategy=speed`: eager navigation; blocks common image/font/media URLs unless
  a screenshot is requested. `accuracy` waits for normal page load.
- `wait_for_selectors`: wait until all CSS selectors match visible elements.
  Content stability, busy indicators and MathJax readiness replace fixed sleeps.
  `wait_for_ms` is an optional minimum wait within the same deadline.
- `user_agent`, `headless`, `allow_insecure_ssl` and `proxy` apply to each request.
- `screenshot=false` by default. A requested screenshot is available only on the
  browser path; choose `mode=js` when a screenshot is required.
- `anonymize=true`: local Presidio redacts Markdown. Missing/failed models produce
  an error with no page text, never an unredacted fallback. Links and screenshots
  are suppressed for these responses. Source URL metadata remains URL metadata;
  automated PII detection is not a guarantee that every identifier is recognized.
- Media `skip`/`none` return `extraction_status=skipped` with empty Markdown.
  `metadata` uses local `ffprobe` with a bounded runtime. See migration notes for
  the restrictions on legacy `full` transcription.

Each browser job has a new profile, preventing cross-request cookies/storage.
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
connections, 2 browser workers and 2 conversion workers. No Chrome/NLP warmup runs
at startup. Both Selenium strategies share the same browser budget. Increasing
`UVICORN_WORKERS` multiplies these capacities; start with the default of 1.

Result cache, rate reservations and minute metrics are shared across processes on
the same host when `RESULT_CACHE_DIR` is shared. Do not place SQLite on a network
filesystem. Identical in-flight requests coalesce within each Uvicorn process;
this is not a distributed coalescing service. Cache keys include effective options,
media policy, model identity and a format version. `force_refresh=true` bypasses
lookup and refreshes the successful result. TTL 0 disables result caching.

Rate limits count document attempts: HTTP requests, HTTP redirects/retries and
browser initial navigation/retries. Browser assets and internal browser redirects
are not counted separately. Fractional values such as 0.5 requests/s work, and
per-request overrides take effect for that acquisition. Global and per-host
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

CI runs unit/API tests on Python 3.11 and 3.12 plus a separate real Chrome job.
The browser fixtures cover dynamic text, upstream 404, isolated cookies,
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
[nginx example](deploy/nginx.conf) includes the maximum request deadline.

## License

[Apache License 2.0](LICENSE).
