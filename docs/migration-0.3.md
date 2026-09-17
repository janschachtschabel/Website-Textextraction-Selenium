# Migrating to 0.3

The `/crawl`, `/crawl/batch`, `/health` and `/stats` endpoints remain. Review these
intentional contract corrections before upgrading a client:

- Check `success`, not just API HTTP 200 or a nonempty Markdown string.
  Conversion failures no longer return explanatory prose as extracted content.
  Skipped media is `skipped` with empty Markdown; partial responses set `truncated`
  or, for rendered pages that never settled, carry a warning (both `success=false`).
- `status_code` is the upstream status and can be null when Chrome cannot observe
  it. `fetch_engine` and `converter` describe the actual successful path.
  `request_mode` remains the requested mode.
- Single and batch defaults come from the same server settings. The default
  timeout is 120 seconds, retries 1, max_bytes 10 MiB, screenshots off.
  All batch URL deadlines start at batch acceptance, including batch-local waiting.
- Unknown request fields are rejected, instead of silently ignored. PII languages
  are restricted to `de` and `en`; unsupported proxy schemes are rejected.
- Anonymization fails closed. Install the `pii` extra and selected language model.
  Raw links/screenshots are suppressed for anonymized results. Source URL metadata
  is retained. Unanonymized failures never enter the result cache.
- HTTP cookies and Chrome profiles no longer persist across independent requests.
  Persistent logged-in crawling is not supported by this anonymous service.
- Private destinations, including DNS answers, browser subrequests and upstream
  proxy servers, are blocked by default. An upstream HTTP(S) proxy must support
  CONNECT to numeric destinations. There is no SOCKS support.
- `max_bytes` limits decoded HTTP content or returned rendered HTML, not aggregate
  Chrome asset traffic. Gzip/deflate decoding is bounded before allocation; wire
  input additionally has a bounded allowance for compression framing. Unsupported
  encodings and corrupt compressed streams fail explicitly.
- The old `full` media mode now returns `unsupported`: MarkItDown audio/YouTube
  transcription can invoke external services outside the guarded fetch path.
  This service keeps conversion local. Use `skip`, `none`, or `metadata`; ffprobe
  is restricted to local file/pipe input and supported media demuxers. Automatic
  remote YouTube transcript discovery is removed. PDF/Office conversion remains
  available through the `documents` extra.
- `SELENIUM_POOL_SIZE`, `SELENIUM_SCALE_THRESHOLD` and `DEFAULT_STEALTH` are removed.
  Set `SELENIUM_MAX_POOL_SIZE` for the single browser capacity budget; both
  strategies share it. `MAX_CONCURRENT_REQUESTS` now controls actual per-URL
  capacity, independent of browser count. Defaults are 1 Uvicorn process,
  2 browser slots and 2 conversion slots, all lazy. Raising Uvicorn workers
  multiplies capacities. Cache/rate/metrics stores remain shared on one host;
  coalescing applies within a Uvicorn process.
- TLS verification and Chrome sandboxing default to enabled. `SELENIUM_NO_SANDBOX`
  is an explicit opt-in for constrained development environments, used by the
  root-owned Colab demo. Configure real production sandbox support instead.
- Cache keys/storage have a new version; old results are not reused. The new
  default path is under the current user's cache directory. Cache TTL 0 disables
  result caching; a positive size limit is required.
- `/health` describes process readiness and capacity; busy workers are healthy.
  `/stats` uses 60 minute aggregates, exposes `coalesced`, and marks histogram
  percentile estimates as approximate. Raw per-request metrics are no longer
  retained, and cached/coalesced results do not enter extraction latency samples.
- The unused `app.llm` module and heavyweight default NLP/all-format dependencies
  are removed. `pyproject.toml` is the only dependency source; version metadata is
  read from `app.__version__`. License metadata matches the existing Apache-2.0
  LICENSE file.

## Deployment

Use a fresh environment, install the desired extras, copy the updated
`.env.example`, and run the tests before switching traffic. Set an API key for
public access. The nginx example allows a 620-second response window for the
maximum 600-second deadline plus cleanup. The Colab notebook uses the same package
and requires a key before exposing a public tunnel; saved outputs are cleared.

## Rollback

Revert the remediation commits and redeploy the previous application environment.
No persistent content-data migration is needed. The v3 cache/state directory can
be discarded after stopping all workers; it contains only derived cache/metrics/
rate state. Do not mix old and new application processes against the same serving
endpoint during client-contract migration.
