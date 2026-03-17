#!/usr/bin/env python3
"""
Load test for: Volltextextraktion Selenium MD API
Combinations: mode=[fast|js|auto] x converter=[trafilatura|markitdown]
Concurrency:  progressively increasing
"""

import asyncio
import json
import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiohttp
import matplotlib.pyplot as plt
import numpy as np

_HERE = Path(__file__).parent  # directory containing this script

# ── Configuration ──────────────────────────────────────────────────────────────

API_BASE     = os.getenv("API_BASE", "http://localhost:8000")
API_ENDPOINT = f"{API_BASE}/crawl"
API_KEY      = os.getenv("API_KEY", "")   # set env var if bearer auth is enabled

MODES                         = ["fast", "js"]       # add "auto" to include preflight mode
CONVERTERS                    = ["trafilatura", "markitdown"]
CONCURRENCY_LEVELS            = [1, 2, 4, 8]
SCREENSHOT_CONCURRENCY_LEVELS = [1, 2, 4]          # JS+screenshot phase (skip conc=8, heavier load)
REQUEST_TIMEOUT               = 180  # seconds per request (js mode can be slow)
URL_FILE           = _HERE / "test_urls.txt"  # one URL per line

TEST_URLS: list[str] = [               # fallback if test_urls.txt is missing
    "https://example.com",
    "https://httpbin.org/html",
    "https://en.wikipedia.org/wiki/Python_(programming_language)",
    "https://www.bbc.com/news",
    "https://arxiv.org/abs/1706.03762",
]

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Result:
    url:                 str
    mode:                str
    converter:           str
    concurrency:         int
    response_time:       float
    status_code:         int | None
    success:             bool
    error:               str | None = None
    error_type:          str | None = None  # timeout | connection | http_NNN | json_parse | empty_body | other
    error_page_detected: bool  = False
    cached:              bool  = False
    markdown_length:     int   = 0
    text_snippet:        str   = ""     # first 300 chars of extracted markdown
    group:               str   = "standard"  # "standard" | "js_screenshot"
    screenshot_kb:       float = 0.0    # decoded PNG size in KB (0 if no screenshot)

# ── Error classification ──────────────────────────────────────────────────────

def classify_error(exc: Exception) -> str:
    """Map an exception to a short category label."""
    name = type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError) or "Timeout" in name:
        return "timeout"
    if isinstance(exc, aiohttp.ClientConnectorError | aiohttp.ServerDisconnectedError | aiohttp.ClientConnectionError) or "Connect" in name or "Disconnect" in name:
        return "connection"
    if isinstance(exc, json.JSONDecodeError) or "JSON" in name or "Decode" in name:
        return "json_parse"
    return f"other:{name}"

# ── Core async logic ───────────────────────────────────────────────────────────

async def fetch(
    session:     aiohttp.ClientSession,
    sem:         asyncio.Semaphore,
    url:         str,
    mode:        str,
    converter:   str,
    concurrency: int,
    screenshot:  bool = False,
    group:       str  = "standard",
) -> Result:
    payload = {
        "url":            url,
        "mode":           mode,
        "html_converter": converter,
        "screenshot":     screenshot,
    }
    headers: dict[str, str] = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    async with sem:
        t0 = time.monotonic()
        try:
            async with session.post(
                API_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                elapsed = time.monotonic() - t0
                raw = await resp.text()
                if resp.status != 200:
                    try:
                        err_body = json.loads(raw)
                        detail   = err_body.get("detail", "")
                        err_msg  = str(detail)[:1000] if detail else raw[:1000]
                    except Exception:
                        err_msg = raw[:1000]
                    return Result(
                        url, mode, converter, concurrency,
                        elapsed, resp.status, False,
                        error=err_msg,
                        error_type=f"http_{resp.status}",
                        group=group,
                    )
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError as jexc:
                    return Result(
                        url, mode, converter, concurrency,
                        elapsed, resp.status, False,
                        error=str(jexc),
                        error_type="json_parse",
                        group=group,
                    )
                markdown     = body.get("markdown", "")         if isinstance(body, dict) else ""
                err_page     = body.get("error_page_detected", False)
                cached       = body.get("cached", False)
                b64          = body.get("screenshot_base64") if isinstance(body, dict) else None
                ss_kb        = len(b64) * 0.75 / 1024 if b64 else 0.0
                if not markdown:
                    return Result(
                        url, mode, converter, concurrency,
                        elapsed, resp.status, False,
                        error="empty markdown in response",
                        error_type="empty_body",
                        error_page_detected=err_page,
                        cached=cached,
                        group=group,
                        screenshot_kb=ss_kb,
                    )
                return Result(
                    url, mode, converter, concurrency,
                    elapsed, resp.status, True,
                    error_page_detected=err_page,
                    cached=cached,
                    markdown_length=len(markdown),
                    text_snippet=markdown[:300],
                    group=group,
                    screenshot_kb=ss_kb,
                )
        except Exception as exc:
            return Result(
                url, mode, converter, concurrency,
                time.monotonic() - t0, None, False,
                error=str(exc),
                error_type=classify_error(exc),
                group=group,
            )


def load_test_urls() -> list[str]:
    """Load URLs from URL_FILE if present, otherwise fall back to built-in TEST_URLS."""
    try:
        with open(URL_FILE, encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip()]
        if urls:
            print(f"Loaded {len(urls)} URLs from {URL_FILE}")
            return urls
    except FileNotFoundError:
        pass
    print(f"({URL_FILE.name} not found in {_HERE} – using built-in TEST_URLS)")
    return TEST_URLS


async def check_health() -> bool:
    """Verify the API is reachable and not still warming up."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{API_BASE}/health",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.json()
                status = body.get("status", "unknown")
                warming = body.get("pools_warming", False)
                print(f"  /health -> status={status}  pools_warming={warming}")
                if warming:
                    print("  Warning: driver pool still initialising – js-mode results may be slow.")
                return resp.status == 200
    except Exception as exc:
        print(f"  /health unreachable: {exc}")
        return False


async def run_level(mode: str, converter: str, concurrency: int,
                    urls: list[str], screenshot: bool = False,
                    group: str = "standard") -> list[Result]:
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession() as session:
        tasks = [
            fetch(session, sem, url, mode, converter, concurrency, screenshot, group)
            for url in urls
        ]
        return await asyncio.gather(*tasks)


async def run_all() -> list[Result]:
    print(f"API: {API_ENDPOINT}")
    print(f"Auth: {'Bearer token set' if API_KEY else 'none (no API_KEY)'}")
    print("Checking /health ...")
    await check_health()
    print()

    urls = load_test_urls()
    all_results: list[Result] = []

    # ── Phase 1: Standard (screenshot=false) ────────────────────────────────
    total = len(MODES) * len(CONVERTERS) * len(CONCURRENCY_LEVELS)
    step  = 0
    print(
        f"── Phase 1: Standard  "
        f"({len(urls)} URLs × {len(MODES)} modes × {len(CONVERTERS)} converters × "
        f"{len(CONCURRENCY_LEVELS)} concurrency levels = {len(urls) * total} requests) ──\n"
    )
    for mode in MODES:
        for converter in CONVERTERS:
            for conc in CONCURRENCY_LEVELS:
                step += 1
                label = (
                    f"[{step:>2}/{total}] "
                    f"mode={mode:<5} converter={converter:<11} conc={conc:>2}"
                )
                print(f"{label} ...", end=" ", flush=True)
                t0 = time.monotonic()
                results = await run_level(mode, converter, conc, urls,
                                         screenshot=False, group="standard")
                wall = time.monotonic() - t0
                ok     = [r for r in results if r.success]
                cached = sum(1 for r in ok if r.cached)
                avg    = statistics.mean(r.response_time for r in ok) if ok else 0.0
                print(
                    f"ok={len(ok)}/{len(results)}  "
                    f"cached={cached}  "
                    f"avg={avg:.1f}s  wall={wall:.1f}s"
                )
                all_results.extend(results)

    # ── Phase 2: JS + Screenshot ─────────────────────────────────────────────
    ss_total = len(SCREENSHOT_CONCURRENCY_LEVELS)
    print(
        f"\n\u2500\u2500 Phase 2: JS + Screenshot  "
        f"({len(urls)} URLs \u00d7 1 mode \u00d7 1 converter \u00d7 "
        f"{len(SCREENSHOT_CONCURRENCY_LEVELS)} concurrency levels = {len(urls) * ss_total} requests) \u2500\u2500\n"
    )
    for ss_step, conc in enumerate(SCREENSHOT_CONCURRENCY_LEVELS, 1):
        label = (
            f"[{ss_step:>2}/{ss_total}] "
            f"mode=js    converter=trafilatura  conc={conc:>2}  +screenshot"
        )
        print(f"{label} ...", end=" ", flush=True)
        t0 = time.monotonic()
        results = await run_level("js", "trafilatura", conc, urls,
                                  screenshot=True, group="js_screenshot")
        wall = time.monotonic() - t0
        ok     = [r for r in results if r.success]
        avg    = statistics.mean(r.response_time for r in ok) if ok else 0.0
        ss_ok  = [r.screenshot_kb for r in ok if r.screenshot_kb > 0]
        avg_kb = statistics.mean(ss_ok) if ss_ok else 0.0
        print(
            f"ok={len(ok)}/{len(results)}  "
            f"avg={avg:.1f}s  wall={wall:.1f}s  "
            f"png_avg={avg_kb:.0f} KB"
        )
        all_results.extend(results)

    return all_results

# ── Plotting ───────────────────────────────────────────────────────────────────

def plot(results: list[Result], save_path: str) -> None:
    results      = [r for r in results if r.group == "standard"]
    n_urls       = len({r.url for r in results})
    combinations = [(m, c) for m in MODES for c in CONVERTERS]
    n_cols = 2
    n_rows = (len(combinations) + n_cols - 1) // n_cols
    fig, axes_flat = plt.subplots(n_rows, n_cols, figsize=(14, 5 * n_rows))
    axes = axes_flat.flatten() if hasattr(axes_flat, "flatten") else [axes_flat]
    fig.suptitle(
        "Load Test – Volltextextraktion Selenium MD\n"
        f"({n_urls} URLs  x  {len(CONCURRENCY_LEVELS)} concurrency levels)",
        fontsize=13,
        fontweight="bold",
    )

    for idx, (mode, converter) in enumerate(combinations):
        ax     = axes[idx]
        subset = [r for r in results if r.mode == mode and r.converter == converter]

        avgs, p50s, p95s, err_pcts = [], [], [], []
        for conc in CONCURRENCY_LEVELS:
            grp      = [r for r in subset if r.concurrency == conc]
            ok_times = [r.response_time for r in grp if r.success]
            avgs.append(statistics.mean(ok_times)           if ok_times else float("nan"))
            p50s.append(float(np.percentile(ok_times, 50))  if ok_times else float("nan"))
            p95s.append(float(np.percentile(ok_times, 95))  if ok_times else float("nan"))
            err_pcts.append(
                (len(grp) - len(ok_times)) / len(grp) * 100 if grp else 0
            )

        ax.plot(CONCURRENCY_LEVELS, avgs, "o-",  color="steelblue",  label="Mean",  lw=2)
        ax.plot(CONCURRENCY_LEVELS, p50s, "s--", color="seagreen",   label="P50",   lw=1.5)
        ax.plot(CONCURRENCY_LEVELS, p95s, "^:",  color="darkorange", label="P95",   lw=1.5)
        ax.set_title(f"mode={mode}  /  converter={converter}", fontsize=11)
        ax.set_xlabel("Concurrency")
        ax.set_ylabel("Response time (s)")
        ax.legend(loc="upper left", fontsize=8)
        ax.set_xticks(CONCURRENCY_LEVELS)
        ax.grid(True, alpha=0.3)

        ax2 = ax.twinx()
        ax2.bar(
            CONCURRENCY_LEVELS, err_pcts,
            width=0.5, alpha=0.25, color="tomato", label="Error %",
        )
        ax2.set_ylabel("Error rate (%)", color="tomato", fontsize=8)
        ax2.set_ylim(0, 105)
        ax2.tick_params(axis="y", labelcolor="tomato")

    for idx in range(len(combinations), len(axes)):
        axes[idx].set_visible(False)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"\nPlot saved  -> {save_path}")
    plt.show()

# ── Summary table ──────────────────────────────────────────────────────────────

def print_summary(results: list[Result], group: str = "standard") -> None:
    subset = [r for r in results if r.group == group]
    if not subset:
        return
    if group == "standard":
        modes, converters, conc_levels = MODES, CONVERTERS, CONCURRENCY_LEVELS
        title = "PHASE 1 — STANDARD (no screenshot)"
    else:
        modes, converters, conc_levels = ["js"], ["trafilatura"], SCREENSHOT_CONCURRENCY_LEVELS
        title = "PHASE 2 — JS + SCREENSHOT"
    w = 96
    print("\n" + "=" * w)
    print(title)
    print(f"{'mode':<6} {'converter':<12} {'conc':>5}  "
          f"{'ok':>4}  {'cached':>6}  {'mean':>7}  {'p50':>7}  {'p95':>7}  {'err%':>6}  {'png_kb':>7}")
    print("-" * w)
    for mode in modes:
        for converter in converters:
            for conc in conc_levels:
                grp      = [r for r in subset
                            if r.mode == mode and r.converter == converter
                            and r.concurrency == conc]
                ok_times = [r.response_time for r in grp if r.success]
                cached   = sum(1 for r in grp if r.success and r.cached)
                mean_t   = statistics.mean(ok_times) if ok_times else float("nan")
                p50_t    = float(np.percentile(ok_times, 50)) if ok_times else float("nan")
                p95_t    = float(np.percentile(ok_times, 95)) if ok_times else float("nan")
                err_pct  = (len(grp) - len(ok_times)) / len(grp) * 100 if grp else 0
                ss_vals  = [r.screenshot_kb for r in grp if r.success and r.screenshot_kb > 0]
                png_avg  = statistics.mean(ss_vals) if ss_vals else 0.0
                print(
                    f"{mode:<6} {converter:<12} {conc:>5}  "
                    f"{len(ok_times):>4}  {cached:>6}  {mean_t:>7.2f}  "
                    f"{p50_t:>7.2f}  {p95_t:>7.2f}  {err_pct:>5.1f}%  {png_avg:>7.0f}"
                )
        print()


def print_error_summary(results: list[Result]) -> None:
    failures = [r for r in results if not r.success]
    if not failures:
        print("\nNo errors recorded.")
        return

    print("\n" + "=" * 78)
    print("ERROR FREQUENCY")
    print("=" * 78)

    # ── Overall error type counts ──
    type_counts: Counter = Counter(r.error_type for r in failures)
    print("\nOverall by error type:")
    for etype, count in type_counts.most_common():
        print(f"  {etype!s:<25}  {count:>5}x")

    # ── Per (mode, converter) breakdown ──
    print("\nPer combination:")
    print(f"  {'combination':<22}  {'error_type':<25}  {'count':>5}")
    print("  " + "-" * 56)
    for mode in MODES:
        for converter in CONVERTERS:
            sub = [r for r in failures if r.mode == mode and r.converter == converter]
            counts = Counter(r.error_type for r in sub)
            for etype, count in counts.most_common():
                print(f"  {f'{mode}+{converter}':<22}  {etype!s:<25}  {count:>5}x")

    # ── Per concurrency breakdown ──
    print("\nPer concurrency level:")
    print(f"  {'conc':>5}  {'error_type':<25}  {'count':>5}")
    print("  " + "-" * 40)
    for conc in CONCURRENCY_LEVELS:
        sub = [r for r in failures if r.concurrency == conc]
        counts = Counter(r.error_type for r in sub)
        for etype, count in counts.most_common():
            print(f"  {conc:>5}  {etype!s:<25}  {count:>5}x")

    # ── Worst offending URLs ──
    url_counts: Counter = Counter(r.url for r in failures)
    print("\nTop failing URLs:")
    for url, count in url_counts.most_common(10):
        print(f"  {count:>4}x  {url}")

# ── Error breakdown plot ─────────────────────────────────────────────────────

def plot_screenshot_phase(results: list[Result], save_path: str) -> None:
    """Single-panel plot for JS+screenshot phase (mode=js, converter=trafilatura)."""
    subset = [r for r in results if r.group == "js_screenshot"]
    if not subset:
        print("No screenshot-phase results to plot.")
        return
    fig, ax = plt.subplots(figsize=(9, 5))
    fig.suptitle(
        "Load Test Phase 2 – JS + Screenshot  (mode=js / trafilatura)",
        fontsize=12, fontweight="bold",
    )
    avgs, p50s, p95s, err_pcts = [], [], [], []
    for conc in SCREENSHOT_CONCURRENCY_LEVELS:
        grp      = [r for r in subset if r.concurrency == conc]
        ok_times = [r.response_time for r in grp if r.success]
        avgs.append(statistics.mean(ok_times)           if ok_times else float("nan"))
        p50s.append(float(np.percentile(ok_times, 50))  if ok_times else float("nan"))
        p95s.append(float(np.percentile(ok_times, 95))  if ok_times else float("nan"))
        err_pcts.append((len(grp) - len(ok_times)) / len(grp) * 100 if grp else 0)
    ax.plot(SCREENSHOT_CONCURRENCY_LEVELS, avgs, "o-",  color="steelblue",  label="Mean",  lw=2)
    ax.plot(SCREENSHOT_CONCURRENCY_LEVELS, p50s, "s--", color="seagreen",   label="P50",   lw=1.5)
    ax.plot(SCREENSHOT_CONCURRENCY_LEVELS, p95s, "^:",  color="darkorange", label="P95",   lw=1.5)
    ax.set_xlabel("Concurrency")
    ax.set_ylabel("Response time (s)")
    ax.legend(loc="upper left", fontsize=9)
    ax.set_xticks(SCREENSHOT_CONCURRENCY_LEVELS)
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    ax2.bar(SCREENSHOT_CONCURRENCY_LEVELS, err_pcts, width=0.3, alpha=0.25,
            color="tomato", label="Error %")
    ax2.set_ylabel("Error rate (%)", color="tomato", fontsize=8)
    ax2.set_ylim(0, 105)
    ax2.tick_params(axis="y", labelcolor="tomato")
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"Screenshot-phase plot saved -> {save_path}")
    plt.show()


def plot_errors(results: list[Result], save_path: str) -> None:
    failures = [r for r in results if not r.success]
    if not failures:
        print("No errors to plot.")
        return

    all_types    = sorted({r.error_type for r in failures if r.error_type})
    combinations = [f"{m}+{c}" for m in MODES for c in CONVERTERS]
    cmap         = plt.cm.get_cmap("tab10", len(all_types))
    type_colors  = {t: cmap(i) for i, t in enumerate(all_types)}

    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(13, 9))
    fig.suptitle(
        "Error Analysis – Volltextextraktion Selenium MD",
        fontsize=13, fontweight="bold",
    )

    # ── Top: stacked bar – error types per combination ──────────────────────
    combo_data: dict[str, Counter] = {}
    for combo in combinations:
        mode, converter = combo.split("+", 1)
        sub = [r for r in failures if r.mode == mode and r.converter == converter]
        combo_data[combo] = Counter(r.error_type for r in sub)

    x = np.arange(len(combinations))
    bottoms = np.zeros(len(combinations))
    for etype in all_types:
        heights = np.array([combo_data[c].get(etype, 0) for c in combinations], dtype=float)
        ax_top.bar(x, heights, bottom=bottoms, label=etype,
                   color=type_colors[etype], alpha=0.85)
        bottoms += heights

    ax_top.set_title("Error count per mode+converter combination", fontsize=11)
    ax_top.set_xticks(x)
    ax_top.set_xticklabels(combinations)
    ax_top.set_ylabel("Number of errors")
    ax_top.legend(title="Error type", fontsize=8, loc="upper right")
    ax_top.grid(axis="y", alpha=0.3)

    # ── Bottom: stacked bar – error types per concurrency level ─────────────
    conc_data: dict[int, Counter] = {}
    for conc in CONCURRENCY_LEVELS:
        sub = [r for r in failures if r.concurrency == conc]
        conc_data[conc] = Counter(r.error_type for r in sub)

    x2 = np.arange(len(CONCURRENCY_LEVELS))
    bottoms2 = np.zeros(len(CONCURRENCY_LEVELS))
    for etype in all_types:
        heights2 = np.array(
            [conc_data[c].get(etype, 0) for c in CONCURRENCY_LEVELS], dtype=float
        )
        ax_bot.bar(x2, heights2, bottom=bottoms2, label=etype,
                   color=type_colors[etype], alpha=0.85)
        bottoms2 += heights2

    ax_bot.set_title("Error count per concurrency level (all combinations)", fontsize=11)
    ax_bot.set_xticks(x2)
    ax_bot.set_xticklabels([str(c) for c in CONCURRENCY_LEVELS])
    ax_bot.set_xlabel("Concurrency")
    ax_bot.set_ylabel("Number of errors")
    ax_bot.legend(title="Error type", fontsize=8, loc="upper left")
    ax_bot.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"Error plot saved -> {save_path}")
    plt.show()

# ── Keyword pattern analysis ─────────────────────────────────────────────────

ERROR_PATTERNS: dict[str, list[str]] = {
    "geo_block":      ["not available in your country", "not available in your region",
                       "your country", "your region", "geoblocked", "territory",
                       "nicht verfügbar", "in deinem land", "ihrem land"],
    "bot_detected":   ["captcha", "cloudflare", "robot", "access denied",
                       "ddos", "security check", "ray id", "please verify",
                       "checking your browser", "403 forbidden", "not a robot"],
    "login_required": ["login", "sign in", "anmelden", "bitte melden sie sich an",
                       "authentication required", "please log in", "members only",
                       "create an account", "registrieren", "einloggen", "passwort"],
    "paywall":        ["paywall", "subscription", "subscribe", "premium",
                       "purchase", "paid content", "bezahlen", "abo ",
                       "freischalten", "jetzt abonnieren", "nur für abonnenten"],
    "js_required":    ["enable javascript", "requires javascript",
                       "javascript is required", "please enable javascript",
                       "javascript must be enabled", "ohne javascript",
                       "javascript aktivieren", "noscript"],
    "not_found":      ["404", "not found", "page not found", "seite nicht gefunden",
                       "existiert nicht", "does not exist", "wurde nicht gefunden"],
    "gone":           ["410", "removed", "no longer available",
                       "nicht mehr verfügbar", "deleted", "gelöscht"],
    "no_content":     ["no text content", "no content was extracted",
                       "nothing to extract", "empty page", "kein inhalt",
                       "no text found", "could not extract", "no content found"],
    "cookie_wall":    ["cookie consent", "cookie-einstellungen", "cookies akzeptieren",
                       "cookie wall", "datenschutzeinstellungen", "privacy settings",
                       "accept cookies", "nur notwendige cookies",
                       "cookie-richtlinie", "alle akzeptieren", "cookiebanner"],
    "timeout":        ["timeout", "timed out", "gateway timeout",
                       "request timeout", "504", "connection timed out",
                       "took too long", "did not respond",
                       "connecttimeout", "readtimeout", "pooltimeout"],
    "server_error":   ["500", "502", "503", "internal server error",
                       "bad gateway", "service unavailable", "server error"],
    "ssl_error":      ["ssl error", "certificate", "ssl handshake",
                       "certificate verify failed", "https error",
                       "secure connection failed",
                       "zertifikat", "gültigkeitszeitraum", "ssl:",
                       "cert_", "sslcertverfication", "sslerror",
                       "handshake failure", "unknown ca", "self signed"],
    "dns_error":      ["getaddrinfo", "name resolution", "nxdomain",
                       "nodename nor servname", "no address associated",
                       "dns", "name or service not known",
                       "gaierror", "errno 11001", "errno -2", "errno -3",
                       "err_name_not_resolved", "err_name_resolution_failed",
                       "net::err_name"],
    "connect_error":  ["connecterror", "connection refused", "connection reset",
                       "network unreachable", "no route to host",
                       "remotedisconnected", "connectionerror",
                       "broken pipe", "errno 10061", "errno 111",
                       "webdriverexception", "failed to fetch url",
                       "unknown error: net", "net::err"],
    "redirect_error": ["too many redirects", "redirect loop",
                       "circular redirect", "redirect chain"],
}


def classify_patterns(text: str) -> list[str]:
    """Return all matching pattern keys for the given text (case-insensitive)."""
    if not text:
        return []
    lower = text.lower()
    return [name for name, keywords in ERROR_PATTERNS.items()
            if any(kw in lower for kw in keywords)]


def print_pattern_analysis(results: list[Result]) -> None:
    failures = [r for r in results if not r.success]
    if not failures:
        print("\nNo failures to analyse.")
        return

    print("\n" + "=" * 78)
    print("PATTERN ANALYSIS  (keyword search over error messages + markdown snippet)")
    print("=" * 78)

    pattern_hits: Counter = Counter()
    pattern_urls: dict[str, set] = {}
    unclassified: list[Result] = []

    for r in failures:
        search_text = " ".join(filter(None, [r.error or "", r.text_snippet or ""]))
        patterns = classify_patterns(search_text)
        if patterns:
            for p in patterns:
                pattern_hits[p] += 1
                pattern_urls.setdefault(p, set()).add(r.url)
        else:
            unclassified.append(r)

    print(f"\n  {'pattern':<20}  {'hits':>5}  {'unique URLs':>11}  example URL")
    print("  " + "-" * 74)
    for pattern, count in pattern_hits.most_common():
        ex = sorted(pattern_urls[pattern])[0]
        print(f"  {pattern:<20}  {count:>5}  {len(pattern_urls[pattern]):>11}  {ex}")

    if unclassified:
        print(f"\n  unclassified: {len(unclassified)} failures (no pattern matched)")
        seen: set = set()
        for r in unclassified[:8]:
            snippet = (r.error or "")[:120]
            if snippet not in seen:
                seen.add(snippet)
                print(f"    {r.url}")
                print(f"    -> {snippet}")


def plot_patterns(results: list[Result], save_path: str) -> None:
    failures = [r for r in results if not r.success]
    if not failures:
        print("No failures for pattern plot.")
        return

    classified: list[tuple] = []
    for r in failures:
        search_text = " ".join(filter(None, [r.error or "", r.text_snippet or ""]))
        patterns = classify_patterns(search_text) or ["unclassified"]
        for p in patterns:
            classified.append((r.mode, r.converter, p))

    all_patterns = sorted({p for _, _, p in classified})
    combinations = [f"{m}+{c}" for m in MODES for c in CONVERTERS]
    cmap   = plt.cm.get_cmap("tab20", max(len(all_patterns), 1))
    colors = {p: cmap(i) for i, p in enumerate(all_patterns)}

    combo_counts: dict[str, Counter] = {c: Counter() for c in combinations}
    for mode, converter, pattern in classified:
        combo_counts[f"{mode}+{converter}"][pattern] += 1

    fig, ax = plt.subplots(figsize=(13, 6))
    fig.suptitle("Root-Cause Pattern Distribution per Mode+Converter",
                 fontsize=13, fontweight="bold")

    x = np.arange(len(combinations))
    bottoms = np.zeros(len(combinations))
    for pattern in all_patterns:
        heights = np.array(
            [combo_counts[c].get(pattern, 0) for c in combinations], dtype=float
        )
        ax.bar(x, heights, bottom=bottoms, label=pattern,
               color=colors[pattern], alpha=0.85)
        bottoms += heights

    ax.set_xticks(x)
    ax.set_xticklabels(combinations)
    ax.set_ylabel("Pattern hits (multi-label per failure)")
    ax.legend(title="Pattern", fontsize=8, bbox_to_anchor=(1.01, 1), loc="upper left")
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    print(f"Pattern plot saved -> {save_path}")
    plt.show()

# ── Persistence ────────────────────────────────────────────────────────────────

def save_json(results: list[Result], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([vars(r) for r in results], f, indent=2, ensure_ascii=False)
    print(f"Raw data saved -> {path}")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = asyncio.run(run_all())
    print_summary(results, group="standard")
    print_summary(results, group="js_screenshot")
    print_error_summary(results)
    print_pattern_analysis(results)
    save_json(results,         str(_HERE / f"loadtest_raw_{ts}.json"))
    plot(results,              str(_HERE / f"loadtest_plot_{ts}.png"))
    plot_screenshot_phase(results, str(_HERE / f"loadtest_screenshot_{ts}.png"))
    plot_errors(results,       str(_HERE / f"loadtest_errors_{ts}.png"))
    plot_patterns(results,     str(_HERE / f"loadtest_patterns_{ts}.png"))
