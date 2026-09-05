# Release process

How releases work, end to end. For anyone preparing a tag.

---

## Versioning scheme

`vMAJOR.MINOR.PATCH`:

| Bump | When to use | Examples |
|---|---|---|
| **MAJOR** (`vX.0.0`) | Production-grade release, or a breaking change. | `v1.0.0` (first prod release), `v2.0.0` (incompatible schema change) |
| **MINOR** (`v0.X.0`) | Milestone / feature drop. New views, new check types, new admin surfaces. Backward-compatible within a major. | `v0.2.0` (adds comprehensive docs site) |
| **PATCH** (`v0.0.X`) | Bug fix / small change. Copy tweaks, UX polish, perf fixes. | `v0.1.1` (category-status-color fix) |

Pre-1.0 the major stays at 0; minor bumps signal feature drops,
patch bumps signal fixes.

---

## Cutting a release

The process is intentionally light — two files to update + a tag push.

### 0. Bump `backend/_version.py`

```python
__version__ = "X.Y.Z"     # no leading v
```

**Do this first, because nothing enforces it.** The version is served at
`/api/version` and rendered in the frontend footer; it is the only way to ask
a running instance what it is without reading its source. Nothing in CI
compares it to the tag, so a release that skips this ships a build that
misreports itself — and the misreport is silent and long-lived.

That is not hypothetical: v0.2.1 and v0.2.2 were both cut without it and both
reported `0.2.0`. The step existed only in `_version.py`'s own docstring,
which pointed at *this* document, which did not mention it — each file
deferring to the other.

Sanity check after deploying:

```bash
curl -s https://<host>/api/version     # {"version":"X.Y.Z"}
```

### 1. Update CHANGELOG.md

Move the `[Unreleased]` heading down to `[vX.Y.Z]` with today's
date, then add a fresh empty `[Unreleased]` section above it.

Group changes under standard Keep-a-Changelog sections:

- **Added** — new features visible to users / operators.
- **Changed** — behavior changes to existing features.
- **Fixed** — bug fixes.
- **Deprecated** — marked-for-removal in a future release.
- **Removed** — features dropped from this release.
- **Security** — security-related fixes (call them out separately).

For non-trivial changes, write more than a one-liner: a sentence
of context per entry beats a terse bullet.

### 2. Verify the deploy artifacts are correct

The release will publish to GHCR. Sanity check:

- `ops/docker-compose.ghcr.yml` references the right image names.
- `ops/.env.prod.example` has every env var the new release
  introduces (with sensible defaults).
- `CHANGELOG.md` is committed.
- `mkdocs build --strict` passes (so the docs site builds for
  this release).

### 3. Commit + tag + push

```bash
git add CHANGELOG.md
git commit -m "release: vX.Y.Z

<one-line summary of the release theme>"

git tag -a vX.Y.Z -m "vX.Y.Z — <theme>

See CHANGELOG.md for full notes."

git push
git push origin vX.Y.Z
```

The tag push triggers `.github/workflows/docker-publish.yml`,
which:

1. Builds the backend + frontend images in parallel.
2. Publishes to GHCR with the version-tag set:
   - `ghcr.io/jkmesches/sentinel-backend:X.Y.Z`
   - `ghcr.io/jkmesches/sentinel-backend:X.Y`
   - `ghcr.io/jkmesches/sentinel-backend:X`
   - `ghcr.io/jkmesches/sentinel-backend:sha-<short>`
   - `ghcr.io/jkmesches/sentinel-backend:latest`
   - (and the same tags on the frontend)

!!! warning "The image tag has no `v`, even though the git tag does"
    `docker/metadata-action`'s `type=semver,pattern={{version}}` parses the
    git ref as a semver and re-emits it **without** the leading `v`. So a git
    tag of `v0.2.0` publishes an image tag of `0.2.0`:

    ```bash
    SENTINEL_TAG=0.2.0     # correct — pulls
    SENTINEL_TAG=v0.2.0    # wrong  — manifest unknown
    ```

    This looks like a typo and invites "fixing", so: it is not. Confirmed
    against the v0.2.0 publish logs, which pushed exactly `0.2.0`, `0.2`,
    `0`, `latest` and `sha-abb11f5`. Every `SENTINEL_TAG=` example in these
    docs was wrong by that one character until 2026-09-03.

    Note also that a semver tag moves `latest` as well — metadata-action's
    default `latest=auto` applies it to any non-prerelease version tag, in
    addition to the explicit main-branch rule.

Watch the run finish:

```bash
gh run watch
```

### 4. Verify the published images

```bash
docker pull ghcr.io/jkmesches/sentinel-backend:X.Y.Z
docker pull ghcr.io/jkmesches/sentinel-frontend:X.Y.Z
```

Both should succeed without authentication errors. If the packages
are still private, you'll need a PAT — see [`02-deployment.md` §
GHCR auth](02-deployment.md#2-ghcr-images-and-authentication).

### 5. Create a GitHub release (optional but recommended)

```bash
gh release create vX.Y.Z \
    --title "vX.Y.Z — <theme>" \
    --notes-from-tag
```

The release notes pull from the annotated tag message. Edit on
github.com afterwards to format with the CHANGELOG section.

---

## Release notes style

Each release should make it easy for an operator to decide whether
to upgrade. A few patterns that have worked:

- **Lead with the theme.** "v0.2.0 — comprehensive docs site." A
  reader skimming the release list can immediately gauge relevance.
- **One entry per discrete change.** Don't bundle "added new check
  type + fixed sparkline bug + restructured admin UI" into one
  bullet.
- **Cross-link to the changed code.** GitHub auto-links commit
  shas; include them for non-obvious changes.
- **Call out migrations.** If the release requires a one-shot
  script run, say so prominently with the command.
- **Call out breaking changes loudly.** Use a `**BREAKING**` prefix
  on bullets if applicable.

---

## Hotfix procedure

When `latest` breaks something and rollback is needed faster than
a normal release cycle:

1. Pin prod to the previous good version on the host:
   ```bash
   SENTINEL_TAG=X.Y.Z-1 docker compose -f ops/docker-compose.ghcr.yml \
       --env-file ops/.env.prod pull
   SENTINEL_TAG=X.Y.Z-1 docker compose -f ops/docker-compose.ghcr.yml \
       --env-file ops/.env.prod up -d
   ```
2. Branch from the last good tag locally:
   ```bash
   git checkout -b hotfix-vX.Y.Z+1 vX.Y.Z-1
   ```
3. Cherry-pick the fix from main:
   ```bash
   git cherry-pick <fix-commit-sha>
   ```
4. Update CHANGELOG with a new patch entry.
5. Tag + push:
   ```bash
   git tag -a vX.Y.Z+1 -m "vX.Y.Z+1 — hotfix"
   git push origin hotfix-vX.Y.Z+1
   git push origin vX.Y.Z+1
   ```
6. CI publishes the new tag. Update prod:
   ```bash
   SENTINEL_TAG=X.Y.Z+1 docker compose -f ops/docker-compose.ghcr.yml \
       --env-file ops/.env.prod pull
   SENTINEL_TAG=X.Y.Z+1 docker compose -f ops/docker-compose.ghcr.yml \
       --env-file ops/.env.prod up -d
   ```
7. Open a PR merging the hotfix back into main so future releases
   include the fix.

---

## What CI does, and what it doesn't

**Does:**

- Build backend + frontend Docker images.
- Push tagged images to GHCR.
- Build the MkDocs docs site and deploy to GitHub Pages.

**Doesn't (yet):**

- Run tests automatically (no test suite shipped yet).
- Run linting / type-checking.
- Sign images (no Sigstore / cosign integration).
- Generate SBOMs.

These are all reasonable additions; not on the v0.X.Y critical
path.

---

## Where to go from here

- **[`CHANGELOG.md`](changelog.md)** — recent releases.
- **[`MAINTENANCE.md`](MAINTENANCE.md) § Upgrading** — the operator
  side of a release.
- **`.github/workflows/docker-publish.yml`** — the CI that runs on
  every release.
