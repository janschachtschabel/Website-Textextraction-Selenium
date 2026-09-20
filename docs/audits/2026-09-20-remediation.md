# Remediation of the audit of 20 September 2026

What happened to every finding of [the audit](2026-09-20-audit.md), `B01`–`B24`.
Released as 0.9.0. Verification for each change is the test named with it; the
whole set runs as 318 unit tests plus 15 against real Chrome.

## Fixed

| ID | Change | Test |
|----|--------|------|
| B01 | Stopping a worker runs on the pool's own threads. The failing request still waits for its own cleanup, so a timeout keeps removing its partial files; a task being cancelled cannot wait, so `close()` does. Measured worst `/health` gap during a browser kill: 512–685 ms before, 26–40 ms after | `test_worker_startup_and_teardown_keep_off_the_event_loop` |
| B02 | `app/mathml.py` translates presentation MathML to LaTeX instead of flattening it to text. The length of a vector on Serlo reads `$$\| \vec{a} \| = \sqrt{a_{1}^{2} + a_{2}^{2}}$$` instead of `$$\| a → \| = a 1 2 + a 2 2$$` | `tests/test_mathml.py`, `test_presentation_mathml_becomes_latex_instead_of_flattened_text` |
| B03 | The substituted formula leaves the `display: none` wrapper that hides a MathML copy from sighted readers. 275 of the 289 formulas of "Satz des Pythagoras" are back; the remaining 14 sit in boilerplate the extractor excludes anyway | `test_a_hidden_accessibility_wrapper_does_not_swallow_the_formula` |
| B04 | robots.txt entries are keyed by origin *and* transport, so a caller's proxy or relaxed TLS no longer decides what the next caller may crawl | `test_robots_entries_of_different_transports_do_not_mix`, `test_a_caller_supplied_proxy_gets_its_own_robots_entry` |
| B05 | A full-page screenshot clamps the width at 4000 pixels as it clamps the height at 20000, and says so in the warnings | `test_a_very_wide_page_is_clipped_too` |
| B06 | Starting a worker runs on the pool's threads too; a cancellation during startup stops the process instead of leaking it, and `close()` waits for a worker that is still starting | `test_a_cancellation_while_a_worker_starts_does_not_leak_it` |
| B08 | Recording a request cannot replace its answer any more: a busy or unwritable state store is logged, not raised out of the `finally` | `test_a_failing_metrics_store_does_not_replace_the_result` |
| B10 | A CONNECT reply without a status line is a rejection, not an `IndexError` that aborts the tunnel | `test_a_malformed_upstream_proxy_reply_is_a_crawl_error` |
| B11 | `DEFAULT_USER_AGENT` is validated at startup like `DEFAULT_ACCEPT_LANGUAGE`, instead of failing every crawl with a 500 | `test_a_user_agent_that_no_request_could_use_fails_at_startup` |
| B12 | `force_refresh` fetches beside an in-flight request instead of taking its answer, and only the request that registered a future removes it again | `test_a_forced_refresh_never_joins_another_request` |
| B17 | The storage threads are registered for shutdown before the stores are opened, so a failing open takes them with it | `test_a_failed_store_open_shuts_the_storage_threads_down` |
| B18 | Joining a leader goes through a coroutine, so an expired deadline closes it rather than dropping a shield nobody awaits | covered by the coalescing tests |
| B21 | `/health` stays public but reports only whether the configured Chrome and ChromeDriver exist; the paths appear only while no API key is configured | `test_a_key_protected_health_endpoint_reports_no_filesystem_paths` |
| B23 | `extract_links` is documented in the README, which is the contract while a key disables `/docs` | - |
| B24 | `failure_reason` names the upstream status before the extraction state, so a rate-limited crawl reads "Upstream status 429" instead of "Extraction blocked" | `test_each_batch_url_uses_global_capacity_and_errors_are_counted` |

Partly addressed:

- **B07** — the worker pool no longer shares the loop's default executor, so a job
  holding a thread for its whole runtime cannot queue behind the DNS lookups of a page
  with many third-party hosts. A DNS cache stays out: measured in 0.7.0 at 0.3–0.9 ms for
  a repeat resolution, because the OS resolver already caches.

## Review round of the change set itself

A second review, with clean context, read `15a9604..e64897e` and found seven defects the
fixes had introduced. All are corrected in the same release.

| Severity | Defect | Fix | Test |
|----------|--------|-----|------|
| high | `asyncio.wrap_future` chains a cancellation back to the thread pool, so a second cancellation - a deadline and `WorkerPool.close()` cancelling the same task - cancelled a `_stop` still queued behind busy threads. The worker and its Chrome children survived, the executor thread blocked in `recv()` was lost, and `close()` had nothing left to wait for. Repeated, the pool stops serving | wait through `asyncio.shield` | `test_a_cleanup_that_is_still_queued_survives_a_second_cancellation` |
| medium-high | `mfenced` used its `open`/`close` attributes unescaped, although a page controls them: `open="$$ ..."` closed the Markdown fence and put arbitrary text outside it | escape them like every other leaf | `test_a_fence_the_page_declares_cannot_escape_the_formula` |
| medium | Conversion recursed without a limit, so about 600 nested `mrow` raised `RecursionError` out of `prepare_html` - outside the per-converter fallback - and failed the crawl with 502 | stop at 64 levels and keep the text from there | `test_deeply_nested_markup_does_not_exhaust_the_stack`, `test_a_deeply_nested_formula_does_not_fail_the_conversion` |
| medium | The escapes for a backslash and a tilde were letter commands with no terminator, so they swallowed the next character | `\backslash{}`, `\sim{}` | `test_an_escape_does_not_swallow_what_follows_it` |
| medium | A degree sign mapped to `^{\circ}` and became a second superscript inside `msup` | map it to `\circ` | `test_a_degree_sign_does_not_become_a_second_superscript` |
| low | A `]` in a root index closed the optional argument early | brace an index that contains one | `test_a_bracket_in_a_root_index_stays_inside_it` |
| low | Unhiding a formula wrapper also revealed text-free siblings, such as a hidden image | stop at an element holding more than the formula | `test_a_hidden_wrapper_holding_more_than_the_formula_keeps_its_styling` |

`WorkerPool.close()` is also a no-op the second time; it used to submit to an executor it
had just shut down (`test_closing_twice_is_a_no_op`). The review confirmed the in-flight
bookkeeping in `app/service.py`, the thread safety of the worker sets and the `2 * size`
thread budget as sound.

After the tightening the three pages the feature exists for are unchanged: Serlo 26
formulas, "Satz des Pythagoras" 275, "Photosynthese" 34.

## Deliberately unchanged

- **B09, per-process gauges.** `extraction_ready`, `extraction_active_requests`,
  `extraction_waiting_requests` and `extraction_pool_workers` come from whichever Uvicorn
  worker answers the scrape. A `pid` label would change the metric contract for every
  existing dashboard, and a shared-store gauge would have to expire per process. The
  counters and `extraction_cache_entries` do aggregate correctly. Documented instead.
- **B13, leftover Chrome profiles.** A directory Windows still has locked is remembered and
  removed by the next `_Slot()` or by `close()`. Retrying it earlier would block whoever
  retries; the current behaviour loses at most a directory until the next worker starts.
- **B14, `killpg` without a liveness guard.** The asymmetry with the Windows branch is
  deliberate: `taskkill /T` needs a live PID, while a process group outlives its leader and
  is the only handle on Chrome children of a worker that already died. Guarding it would
  trade a very narrow PID-reuse window for a Chrome that nothing kills.
- **B15, rate-limit reservations.** A cancelled request leaves its slot unused until the
  next one. Returning it means rolling back a committed value in a store shared across
  processes, which can hand two requests the same slot - worse than a slot going unused.
- **B16, `JobRunner.submit` during shutdown.** The lifespan stops accepting requests before
  `close()` runs, and `status()` already degrades an orphaned record to "lost".
- **B19, aggregate batch size.** 50 URLs times `max_bytes` is the caller's own budget, and
  the caller is authenticated. A second, aggregate limit would need its own setting and its
  own failure mode for a risk an operator can already bound with `MAX_BYTES`.
- **B20, destination ports.** Restricting them would break crawling of the many sites that
  publish on `:8080` and friends. The address itself is validated on every hop and every
  connection.
- **B22, the loopback egress guard.** It is reachable by other local users on a shared host,
  but every destination is still policy-checked, so it relays rather than bypasses. Binding
  it to an authenticated socket would mean handing Chrome proxy credentials on its command
  line.

Unchanged by design, noted because the audit measured it: a request whose browser job is
killed still waits for that cleanup before it answers, so a 4-second deadline returns after
about 4.5 seconds. The alternative - answering first and cleaning up later - would drop the
guarantee that a timed-out crawl has already removed its partial files.
