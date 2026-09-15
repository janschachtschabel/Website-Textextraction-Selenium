# Audit remediation, retaining Selenium

## Goal and scope

Resolve findings F01–F18 from the 15 September audit, improve the HTTP-first
extraction path, retain Selenium, and add meaningful regression coverage.
The user has authorized implementation and the recommended improvements.
Keep `/crawl`, `/crawl/batch`, `/health`, and `/stats`; document intentional
contract corrections in version 0.3.0. No Playwright dependency or migration.

## Decisions

Three approaches were considered: patch the existing monolithic paths, extract
their existing responsibilities and repair them, or replace the browser stack.
Choose responsibility extraction plus fixes: a shared URL service fixes the
batch/capacity and routing defects together. A browser migration is excluded by
the user. FastAPI, HTTPX, Trafilatura, MarkItDown and diskcache remain.

Network safety cannot stop at checking the input URL. Use a small local egress
proxy for HTTP and Chrome: resolve destinations, reject non-public addresses,
and connect to the validated IP without a second DNS lookup. Revalidate HTTP
redirects in the fetcher; the proxy also protects browser subrequests. Preserve
TLS hostname verification at the client. Upstream HTTP(S) proxies must receive
validated numeric destinations; unsupported proxy protocols are rejected.
Do not disable browser web security or sandboxing by default.

Use bounded, reusable subprocess workers for browser and conversion operations.
An absolute monotonic deadline includes queueing, rate waits, fetch, retries,
conversion and anonymization. Terminate an overdue worker and its descendants
before replacing its slot. Browser sessions must use isolated contexts; verify
Chrome's CDP context support with a real browser, with a fresh-driver fallback
if context creation is unavailable. The normal and eager modes share one
capacity budget rather than two eagerly created pools.

The API remains a single deployable application. No Redis or new crawler
framework. Request options are resolved once from server defaults; batch and
single URL requests share the same option model. Screenshots become opt-in.
PII processing loads only the selected language on demand and fails closed.
Anonymization covers Markdown; links/screenshots are suppressed when requested
with anonymization to avoid returning an unprotected parallel representation.

## Files and interfaces

- `app/schemas.py`, `app/config.py`: one request-options model and validated
  server defaults. Additional response fields describe actual fetch/converter,
  extraction status, truncation, warnings and timing.
- `app/results.py`, `app/deadline.py`: typed FetchResult/ConversionResult,
  sanitized domain errors and a shared absolute deadline.
- `app/converter.py`, `app/html_converter.py`, `app/embedded_content.py`:
  document conversion, in-memory HTML extraction and existing embedded payloads.
- `app/security.py`, `app/egress_proxy.py`: public-target validation and pinned
  network connections. Proxy lifecycle is owned by application resources.
- `app/http_fetcher.py`, `app/preflight.py`: one streaming HTTP implementation;
  format/status classification and extraction-quality routing without refetching
  a truncated preview as though it were complete.
- `app/workers.py`, `app/worker_tasks.py`: bounded, deadline-controlled execution
  with lazy per-process initialization and explicit shutdown.
- `app/js_fetcher.py`, `app/selenium_driver.py`, `app/browser_readiness.py`:
  Selenium adapter, isolated driver lifecycle, actual navigation status and
  bounded content readiness. No heuristic bot-wall bypass.
- `app/service.py`, `app/capacity.py`, `app/result_cache.py`: per-URL pipeline,
  fair bounded admission, versioned cache and identical-request coalescing.
- `app/rate_limiter.py`, `app/metrics.py`: process-shared reservation intervals
  and bounded rolling aggregates, keeping diskcache I/O off the event loop.
- `app/main.py`: application lifecycle, auth, endpoint adaptation and health.
- `tests/`: deterministic converter, HTTP, safety, capacity, worker and API
  regression suites, plus an opt-in real Chrome integration suite.
- `helper/loadtest.py`: cache scenarios, complete-body timing and genuine success.
- `README.md`, `.env.example`, `pyproject.toml`, `requirements.txt`, `run.py`,
  notebook and nginx config: consistent installation, defaults and migration.
- `.github/workflows/ci.yml`: lint, tests, compile and build on Python 3.11/3.12.

## Acceptance criteria and ordered work packages

Each package starts by refreshing `/better-coding-workflow`. Each fix receives
a failing regression before implementation, followed by the focused test run,
diff review and a logical commit. New interfaces receive contract tests before
their implementation. The full suite is run after the integration packages.

1. **Extraction (F01/F02/F13):** prove the Trafilatura output excludes navigation,
   repeated BS4 calls leak no descriptors, code pipes and valid tables remain
   unchanged, articleBody takes precedence, and source equations survive.
   Files: converter/html_converter/embedded_content and `tests/test_conversion.py`.
   Command: `python -m pytest tests/test_conversion.py -q`.
2. **Options and result contract (F05/F07/F11/F12/F15):** omitted options inherit
   server settings, single/batch defaults match, cached keys distinguish media
   policies and effective settings, empty/failed results are not successes,
   unavailable PII cannot return original text. Tests: options/results/privacy.
3. **Network (F03/F06/F08/F09):** preserve HTML above 512 KiB within the request
   limit; report truncation; reject private DNS, mapped IPv6 and redirect targets;
   test pinned connections and browser egress; honor fractional rates and changed
   domain rates; no browser escalation for successful short pages or HTTP 429.
4. **Bounded execution (F04/F10/F14/F17):** count every batch URL, keep cache hits
   outside expensive capacity, include queue time in deadlines, terminate hung
   workers, preserve browser session isolation, obtain actual HTTP status, use
   readiness signals, and report busy healthy workers as healthy.
5. **Integration and maintenance (F12/F15/F16/F18):** exercise API branches, cache
   coalescing, privacy, failures and shutdown; correct benchmark scenarios and
   bounded metrics; remove dead LLM code; align Apache-2.0 metadata with LICENSE;
   update deployment docs, examples, notebook and CI.

## Verification and rollout

Baseline: main commit 82b6bfb9b75adf24873042a028cc06fd35f1f8c5. No existing tests.
Keep captured red/green evidence in the remediation notes. Final gates:
`python -m pytest -q`, `python -m ruff check app tests helper/loadtest.py`,
`python -m compileall -q app`, and `python -m build`.
Run real Selenium against local fixture pages if Chrome is available, checking
dynamic text, status, isolation, network guard and timeout cleanup. This is not
a production throughput benchmark; do not promise a measured speedup.

Use separate logical commits on `fix/extraction-audit-2026-09`, review the final
diff with the requested review/verify skills, push the branch and create a PR.
Rollback is the corresponding commit revert; no persistent application data
migration is required. Cache keys are versioned so old responses cannot leak into
the corrected behavior. Runtime/CI limitations must be reported explicitly.
