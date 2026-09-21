# Website Text Extraction with Selenium

An HTTP-first FastAPI service that extracts web pages and documents into Markdown.
It uses Trafilatura for main content, MarkItDown for document conversion, and
Selenium/Chrome when JavaScript rendering is needed. No Playwright dependency.

Version 0.10 makes the request surface readable - every field of every model says what it
is for, and `/docs` offers a request that works instead of a body of `"string"` - and ships
a Debian 13 image with Chromium; 0.9 kept a page's mathematics, turning presentation MathML
into LaTeX and no longer dropping formulas a page hides from sighted readers, and stopped
worker teardown from blocking the event loop; 0.8 added Python 3.14 support, full-page screenshots and worker
coverage; 0.7 added request correlation ids, browser status in `/health` and a faster auto
mode;
0.6 added Prometheus metrics, background jobs, an optional robots.txt check and
conditional revalidation; 0.5 added page metadata, an inbound rate limit and faster browser
jobs; 0.4 closed
the exposure gaps found in the September 2026 audit; 0.3 corrected
extraction, privacy, network safety and resource limits. See the
[changelog](CHANGELOG.md), the [migration notes](docs/migration-0.3.md) and the
[audit remediation record](docs/audit-remediation.md).

## Install and run

Python 3.11 to 3.14 and a local writable cache directory are required. Chrome is
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
`/health` is public and reports process readiness even while all workers are busy, plus
whether the configured Chrome and ChromeDriver exist - their paths only while no key is
configured. A missing binary is also logged as a warning at startup instead of failing
the first browser job.
`/stats` uses the same Bearer authentication as the crawl endpoints, and so does
`/metrics`, which serves Prometheus text format: cumulative request, cache-hit and
coalescing counters, a latency histogram of fresh successful extractions, and gauges
for readiness, capacity, worker pools and cache size. The counters live in the shared
state store, so all Uvicorn workers report one series. Scrape it with
`authorization: {credentials: <API_KEY>}` in the Prometheus job. Request bodies
above `MAX_REQUEST_BYTES` (1 MiB) are answered with 413 before authentication. The rest of
the upload is read, bounded to 4 MiB and one second, so the client can read the answer
instead of a connection reset; beyond that the connection is closed.

`INBOUND_RATE_LIMIT_RPS` (off by default) limits crawl requests after authentication:
every URL, also inside a batch, costs one token from a bucket of
`INBOUND_RATE_LIMIT_BURST` (20) tokens that refills at the configured rate. Excess
requests get 429 with `Retry-After`. The bucket is per process and shared by all
clients; failed authentication never consumes tokens.

Every response carries an `X-Request-ID`: the client's own value when it is a plain token
of at most 64 characters, otherwise a generated one. Its failure log lines and those of a
job it started carry the same id.

Every failed crawl is logged as one line with host, mode, upstream status, extraction
status and elapsed time - never the path or query string. `LOG_JSON=true` emits the
same fields as JSON and, like the readable sink, without exception variable values.

For PDF and Office files, install `pip install -e '.[documents]'`. For local PII
processing, install `pip install -e '.[pii]'` and then, for example,
`python -m spacy download de_core_news_lg`. Only the requested language is loaded,
on first use. Models are never downloaded while processing a request.
`requirements.txt` installs the same core dependencies from `pyproject.toml`.

The declared ranges resolve to the newest compatible releases, which is what CI
tests. There are two pinned sets beside them, for two different jobs:

- `constraints.txt` is the version set verified together for development, on any
  platform: `pip install -e '.[documents]' -c constraints.txt`. It pins versions but
  carries no hashes. Refresh it by re-resolving without it, running both suites and
  `pip-audit`, then `pip freeze --exclude-editable`.
- `requirements.lock` is what the container installs. It was resolved on Debian 13
  with Python 3.13 and names a hash for every artefact, so `--require-hashes` rejects
  a package that was re-uploaded under the same version. Being one resolution for one
  target, it is not a cross-platform file; the section on Docker says how to refresh
  it.

Preinstall compatible Chrome and ChromeDriver binaries in production and set
`CHROME_BINARY` and `CHROMEDRIVER_PATH`. Otherwise Selenium Manager locates/downloads
them. Chrome sandboxing and TLS verification are enabled by default.
On Ubuntu 23.10+, prefer packaged Google Chrome at its standard installation path:
Ubuntu's AppArmor profile does not cover arbitrary downloaded Chrome binaries.
See the [Chromium sandbox documentation](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).
CI uses the runner's packaged Chrome and matching ChromeDriver, and checks that
Chrome starts with sandboxing enabled before running browser fixtures.

## Run in Docker on Debian 13

The image is built on Debian 13 (trixie) and takes Chromium and its driver from the
distribution, which ships them as a matched pair - a Chrome fetched separately drifts out
of step with its driver at the next rebuild. It carries the `documents` extra, so PDF and
Office conversion works out of the box. The image is 1.3 GB: 738 MB of that is Chromium
and the libraries it pulls in, 462 MB the Python environment.

### Install Docker

Debian's own `docker.io` package lags behind; these are the steps for Docker's repository,
which publishes for trixie.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### Build and start

```bash
git clone https://github.com/janschachtschabel/Website-Textextraction-Selenium.git
cd Website-Textextraction-Selenium
echo "API_KEY=$(openssl rand -hex 24)" > .env
sudo docker compose up -d --build
```

`API_KEY` is required. The container binds to `0.0.0.0`, and the service refuses any
non-loopback address without a key, so a missing one stops the stack before it starts
rather than exposing an open crawler. The key is passed at run time and never enters the
image. Because a key is set, `/docs` is not published - read the schema from a local
keyless run, as under "Install and run".

Port 8000 is crowded on most machines; `HOST_PORT=8188 docker compose up -d` moves the
published port without editing the file. The service always listens on 8000 inside.

### Check it

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/crawl \
  -H "authorization: Bearer $API_KEY" -H 'content-type: application/json' \
  -d '{"url": "https://example.com", "mode": "js"}'
```

`mode=js` is the one worth trying: it proves Chrome starts inside the container.

### What this setup decides, and why

- **Chrome runs without its own sandbox.** Docker's default seccomp profile blocks the
  namespaces that sandbox needs, and Chrome aborts with "Failed to move to new namespace".
  The container is the boundary instead: an unprivileged user (uid 10001),
  `no-new-privileges`, and the syscall filter intact. The alternative is to make Chrome's
  sandbox the boundary - `SELENIUM_NO_SANDBOX=false` together with
  `--security-opt seccomp=unconfined` - which works, but gives up the container's syscall
  filter in exchange. Pick one. Relaxing seccomp while leaving `SELENIUM_NO_SANDBOX=true`
  is the combination to avoid: it drops the filter and gains nothing.
- **The cache is a named volume.** The service refuses to open a result cache that is not
  private to its user, and a named volume inherits the directory's owner and its `0700`
  mode from the image. A bind mount does not, so `-v ./cache:/var/cache/...` fails at
  startup until the host directory is owned by uid 10001 with mode 0700.
- **`init: true`.** Chrome forks helpers that outlive a crash; without an init process to
  reap them they accumulate as zombies. Use `docker run --init` outside compose.
- **The port is bound to `127.0.0.1`.** Put a reverse proxy in front for anything else;
  `deploy/nginx.conf` is a starting point with the rate limits this service expects.

### Refreshing the lockfile

`requirements.lock` is one resolution for one target, so it is refreshed in that target
rather than on a developer's machine. Every artefact is downloaded to be hashed, which
takes several minutes.

```bash
docker run --rm -v "$PWD:/src:ro" -w /work python:3.13-slim-trixie sh -c '
  pip install -q pip-tools >&2
  mkdir -p app && cp /src/pyproject.toml /src/README.md /src/LICENSE . && cp /src/app/__init__.py app/
  pip-compile -q --generate-hashes --strip-extras --extra documents -o out.lock pyproject.toml >&2
  cat out.lock' > new.lock && mv new.lock requirements.lock
```

The copies of `README.md`, `LICENSE` and `app/__init__.py` are there because the project
metadata declares them. Afterwards rebuild the image and run the suites; a dependency the
lock misses fails the build rather than the deployment, because `--require-hashes` refuses
to install anything the file does not name.

### Operating it

```bash
sudo docker compose logs -f     # LOG_JSON=true is set, one object per line
sudo docker compose ps          # the health check calls the public /health
sudo docker compose down        # add -v to discard the cache volume as well
```


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
files instead of rendering them: the result has no text and a warning. Formulas
appear as `$$...$$`: a page's own TeX when it publishes one, otherwise its
presentation MathML translated to LaTeX, so radicals, fractions, exponents and
vector arrows survive. A MathML copy that the page hides from sighted readers is
unhidden, because extractors drop hidden content. Formulas present only as pixels
cannot be reconstructed reliably.

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
| `revalidated` | Cached result the upstream confirmed unchanged with 304 |
| `links` / `metadata` | Classified links and page metadata, when requested |
| `elapsed_ms` | Duration of this call, including waiting |

An HTTP 200 **from this API** can contain an upstream error or an unsuccessful
extraction. Check `success`, `status_code` and `extraction_status`. Operational
failures use a sanitized API error: 400 for prohibited/invalid destinations, 403 when
the opt-in robots.txt check disallows the URL,
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

### Background jobs

A client that cannot hold a connection open for the whole batch, for example behind
the Colab tunnel, which ends requests after about 125 seconds, submits the same body
to `POST /jobs`. It answers 202 with a `job_id` at once; `GET /jobs/{job_id}` returns
`queued`, `running`, `done` with the batch `result`, or `failed` with an `error`:

```bash
curl http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com","https://www.python.org"],"timeout_ms":300000}'
curl http://127.0.0.1:8000/jobs/<job_id> -H "Authorization: Bearer $API_KEY"
```

Job records live in the shared state store for `JOB_RESULT_TTL` (one hour) after they
finish, so any Uvicorn worker answers the poll. The work runs in the process that
accepted it; `MAX_ACTIVE_JOBS` (10) bounds unfinished jobs per process (503 beyond).
A shutdown marks unfinished jobs `failed`, and a job whose process stopped without
that is reported as lost 30 seconds after its deadline.

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
- `respect_robots_txt` (default `RESPECT_ROBOTS_TXT`, off) checks the requested URL
  against the site's robots.txt (RFC 9309, longest match wins, groups by the user
  agent's product token) and answers 403 when it is disallowed. robots.txt is fetched
  through the same guarded path and cached per origin for an hour; 4xx means no
  restrictions, 5xx or an unreachable server a complete disallow for five minutes.
  Redirect targets are not checked again.
- `accept_language` (default `DEFAULT_ACCEPT_LANGUAGE`, empty) selects the language
  variant of multilingual sites, for example `de,en;q=0.8`. HTTP requests send it as
  given; Chrome receives the language list and sets its own q-values. HTTP requests
  always send a browser-like `Accept` header.
- `screenshot=false` by default. A requested screenshot is available only on the
  browser path; choose `mode=js` when a screenshot is required. `screenshot_full_page=true`
  captures the whole document instead of the viewport, up to 4000 pixels wide and 20000
  pixels high - the page declares its own layout size, and `max_bytes` does not cover a
  screenshot. A larger page is cut and the response carries a warning.
- `extract_links=true`: `links` with every anchor of an HTML page - the absolute URL, its
  visible text (or `aria-label`/`title` for icon-only links), whether it stays on the host,
  and a category: `content`, `nav`, `social`, `auth`, `legal`, `search`, `contact`,
  `download`, `anchor` or `other`. Repeated targets appear once; `javascript:`, `data:`,
  `blob:` and `vbscript:` targets are omitted. Off by default.
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

A successful HTTP result is also kept with its `ETag`/`Last-Modified` for
`REVALIDATION_TTL` (one day). When the fresh entry has expired, the next crawl sends
`If-None-Match`/`If-Modified-Since`; a 304 returns the stored result with
`revalidated: true` and neither downloads nor converts the page again. Browser results
are not revalidated, because a rendered page can change while the document stays the
same. `force_refresh=true` always fetches unconditionally.

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
coverage run -m pytest -q -m 'not selenium' && coverage combine && coverage report
```

The coverage run includes the spawned conversion and browser workers: an idle worker is
asked to exit and flushes its data instead of being killed. Measured that way the suite
covers 92 % of `app/`, against 81 % when only the main process is counted.

CI runs unit/API tests on Python 3.11 to 3.14 plus a separate real Chrome job.
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
