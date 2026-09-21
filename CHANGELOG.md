# Changelog

Versions describe the request/response contract and the operational defaults, not
the internal structure. Dates are release dates of this repository.

## 2.0.0 - 2026-09-21

The number jumps because it had been going backwards. This repository was tagged `v1.0.0`
in March; the rework that followed restarted the in-code version at 0.3.0, so the tag and
the GitHub release still presented the pre-audit code as the latest thing here. 2.0.0 puts
the line back in order and says what is true of it: the service is **not** compatible with
1.0.0. An API key is required to bind beyond loopback, `/docs` is withheld once one is set,
the result cache has a different format and the response schema has changed.

Everything released as 0.3.0 through 0.10.0 is part of this release; those entries record
how it got here. What follows is what changed since 0.10.0.


### Changed - the image installs a locked, hash-checked dependency set

- `requirements.lock` pins every direct and transitive dependency and names a hash for
  every artefact, resolved on Debian 13 with Python 3.13 - the image's own target. The
  Dockerfile installs it with `--require-hashes`, so a package re-uploaded under a version
  already pinned is rejected rather than installed. This closes `A07` of the audit of 17
  September, open until now because such a lock has to be produced on the target platform
  and this repository is developed on Windows; the container added in 0.10.0 is that
  platform.
- The image no longer builds the project. Nothing reads its package metadata, so the
  source is copied in and the build backend - fetched unpinned, and running code at build
  time - is never needed. Dependency layers now also stay cached when only the source
  changes.
- `constraints.txt` keeps its own job: the version set verified for development installs
  on any platform, without hashes. The README says which file answers which need.

## 0.10.0 - 2026-09-21

### Changed - the documented surface is the real one

- Every field of every request and response model carries a description, and each model
  carries an example. FastAPI renders an undocumented field as a placeholder for its type,
  so the body `/docs` offered was `url: "string"` with `timeout_ms: 0` and `max_bytes: 0`:
  rejected by the validation, and a reader who corrected only the URL then crawled with
  `User-Agent: string`. 20 of 97 fields were described; no endpoint was described at all.
  Each body endpoint offers two, in a dropdown: the shortest body that works, and one
  naming every option at a value that works - the first attempt showed only the short one,
  which moved the option list behind the schema tab. The response examples come from real
  crawls of example.com.
- `/crawl/batch` and `/jobs` now say how they differ: the same crawling, delivered by a
  held-open connection or by an immediate job id.
- A `proxy` of `"string"` is a 422 instead of being read silently as "no proxy". The
  workaround existed only to absorb the placeholder, and it hid a mistyped proxy just as
  quietly.

Reading the code to write the descriptions corrected two documented behaviours:
`media_conversion_policy=full` is not implemented and answers `unsupported`, and a failed
batch entry carries its result as well as its reason - the result is absent only when the
URL was refused before it was crawled.

### Added - a container image

- `Dockerfile`, `.dockerignore` and `docker-compose.yml` build a Debian 13 (trixie) image
  with Chromium and its driver from the distribution, which ships them as a matched pair.
  It runs as an unprivileged user, creates the result cache with the 0700 mode the service
  insists on, and has a health check against the public `/health`.
- The image sets `SELENIUM_NO_SANDBOX=true`: Docker's default seccomp profile blocks the
  namespaces Chrome's own sandbox needs, so the container is the boundary instead. The
  README documents the alternative - Chrome's sandbox with `seccomp=unconfined` - and what
  it costs.
- The README gained an installation guide for Debian 13, from Docker's own repository to a
  crawl that proves the browser starts inside the container.

### Housekeeping

- `uv.lock`, `poetry.lock` and `Pipfile.lock` are ignored. An unreferenced `uv.lock` had
  been sitting in the tree since 17 September, resolving to the versions of 0.7.0.

## 0.9.0 — 2026-09-20

Findings from [the audit of 20 September 2026](docs/audits/2026-09-20-audit.md) are
referenced as `B01`-`B24`; [the remediation](docs/audits/2026-09-20-remediation.md)
records what happened to each one.

### Fixed — a page keeps its mathematics

- Presentation MathML is translated to LaTeX instead of being flattened to its text.
  Serlo publishes no TeX annotation, so the length of a vector arrived as
  `$$| a -> | = a 1 2 + a 2 2$$`: the radical gone, the exponents turned into
  neighbouring digits, and the `$$` fences claiming a precision that was not there (B02).
- A formula the page hides from sighted readers is unhidden, because extractors drop
  hidden content. Wikipedia ships its MathML that way, so all 289 formulas of "Satz des
  Pythagoras" were missing from the extraction; 275 are back, the rest sit in boilerplate
  the extractor excludes anyway (B03).

### Fixed — the service keeps serving

- Stopping and starting a worker no longer runs on the event loop. Every browser error,
  deadline and cancellation used to freeze the whole service for as long as `taskkill`,
  the process join and the profile removal took. Measured with `/health` pinged every
  20 ms during a browser kill: the worst gap fell from 512-685 ms to 26-40 ms. The unit
  suite runs in 79 seconds instead of 137 (B01, B06). The pool also has its own threads,
  so a job cannot queue behind the DNS lookups of a page with many hosts (B07).
- Recording a request in a `finally` can no longer replace its answer with a 500 when the
  shared state store is busy or unwritable (B08).

### Changed — review before upgrading

- `failure_reason` names the upstream status first, so a batch item or job result for a
  404 or 429 reads `Upstream status 429` instead of `Extraction blocked` (B24).
- `/health` reports whether the configured Chrome and ChromeDriver exist; their paths
  appear only while no `API_KEY` is set, as with `/docs` (B21).
- `force_refresh` no longer joins an identical request already in flight, so it always
  fetches as documented (B12).
- The service refuses to start on a `DEFAULT_USER_AGENT` that is empty, longer than 512
  characters or not printable ASCII, instead of answering every crawl with a 500 (B11).
- The default user agent is `WebsiteTextExtraction/0.9`.

### Fixed — found by reviewing the change set itself

- A cancelled task could cancel the worker stop it had just queued, because
  `asyncio.wrap_future` chains cancellation back to the thread pool. The worker and its
  Chrome children survived and the pool eventually stopped serving. Waiting goes through a
  shield now, and closing a pool twice is a no-op.
- A page controls the fence attributes of `mfenced`, which were not escaped: `open="$$ ..."`
  ended the Markdown fence and put arbitrary text outside it.
- Converting MathML stops at 64 levels of nesting. About 600 nested elements raised
  `RecursionError` where 0.8.0 took the iterative text path, failing the crawl with a 502.
- Three smaller formula defects: the escapes for a backslash and a tilde swallowed the next
  character, a degree sign became a second superscript inside `msup`, and a bracket in a
  root index closed the optional argument early.
- Unhiding a formula wrapper stops at an element that holds more than the formula, even a
  text-free one such as a tracking pixel.

### Fixed — smaller

- robots.txt entries are kept per origin *and* transport: a caller's own proxy or
  `allow_insecure_ssl` decided what every later robots-respecting request was allowed to
  crawl, in both directions (B04).
- A full-page screenshot clamps its width at 4000 pixels as it already clamped the height
  at 20000; the width came straight from the page's declared layout size (B05).
- A CONNECT reply without a status line answers 502 instead of aborting the tunnel with an
  uncaught `IndexError` (B10).
- A failing store open shuts the storage threads down (B17), and an expired deadline no
  longer drops an unawaited shield when joining a leader (B18).
- `extract_links` is documented in the README (B23).

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
[the remediation record](docs/audits/2026-09-15-remediation.md) for the findings behind them.
