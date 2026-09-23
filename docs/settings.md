# Settings

Every environment variable the service reads, with its default. The defaults are the ones
in [`.env.example`](../.env.example), which is a plain `NAME=VALUE` list on purpose so it can
be pasted anywhere - the explanations live here instead of in comments beside them.

All of them are validated at startup. An invalid value stops the service with a message
naming the setting, rather than failing later on a request.

## How to supply them

- **Running it directly:** copy `.env.example` to `.env` beside `run.py`.
- **A container:** pass what you want to change, `docker run -e API_KEY=...`.
- **A panel that deploys a compose file:** set them in the panel's environment editor.
  `docker-compose.yml` names every setting below without a value, so compose takes each from
  whatever the panel provides and leaves out the ones nobody names. `HOST` and `PORT` are
  the exception - see the next section.

Requests may override some of these per crawl; see "Options and privacy" in the
[README](../README.md). An omitted or null option inherits the value here, while an explicit
`false` or `0` in a request is an override.

## In a container, four rules differ

**Do not set `HOST` or `PORT`.** The image sets them to `0.0.0.0` and `8000`, and the
container's own network namespace is what makes that safe. `HOST=127.0.0.1` inside a
container means the service listens on the *container's* loopback, where a published port
reaches nothing - the container starts, looks healthy from the outside, and refuses every
connection.

**`API_KEY` is required.** The service refuses to start on a non-loopback address without
one, so every container needs it. An empty value counts as missing.

**`BIND_ADDRESS` and `HOST_PORT` are read by docker compose, not by the service.** They
decide where the container's port 8000 is published on the host. Setting them in a panel's
environment editor does nothing useful; they belong in a `.env` beside the compose file.

**Leave `RESULT_CACHE_DIR` alone.** The image creates
`/var/cache/website-text-extraction` owned by the service user with mode 0700, and the
compose file mounts a named volume there. Pointing the setting somewhere else aims past that
volume, and the service refuses to start on a cache directory that is not private to its
user - so the cache is lost on every restart at best, and the container does not come up at
worst.

## Crawling in bulk

The defaults suit interactive use: 50 URLs, ten minutes. A deployment that crawls thousands
of pages per run raises three of them together, because they bind each other:

```
MAX_URLS_PER_REQUEST=2000
MAX_TIMEOUT_SECONDS=7200
DEFAULT_MAX_BYTES=2097152
```

`timeout_ms` covers the **whole** batch including queue time, so the deadline has to fit
`URLs × seconds each ÷ max_concurrency`. With four browser workers and about nine seconds a
page, 2000 URLs need roughly 75 minutes; `mode=fast` needs about two. `DEFAULT_MAX_BYTES`
is in that list because 2000 results at the 10 MiB default is a 20 GB worst case.

**If `INBOUND_RATE_LIMIT_RPS` is on, raise `INBOUND_RATE_LIMIT_BURST` with them or turn it
off.** Every URL costs one token, also inside a batch, and a request larger than the burst
is admitted once and then pays off its excess. Measured at the default burst of 20 with a
rate of 2: one 2000-URL request leaves the bucket at -1980, and the next request of any size
waits 990 seconds. That is the limiter working as designed - one bulk request is worth a
thousand seconds of its budget - but at these sizes it reads as an outage. It is off by
default (`INBOUND_RATE_LIMIT_RPS=0`), so this only applies if you turned it on.

One big job is gentler on the service than many small ones. A job's own `max_concurrency`
(at most 10) is applied *before* the service-wide capacity, so a 2000-URL job only ever
presents ten URLs to the queue. Forty jobs of fifty would present forty times their own
concurrency at once and start collecting `503 Crawl queue is full`.

While it runs, `GET /jobs/{job_id}` reports `progress` - how many URLs are finished and how
many produced a result - and each finished URL can be read at once from `results_url`, as a
row naming its position in the request. A job holds at most `max_concurrency` results in
memory. Before 3.0.0 it held all of them until it ended: measured at 2000 URLs, 22 MB where
pages yield 10 KB of Markdown, 63 MB at 30 KB, 165 MB at 80 KB - per job and per worker
process, with `MAX_ACTIVE_JOBS` multiplying it.

One limit remains at this size: a container restart still ends an unfinished job, which is
then reported `failed`. The rows it wrote before stay readable, and their positions say
which URLs to submit again.

## Binding and access

| Setting | Default | What it does |
|---|---|---|
| `HOST` | `127.0.0.1` | Address the service binds. Anything other than `127.0.0.1`, `::1` or `localhost` requires `API_KEY`. |
| `PORT` | `8000` | Port the service binds. |
| `API_KEY` | *(empty)* | Bearer token for `/crawl`, `/crawl/batch`, `/jobs`, `/stats` and `/metrics`. It also protects `/docs`, `/redoc` and `/openapi.json`, which additionally accept it as the password of an HTTP Basic prompt so a browser can reach Swagger UI; the username is not checked. `/` and `/health` stay public. |
| `MAX_REQUEST_BYTES` | `1048576` | Request bodies above this are answered 413 before authentication. |
| `INBOUND_RATE_LIMIT_RPS` | `0` | Crawl requests per second after authentication, `0` for off. Every URL costs one token, also inside a batch. |
| `INBOUND_RATE_LIMIT_BURST` | `20` | Size of that token bucket. |
| `MAX_URLS_PER_REQUEST` | `50` | URLs one `/crawl/batch` or `/jobs` request may carry. Above it the request is refused with 422 naming the limit. Caps at 10000, which is roughly 600 KB of request body. |
| `MAX_TIMEOUT_SECONDS` | `600` | Largest `timeout_ms` a request may ask for, and the ceiling `DEFAULT_TIMEOUT_SECONDS` must stay under. A request above it is refused rather than quietly cut short, because a batch clipped to a shorter deadline fails on its tail with nothing saying why. Caps at 86400. |
| `BIND_ADDRESS` | `127.0.0.1` | **docker compose only.** Host address the container's port is published on. The compose file fetched by itself defaults to `0.0.0.0`. |
| `HOST_PORT` | `8000` | **docker compose only.** Host port the container's 8000 is published on. |

## Logging

| Setting | Default | What it does |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Standard Python level name. |
| `LOG_JSON` | `false` | One JSON object per line instead of readable text. Neither sink includes exception variable values. |

## Crawl defaults

Each of these is what a request gets when it does not say otherwise.

| Setting | Default | What it does |
|---|---|---|
| `DEFAULT_MODE` | `auto` | `fast` never starts a browser, `js` always does, `auto` starts one only when the plain HTML yields too little text - fewer than 500 visible characters from a page that runs JavaScript - or a bot challenge. |
| `DEFAULT_TIMEOUT_SECONDS` | `120` | Deadline for one URL, including retries and cleanup. |
| `DEFAULT_RETRIES` | `1` | Retries after a transient failure. Certificate and policy errors are never retried. |
| `DEFAULT_MAX_BYTES` | `10485760` | Largest response body read per URL, after decompression. |
| `DEFAULT_USER_AGENT` | *(empty)* | Empty uses `WebsiteTextExtraction/<version>` with the project URL. Keep a contact URL in your own value: sites with a bot policy block anonymous crawlers, and Wikimedia answers 403. |
| `DEFAULT_ACCEPT_LANGUAGE` | *(empty)* | `Accept-Language` for every crawl; empty sends none. Example: `de,en;q=0.8`. |
| `RESPECT_ROBOTS_TXT` | `false` | Check robots.txt before each crawl (RFC 9309). |
| `DEFAULT_HEADLESS` | `true` | Run Chrome headless. |
| `DEFAULT_JS_STRATEGY` | `speed` | `speed` also drops images, fonts and media, which it can only do when no screenshot is wanted. `accuracy` loads everything. Applies to `mode=js`: `auto` renders with `accuracy` unless a request names a strategy. |
| `DEFAULT_JS_AUTO_WAIT` | `true` | Wait for the page to settle rather than returning at load - its text still and its data and script requests done, the latter for at most 5 s - and give a bot challenge up to 10 s to let the browser through. |
| `HTML_CONVERTER` | `trafilatura` | First converter to try: `trafilatura`, `markitdown` or `bs4`. The others follow as fallbacks. |
| `TRAFILATURA_CLEAN_MARKDOWN` | `true` | Extract the main content as Markdown. `false` returns the whole page as plain text. |
| `MEDIA_CONVERSION_POLICY` | `skip` | `skip`/`none` ignore audio and video, `metadata` reads their tags with `ffprobe`, which the published image carries. `full` is not implemented and is refused as unsupported. |
| `ALLOW_INSECURE_SSL` | `false` | Accept invalid certificates. |
| `SSRF_PROTECTION` | `true` | Validate every hop and every connection against private and link-local address ranges. Leave it on. |

## Capacity

These are per Uvicorn process, so multiply by `UVICORN_WORKERS`.

| Setting | Default | What it does |
|---|---|---|
| `UVICORN_WORKERS` | `1` | Worker processes serving HTTP. |
| `MAX_CONCURRENT_REQUESTS` | `8` | URLs processed at once. |
| `MAX_QUEUE_SIZE` | `50` | URLs waiting for capacity before new ones are refused. |
| `QUEUE_TIMEOUT_SECONDS` | `60` | How long a URL waits for capacity before it fails. |
| `MAX_ACTIVE_JOBS` | `10` | Unfinished background jobs (`POST /jobs`) per process. |
| `JOB_RESULT_TTL` | `3600` | Seconds a finished job's record and its result rows stay readable. |
| `HTTP_MAX_CONNECTIONS` | `16` | Connection pool for the HTTP fetcher. |
| `SELENIUM_MAX_POOL_SIZE` | `2` | Browser worker processes. None start until a browser job arrives. |
| `CONVERSION_WORKERS` | `2` | Conversion worker processes. |
| `WORKER_MAX_JOBS` | `100` | A worker is replaced after this many successful jobs, which bounds memory growth. |

## Browser

| Setting | Default | What it does |
|---|---|---|
| `CHROME_BINARY` | *(empty)* | Path to Chrome or Chromium. Empty lets Selenium Manager locate or download one; preinstall a matching pair in production. |
| `CHROMEDRIVER_PATH` | *(empty)* | Path to the matching driver. |
| `SELENIUM_NO_SANDBOX` | `false` | Runs Chrome without its own sandbox. The image sets this to `true` because Docker's seccomp profile blocks the namespaces that sandbox needs; the container is the boundary instead. Outside a container, turn it on only for something like a root-owned Colab runtime. |

## Cache and politeness

| Setting | Default | What it does |
|---|---|---|
| `RESULT_CACHE_DIR` | *(empty)* | Where results are cached. Empty uses `$XDG_CACHE_HOME/website-text-extraction` or `~/.cache/website-text-extraction`. All Uvicorn workers on one machine must share it, and the service refuses a directory that is not private to its user. |
| `RESULT_CACHE_TTL` | `300` | Seconds a result is served from cache. |
| `RESULT_CACHE_MAX_SIZE` | `200` | Entries kept. |
| `REVALIDATION_TTL` | `86400` | Seconds an HTTP result stays available for a conditional request with `ETag`/`Last-Modified`; `0` for off. |
| `GLOBAL_RATE_LIMIT_RPS` | `0` | Document attempts per second across all hosts, `0` for off. Counts HTTP retries and redirects and the browser's initial navigation, not the browser's own subrequests. |
| `DEFAULT_DOMAIN_RATE_LIMIT_RPS` | `0` | Per-host attempts per second. Above `0` this is a ceiling: a request may lower it, never raise it. |

## PII removal

The published image and `pip install -e '.[pii]'` bring spaCy's `md` models for both
languages. A model is loaded on first use, in each conversion worker, and never downloaded
while a request is running; one that is not installed answers 503.

Measured on the 3.1.0 image: the first anonymized request in a worker takes 10 to 40 seconds
while the model loads, later ones about 0.4 seconds. A loaded `md` model holds about 350 MiB
in its worker, so two conversion workers with both languages loaded need about 1.4 GB on
top of the service. A worker replaced after `WORKER_MAX_JOBS` loads its model again on its
next anonymized request.

| Setting | Default | What it does |
|---|---|---|
| `PRESIDIO_DE_MODEL` | `de_core_news_md` | German model for `anonymize`. `de_core_news_lg` recognizes a little more at ten times the size, once installed. |
| `PRESIDIO_EN_MODEL` | `en_core_web_md` | English model; `en_core_web_lg` likewise. |
