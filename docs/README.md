# Documents

The record of how this service got to where it is. The [changelog](../CHANGELOG.md) says
what changed in each release; these say what was found, what was measured and why some
things were deliberately left alone.

## Audits, and what happened to every finding

Each audit report is kept as it was written. The matching remediation document is the
living half: when a finding that was left open is closed later, that document is updated
to say so, with the release that closed it.

| Audited | Report | Remediation | Released as |
|---|---|---|---|
| 15 September 2026 | not kept in this repository | [F01-F18](audits/2026-09-15-remediation.md) | 0.3.0 |
| 17 September 2026 | [A01-A25](audits/2026-09-17-audit.md) | [record](audits/2026-09-17-remediation.md) | 0.4.0, the rest through 2.0.0 |
| 20 September 2026 | [B01-B24](audits/2026-09-20-audit.md) | [record](audits/2026-09-20-remediation.md) | 0.9.0 |

Two kinds of entry are worth knowing about before reading them:

- **Deliberately unchanged.** Findings closed by deciding *not* to change anything, each
  with its reason - per-process Prometheus gauges, `killpg` without a liveness guard, the
  aggregate batch size, the loopback egress guard, and others. They are decisions, not
  leftovers.
- **Still open.** One item: moving the live parameter harness against real sites into the
  repository as an opt-in job.

## The rest

- [settings.md](settings.md) - every environment variable the service reads, its default,
  and the three that behave differently inside a container.
- [migration-0.3.md](migration-0.3.md) - what a caller of the pre-audit code has to change.
- [migration-3.0.md](migration-3.0.md) - reading job results page by page instead of from one
  record.
- [plans/2026-09-15-audit-remediation.md](plans/2026-09-15-audit-remediation.md) - the plan
  the first remediation was worked against.
- [plans/2026-09-23-job-result-rows.md](plans/2026-09-23-job-result-rows.md) - the plan for
  storing each job result as a row (3.0.0), with the two decisions it rests on.
- [plans/2026-09-23-image-anonymization-and-media.md](plans/2026-09-23-image-anonymization-and-media.md) -
  the plan for anonymization and media metadata in the published image (3.1.0).
- [plans/2026-09-23-auto-js-fallback.md](plans/2026-09-23-auto-js-fallback.md) - the plan for
  auto's fallback to Chrome, the waits, and the run over WLO's largest sources (3.2.0).
- [plans/2026-09-23-document-links.md](plans/2026-09-23-document-links.md) - why the text keeps
  the files a page's content links to, and what the open points of that run turned out to be
  (3.2.1).

## Where the other answers live

- [README](../README.md) - installing and running it, every request option, the Docker
  image on Debian 13, the network policy.
- [CHANGELOG](../CHANGELOG.md) - the request/response contract and the operational
  defaults, per release.
- [.env.example](../.env.example) - every setting the service reads, with its default.
- `/docs` - the live OpenAPI schema with a working example per endpoint, served while no
  API key is configured.
