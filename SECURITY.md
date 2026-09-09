# Security policy

## Reporting a vulnerability

Report privately, not as a public issue: use GitHub's
[private vulnerability reporting](https://github.com/jkmesches/AQPI-Sentinel/security/advisories/new)
on this repository.

Please include the version (`/api/version`, or the image tag you deployed),
what an attacker can do with it, and the smallest set of steps that shows it.
A working exploit is not required and is not expected in the initial report.

Expect an acknowledgment within a week. There is no bounty; this is a
single-maintainer research project.

## What is in scope

The code in this repository and the published
`ghcr.io/jkmesches/sentinel-{backend,frontend}` images.

Anything that lets a request cross a boundary Sentinel is supposed to hold is
in scope even if it needs a valid login, because the login is not the boundary
that matters:

- Reaching another user's session, subscription, or device routing.
- Reaching admin surfaces (settings, users, thresholds, reprocess, digest
  config) without an admin role.
- Reading or writing the image archive, or the database, outside the paths the
  API exposes.
- Turning an operator-supplied value — a check target, an alarm route, an SMTP
  or webhook destination — into a request Sentinel was not meant to make, or a
  command it was not meant to run.
- Leaking configured credentials (SMTP, VAPID private key, database URL) into
  any response, log line, alarm payload, or the daily report.

## What is not in scope

- **The upstream being monitored.** Sentinel reads public endpoints on
  `radarca.engr.colostate.edu` and `radardisplay.engr.colostate.edu`, which
  belong to Colorado State University. Findings about *those* systems should
  go to CSU, not here. Do not test against them to demonstrate a Sentinel bug —
  point your instance at a system you own.
- **A deployment's own configuration.** An exposed instance with no reverse
  proxy, a default admin password left in place, `SENTINEL_RD_VERIFY_TLS=0`
  set by hand, or a database port published to the internet are deployment
  choices, and `docs/02-deployment.md` says so. A report that Sentinel *makes
  such a mistake easy to commit silently* is in scope and welcome.
- Missing hardening headers or a rate limit with no demonstrated impact, and
  findings whose only evidence is a scanner's output.

## Supported versions

The most recent minor release only — currently `0.4.x`. Fixes ship as a new
patch release rather than as a patch against an older tag; there is no
long-term-support branch and pretending otherwise would be worse than saying
this plainly.
