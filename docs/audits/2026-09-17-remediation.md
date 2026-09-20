# Remediation of the audit of 17 September 2026

Status of every finding in [2026-09-17-audit.md](2026-09-17-audit.md), released as 0.4.0.
Verified on Python 3.12.9 with the dependency set in `constraints.txt`: 196 unit tests
and 12 real-Chrome tests pass, ruff and the build are clean, and `pip-audit` reports only
the diskcache advisory that the JSON cache makes unreachable. The unit suite also passes
on Python 3.13.15.

| ID | Finding | Status | Commit |
|---|---|---|---|
| A01 | Open binding and optional authentication by default | Fixed: loopback default, startup refusal without a key | `3ed0816` |
| A02 | Request bodies read in full before authentication | Fixed: `BodySizeLimit` answers 413 before routing | `3ed0816` |
| A03 | Pickled cache on a directory with unenforced permissions | Fixed: `JSONDisk` plus an owner check on POSIX | `f197678` |
| A04 | `diagnose=True` in the JSON log sink | Fixed: `diagnose`/`backtrace` off | `ecfe566` |
| A05 | Failed crawls not logged server-side | Fixed: one structured line per failure | `ecfe566` |
| A06 | Clients can disable the operator rate limit | Fixed: the server default is a ceiling | `063870f` |
| A07 | No lockfile, no dependency scan in CI | Partly fixed: `constraints.txt` and a `pip-audit` step once per workflow; see follow-ups | `6bdf614`, `f197678`, `d8fc363` |
| A08 | `links` feature untested | Fixed: 33 cases for extraction and classification | `fa4f82c` |
| A09 | HTML parsed up to three times | Fixed: routing parse removed outside auto mode, and inside it whenever the extraction proves the page is not a shell | `d56f751`, `ed9dfea` |
| A10 | Dead helpers contradicting documented behaviour | Fixed: removed, rest moved to `app/links.py` | `1761823` |
| A11 | Dev cap blocks a cryptography fix | Fixed: `cryptography>=50`, no upper bound | `6bdf614` |
| A12 | Public docs with a key configured | Fixed: docs routes off when `API_KEY` is set | `3ed0816` |
| A13 | nginx example serves plain HTTP without rate limits | Fixed: HTTPS-first example, zones in a separate include | `25ebd38` |
| A14 | Loopback egress guards accept any local client | Documented: single-tenant host assumption in the README | `25ebd38` |
| A15 | Batch orchestration in the API factory | Fixed: `CrawlService.crawl_batch` | `5635274` |
| A16 | Raw deflate bodies rejected | Fixed: wrapper detected from the first bytes | `4df44e1` |
| A17 | Batch failure reason mirrors the success rule | Fixed: `failure_reason` sits next to the rule | `ecfe566` |
| A18 | Deterministic browser failures retried | Fixed: certificate and policy errors fail at once | `4df44e1` |
| A19 | Stale lint configuration | Fixed: three obsolete entries removed | `faa9e79` |
| A20 | Cache format version in two places | Fixed: the directory names the storage layout | `f197678` |
| A21 | Six functions above complexity 10 | Partly fixed: `create_app` resolved; four unchanged, `read_body` gained one branch for raw deflate | `5635274` |
| A22 | Version unchanged after contract changes | Fixed: 0.4.0 with `CHANGELOG.md` | `faa9e79` |
| A23 | Python support statement inconsistent | Fixed: 3.11-3.13 in metadata, CI and README | `faa9e79` |
| A24 | README lists fewer browser fixtures than CI runs | Fixed | `faa9e79` |
| A25 | Wall-clock thresholds can flake | Fixed: larger margins, one assertion replaced by the mechanism | `faa9e79` |

## Follow-ups, deliberately not done here

- **Hashed lockfile (A07).** `constraints.txt` records the verified versions, but it has
  no hashes and was resolved on Windows. A `--require-hashes` lock has to be produced on
  the target platform, which this repository cannot verify from a Windows checkout. CI
  still installs the newest compatible releases on purpose, so upstream breakage surfaces
  early; `pip-audit` gates every build, once per workflow because the advisory service
answers the same for every entry of the matrix.
- **One parse per document (A09).** Closed for the routing check in 0.7.0. Sharing the
  conversion tree with link extraction still requires `convert_document` to hand out its soup,
  which would change its contract for every content type; that parse only happens when
  `extract_links` is requested.
- **Complexity of `_classify_link`, `convert_html`, `embedded_html`, `selenium_fetch`,
  `read_body` (A21).** Left for the next functional change in each, as the audit suggests.
- **Coverage for spawned workers.** In-process coverage still under-reports worker code;
  measuring it needs coverage.py's multiprocessing support in the worker entry points.
- **Live parameter harness.** The 140-case run against real sites still lives outside the
  repository; moving it in as an opt-in job (`RUN_LIVE_TESTS=1`) remains open.
- **Python 3.14.** Tested and supported since 0.8.0: the unit and real-Chrome suites pass on
  3.14.7, and CI covers 3.11 to 3.14.
