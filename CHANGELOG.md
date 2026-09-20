# Changelog

Versions describe the request/response contract and the operational defaults, not
the internal structure. Dates are release dates of this repository.

## 0.8.0 — 2026-09-20

### Added

- Python 3.14 is supported: every declared dependency installs on 3.14.7 and both suites pass
  there, so `requires-python` allows it and CI covers 3.11 to 3.14. A test keeps the metadata
  and the CI matrix in step (audit finding A23).
- `screenshot_full_page=true` captures the whole document instead of the viewport, bounded to
  20000 pixels; a longer page is cut and says so in its warnings.

### Changed

- An idle worker process is asked to exit and given two seconds before it is killed, so it can
  flush what it holds. Busy or unresponsive workers, timeouts and cancellations still kill the
  whole process group. Coverage can therefore measure the spawned workers: 92 % of `app/`
  against the 81 % that counting only the main process reported.

## 0.7.0 — 2026-09-20

### Added

- `/health` reports the configured Chrome and ChromeDriver paths and whether they exist; a
  configured path that does not exist is logged as a warning at startup.
- Every response carries `X-Request-ID` (the client's plain token of at most 64 characters, or a
  generated one), and the failure log lines of that request - including its batch items and
  background job - carry the same id.

### Changed

- Auto mode no longer parses the document a second time when the extraction already proves the
  page is not a JavaScript shell: 1000 characters of visible extracted text answer both shell
  rules. Measured on a 540 KiB article, the conversion worker fell from 271 ms to 165 ms, the
  same as fast mode. This closes the open part of audit finding A09.
- An oversized upload is read to its end, bounded to 4 MiB and one second, before the 413 goes
  out, so clients read the error instead of a connection reset.

### Measured and deliberately unchanged

- Metrics buffering: the shared state store takes 3700 records per second, far beyond anything
  the service can produce.
- A DNS cache: a repeated resolution costs 0.3-0.9 ms because the OS resolver already caches.
- A browser circuit breaker: deterministic failures are already excluded from retries, and a
  breaker would add state that can wrongly block a site after a transient run of failures.

## 0.6.0 — 2026-09-18

### Added

- `GET /metrics` in Prometheus text format (Bearer-protected): cumulative request, cache-hit
  and coalescing counters, a latency histogram of fresh successful extractions, and gauges
  for readiness, capacity, worker pools and cache size. New dependency: `prometheus-client`.
- Background jobs: `POST /jobs` takes a batch body and answers 202 with an ID;
  `GET /jobs/{id}` reports `queued`, `running`, `done` (with the batch result) or `failed`.
  For clients such as the Colab tunnel that cannot hold a connection for a long batch.
  `MAX_ACTIVE_JOBS` (10), `JOB_RESULT_TTL` (1 h).
- Opt-in robots.txt check (`respect_robots_txt` / `RESPECT_ROBOTS_TXT`), RFC 9309 semantics
  via `protego`; a disallowed URL answers 403. New dependency: `protego`.
- Conditional revalidation: an expired HTTP result is revalidated with its ETag/Last-Modified;
  a 304 returns it with `revalidated: true` without downloading or converting again
  (`REVALIDATION_TTL`, one day).
- `accept_language` / `DEFAULT_ACCEPT_LANGUAGE` for the HTTP and browser paths; HTTP requests
  now always send an `Accept` header.

### Changed

- The default user agent is `WebsiteTextExtraction/<version>` plus the project URL as contact;
  Wikimedia answered the bare default with 403 "Please respect our robot policy".
  `.env.example` no longer pins a user agent.

## 0.5.0 — 2026-09-18

### Added

- `extract_metadata=true` returns `metadata` with title, description, author, publication
  date, site name, canonical URL and `<html lang>`, as the page declares them. Opt-in: it
  parses the page once more (about 15-60 ms). A failure leaves the text intact and adds a
  warning; anonymized responses never carry metadata.
- `INBOUND_RATE_LIMIT_RPS` / `INBOUND_RATE_LIMIT_BURST`: a token bucket in front of the crawl
  endpoints, applied after authentication. Each URL costs one token; excess requests get
  429 with `Retry-After`. Off by default; the Colab notebook enables 2 URLs/s.
- `WORKER_MAX_JOBS` (100): each worker process is replaced after that many successful jobs.

### Changed

- A browser job ends about two seconds sooner: ChromeDriver is stopped directly once the
  session ends instead of through Selenium's shutdown, which polls in one-second steps.
  A session that cannot be ended now restarts the worker instead of being ignored.
- `DEFAULT_USER_AGENT` is `WebsiteTextExtraction/0.5`.

## 0.4.0 — 2026-09-17

Findings from [the audit of 17 September 2026](docs/audits/2026-09-17-audit.md) are
referenced as `A01`–`A25`.

### Changed — review before upgrading

- `HOST` defaults to `127.0.0.1`, and the service refuses to start on a non-loopback
  address while `API_KEY` is empty. A deployment that relied on binding `0.0.0.0`
  without authentication must now set a key (A01).
- `/docs`, `/redoc` and `/openapi.json` exist only while no `API_KEY` is configured (A12).
- Request bodies above `MAX_REQUEST_BYTES` (default 1 MiB) are answered with 413 before
  authentication; FastAPI reads the whole body before the auth dependency runs (A02).
- A positive `DEFAULT_DOMAIN_RATE_LIMIT_RPS` is a ceiling. `crawl_rate_limit_rps` can
  only lower it for a request, and `0` no longer disables the per-domain limit (A06).
- The result cache and the rate/metric state store JSON instead of pickled objects and
  live in `results-json-v1` and `state-json-v1`. On POSIX the service refuses to start
  when `RESULT_CACHE_DIR` is accessible beyond its owner. Cached data is derived, so the
  old `results-v3` and `state-v3` directories can be deleted (A03, A20).
- Rendered pages that never settle return `success=false`, Chrome certificate errors
  return 502, an HTTP timeout reports 504, and trafilatura output starts with the page
  heading when the extractor dropped it. The cache key version is `extraction-v4`.
  These behaviours changed after 0.3.0 without a version of their own (A22).
- The default user agent is `WebsiteTextExtraction/0.4`.

### Added

- One log line per failed crawl with host, mode, upstream status, extraction status and
  elapsed time - paths and query strings stay out of the log (A05).
- `constraints.txt` with the dependency set the release was verified against, and a
  `pip-audit` step in CI (A07).
- Tests for link extraction and classification, the exposure rules, the storage layer
  and the browser retry policy (A08).

### Fixed

- JSON logs no longer carry local variable values from exception traces; loguru's
  `diagnose` and `backtrace` defaults applied only to the JSON sink (A04).
- Raw deflate response bodies are decoded instead of failing as invalid (A16).
- Certificate errors and policy blocks are no longer retried with back-off; they cannot
  succeed on a second attempt and each retry costs a Chrome start (A18).
- Browser routing is computed only in auto mode, which removes one HTML parse per
  document in fast and js mode (A09).
- Links whose target contains `datenschutzerklaerung` are classified as `legal`.

### Removed

- Unused helpers in `app/utils.py` that contradicted documented behaviour: a proxy
  normalizer that accepted SOCKS URLs and a rotating user-agent pool. What remains is
  `app/links.py` (A10).

## 0.3.0

Extraction, privacy, network safety and resource limits were corrected. See
[docs/migration-0.3.md](docs/migration-0.3.md) for the contract changes and
[docs/audit-remediation.md](docs/audit-remediation.md) for the findings behind them.
