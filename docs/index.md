# AQPI Sentinel

Monitoring for the radarca.engr.colostate.edu radar stack — checks
that radar imagery, products, web infrastructure, and image quality
remain healthy, surfaces issues on a desktop + mobile dashboard,
and pages the right people via email + Web Push.

**New here, or not a developer?** Start with the
[FAQ for the lab team](04-faq.md) — what Sentinel watches, where the
numbers come from, and how it decides something is wrong. The same page
is available inside a running Sentinel at `/faq`.

This documentation site covers:

- **Operate** — deploying Sentinel from scratch, day-to-day
  maintenance, troubleshooting, and the
  [FAQ](04-faq.md).
- **Reference** — the published changelog, the API surface, glossary.
- **Background** — the original radarca characterization + monitoring
  strategy plan.

Comprehensive documentation is being written incrementally — see the
[CHANGELOG](changelog.md) for what's landed in each release. The two
in-depth references that exist today are
[Architecture](ARCHITECTURE.md) and [Maintenance](MAINTENANCE.md).

For developers who want to extend Sentinel (add a check, add an API
endpoint, change the UI) — those guides are on the roadmap; for now
the inline docstrings on every check module + the architecture doc
are the source of truth.

---

## Quick links

- **FAQ for the lab team**: [what Sentinel watches and how it decides](04-faq.md).
- **Latest release**: see [CHANGELOG](changelog.md).
- **Container images**: `ghcr.io/jkmesches/sentinel-{backend,frontend}`.
- **Repository**: [github.com/jkmesches/AQPI-Sentinel](https://github.com/jkmesches/AQPI-Sentinel)
- **License**: Apache-2.0. Sentinel monitors, and is not affiliated with or
  endorsed by, the AQPI network or Colorado State University.
- **API reference** (live, against a running backend): `https://<your-host>/api/docs`
- **Quickstart**: pull `ops/docker-compose.ghcr.yml` from the repo,
  copy `ops/.env.prod.example` to `ops/.env.prod`, fill in the
  values, then `docker compose -f ops/docker-compose.ghcr.yml pull && up -d`.

The expanded "comprehensive docs" effort tracked in the
[CHANGELOG](changelog.md) will replace this stub homepage with a
proper getting-started flow.
