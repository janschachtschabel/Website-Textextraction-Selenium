# Changelog

Versions describe the request/response contract and the operational defaults, not
the internal structure. Dates are release dates of this repository.

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
