# Audit remediation evidence

Scope: findings F01–F18 from the 15 September 2026 audit of
`82b6bfb9b75adf24873042a028cc06fd35f1f8c5`. Selenium is retained. The user
requested fixes, the recommended improvements, and important regression tests.

## Findings and verification

| Finding | Change | Evidence |
|---|---|---|
| F01 — incorrect Trafilatura keywords/fallback | Supported `url` argument and raw `html2txt` call; report the converter actually used | `test_selected_trafilatura_extracts_main_content` |
| F02 — temporary descriptors leaked by HTML returns | In-memory HTML/MarkItDown streams; temporary files only for local media metadata | `test_repeated_html_conversion_closes_descriptors` |
| F03 — truncated preflight reused as a full response | One streaming HTTP path, request-specific decoded limit, explicit truncation; bound rendered HTML before WebDriver transfer | `test_http_reads_full_document_and_reports_request_limit`, bounded browser and compressed-body tests |
| F04 — batch bypassed admission; counters/defaults drifted | One per-URL service and admission queue used by both endpoints; independent HTTP/browser/converter budgets | API batch-capacity and cancelled-queue tests |
| F05 — Selenium returned fake 200 and incompatible tuples | One FetchResult contract; main-frame network status observed, unknown status null; no secondary bypass path | Main-frame/asset status tests; real Chrome 404 gate in CI |
| F06 — DNS/redirect/subrequest SSRF gaps | Numeric-IP egress dialing, validate DNS and redirects, guard HTTP and browser traffic, block implicit loopback bypass; bounded compressed input | Private/mapped-IP, pinned CONNECT, upstream proxy, redirects and Chrome subrequest tests |
| F07 — unredacted PII fallback/caching | Lazy selected-language models; fail closed; suppress raw links/screenshots; cache only successful redacted output | PII-unavailable and redacted-cache API tests |
| F08 — fractional rates and first-override-wins | Atomic shared reservation intervals; each acquisition uses its requested rate; retries/HTTP redirects acquire again | Fractional/changed/shared rate and retry tests |
| F09 — incorrect preflight routing | Route after extraction; short pages and RSS discovery links stay on HTTP; no escalation for blocked responses; binary document conversion and articleBody precedence | Shell/short/blocked/feed routing, PDF/DOCX and embedded metadata tests |
| F10 — fixed waits and ignored readiness options | Visible selectors, content stability, busy indicators and MathJax readiness under one deadline | Browser configuration/unit gates and dynamic fixture in real Chrome CI |
| F11 — ineffective request/server options | One option model and one resolver; single/batch inherit identical defaults; fresh browser configuration per job | Defaults/explicit-false/proxy validation/browser-option tests |
| F12 — inconsistent errors, success and timing | Explicit extraction statuses, actual fetch/converter fields, current-call elapsed time, truthful batch/metric success | Empty/upstream error/cache/fallback API tests |
| F13 — table/math/charset/embedded-content corruption | Conservative table detection, preserved source equations, declared charset decoding, articleBody before summary, correct KMap URL origins | Converter regression suite including code pipes, table columns, SVG/LaTeX and legacy encoding |
| F14 — duplicate eager browser pools and model startup | One lazy browser capacity, reusable Python processes, fresh profiles; optional document/NLP extras; screenshots opt-in | Lazy/reused worker, health and browser configuration tests; dependency metadata |
| F15 — incomplete cache identity/session mixing | Versioned keys use effective options and PII models; no shared HTTP cookie jar, fresh browser profiles, per-worker coalescing | Cache-key, coalescing/refresh, HTTP cookie and Chrome cookie-isolation tests |
| F16 — benchmark/cache/success and metric costs | Explicit cold/warm scenarios; full-body timing; upstream/extraction/screenshot checks; fixed minute metric aggregates | Load-test regressions and API `/stats` assertions |
| F17 — busy health failures, incomplete deadlines/cleanup | Health independent of idle browsers; deadlines include queues; terminate worker process groups and remove partial files; close active tunnels before waiting for server shutdown | Worker timeout/cancel/temp-file, queue, health and active-tunnel shutdown tests |
| F18 — tests/CI/package/docs drift and dead LLM module | Regression suite, Python 3.11/3.12 + Chrome CI, single dependency/version source, Apache-2.0 metadata, updated notebook/env/nginx, removed invalid dead module | Full test/lint/compile/build gates; notebook syntax and package inspection |

## Observed red/green evidence

Tests were added before the corresponding fixes and run at each boundary:

- The initial conversion regression run produced **8 failed, 1 passed** against
  the original extraction logic. The corrected conversion tests pass.
- New option/result, network, worker and API contract tests initially failed on
  missing interfaces or the original removed/incorrect paths; focused suites were
  run after each work package. Additional tests verify PDF/Office conversion.
- A fresh, read-only network review found decompression allocation before size
  enforcement and shutdown waiting on live CONNECT tunnels. The reproduced gzip
  case expanded 16,328 bytes into 16 MiB and allocated approximately 50 MiB with
  a 1 KiB request limit. Both defects were captured in failing tests and fixed.
- Re-review identified compression framing and concatenated-member boundary
  errors. Both were reproduced, then corrected without removing either limit.
  The review closed with **zero outstanding findings** in its bounded network scope.
- A timeout cleanup test failed because a partial output survived. Worker-owned
  temporary directories now make cleanup the parent's responsibility.
- A malformed non-ASCII Bearer token reproduced a TypeError; comparisons now use
  bytes and the authentication test verifies 401 responses for malformed tokens.
- A browser transfer test rejected unbounded `page_source` access; the adapter now
  slices DOM output before transferring it to Python, then enforces the byte cap.
- Batch regressions reproduced missing metrics for URLs that expired in the
  batch queue and an unexpected transport exception escaping the entire batch.
  Both now produce counted, isolated per-URL failures.

The final local run on Python 3.12 passed **68 tests**, with the **three real
Chrome tests skipped**. Ruff, compilation, wheel and source-package builds passed.
A smoke test against a running Uvicorn server verified health (200), unauthorized
access (401), rejection of a private destination (400), and successful extraction
with a cached repeat from a controlled local origin. Only that isolated fixture
process disabled private-destination protection to reach its own origin.

The first remote browser run failed before navigation. A direct Chrome startup
diagnostic reproduced `No usable sandbox`: Ubuntu AppArmor did not cover the
downloaded Chrome-for-Testing path. The CI job now uses the runner's packaged
Chrome and matched ChromeDriver, covered by Ubuntu's existing Chrome profile.
Sandboxing and the host's AppArmor policy remain enabled. Fixture exceptions
include their chained cause in test output while public API errors stay sanitized.

## Design choices and compatibility

The implementation follows the written remediation plan with one deliberate
browser choice: fresh Chrome profiles per job, with warm Python workers. CDP
context reuse could not be validated in the local runtime, whose security policy
prevents Chrome's process-singleton socket. Fresh profiles provide verifiable
session isolation at the cost of a Chrome startup for each rendered URL. The
HTTP-first path and lazy startup remove browser work from static pages.

No production throughput benchmark was run and no numerical speedup is claimed.
The local browser limitation is handled by a separate real-Chrome CI job, not by
weakening the host runtime's security policy. Successful Presidio detection still
requires installing the optional models in the target deployment; tests exercise
the external NLP boundary and fail-closed behavior, not NER accuracy.

See [migration-0.3.md](../migration-0.3.md) for intentional API/default changes,
legacy remote transcription restrictions and rollout/rollback. Limits are
per-process except the on-host shared cache/rate/metric state. Browser `max_bytes`
bounds returned HTML, not total asset traffic or a browser's internal memory.

## Verification commands

```bash
pytest -q
ruff check app tests helper/loadtest.py
python -m compileall -q app helper run.py
python -m build --no-isolation
RUN_SELENIUM_TESTS=1 pytest -q tests/test_selenium_integration.py
```

The PR records the final observed command outputs and CI status. Real browser
results must be assessed from the Chrome job; skipped local integration tests are
not represented as passing browser tests. Static review and deterministic network
regressions do not constitute a penetration test or a dependency vulnerability scan.
