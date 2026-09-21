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
| A07 | No lockfile, no dependency scan in CI | Fixed in 2.0.0: `requirements.lock` names a hash for every artefact and the image installs it with `--require-hashes`; `constraints.txt` and a `pip-audit` step per workflow came first | `6bdf614`, `f197678`, `d8fc363`, `4c021c6` |
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
| A21 | Six functions above complexity 10 | Fixed in 2.0.0: each split along a responsibility, `read_body` kept at 12 with a reason, and `C90` now gates it; 0.4.0 had resolved `create_app` alone, which then grew back | `5635274`, `75a5b52`, `2d07929` |
| A22 | Version unchanged after contract changes | Fixed: 0.4.0 with `CHANGELOG.md` | `faa9e79` |
| A23 | Python support statement inconsistent | Fixed: 3.11-3.13 in metadata, CI and README | `faa9e79` |
| A24 | README lists fewer browser fixtures than CI runs | Fixed | `faa9e79` |
| A25 | Wall-clock thresholds can flake | Fixed: larger margins, one assertion replaced by the mechanism | `faa9e79` |

## Follow-ups, deliberately not done here

- **Hashed lockfile (A07).** Closed in 2.0.0. `requirements.lock` pins every direct and
  transitive dependency and names a hash for every artefact, and the image installs it with
  `--require-hashes`. It stayed open this long because such a lock has to be resolved on the
  target platform and this repository is developed on Windows; the container added in 0.10.0
  is that platform. `constraints.txt` keeps its own job, the version set for development
  installs on any platform, without hashes. CI still installs the newest compatible releases
  on purpose, so upstream breakage surfaces early; `pip-audit` gates every build, once per
  workflow because the advisory service answers the same for every entry of the matrix.
- **One parse per document (A09).** Closed in 2.0.0 by measuring it rather than changing it.
  The routing parse went in 0.7.0. What remained was sharing conversion's tree with link
  extraction, and that tree is not one link extraction may read: `_prepared` makes every
  `href` absolute, decomposes `template`, and can replace the document with an embedded
  payload. Taking the links from it instead, on the 942 KiB "Photosynthesis" article,
  reclassifies 287 of 2019 - every in-page anchor becomes a content link, because
  `#cite_ref-26` is no longer a bare fragment. Sharing the tree from before those mutations
  needs a copy per consumer, and on that page `copy.copy` costs 229 ms against 244 ms to
  parse again: 6 % saved for a change to `convert_document`'s contract for every content
  type. The second parse stays, and it only happens when `extract_links` is requested.
- **Complexity (A21).** Closed in 2.0.0. `create_app`, which 0.4.0 had brought under the
  threshold, had grown back to 19 because nothing checked it, and five more functions sat
  between 11 and 13. Each was split along a responsibility rather than a line count: the
  routes into four groups by concern, the settings validation into counts, limits and
  headers, the link classifier into scheme rules and an ordered pattern table, the embedded
  payload's attachment handling into its own function, the converter chain away from
  document preparation, and the browser's CDP setup away from reading the page.
  `read_body` stays at 12 with a `# noqa` and a reason: its branches are one job, and it is
  the code that bounds a decompression bomb. `C90` is now part of `ruff check` with
  `max-complexity = 10`, so this cannot creep back unnoticed a second time.
- **Coverage for spawned workers.** Closed in 0.8.0: idle workers exit on request instead of
  being killed, so coverage.py's multiprocessing support can measure them. The suite covers
  92 % of `app/`, against 81 % counting only the main process.
- **Live parameter harness.** The 140-case run against real sites still lives outside the
  repository; moving it in as an opt-in job (`RUN_LIVE_TESTS=1`) remains open.
- **Python 3.14.** Tested and supported since 0.8.0: the unit and real-Chrome suites pass on
  3.14.7, and CI covers 3.11 to 3.14.
