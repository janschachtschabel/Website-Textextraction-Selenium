# Website Textextraction Selenium

Lightweight FastAPI service for asynchronously crawling web pages and automatically converting
them to structured Markdown – with local PII anonymisation (Presidio) and screenshot support.

## Features

- **Three crawl modes**: `fast` (httpx/HTTP2), `js` (Selenium Chrome), `auto` (preflight analysis)
- **Automatic Markdown conversion** for all formats supported by `markitdown`: HTML, PDF, DOCX, PPTX, XLSX, images …
- **Preflight intelligence**: automatically detects PDF, RSS, YouTube, SPA, CMP, BLOCKED
- **JS rendering**: headless Chrome, stealth mode, cookie banner dismissal, dynamic driver pool with auto-scaling
- **HTTP retries**: automatic retry on 5xx and 429 with `Retry-After` support
- **SSRF protection**: requests to private/loopback addresses are blocked
- **Result cache**: TTL-based shared cache with `cached` flag in the response
- **Batch crawl**: up to N URLs in parallel in a single request (`POST /crawl/batch`)
- **SSL via OS certificate store**: `truststore` integrated – Windows CertStore / macOS Keychain / Linux system certs, no manual CA bundle needed
- **Optional Bearer token authentication** (API_KEY)
- **Structured logging** via Loguru (JSON mode for ELK/Loki, coloured console)
- **PII anonymisation** locally via [Microsoft Presidio](https://microsoft.github.io/presidio/) — no LLM, no external API key, supports German & English
- **Screenshot feature**: viewport PNG 1920×1080 px as Base64 from the rendered browser (JS mode only)
- **Link extraction** with category classification (content, nav, social, legal …)
- **Rate limiting**: two independent layers — global cap + configurable per-domain limit, overridable per request

---

## Quick Start

```powershell
git clone <repo-url>
cd Website-Textextraction-Selenium

# Install dependencies (pyproject.toml)
pip install -e .

# Copy and adjust configuration
copy .env.example .env

# Start the API
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

API running at: <http://localhost:8000>
Interactive docs: <http://localhost:8000/docs>

---

## Installation

### Prerequisites

- Python ≥ 3.11
- Google Chrome (for JS mode; WebDriver is loaded automatically)

### Option A – pip (classic)

```powershell
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -e .                # installs all dependencies from pyproject.toml
```

### Option B – uv (recommended, faster)

```powershell
pip install uv
uv sync                         # reads pyproject.toml, creates .venv automatically
```

### Option C – conda (existing environment)

```powershell
pip install -e .
```

> **SSL note (Windows/conda):** `truststore` is included as a dependency and
> automatically uses the Windows certificate store. Manually setting
> `ALLOW_INSECURE_SSL=true` is normally **not** required.

### With dev dependencies (ruff, pytest)

```powershell
pip install -e ".[dev]"
# or
uv sync --extra dev
```

---

## Configuration

Copy `.env.example` to `.env` and adjust the values.

### Full `.env` Reference

| Variable | Type / Values | Default | Description |
|---|---|---|---|
| **Server** | | | |
| `HOST` | string | `0.0.0.0` | Bind address |
| `PORT` | int | `8000` | TCP port |
| **Logging** | | | |
| `LOG_LEVEL` | DEBUG\|INFO\|WARNING\|ERROR | `INFO` | Minimum log level (Loguru) |
| `LOG_JSON` | true\|false | `false` | JSON format for log aggregation (ELK, Loki) |
| **Authentication** | | | |
| `API_KEY` | string | *(empty)* | When set: Bearer token required. Empty = open access. |
| **Crawl Defaults** | | | |
| `DEFAULT_MODE` | auto\|fast\|js | `auto` | Default crawl mode |
| `DEFAULT_JS_STRATEGY` | speed\|accuracy | `speed` | JS mode strategy |
| `DEFAULT_TIMEOUT_SECONDS` | int 1–600 | `240` | Total timeout per request |
| `DEFAULT_RETRIES` | int 0–10 | `2` | Retries (network + 5xx/429) |
| `DEFAULT_MAX_BYTES` | int | `10485760` | Max. response size (10 MB) |
| `DEFAULT_HEADLESS` | true\|false | `true` | Headless Chrome |
| `DEFAULT_STEALTH` | true\|false | `true` | Anti-detection in JS mode |
| `DEFAULT_JS_AUTO_WAIT` | true\|false | `true` | Internal auto-waits |
| `DEFAULT_USER_AGENT` | string | Chrome 136 | User-agent for Selenium |
| **Selenium Pool** | | | |
| `SELENIUM_POOL_SIZE` | int ≥1 | `2` | Initial pool size |
| `SELENIUM_MAX_POOL_SIZE` | int ≥POOL_SIZE | `4` | Auto-scaling upper limit |
| `SELENIUM_SCALE_THRESHOLD` | float 0–1 | `0.8` | Utilisation threshold for scale-up (80 %) |
| **Capacity & Queue** | | | |
| `MAX_CONCURRENT_REQUESTS` | int ≥1 | `8` | Max. parallel requests |
| `MAX_QUEUE_SIZE` | int ≥0 | `50` | Queue capacity |
| `QUEUE_TIMEOUT_SECONDS` | int ≥0 | `60` | Max. wait time in queue (s) |
| **Media Handling** | | | |
| `MEDIA_CONVERSION_POLICY` | skip\|metadata\|full\|none | `skip` | Audio/video handling (see below) |
| **HTML Conversion** | | | |
| `HTML_CONVERTER` | trafilatura\|markitdown\|bs4 | `trafilatura` | HTML→Markdown engine |
| `TRAFILATURA_CLEAN_MARKDOWN` | true\|false | `true` | Clean Markdown (true) vs. raw text (false) |
| **Security** | | | |
| `ALLOW_INSECURE_SSL` | true\|false | `false` | Disable SSL validation (for testing only) |
| `SSRF_PROTECTION` | true\|false | `true` | Block private/loopback IPs |
| **Result Cache** | | | |
| `RESULT_CACHE_TTL` | int ≥0 | `1800` | Cache lifetime in seconds (0 = disabled) |
| `RESULT_CACHE_MAX_SIZE` | int ≥1 | `500` | Max. cache size in MB |
| **Rate Limiting** | | | |
| `GLOBAL_RATE_LIMIT_RPS` | float ≥0 | `0` | Global limit: max. crawl requests/s across all domains (0 = disabled) |
| `DEFAULT_DOMAIN_RATE_LIMIT_RPS` | float ≥0 | `0` | Per-domain default: max. requests/s per target domain (0 = disabled) |
| **PII Anonymisation (Presidio)** | | | |
| `PRESIDIO_DE_MODEL` | string | `de_core_news_lg` | spaCy model for German (once: `python -m spacy download de_core_news_lg`) |
| `PRESIDIO_EN_MODEL` | string | `en_core_web_lg` | spaCy model for English (once: `python -m spacy download en_core_web_lg`) |
| **Multi-Worker** | | | |
| `UVICORN_WORKERS` | int ≥1 | `2` | Number of Uvicorn worker processes (via `python run.py`) |

---

## API Reference

### `POST /crawl`

Crawls a URL and returns structured Markdown.

**Authentication** (when `API_KEY` is set): `Authorization: Bearer <API_KEY>`

#### Request Schema

```json
{
  "url": "https://example.com",
  "mode": "auto",
  "js_strategy": "speed",
  "html_converter": "trafilatura",
  "trafilatura_clean_markdown": true,
  "media_conversion_policy": "skip",
  "allow_insecure_ssl": null,
  "extract_links": false,
  "screenshot": true,
  "anonymize": false,
  "anonymize_language": "de",
  "crawl_rate_limit_rps": null,
  "retries": 2,
  "timeout_ms": 30000,
  "max_bytes": 1048576
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | **required** | URL to crawl |
| `mode` | auto\|fast\|js | `.env` | Crawl mode |
| `js_strategy` | speed\|accuracy | `.env` | JS wait strategy |
| `html_converter` | trafilatura\|markitdown\|bs4 | `.env` | Overrides `HTML_CONVERTER` |
| `trafilatura_clean_markdown` | bool\|null | `.env` | null = `.env` default |
| `media_conversion_policy` | skip\|metadata\|full\|none | `.env` | Overrides `MEDIA_CONVERSION_POLICY` |
| `allow_insecure_ssl` | bool\|null | `.env` | null = `.env` default |
| `extract_links` | bool | `false` | Extract and categorise all links |
| `screenshot` | bool | `true` | Viewport screenshot 1920×1080 px as Base64 PNG (only when Selenium active: `mode=js` or `auto`+JS path; otherwise `null`) |
| `anonymize` | bool | `false` | Enable PII anonymisation via Presidio (local, no API key) |
| `anonymize_language` | de\|en | `de` | Language for PII detection (`de` = German, `en` = English) |
| `crawl_rate_limit_rps` | float\|null | `.env` | Per-domain limit for **this** request: `null` = server default, `0.0` = no limit, `> 0` = max req/s |
| `retries` | int 0–10 | `.env` | Retries on network errors or 5xx/429 |
| `timeout_ms` | int | `.env` | Timeout in milliseconds (overrides `DEFAULT_TIMEOUT_SECONDS`) |
| `max_bytes` | int | `.env` | Max. response size in bytes (overrides `DEFAULT_MAX_BYTES`) |

#### Response Schema

```json
{
  "request_mode": "js",
  "requested_url": "https://example.com/",
  "final_url": "https://example.com/",
  "status_code": 200,
  "redirected": false,
  "content_type": "text/html",
  "markdown": "# Example Domain\n\n...",
  "markdown_length": 167,
  "error_page_detected": false,
  "links": null,
  "screenshot_base64": "iVBORw0KGgoAAAANSUhEUgAA...",
  "anonymization": null,
  "elapsed_ms": 8210,
  "cached": false
}
```

| Field | Type | Description |
|---|---|---|
| `request_mode` | string | Actually used mode |
| `requested_url` | string | Original URL from the request |
| `final_url` | string | URL after redirects |
| `status_code` | int | HTTP status code of the target page |
| `redirected` | bool | Whether any redirects occurred |
| `content_type` | string | MIME type of the response |
| `markdown` | string | Extracted Markdown content (already anonymised when `anonymize=true`) |
| `markdown_length` | int | Character count of the Markdown |
| `error_page_detected` | bool | Error page detection (even at HTTP 200) |
| `links` | array\|null | Extracted links (only when `extract_links: true`) |
| `screenshot_base64` | string\|null | Viewport PNG as Base64 (only when `screenshot: true` and Selenium active) |
| `anonymization` | object\|null | PII metadata (only when `anonymize: true`) |
| `elapsed_ms` | int | Total duration in milliseconds |
| `cached` | bool | `true` = result was served from cache |

#### Rate Limiting

Rate limiting consists of **two independent layers** that can both be active simultaneously:

| Layer | Controlled by | Overridable per request? |
|---|---|---|
| **Global** | `GLOBAL_RATE_LIMIT_RPS` in `.env` | ❌ server-side only |
| **Per-domain (default)** | `DEFAULT_DOMAIN_RATE_LIMIT_RPS` in `.env` | ✅ via `crawl_rate_limit_rps` in request |
| **Per-domain (request)** | `crawl_rate_limit_rps` in request body | ✅ directly |

Example: server has `DEFAULT_DOMAIN_RATE_LIMIT_RPS=1` (1 req/s per domain). A single request can reduce this further with `"crawl_rate_limit_rps": 0.5` or disable it for this domain with `"crawl_rate_limit_rps": 0.0`.

#### Screenshot Feature

`"screenshot": true` captures a **1920×1080 px PNG screenshot** after full rendering:

- Captured **after** cookie banner dismissal and DOM stabilisation
- Returned as a Base64-encoded string in `screenshot_base64`
- **Only available** when Selenium is active (`mode=js` or `mode=auto` with JS path)
- For `mode=fast` or `auto`+HTTP path: `screenshot_base64` is always `null`
- Average PNG size: ~400–500 KB (before Base64 encoding)

#### PII Anonymisation

`"anonymize": true` detects and replaces personally identifiable information in the extracted Markdown **locally via [Microsoft Presidio](https://microsoft.github.io/presidio/)** — no LLM, no external API call:

| Entity type | Example | Replacement |
|---|---|---|
| `PERSON` | John Smith | `<PERSON>` |
| `EMAIL_ADDRESS` | john@example.com | `<EMAIL_ADDRESS>` |
| `PHONE_NUMBER` | +1-555-0100 | `<PHONE_NUMBER>` |
| `LOCATION` | New York | `<LOCATION>` |
| `ORGANIZATION` | Acme Corp | `<ORGANIZATION>` |
| `DATE_TIME` | 2024-01-01 | `<DATE_TIME>` |
| `IP_ADDRESS` | 192.168.1.1 | `<IP_ADDRESS>` |

Models are loaded in the background at server start (~30–60 s). Until then `anonymization.warning` returns a notice and the text remains unchanged.

**Anonymisation sub-object** (when `anonymize: true`):

```json
{
  "anonymization": {
    "entities_found": ["EMAIL_ADDRESS", "LOCATION", "PERSON", "PHONE_NUMBER"],
    "entity_count": 5,
    "warning": null
  }
}
```

Possible entity types: `PERSON` · `EMAIL_ADDRESS` · `PHONE_NUMBER` · `LOCATION` · `ORGANIZATION` · `DATE_TIME` · `IP_ADDRESS` · and more.

> Models are loaded in the background at server start (~30–60 s). Until then `anonymization.warning` returns a notice and the text remains unchanged.

**Link object** (when `extract_links: true`):

```json
{
  "links": [
    {
      "url": "https://www.iana.org/domains/example",
      "text": "More information...",
      "internal": false,
      "category": "content"
    }
  ]
}
```

Possible `category` values: `content` · `nav` · `social` · `auth` · `legal` · `search` · `contact` · `download` · `anchor` · `other`

---

### `POST /crawl/batch`

Crawls multiple URLs in parallel in a single request.

**Authentication**: same as `/crawl`

```json
{
  "urls": ["https://example.com", "https://httpbin.org/html"],
  "mode": "fast",
  "html_converter": "trafilatura",
  "screenshot": true,
  "anonymize": false,
  "anonymize_language": "de",
  "crawl_rate_limit_rps": null,
  "max_concurrency": 4
}
```

All fields from `/crawl` (except `url`) are also valid and are applied to **all** URLs.

| Field | Type | Default | Description |
|---|---|---|---|
| `urls` | array | **required** | List of URLs (max. 50) |
| `max_concurrency` | int 1–10 | `3` | Parallel crawls within the batch |
| `screenshot` | bool | `true` | Screenshot for all URLs (JS mode only) |
| `anonymize` | bool | `false` | PII anonymisation for all URLs |
| `anonymize_language` | de\|en | `de` | Language for PII detection |
| `crawl_rate_limit_rps` | float\|null | `.env` | Per-domain limit for all batch URLs |
| *(all other /crawl fields)* | | | Applied to all URLs |

**Response:**


```json
{
  "total": 2,
  "succeeded": 2,
  "failed": 0,
  "elapsed_ms": 1401,
  "results": [
    {
      "url": "https://example.com/",
      "success": true,
      "result": { "..." : "..." },
      "error": null
    }
  ]
}
```

---

### `GET /health`

System status and driver pool info.

```json
{
  "status": "ok",
  "driver_pool_ready": true,
  "pools_warming": false,
  "selenium_pools": {
    "normal": { "size": 2, "usage": 0, "available": 2 },
    "eager":  { "size": 2, "usage": 0, "available": 2 }
  },
  "cache": {
    "size": 12,
    "maxsize": 200,
    "ttl": 300
  }
}
```

| `status` | Meaning |
|---|---|
| `ok` | Pools ready, all systems normal |
| `starting` | Driver pool is being initialised (shortly after start) |
| `degraded` | Pool not ready or error during initialisation |

---

### `GET /stats`

Real-time capacity monitoring.

**Authentication**: same as `/crawl`

```json
{
  "concurrent_requests": 3,
  "max_concurrent": 8,
  "queue_size": 0,
  "max_queue_size": 50,
  "selenium_pools": {
    "normal": { "size": 4, "usage": 3, "available": 1 },
    "eager":  { "size": 2, "usage": 0, "available": 2 }
  },
  "capacity_utilization": {
    "processing": "3/8",
    "queue": "0/50",
    "total_capacity": "3/58"
  }
}
```

---

## Curl Examples

```bash
# Simple fast crawl
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "mode": "fast"}'

# Auto mode with link extraction
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "mode": "auto", "extract_links": true}'

# With Bearer token (when API_KEY is set)
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer my-secret-key" \
  -d '{"url": "https://example.com"}'

# Capture screenshot (JS mode, default: screenshot=true)
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "mode": "js", "screenshot": true}'

# Disable screenshot (fast mode, no Selenium)
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "mode": "fast", "screenshot": false}'

# PII anonymisation (local via Presidio, no API key needed)
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "anonymize": true, "anonymize_language": "en"}'

# Rate limiting: max. 0.5 requests/s to this domain
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "crawl_rate_limit_rps": 0.5}'

# Disable rate limiting for this request
curl -X POST http://localhost:8000/crawl \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "crawl_rate_limit_rps": 0.0}'

# Batch crawl with screenshot and rate limit
curl -X POST http://localhost:8000/crawl/batch \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://example.com", "https://httpbin.org/html"],
    "mode": "js",
    "screenshot": true,
    "crawl_rate_limit_rps": 1.0,
    "max_concurrency": 2
  }'

# Health check
curl http://localhost:8000/health

# Capacity monitoring
curl http://localhost:8000/stats
```

---

## Architecture

### Crawl Modes

**`fast`** – httpx (async)
- HTTP/2, automatic redirects, cookie persistence
- Retries on 5xx and 429 (with `Retry-After` header support)
- SSL via `truststore` (OS certificate store)
- Suitable for: static HTML, PDFs, Office documents

**`js`** – Selenium Chrome (thread-based pool)
- Headless Chrome with stealth flags
- Anti-automation detection: `--disable-blink-features=AutomationControlled`
- Cookie banner dismissal (heuristic selectors, JS-click fallback)
- Strategies: `speed` (1 s settle, ~2–6 s total) · `accuracy` (2 s settle, ~8–12 s)
- Fallback: if `speed` fails with renderer timeout, one `accuracy` retry

**`auto`** – Preflight → Routing
1. httpx probe of the URL
2. BeautifulSoup analysis: text density, SPA markers, CMP patterns, RSS links
3. Routed to one of: `HTTP_ONLY`, `JS_LIGHT`, `PDF`, `RSS`, `YOUTUBE`, `BLOCKED`
4. `BLOCKED` on HTTP 403 / 407 / 429 / 5xx (Selenium attempt is skipped)

### Driver Pool

- **Start**: `SELENIUM_POOL_SIZE` Chrome instances (default: 2)
- **Auto-scaling**: above 80 % utilisation, scales up to `SELENIUM_MAX_POOL_SIZE` (default: 8)
- **Health check**: broken drivers are automatically detected and replaced
- **Warm-up**: all drivers are initialised at startup; `/health` reports `"status": "starting"` until done
- **Two pools**: `normal` (standard) and `eager` (eager page-load strategy)

### HTML Conversion & Fallback Chain

```
trafilatura (default)
  └─► Fallback: MarkItDown
        └─► Fallback: BeautifulSoup

markitdown
  └─► Fallback: BeautifulSoup

bs4 (no fallback)
```

After extraction, `preserve_mathematical_content()` and `enhance_table_structure()` are always applied.

**Circuit breaker**: after several unexpected MarkItDown errors, MarkItDown is automatically
disabled process-wide and the fallback is used. Expected conversion errors (broken PDFs etc.) do not trigger the breaker.

### Result Cache

- SQLite-backed `diskcache` (process- and thread-safe), cache key = `(url, mode, html_converter, …)`
- **Shared across all worker processes** — cache hits apply in multi-worker deployments too
- `cached: true` in the response marks a cache hit
- Configurable: `RESULT_CACHE_TTL` (seconds) and `RESULT_CACHE_MAX_SIZE` (MB)
- Cache statistics visible in `GET /health` under `cache`

### Security

- **SSRF protection**: requests to `127.x`, `10.x`, `172.16–31.x`, `192.168.x`, `169.254.x` and `::1`
  are rejected with HTTP 400 (configurable via `SSRF_PROTECTION`)
- **Bearer token auth**: optional via `API_KEY` in `.env`; protects `/crawl`, `/crawl/batch`, `/stats`
- **SSL**: `truststore` uses the OS certificate store – no manually managed CA bundle needed

### Media Handling (`MEDIA_CONVERSION_POLICY`)

| Value | Behaviour |
|---|---|
| `skip` | Audio/video not converted; short placeholder Markdown (recommended) |
| `metadata` | Metadata via `ffprobe` as JSON in Markdown (ffprobe/ffmpeg required) |
| `full` | Full conversion via markitdown/ffmpeg (slow, high resource usage) |
| `none` | No Markdown output for media |

---

## Logging

Logging is based on [Loguru](https://loguru.readthedocs.io/).

```bash
# Coloured console (default)
LOG_LEVEL=INFO LOG_JSON=false uvicorn app.main:app ...

# JSON format for log aggregation (ELK, Grafana Loki)
LOG_JSON=true uvicorn app.main:app ...
```

Verbose logs from Selenium, httpx and webdriver-manager are automatically throttled to `WARNING`.

---

## Error Page Detection

`error_page_detected: true` is set when:
- HTTP status ≥ 400, **or**
- The text contains typical indicators: “not found”, “forbidden”, “captcha”, “cloudflare”, “404”, etc.

Note: Some pages return error content with HTTP 200 (branded 404 pages). In that case
`status_code=200` but `error_page_detected` can be `true`. The API does **not** fail in this case.

---

## Troubleshooting

**Server starts but `/health` shows `"status": "starting"`**
→ Chrome drivers are being initialised. Wait briefly (~5–15 s depending on hardware).

**SSL error (`CERTIFICATE_VERIFY_FAILED`)**
→ `truststore` is active by default and uses the OS certificate store.
If issues still occur (e.g. corporate proxy with custom CA):
```ini
# Last resort — for testing only!
ALLOW_INSECURE_SSL=true
```

**JS mode not working**
→ Chrome WebDriver is downloaded automatically on first start.
→ Check antivirus/firewall (Chrome processes may be blocked).
→ Set `SELENIUM_POOL_SIZE=1` in `.env` for a minimal debug start.

**HTTP 503 Service Unavailable**
→ Server queue full. Increase `MAX_QUEUE_SIZE` or `QUEUE_TIMEOUT_SECONDS`, or reduce load.

**HTTP 504 Gateway Timeout**
→ Request waited too long in the queue. Increase `QUEUE_TIMEOUT_SECONDS`.

**HTTP 429 / 403 from target site → empty Markdown**
→ Expected behaviour: `error_page_detected: true`, `markdown_length: 0`.
Auto mode switches to the `BLOCKED` strategy and skips Selenium.

**Presidio anonymisation returns `warning`**
→ Models not yet loaded (running in the background, ~30–60 s after start).
→ Check `python -m spacy download de_core_news_lg en_core_web_lg` — models must be installed once.

**Pool tuning**
- Low traffic: `SELENIUM_POOL_SIZE=1`, `SELENIUM_MAX_POOL_SIZE=4`
- Burst load: `MAX_QUEUE_SIZE=100`, `QUEUE_TIMEOUT_SECONDS=120`
- Sustained high load: increase `SELENIUM_MAX_POOL_SIZE`, check CPU/RAM

---

## Helper Scripts (`helper/`)

Utility and test scripts not part of the API core.

### `helper/loadtest.py` – Load Test

Asynchronous load test against the running API.

```powershell
# All output (JSON + plots) goes to helper/
python helper/loadtest.py

# Against a remote instance or with auth:
$env:API_BASE="http://remotehost:8000"
$env:API_KEY="my-token"
python helper/loadtest.py
```

The test runs in **two phases**:

| Phase | Modes | Converters | Concurrency | Screenshot |
|---|---|---|---|---|
| 1 – Standard | fast, js | trafilatura, markitdown | 1, 2, 4, 8 | no |
| 2 – JS+Screenshot | js | trafilatura | 1, 2, 4 | **yes** |

**Configuration** (top of the file):

| Variable | Default | Meaning |
|---|---|---|
| `MODES` | `["fast", "js"]` | Phase-1 modes; optionally add `"auto"` |
| `CONVERTERS` | `["trafilatura", "markitdown"]` | HTML converters for Phase 1 |
| `CONCURRENCY_LEVELS` | `[1, 2, 4, 8]` | Concurrent requests per level (Phase 1) |
| `SCREENSHOT_CONCURRENCY_LEVELS` | `[1, 2, 4]` | Concurrent requests for Phase 2 |
| `REQUEST_TIMEOUT` | `180` s | Timeout per request |

**URL list**: `helper/test_urls.txt` (one URL per line, currently ~40 verified URLs).
If the file is missing, 5 built-in fallback URLs are used.

**Output files** (all in `helper/`):

- `loadtest_raw_<ts>.json` – raw data for all requests (incl. `group`, `screenshot_kb`)
- `loadtest_plot_<ts>.png` – Phase-1 chart (mean / P50 / P95 per mode+converter)
- `loadtest_screenshot_<ts>.png` – Phase-2 chart (JS+screenshot)
- `loadtest_errors_<ts>.png` – error distribution by type and concurrency level
- `loadtest_patterns_<ts>.png` – root-cause analysis (keyword patterns)

> **Dependencies** (load test only): `aiohttp`, `matplotlib`, `numpy`
> ```powershell
> pip install aiohttp matplotlib numpy
> ```

---

## Production — Multi-Worker + nginx

### Starting the Server (Multi-Worker)

```powershell
# Production: reads UVICORN_WORKERS from .env (default: 2)
python run.py

# or via hatch:
hatch run serve
```

### Deployment Profiles

Two pre-configured profiles for different requirements:

#### ⚡ Performance Setting *(default — recommended from 8 GB RAM / 2 CPU cores)*

```ini
UVICORN_WORKERS=2          # 2 parallel worker processes
SELENIUM_POOL_SIZE=2       # 4 Chrome at startup (2 per worker)
SELENIUM_MAX_POOL_SIZE=4   # max. 8 Chrome (4 per worker) → 8 parallel JS renders
RESULT_CACHE_TTL=1800      # 30 min cache — reduces crawl load for recurring URLs
RESULT_CACHE_MAX_SIZE=500  # 500 MB cache
```

> **Load-test result**: sweet-spot at `conc=4` — ~3 s mean latency, wall time 32 s for 39 URLs, 0 errors.
> Scaling to `conc=8` is possible; above that latency rises to ~5 s (pool saturation).

#### 🛡️ Safe Setting *(development / low-resource environments / first run)*

```ini
UVICORN_WORKERS=1          # single worker — easy to debug
SELENIUM_POOL_SIZE=1       # 2 Chrome at startup (1 per worker)
SELENIUM_MAX_POOL_SIZE=4   # max. 8 Chrome under load
RESULT_CACHE_TTL=300       # 5 min cache
RESULT_CACHE_MAX_SIZE=200  # 200 MB cache
```

> **RAM footprint**: ~2–3 GB (startup) to ~5 GB (under load). Suitable for Colab Free, small VPS (4 GB RAM).

> **Note:** Cache and rolling-window metrics (`/stats`) are shared across workers via `diskcache` (SQLite, WAL mode)
> — no Redis required. Rate limiters and Presidio models are isolated per worker.

### Resource Estimation

#### Formula

```
Chrome instances at startup  =  UVICORN_WORKERS  ×  SELENIUM_POOL_SIZE  ×  2
Chrome instances maximum     =  UVICORN_WORKERS  ×  SELENIUM_MAX_POOL_SIZE  ×  2
                                                     (factor 2 = normal + eager pool)

RAM estimate                 =  Chrome instances  ×  ~350 MB
                             +  UVICORN_WORKERS   ×  ~200 MB  (Python process + Presidio)
                             +  ~300 MB base (OS, diskcache, API overhead)
```

#### Configuration Profiles

| Profile | `UVICORN_WORKERS` | `SELENIUM_POOL_SIZE` | `SELENIUM_MAX_POOL_SIZE` | Chrome Start | Chrome Max | RAM Min | RAM Max |
|---|---|---|---|---|---|---|---|
| 🛡️ **Safe** | `1` | `1` | `4` | 2 | 8 | ~2 GB | ~5 GB |
| ⚡ **Performance** *(default)* | `2` | `2` | `4` | 8 | 16 | ~4 GB | ~8 GB |
| **Production (4 cores)** | `4` | `2` | `6` | 16 | 48 | ~8 GB | ~22 GB |
| **High throughput** | `4` | `4` | `8` | 32 | 64 | ~14 GB | ~28 GB |

> RAM values are estimates. Presidio (`de_core_news_lg` + `en_core_web_lg`) uses ~600–800 MB per worker.
> Load-test basis: Performance Setting, JS mode, 39 URLs, 0 errors up to `conc=4` (~3 s latency), degradation above `conc=8`.

#### CPU Recommendations

| `UVICORN_WORKERS` | Recommended CPU cores | Typical scenario |
|---|---|---|
| `1` | 1–2 cores | Development, single user |
| `2` | 2–4 cores | Small team, moderate load |
| `4` | 4–8 cores | Production, multiple parallel users |
| `8` | 8+ cores | High load, batch crawling |

> Rule of thumb: **1 worker per physical CPU core**. More workers than cores provide no advantage
> due to Selenium’s high thread demand and unnecessarily increase RAM usage.

### nginx as Reverse Proxy

A ready-made configuration is available at `deploy/nginx.conf`.

```bash
# Set up configuration (Linux/macOS)
sudo cp deploy/nginx.conf /etc/nginx/sites-available/volltextextraktion
sudo ln -s /etc/nginx/sites-available/volltextextraktion /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Key nginx settings in `deploy/nginx.conf`:

| Parameter | Value | Purpose |
|---|---|---|
| `proxy_read_timeout` | 200s | JS mode can take up to 180 s |
| `keepalive` | 32 | Reuse TCP connections to the backend |
| `client_max_body_size` | 2M | Sufficient for batch requests |
| `/health` | `access_log off` | Do not log monitoring pings |

HTTPS via Certbot (Let’s Encrypt) is prepared and commented out in the configuration.

---

## Development

```powershell
# Install dependencies including dev tools
pip install -e ".[dev]"

# Linter
ruff check app/
# or via hatch:
hatch run lint

# Formatter
ruff format app/
# or via hatch:
hatch run fmt

# Server with auto-reload
hatch run dev
# or directly:
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## License

MIT – see [LICENSE](LICENSE)
