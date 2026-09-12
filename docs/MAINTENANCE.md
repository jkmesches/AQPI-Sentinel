# Maintenance

Day-2 operational reference for a running Sentinel deployment. The
first half is "things you do regularly," the second half is "what
to look at when something's wrong."

**Related docs:**

- [`01-getting-started.md`](01-getting-started.md) — first install.
- [`02-deployment.md`](02-deployment.md) — every deployment knob.
- [`03-administration.md`](03-administration.md) — admin-UI surfaces.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how a check flows end-to-end.

---

## Common operations

### Restart a service

Prod (on the remote host):
```bash
cd /srv/sentinel
docker compose -f ops/docker-compose.prod.yml --env-file ops/.env.prod \
  restart backend       # or: frontend, postgres
```

Dev (from the project root):
```bash
bash scripts/restart-be.sh        # backend only
bash scripts/restart-fe.sh        # frontend only (also clears Vite cache)
bash scripts/status.sh            # show PIDs + log paths
```

### Deploy a code update

Sentinel publishes tagged container images to GHCR
(`ghcr.io/jkmesches/sentinel-{backend,frontend}`) on every push to
`main` and on every `vX.Y.Z` tag. The recommended deploy path is to
pull the desired image on the prod host:

1. Land your change on `main` (or cut a release tag). Wait for the
   `docker-publish` GitHub Actions workflow to complete — it builds
   both images and pushes them to GHCR.
2. On the prod host:
   ```bash
   cd /srv/sentinel
   SENTINEL_TAG=latest docker compose -f ops/docker-compose.ghcr.yml \
       --env-file ops/.env.prod pull
   SENTINEL_TAG=latest docker compose -f ops/docker-compose.ghcr.yml \
       --env-file ops/.env.prod up -d
   ```
   Pin `SENTINEL_TAG=0.1.0` (or any released version) for predictable
   rollback.

If you'd rather **build locally on the prod host** (e.g. no internet,
or you want to test an unmerged branch), use the source-build compose
instead:
```bash
cd /srv/sentinel
docker compose -f ops/docker-compose.prod.yml --env-file ops/.env.prod \
    up -d --build backend frontend
```

This requires the source tree to be on the host. How you get it there
(`git pull`, `scp`, `rsync`, etc.) is up to you — just make sure
`ops/.env.prod` is preserved between deploys (it holds the postgres
password and is gitignored locally).

### Rolling back

Pin to the previous tag and re-pull:
```bash
SENTINEL_TAG=0.0.9 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod pull
SENTINEL_TAG=0.0.9 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod up -d
```

Backwards-incompatible schema changes are rare (`schema.sql` is
idempotent and only ever adds), but if the rolled-back code can't
read the current schema, restore the matching `pg_dump` snapshot
(see "Backups" below).

### View logs

Prod (replace `<prod-host>` with your host alias):
```bash
ssh <prod-host> 'docker logs -f --tail 100 sentinel-backend'
ssh <prod-host> 'docker logs -f --tail 100 sentinel-frontend'
ssh <prod-host> 'docker logs -f --tail 100 sentinel-postgres'
```

Dev: tails are auto-written under `/tmp/sentinel-{be,fe}.log`.

### Inspect runtime state

`/api/_debug/stats` is a cheap snapshot of:
- event-loop lag (sub-millisecond if healthy)
- asyncpg pool size + in-use
- httpx in-flight requests
- WebSocket client count
- scheduler task health
- image cache + network monitor state

```bash
# Direct against the backend port:
curl -s http://<prod-host>:8000/api/_debug/stats | python3 -m json.tool
# Or via your reverse proxy:
curl -s https://<your-domain>/api/_debug/stats | jq .
```

Hit this **first** whenever the UI feels stuck — it instantly tells you
whether the backend is wedged or whether the issue is browser-side.

### Performance tuning

Sentinel runs comfortably on a 2-vCPU / 4 GB host for the radarca
scale (43 checks, ~30 outbound req/min). Symptoms of under-resourcing
that show up in `/api/_debug/stats`:

| Symptom | Likely cause | Fix |
|---|---|---|
| `event_loop_lag_ms` > 100 sustained | Backend CPU-bound. Usually Playwright (L3 overlay check). | Inspect with `py-spy top` against the backend PID. If it's the Playwright Chromium worker, scale to a bigger host or disable L3 in `backend/checks/__init__.py`. |
| `asyncpg.in_use == max_size` sustained | Pool saturation. | Bump pool max in `backend/db/store.py` (`Store.connect` → `max_size`). Default 10; 20 is fine for the radarca scale. |
| `httpx.in_flight` consistently > 10 | Upstream slow OR check cadences too tight. | Verify with the per-cadence math in [`02-deployment.md` § Choosing a host](02-deployment.md#1-choosing-a-host). Loosen cadences in `backend/config.py` if needed. |
| `image_cache.size` at max + sluggish UI | LRU eviction churn — usually fine, but if RAM is tight, lower `_IMAGE_CACHE_MAX` in `backend/api/routes/upstream.py`. | |
| `scheduler.tasks_running < tasks_total` | A check task crashed and didn't restart. | Restart backend. If it recurs, the check has a bug — check logs for the crash trace. |
| Frequent `WebSocket context lost` in mobile logs | iOS Safari dropped the WS while the tab was backgrounded — this is normal, the client reconnects. | No fix needed; only worry if it's not reconnecting. |

The **5-minute health drill** when something feels off:

1. `curl /api/_debug/stats | jq` — single source of truth.
2. `docker logs --since=5m sentinel-backend | grep -iE 'error|warn'` — look for recent exceptions.
3. `/api/_debug/stats → network_monitor.online + dns_ok` — confirm Sentinel's own connectivity.
4. `/admin/audit` — last 10 admin actions, in case a config change preceded the issue.

### Rolling back a release

If a deploy breaks things, pin to the previous tag and re-pull:

```bash
ssh <prod-host>
cd /srv/sentinel

# What's currently running?
docker ps --format '{{.Names}}\t{{.Image}}'

# Roll to a known-good version (substitute your last good tag):
SENTINEL_TAG=0.0.9 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod pull
SENTINEL_TAG=0.0.9 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod up -d
```

Containers swap in seconds. The data volumes (`sentinel_pgdata`,
`sentinel_archive`) are untouched — they belong to the host, not the
container — so a rollback is a no-op for state.

**The one rollback caveat:** `schema.sql` is forward-only by design.
A rollback to a version that doesn't know about a newer column won't
break (the extra column is harmless), but a rollback that crosses a
column rename / drop won't work. We haven't had a destructive
schema change yet; the pattern when one becomes necessary is "add new,
migrate data, deprecate old in N+1, drop old in N+2" — same as any
zero-downtime DB migration.

### Upgrading between versions

The version scheme (vMAJOR.MINOR.PATCH — see [`CHANGELOG.md`](changelog.md))
gives you a quick read on the upgrade risk:

| Bump | Typical upgrade behavior |
|---|---|
| **PATCH** (v0.1.0 → v0.1.1) | Bug fix. Pull, restart. Schema additions if any are backward-compatible. Zero-downtime, no config changes. |
| **MINOR** (v0.1.x → v0.2.0) | New features. Read the [CHANGELOG](changelog.md) for `Added` and `Changed` entries; you may need to flip a setting to take advantage of new behavior, but the old behavior keeps working. |
| **MAJOR** (v0.x.x → v1.0.0) | Potentially breaking. Read the migration notes in that release's CHANGELOG entry carefully. Test on a staging deploy first. |

**Standard upgrade procedure:**

```bash
# Set this once — everything below uses it, so there is no version to
# forget in the middle. The git tag carries a leading v; the image tag
# does not (see 99-release-process.md).
VER=0.4.5

# Read the CHANGELOG between your current version and the target.
gh release view "v$VER" --repo jkmesches/AQPI-Sentinel

# Pull the new tag.
SENTINEL_TAG=$VER docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod pull

# Check what changed in the env example — new variables may have
# appeared with sensible defaults you might want to override.
diff ops/.env.prod.example <(curl -fsSL \
    "https://raw.githubusercontent.com/jkmesches/AQPI-Sentinel/v$VER/ops/.env.prod.example")

# Apply.
SENTINEL_TAG=$VER docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod up -d

# Verify.
curl -s http://<host>:8000/api/_debug/stats | jq '.scheduler'
```

If the upgrade fails healthcheck, roll back per the previous section.

### Monitoring Sentinel itself

The system that watches everything else needs its own watcher. The
risk profile is bounded — Sentinel is mostly stateless other than
Postgres — but a silently wedged backend is bad.

**Lightweight options:**

- **External heartbeat** — any external monitor (uptimerobot,
  healthchecks.io, a tiny cron) hits `/api/_debug/stats` every 5
  minutes. Alert if absent or `event_loop_lag_ms > 1000`.
- **Postgres uptime** — your DB monitoring (if you have it) pointed
  at the `sentinel-postgres` container.
- **A second Sentinel instance** — overkill for most deploys; gives
  you the full alarm pipeline for free if you're already running
  more than one.

**What to alarm on, with thresholds:**

| Signal | Threshold | Why |
|---|---|---|
| Backend HTTP 5xx rate | > 1% over 5 min | App-layer failure |
| `event_loop_lag_ms` | > 100ms sustained | Backend overloaded |
| `scheduler.tasks_running != tasks_total` | sustained > 1 min | A check task died |
| `asyncpg.in_use == max_size` | sustained > 30s | Pool saturation |
| Container restart count | > 1/hr | Crash loop |

### Backups

Replace `<prod-host>` with your host alias. Recipes assume the
compose project name `sentinel` (the default).

Postgres snapshot:
```bash
ssh <prod-host> 'docker exec sentinel-postgres pg_dump -U sentinel -d sentinel \
  | gzip > /tmp/sentinel-db-$(date +%F).sql.gz'
scp '<prod-host>:/tmp/sentinel-db-*.sql.gz' ~/backups/
```

Image archive (PNGs from L4 captures):
```bash
ssh <prod-host> 'docker run --rm -v sentinel_archive:/v -v /tmp:/out alpine \
  tar czf /out/sentinel-archive-$(date +%F).tar.gz -C /v .'
scp '<prod-host>:/tmp/sentinel-archive-*.tar.gz' ~/backups/
```

Restore (warning: clobbers current state):
```bash
ssh <prod-host> 'cd /srv/sentinel && docker compose -f ops/docker-compose.ghcr.yml \
  --env-file ops/.env.prod stop backend'
# Postgres:
gunzip < ~/backups/sentinel-db-YYYY-MM-DD.sql.gz \
  | ssh <prod-host> 'docker exec -i sentinel-postgres psql -U sentinel -d sentinel'
# Archive:
scp ~/backups/sentinel-archive-YYYY-MM-DD.tar.gz <prod-host>:/tmp/
ssh <prod-host> 'docker run --rm -v sentinel_archive:/v -v /tmp:/in alpine \
  tar xzf /in/sentinel-archive-YYYY-MM-DD.tar.gz -C /v'
ssh <prod-host> 'cd /srv/sentinel && docker compose -f ops/docker-compose.ghcr.yml \
  --env-file ops/.env.prod start backend'
```

### Schema migrations

There's no migration tool (Alembic etc.). `backend/db/schema.sql` is
**fully idempotent** (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF
NOT EXISTS`, etc.) and `Store.connect()` applies it on every backend
startup. So:

1. Add the new `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE … IF NOT
   EXISTS` to `schema.sql`.
2. Restart the backend. New tables/columns are created on first
   connect.

For destructive changes (drop a column, rename a table) you do need a
custom one-off migration script — there are no examples yet because
we haven't needed one. Keep `schema.sql` idempotent above all.

### Reprocess historical data

Two ways:

**1. Admin UI (preferred for threshold-driven changes).**
`/admin/thresholds` → scroll to the *Retroactive reprocess* panel at
the bottom → set time range, choose stages (L1 / L2 / L4-T1T2), type
`APPLY`, hit Start. Live progress bar polls at 1 Hz; the job is
cancellable. Walks `check_runs` in the window, re-classifies each row
via `_reverdict_l1` / `_reverdict_l2` / `_reverdict_l4` in
`backend/reprocess_engine.py`. Preserves rows where
`payload.reason in {local_dns_error, transport_error}` and rows whose
original status is `skip` / `error` (transport failures don't get
reclassified as heuristic verdicts).

**2. Standalone CLI scripts** (for one-off historical migrations the
admin UI doesn't cover — schema reshape, payload migration, etc.):
```bash
.venv/bin/python -m backend.reprocess_l4 --dry-run    # L4 image-QC verdicts
.venv/bin/python -m backend.reprocess_l4              # apply
.venv/bin/python -m backend.reprocess_l1_forecasts    # L1 forecast products
.venv/bin/python -m backend.reprocess_l2_dead_moments # RhoHV/X-band filter
.venv/bin/python -m backend.reprocess_dns_errors      # demote historical DNS errors
.venv/bin/python -m backend.tune_silent_fail          # cadence threshold review
```

### Writing a one-shot data migration

`scripts/humanize_history.py` and `scripts/cascade_retro.py` are
working examples of a recurring pattern: rewrite historical
`check_runs` / `alarms` rows under new logic without affecting live
behavior. Use them as templates when a future change has the same
shape ("we changed how we record X; backfill the history").

**The pattern:**

1. **Filter** the rows that need rewriting via a SQL `WHERE` clause
   that includes a `payload.<marker>_v IS NULL` guard. Without the
   guard, re-running the script double-processes.
2. **Compute** the new value for each row in Python, reading the
   row's existing fields.
3. **Stash** the original value in `payload.original_<field>` so
   the drilldown can still show the raw observation.
4. **Update** the row's user-visible fields, set `payload.<marker>_v
   = 1`.
5. **Dry-run mode** (`--dry` flag) prints what would change without
   touching the DB. Always run dry first.
6. **Run inside the backend container** so the script picks up the
   `SENTINEL_DB_URL` env var and any shared backend modules.

```bash
# Copy script onto the host, then into the running container:
docker cp scripts/my_migration.py sentinel-backend:/tmp/
docker exec sentinel-backend python /tmp/my_migration.py --dry   # preview
docker exec sentinel-backend python /tmp/my_migration.py         # apply
```

Idempotent re-runs are the bedrock — if your script can't safely
be re-run, the rollout window during which it's "partially applied"
is a window where the system is in a state nobody documented.

### Edit detection thresholds

`/admin/thresholds` is the canonical place. The page has four sections:

- **Globals** — `hysteresis`, `cadence_tol`, `step_count_tol`,
  `silent_fail_default`.
- **Products** — per-product `max_freshness_s`, `min_png_bytes`,
  `expected_steps`, `cadence_s`. Empty cell = "use default" (placeholder
  shows the `config.py` value).
- **Radars** — per-radar `silent_fail_s` for L2 ghost-up detection.
- **Image QC (L4)** — per-product `extreme_threshold`,
  `frozen_min_cov_pct`, `skip_frozen`, `skip_range_ring`.

Hit *Review & apply* → diff modal → *Apply*. The in-process cache
refreshes; the next check tick reads the new values. **No restart
needed**. To bring historical rows in line with the new thresholds,
follow with a retroactive reprocess (above).

Direct DB inspection if needed:
```sql
SELECT jsonb_pretty(value) FROM settings WHERE key = 'thresholds';
```

### Define a notification group

`/admin/groups` is master/detail:

- **Name + description** + optional **parent group** (inherits the
  parent's schedule — the AND of both verdicts).
- **Schedule kind**: `always` / `weekly` / `biweekly`.
  - Weekly: pick weekdays (Mon-Sun chip mask) + time windows
    (multiple pairs allowed; overnight `start > end` wraps midnight).
  - Biweekly: same + an **anchor date** that fixes the on-week parity.
    Two adjacent groups created on different weeks have opposite
    phases; set the anchor explicitly if you need them aligned.
- **One-off downtime** — specific date ranges (vacations,
  maintenance windows).
- **Recurring downtime** — repeats every week. Pick weekdays + a time
  window. Overnight windows ("22:00 → 06:00") wrap correctly.
- **Members** — checkbox-add from the full user roster.

*Refresh preview* surfaces the next 5 on-windows from the draft
schedule via `POST /api/admin/groups/{gid}/preview` so you can
sanity-check before saving.

Groups feed alert dispatch via `Receiver.group_ids` and
`EscalationStep.group_ids` (both editable in `/admin/alerts`'s
recipient + step UIs). Off-duty groups are skipped entirely; if a
person is reachable through both a group and a direct recipient in
the same step, they get one notification (per-step email dedup).

### Configure per-device push routing

Each push subscription carries a per-device routing config:
- **Minimum severity** (info / warn / critical) — drop below-rank
  notifications.
- **Match patterns** — substring match against the alarm's
  `tag` / `title` / `body`. Empty list = receive everything.
- **Delay before notifying** — minutes; deferred via
  `asyncio.create_task` in `backend/push.py`. **In-memory only** —
  backend restart cancels pending delays. Fine for "snooze me 5 min",
  not durable for hours-long.
- **On-duty schedule** — same shape as groups schedules. Recurring
  quiet-hours layer on top.

UI: `/m/push-settings` (mobile native) or `/settings/devices`
(desktop mirror). Both work on the same backing data; the user can
configure on a laptop and the iPhone picks it up.

### Create / disable a user

UI: log in as admin → **Admin → Users** → invite / disable / change
role / reissue reset link. The Users list also surfaces each user's
group memberships (chip-style); the invite form lets you select
initial groups when creating a user.

CLI (if locked out):
```bash
ssh <prod-host> 'docker exec -it sentinel-postgres psql -U sentinel -d sentinel'
-- then in psql:
SELECT id, email, role, disabled_at FROM users;
-- to re-enable yourself:
UPDATE users SET disabled_at = NULL WHERE email = 'you@example.com';
-- to reset everyone's sessions (force re-login):
DELETE FROM sessions;
```

The bootstrap admin env vars (`SENTINEL_ADMIN_EMAIL` etc.) only fire
when the `users` table is empty. If you're locked out and the table
isn't empty, you have to use psql.

---

## Disk space

The production LXC's local disk is the scarce resource in this deployment,
and it is what took Sentinel down for six days in August 2026. Read this
section before changing anything that writes to disk.

### Storage layout

| What | Where | Why there |
|---|---|---|
| `pgdata` | local disk, `sentinel_pgdata` volume | **Must stay local.** Postgres relies on fsync + locking semantics that NFS does not reliably provide. |
| Image archive (PNGs) | NFS — `/mnt/aqpi-data/archive` | Write-once, read-rarely, 18 GB and growing. Nothing is gained by keeping it on the container disk. |
| Cold exports (offloaded rows) | NFS — `/mnt/aqpi-data/cold` | Archival CSV. Only read when restoring history. |
| Nightly `pg_dump` | NFS — `/mnt/aqpi-data/backups/db` | Off-box by definition. |
| Container logs | local disk, rotated | Capped at 50 MB × 3 per service by the compose `x-logging` anchor. |

The NFS share in the reference deployment is a 2.6 TB export attached to the LXC
as a **Proxmox bind mount declared in the container config on the host**
(`mp0:` in `/etc/pve/lxc/123.conf`), not by the guest. It is persistent across
reboots. Note that this means `/etc/fstab` inside the container is empty and
`systemctl` reports the mount as `loaded (/proc/self/mountinfo)` — that is the
expected appearance for a host-side bind mount, **not** evidence that the
mount is temporary. Check the host's container config, not the guest's fstab.

Point the backend at it with `SENTINEL_ARCHIVE_HOST_PATH` /
`SENTINEL_COLD_HOST_PATH` in `ops/.env.prod` — both accept either a bare
volume name (dev default) or a host path (bind mount).

### What filled the disk on 2026-08-01

Worth understanding in full, because the failure mode is silent and the fix
is not obvious from the symptom.

`Store.latest_per_check()` was spelled as `SELECT DISTINCT ON (check_id) …
ORDER BY check_id, finished_at DESC`. The only index on `check_runs` that
could have served it was `idx_run_check`, which is on **`started_at`**, not
`finished_at` — so the planner fell back to a Seq Scan plus a full Sort of
the table. With `work_mem=4MB` and `temp_file_limit=-1`, each call spilled
~1.4 GB into `base/pgsql_tmp`.

That method is called from three hot paths — the scheduler tick, every alarm
evaluation, and every `/api/status` poll. At 25 k rows (May) the sort ran in
memory in milliseconds and nobody noticed. At 2.2 M rows (August) the calls
took ~2 minutes each, arrived faster than they drained, and stacked: 15
concurrent copies, 13 GB of temp files, disk full, Postgres wedged. Data
collection stopped for 6 d 17 h until a host reboot cleared the temp files.

Three defences are now in place, and all three matter:

1. `idx_run_check_finished (check_id, finished_at DESC)` — gives the query an
   index at all.
2. The query is a recursive loose index scan, not `DISTINCT ON` — see the
   comment on `Store.latest_per_check`. Postgres has no skip scan, so
   `DISTINCT ON` reads *every* row even with a perfect index. 3.2 ms vs
   6.8 s, and flat as the table grows.
3. `temp_file_limit=1GB` in the compose `command:` — any future query that
   tries to spill more now fails loudly instead of taking the host down.

If you ever see `pgsql_tmp` growing into the gigabytes, some query has lost
its index. Find it with:

```bash
ssh <prod-host> 'du -sh /var/lib/docker/volumes/sentinel_pgdata/_data/base/pgsql_tmp'
docker exec sentinel-postgres psql -U sentinel -d sentinel -c \
  "SELECT pid, now()-query_start AS dur, left(query,120) FROM pg_stat_activity
    WHERE state='active' ORDER BY dur DESC LIMIT 10;"
```

`log_temp_files=64MB` is on, so offenders also announce themselves in the
Postgres log.

### Retention / offload

`backend/retention.py` runs a daily sweep at `SENTINEL_RETENTION_HOUR_UTC`
(default 09:00 UTC). It is **offload, not delete**: rows are exported to
`/data/cold/<table>/<table>_<from>_<to>.csv.gz`, the file is fsync'd, and the
COPY row count is verified against the count about to be removed. Any
mismatch aborts before the delete.

| Env var | Effect |
|---|---|
| `SENTINEL_DB_RETENTION_DAYS` | Offload `check_runs` + `metric_samples` older than N days. Unset = keep everything. Production: **60**. |
| `SENTINEL_ARCHIVE_RETENTION_DAYS` | Delete archived images not seen in N days. Unset = permanent. Production: **unset** — the archive lives on 2.5 TB of NFS, there is nothing to gain by pruning it. |
| `SENTINEL_RETENTION_HOUR_UTC` | Hour of the daily sweep. Deliberately wall-clock, so a restart doesn't re-trigger it. |

Note that `SENTINEL_ARCHIVE_RETENTION_DAYS` was parsed into `Settings` but
read by **nothing** from the archive feature shipping until 2026-08-08. If
you are reading old notes that claim it prunes anything before that date, it
did not.

Deleting rows returns space to Postgres' free space map, not to the
filesystem — the table stops growing rather than shrinking. That is the
intent. To actually return space to the OS you need `VACUUM FULL`, which
takes an exclusive lock and needs free space equal to the table size:

```bash
# Rare. Stop the backend first; this locks check_runs for the duration.
docker compose -f ops/docker-compose.prod.yml --env-file ops/.env.prod stop backend
docker exec sentinel-postgres psql -U sentinel -d sentinel -c "VACUUM FULL check_runs;"
docker compose -f ops/docker-compose.prod.yml --env-file ops/.env.prod start backend
```

### Restoring offloaded rows

The cold exports are plain gzipped CSV with a header row:

```bash
gunzip < /mnt/aqpi-data/cold/check_runs/check_runs_20260516T015206_20260610T000000.csv.gz \
  | docker exec -i sentinel-postgres psql -U sentinel -d sentinel \
      -c "COPY check_runs (id, check_id, target, stage, status, started_at,
                           finished_at, summary, payload, artifacts)
          FROM STDIN WITH (FORMAT csv, HEADER true)"
```

`check_runs.id` is preserved in the export, so re-imported rows keep their
original identity. If the sequence has since passed those ids there is no
conflict; if you are restoring into a fresh database, run
`SELECT setval('check_runs_id_seq', (SELECT max(id) FROM check_runs));`
afterwards.

### Backups

`ops/backup.sh` dumps Postgres to the NFS share and keeps the newest 14.
Install it on the prod host:

```bash
ssh <prod-host> 'crontab -l 2>/dev/null; echo "0 8 * * * /srv/sentinel/ops/backup.sh >> /var/log/sentinel-backup.log 2>&1"' | ssh <prod-host> crontab -
```

It runs at 08:00 UTC, an hour ahead of the retention sweep, so every night's
dump predates the offload that removes rows.

### Self-monitoring

`layer0.self.disk` (L0, 5-minute cadence) reports local-disk and archive-mount
headroom, warning at 75% and failing at 88% — both overridable via the global
thresholds `disk_warn_pct` / `disk_fail_pct` in `/admin/thresholds`. It exists
because in August 2026 every radar check was green while the box hosting them
ran out of disk.

---

## Troubleshooting

### "The dashboard is frozen"

Run the diagnostic checklist:

1. `curl /api/_debug/stats`. If `event_loop_lag_ms` > 100 ms or
   `httpx.in_flight` is pegged, it's a backend wedge. Restart backend.
2. If backend is healthy and `websocket.clients: 0`, the browser tab
   is wedged client-side. Hard-refresh.
3. If hard-refresh hangs, the Vite dev server (in dev) may have
   drifted — `bash scripts/restart-fe.sh` to clear `.svelte-kit/` +
   `node_modules/.vite/`.
4. Use `?diag=...` flags to bisect (see "Diag harness" below).

### "Side panels are empty but the map loads"

Almost certainly CORS or the fetch-prefix patch failing.
- Open DevTools Network tab. Look at `/api/status` — does it return
  200, or does it fail before sending?
- If the URL the browser uses isn't right (wrong port, wrong host),
  it's the `frontend/src/lib/origin.ts` heuristic. See the comments
  there — port 3000 = "no proxy, backend at :8000"; anything else =
  "same-origin, behind a reverse proxy".
- If the URL is right but CORS rejects, check the backend's CORS
  middleware in `backend/api/app.py`. With Bearer-token auth we do
  NOT use `allow_credentials=True` — combining that with
  `allow_origins=["*"]` is forbidden by browsers.

### "Map loads but no radar overlays"

Click radar names in the **Layers** panel to make them "active" —
overlays are off by default.

### "Basemap doesn't render (only blank gray tiles)"

Stadia Maps is 401'ing because the domain you're browsing from isn't
in the allow-list for their free tier. Go to
**https://client.stadiamaps.com** → Properties → Authorized Domains →
add your domain. Takes ~2 min to propagate.

### "WebSocket isn't connecting"

Check `/api/_debug/stats → websocket.clients`. If `0` while you have
the tab open:
- **Direct compose deploy**: WebSocket should just work. Check the
  Vite proxy in dev — `vite.config.ts` needs `ws: true` on the
  `/api` proxy entry (already set, but if you reset the config…).
- **Behind a reverse proxy**: the `/api` router must forward the
  WebSocket upgrade headers (`Connection: Upgrade`, `Upgrade:
  websocket`). Most modern proxies (Traefik, Caddy, nginx with
  `proxy_set_header Upgrade $http_upgrade; proxy_set_header
  Connection $connection_upgrade`) do this when configured; the
  default plain-HTTP forwarder in some setups does NOT. If the WS
  fails but the page loads, this is almost always the cause.

The dashboard works WITHOUT WS — it falls back to 5s polling.
Live pulse animations and instant alarm-fire just get deferred.

### "Login fails with the right password"

Check the `users` table:
```sql
SELECT id, email, role, password_hash IS NOT NULL AS has_pw, disabled_at FROM users;
```
- `has_pw = f`: user was invited but never set a password. Re-issue
  reset link via Admin → Users.
- `disabled_at NOT NULL`: user is disabled.
- Otherwise: the password is wrong. `SENTINEL_ADMIN_PASSWORD` env
  vars are ignored once any user exists — they don't reset it.

### "A check just went red with `transport: [Errno -5] No address associated with hostname`"

That's a local DNS resolver flake — our side, not radarca's. The
scheduler's two-path demote should catch it:

- For raw bubbled exceptions: `scheduler._is_local_dns_error()` walks
  the cause chain.
- For in-check-caught errors that surface as `status=error` with a DNS
  marker in `summary`: `scheduler._maybe_downgrade_for_dns_summary()`
  matches by substring.

If a row stuck on `status=error` with a DNS marker, the in-check-catch
demote didn't fire. Verify the summary string contains one of the
markers in `scheduler._DNS_ERROR_MARKERS` (`gaierror`, `[errno -2]`,
`[errno -3]`, `[errno -5]`, `name or service not known`, `no address
associated with hostname`, `temporary failure in name resolution`).
If your evaluator catches transport exceptions, format the original
error into the summary so the post-result pass can match it.

Historical rows already painted red can be batch-demoted via
`/admin/thresholds` → retroactive reprocess, or
`python -m backend.reprocess_dns_errors`.

### "Failures vanished when I switched timeline grain"

`/api/history/timeline` snaps `until` UP to the next bucket boundary.
If a row had a fail at wall-clock 23:08 and you're at 15 m grain, that
falls in the bucket aligned to 23:00–23:15 — which is included.
If you're seeing failures disappear, check `_BUCKETS_S` in
`backend/api/routes/history.py` is correct and the snap direction
in `history_timeline()` is still rounding up, not down. (Was a bug
prior to 2026-05-18.)

### "L4 image previews show 'Image no longer available'"

Radarca rotates files out of its ~2h rolling window. For runs older
than that, the upstream PNG is gone. **From v0.1.x onward we archive
captured PNGs to a local volume** (`sentinel_archive` named volume,
`data/archive/<sha[:2]>/<sha>.png` on disk), so this only happens
for runs from before the archive feature shipped.

### "I wiped `ops/.env.prod` somehow"

If the env file is gone on the prod host but the postgres container
is still running, the password is recoverable from the running
container's env:
```bash
ssh <prod-host> '
  PG_PASS=$(docker exec sentinel-postgres printenv POSTGRES_PASSWORD)
  echo "POSTGRES_PASSWORD=$PG_PASS" > /srv/sentinel/ops/.env.prod
  # then add the remaining vars from ops/.env.prod.example
'
```
The recommended deploy path (GHCR pull) doesn't touch the env file
at all, which is the cleanest way to avoid this class of accident.
If you're using a source-tree deploy (`rsync`, `scp`, etc.), make
sure `ops/.env.prod` is excluded from the copy.

---

## Diag harness

The frontend has a `?diag=` URL param that toggles off subsystems on
the Live page, useful for bisecting freezes / perf issues:

| Flag | What it disables |
|---|---|
| `no-ws` | WebSocket subscription |
| `no-poll` | 5s polling refresh |
| `no-map` | MapView |
| `no-spark` | Sparkline SVGs |
| `no-tick` | 1s clock tick |
| `no-pulse` | Status-dot transition animation |

Combine with commas: `?diag=no-ws,no-map`. A yellow banner shows
which flags are active. The "clear" link in the banner resets.

---

## Common pitfalls (things that bit us)

| What | Why | Where |
|---|---|---|
| Source-tree deploys can wipe `ops/.env.prod` | rsync/scp with delete-extraneous flags will remove the gitignored env file if the source doesn't have one. | Prefer the GHCR-pull deploy path; if you must copy source, exclude `ops/.env.prod`. See troubleshooting recipe above. |
| Vite proxy: WS appeared to work but tab slowly leaked memory | Without `ws: true` Vite tangles WS with its HMR socket. | `frontend/vite.config.ts` — keep the `ws: true` flag. |
| Cross-origin cookies fail on plain HTTP LAN | `SameSite=None` requires `Secure` requires HTTPS. | We use Bearer tokens instead — don't switch back to cookies without putting TLS in front. |
| `credentials: 'include'` + `allow_origins=['*']` | Browser rejects the response — can't combine wildcard with credentials. | Don't add `credentials: 'include'` to fetch calls. Auth flows through `Authorization: Bearer` from `installFetchPrefix()`. |
| Reverting "weird" CSS `contain` rules | `contain: layout style paint` on timeline cells cuts ~80% of display-list rebuilds. Without it the timeline scrolls slowly. | `frontend/src/routes/timeline/+page.svelte` `<style>` block. Don't remove the comment block above it. |
| Mutating `sentinel.rollup.stages` in place | Svelte 5 deep-reactivity tracks property writes; on ~30 events/min this re-renders every subscriber. | Use the atomic-replacement pattern in `flushMerge`. The microtask coalescing is what makes the live tab not freeze. |
| Adding logging to every WS event | Same problem — every event hits ~17 reactive subscribers. | Backend `_maybe_broadcast` only emits run events on status transitions. If you change this, profile the idle tab afterward. |
| Reading `config.PRODUCTS[…]` directly from a new check | Bypasses the threshold registry → admin edits don't take effect. | Use `backend.thresholds.get_product/get_radar/get_l4/get_global` instead. The getters fall through to `config.py` on a fresh DB so behavior is unchanged. |
| Returning an evaluator's transport error as `status=error` without a DNS-marker summary | DNS-flake demote can't see the exception (it was caught in-check) AND can't see it in summary. | Format the original exception into the summary string (`f"A_api transport: {e}"`) so `_maybe_downgrade_for_dns_summary` can match it. |
| Adding a new push/SMS/Slack listener via `engine.add_listener` | Silences gate `engine._process`, NOT listeners. Pages get sent even when silenced. | Fetch `await store.list_active_silences(now)` + call `find_active_silence(payload, now)` before dispatching. |
| Snapping the timeline `until` DOWN to the bucket boundary | Truncates 0..(bucket_s−1) seconds of fresh data → failures vanish on grain switch. | `/api/history/timeline` rounds UP. The dense-bucket loop tolerates a partial in-flight bucket. |
| `<a href>` for navigation inside `MobileDrillDown` | Same-route param-only nav doesn't unmount the drilldown — the click silently no-ops. | Use `<button onclick={() => { detailOpen = false; goto(url); }}>` so the drilldown closes explicitly. |

---

## Where things live

| What | Path |
|---|---|
| Backend code | `backend/` |
| Check definitions | `backend/checks/layer{0,1,2,3,4}_*.py` |
| Alarm engine | `backend/alarms/` (engine + models + sinks + templates) |
| Notification group evaluator | `backend/groups.py` |
| Threshold registry + cache | `backend/thresholds.py` |
| Stage descriptor map (backend) | `backend/stages.py` |
| Retroactive reprocess engine | `backend/reprocess_engine.py` |
| Web Push dispatch + filters | `backend/push.py` |
| Authentication | `backend/auth/`, `backend/api/routes/auth.py` |
| API routes | `backend/api/routes/*.py` |
| Schema | `backend/db/schema.sql` (idempotent; auto-applied) |
| Image archive helpers | `backend/archive/` |
| One-off reprocess scripts | `backend/reprocess_l1_forecasts.py`, `reprocess_l2_dead_moments.py`, `reprocess_l4.py`, `reprocess_dns_errors.py` |
| Threshold calibration helper | `backend/tune_silent_fail.py` |
| Frontend code | `frontend/src/` |
| Routes (desktop pages) | `frontend/src/routes/` |
| Mobile shell + routes | `frontend/src/routes/m/`, `frontend/src/routes/+layout.svelte` (isMobile gate) |
| Mobile UA-sniff redirect | `frontend/src/hooks.server.ts` |
| Stage / product vocabulary | `frontend/src/lib/format.ts` |
| Auth/state stores | `frontend/src/lib/stores/` |
| API client + origin helper | `frontend/src/lib/api.ts`, `frontend/src/lib/origin.ts` |
| Shared components | `frontend/src/lib/components/` (MultiSelectChips, PieStatus, MobileDrillDown, HistoryDetailModal, ReportExportModal, SilenceMatcherPicker, PushRoutingEditor, …) |
| Mobile components | `frontend/src/lib/components/mobile/` |
| Service worker + manifest | `frontend/static/sw.js`, `frontend/static/manifest.webmanifest` |
| Docker build files | `ops/Dockerfile.{backend,frontend}` |
| Production compose | `ops/docker-compose.prod.yml` |
| Dev compose (Postgres only) | `ops/docker-compose.dev.yml` |
| Dev scripts | `scripts/*.sh` (called via `make dev` / `make stop`) |
| Architecture diagram + walkthrough | `docs/ARCHITECTURE.md` |
| Upstream characterization | `docs/radarca-public-characterization.md` |
| Original implementation plan | `docs/radarca-implementation-plan.md` |

---

## Auto-generated API docs

FastAPI emits OpenAPI specs you can browse interactively under the
`/api/` prefix so they route through the same reverse-proxy rules as
every other backend endpoint:

- `https://<your-host>/api/docs` — Swagger UI
- `https://<your-host>/api/redoc` — alternative renderer
- `https://<your-host>/api/openapi.json` — raw spec

Use these instead of grepping `routes/*.py` when you want to know
what an endpoint accepts. All three are public by default — if you
ever want to gate them behind auth, set `docs_url=None,
redoc_url=None, openapi_url=None` in `create_app()` and mount them
on protected routes manually.
