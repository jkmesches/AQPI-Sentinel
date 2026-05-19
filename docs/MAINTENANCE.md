# Sentinel Maintenance Runbook

Operational reference for keeping Sentinel running in production
(`docker compose -f ops/docker-compose.prod.yml`) and locally
(`make dev`). The first half is "things you do regularly," the second
half is "what to look at when something's wrong."

If you're new to the project, also read [ARCHITECTURE.md](ARCHITECTURE.md)
first — it walks through how a check flows end-to-end and which files
own which responsibility.

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

1. Edit code locally, test in dev (`make dev`).
2. Rsync to the remote (note the `.env.prod` exclude — see "Common pitfalls"):
   ```bash
   rsync -avz --delete \
     --exclude='.git/' --exclude='.venv/' \
     --exclude='frontend/node_modules/' --exclude='frontend/.svelte-kit/' \
     --exclude='frontend/build/' \
     --exclude='data/' --exclude='__pycache__/' --exclude='*.pyc' \
     --exclude='.env' --exclude='ops/.env.prod' \
     ./ aqpisentinel:/srv/sentinel/
   ```
3. Rebuild + restart whatever changed:
   ```bash
   ssh aqpisentinel 'cd /srv/sentinel && \
     docker compose -f ops/docker-compose.prod.yml --env-file ops/.env.prod \
     up -d --build backend frontend'
   ```
   (Omit a service name to rebuild everything.)

### View logs

Prod:
```bash
ssh aqpisentinel 'docker logs -f --tail 100 sentinel-backend'
ssh aqpisentinel 'docker logs -f --tail 100 sentinel-frontend'
ssh aqpisentinel 'docker logs -f --tail 100 sentinel-postgres'
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
curl -s http://10.25.3.148:8000/api/_debug/stats | python3 -m json.tool
# or via Traefik:
curl -s https://aqpi.local.shirejoe.com/api/_debug/stats | jq .
```

Hit this **first** whenever the UI feels stuck — it instantly tells you
whether the backend is wedged or whether the issue is browser-side.

### Backups

Postgres snapshot:
```bash
ssh aqpisentinel 'docker exec sentinel-postgres pg_dump -U sentinel -d sentinel \
  | gzip > /tmp/sentinel-db-$(date +%F).sql.gz'
scp 'aqpisentinel:/tmp/sentinel-db-*.sql.gz' ~/backups/
```

Image archive (PNGs from L4 captures):
```bash
ssh aqpisentinel 'docker run --rm -v sentinel_archive:/v -v /tmp:/out alpine \
  tar czf /out/sentinel-archive-$(date +%F).tar.gz -C /v .'
scp 'aqpisentinel:/tmp/sentinel-archive-*.tar.gz' ~/backups/
```

Restore (warning: clobbers current state):
```bash
ssh aqpisentinel 'cd /srv/sentinel && docker compose -f ops/docker-compose.prod.yml \
  --env-file ops/.env.prod stop backend'
# Postgres:
gunzip < ~/backups/sentinel-db-YYYY-MM-DD.sql.gz \
  | ssh aqpisentinel 'docker exec -i sentinel-postgres psql -U sentinel -d sentinel'
# Archive:
scp ~/backups/sentinel-archive-YYYY-MM-DD.tar.gz aqpisentinel:/tmp/
ssh aqpisentinel 'docker run --rm -v sentinel_archive:/v -v /tmp:/in alpine \
  tar xzf /in/sentinel-archive-YYYY-MM-DD.tar.gz -C /v'
ssh aqpisentinel 'cd /srv/sentinel && docker compose -f ops/docker-compose.prod.yml \
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
ssh aqpisentinel 'docker exec -it sentinel-postgres psql -U sentinel -d sentinel'
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
- **Behind Traefik**: the `/api` router needs to forward WebSocket
  upgrade headers. Traefik v3 does this transparently if the request
  arrives with `Connection: Upgrade` + `Upgrade: websocket`; the
  current LXC route only forwards plain HTTP. Add WS-passthrough to
  the Traefik service.

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

### "I broke something with `--delete` rsync"

If you wiped `ops/.env.prod` on the remote (this happened during the
initial deploy — see git history), the postgres password is lost but
recoverable from the still-running container:
```bash
ssh aqpisentinel '
  PG_PASS=$(docker exec sentinel-postgres printenv POSTGRES_PASSWORD)
  echo "POSTGRES_PASSWORD=$PG_PASS" > /srv/sentinel/ops/.env.prod
  # plus the other vars from .env.prod.example
'
```
The fix is to always rsync with `--exclude='ops/.env.prod'`. The
"Deploy a code update" recipe above does this.

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
| `--delete` rsync wiped `.env.prod` | Source didn't have one, rsync deleted from dest. | Always add `--exclude='ops/.env.prod'`. |
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

FastAPI emits OpenAPI specs you can browse interactively. Just hit:

- `http://localhost:8000/docs` — Swagger UI
- `http://localhost:8000/redoc` — alternative renderer

Use these instead of grepping `routes/*.py` when you want to know
what an endpoint accepts. Both are enabled by default; if you ever
want to gate them behind auth, set `docs_url=None, redoc_url=None`
in `create_app()` and mount them on protected routes manually.
