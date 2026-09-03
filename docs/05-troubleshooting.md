# Troubleshooting

Symptom-driven recipes for the failures we've actually seen. Each
entry: what the user sees, what's actually happening, how to fix it.

Organized by the layer at which the symptom shows up:
[client](#client-symptoms), [routing &
notifications](#routing-notification-symptoms),
[detection](#detection-symptoms), [data](#data-symptoms),
[deployment](#deployment-symptoms).

Before working through any of these, run the **5-minute health drill**
from [`MAINTENANCE.md` § Performance tuning](MAINTENANCE.md#performance-tuning):

1. `/api/_debug/stats | jq` — one-shot health snapshot.
2. `docker logs --since=5m sentinel-backend | grep -iE 'error|warn'`.
3. `network_monitor.online + dns_ok` in stats — confirms Sentinel's
   own connectivity.
4. `/admin/audit` — last 10 admin actions, in case a config change
   preceded the issue.

---

## Client symptoms

### "The dashboard is frozen"

The UI stopped updating. Three layers can be at fault:

1. **Backend wedge.** `/api/_debug/stats` shows
   `event_loop_lag_ms > 100` or `httpx.in_flight` pegged. Restart
   the backend container.
2. **WebSocket dropped.** Stats show `websocket.clients: 0` while
   you have the tab open. The frontend fell back to 5s polling
   (live updates work, just slower). Hard-refresh the tab to
   reconnect. Persistent disconnects mean your reverse proxy isn't
   forwarding the WS upgrade headers — see [WebSocket isn't
   connecting](#websocket-isnt-connecting) below.
3. **Browser tab wedge.** Hard-refresh hangs or the page is at
   100% CPU. Bisect with the `?diag=` flag (see [Diag harness](#diag-harness)
   below) to identify which subsystem is the culprit.

### "Side panels are empty but the map loads"

CORS or the fetch-prefix patch failing. Open DevTools → Network.

- **`/api/status` returns 200**: data is loading but the panels'
  reactive subscribers aren't firing. Hard-refresh.
- **`/api/status` fails before sending**: wrong URL. The frontend's
  `lib/origin.ts` heuristic picks the backend origin based on the
  current port. Port 3000 = "no proxy, backend at :8000"; anything
  else = "same-origin, backend behind reverse proxy". If you're on
  3000 but the proxy puts backend at a different host, set
  `PUBLIC_SENTINEL_API_BASE` and rebuild the frontend image.
- **CORS rejects the response**: the backend's CORS middleware in
  `backend/api/app.py` allows `*` but **without** `allow_credentials=True`
  (that combination is forbidden by browsers). If you've added
  `credentials: 'include'` to a fetch call somewhere, the browser
  will reject — auth goes through `Authorization: Bearer` instead,
  set globally by `installFetchPrefix()`.

### "Basemap shows blank gray tiles"

Stadia Maps 401'd because the hostname you're browsing from isn't on
their domain allowlist for your free-tier account.

Fix: [client.stadiamaps.com](https://client.stadiamaps.com) →
Properties → Authorized Domains → add the hostname → ~2 min to
propagate. Or set up your own paid API key — see
[`02-deployment.md` § Stadia Maps](02-deployment.md#8-stadia-maps-tiles).

### "Map loads but no radar overlays"

The overlay layers are off by default. Click radar names in the
**Layers** panel to activate them.

### "Mobile map shows 'Map paused (graphics context dropped)'"

iOS Safari dropped the WebGL context — usually because the tab was
backgrounded under memory pressure. MapLibre can't recover the style
on its own, so we surface a "Reload map" button to give you a clean
reset. Tap it.

If this happens frequently on a specific device, that device is
likely tight on memory; close other tabs.

### WebSocket isn't connecting

`/api/_debug/stats → websocket.clients: 0` while the tab is open.

- **Direct compose deploy** (no reverse proxy): WS should just work.
  If it doesn't, check that the frontend can reach `:8000` from the
  browser's network position (sometimes a host firewall blocks
  intra-LAN traffic to non-standard ports).
- **Behind a reverse proxy**: the proxy's `/api/*` route must
  forward the `Connection: upgrade` and `Upgrade: websocket`
  headers. See [`02-deployment.md` § TLS + reverse
  proxy](02-deployment.md#5-tls-reverse-proxy) for drop-in configs
  that handle this correctly for Caddy, nginx, and Traefik.

The dashboard works **without** WebSocket — it falls back to 5s
polling. Live pulse animations and instant alarm-fire are deferred,
but no data is lost.

---

## Routing & notification symptoms

### "I'm not getting alerts for a failure I'm watching"

Walk the alert pipeline backwards from the missing notification to
the alarm. The pipeline (see also [`03-administration.md` § Alert
lifecycle](03-administration.md#1-the-alert-lifecycle-read-this-first)):

```
check_run → alarm opened → matched by route → routed through policy
→ recipients resolved → groups expanded → schedule gate → email/push fired
```

Layer-by-layer:

1. **Did the alarm open?** Check `/admin/audit` for an `alarm_open`
   entry. Or query: `SELECT * FROM alarms WHERE opened_at > now() -
   interval '1 hour' ORDER BY opened_at DESC LIMIT 10;`. If no row,
   the check didn't return a non-pass status — see [detection
   symptoms](#detection-symptoms).
2. **Did a route match?** `/admin/alerts` → look at each routing
   rule's match block. Remember: **first match wins**. If a
   too-broad rule above your target rule matched first, the target
   rule never fires.
3. **Did the policy fire?** Each escalation policy step has its own
   recipients + delay. If you're missing the step-0 notification,
   step 0's recipients are wrong. If step-1 didn't fire 15 minutes
   later, the alarm was acked before the delay elapsed (intended
   behavior).
4. **Did recipients resolve?** A recipient row with only an empty
   `emails[]` and an empty `group_ids[]` is a no-op. A recipient
   pointing at a group whose schedule says "off duty right now"
   silently expands to zero members.
5. **Did the group's schedule include you?** Open the group in
   `/admin/groups` → preview pane → "is this group on duty right
   now?" Should be "Yes" with you in the member list.
6. **Did the silence list swallow it?** Check `/admin/silences`
   for any active silence whose matchers cover the alarm. Active
   silences mute notifications but still record alarms — so the
   alarm shows in the timeline, just no email/push went out.
7. **Did the email/push subsystem actually try?** Backend logs at
   `INFO` level include lines like
   `web-push <check>/<target>: sent=N failed=N expired=N filtered=N
   deferred=N` for every push attempt. Grep them. For email,
   bump `SENTINEL_LOG_LEVEL=DEBUG` and replay; the SMTP exchange
   prints in full.

### "Push notifications stopped arriving on my phone"

Three usual causes:

1. **Subscription expired.** iOS/Android push services tear down
   subscriptions after long inactivity. Open `/m/more` on the phone
   and toggle push off + on — re-subscribes you.
2. **Per-device routing is filtering them out.** Check
   `/m/push-settings/edit/<id>` → severity floor, match patterns,
   on-duty schedule, delay. A pattern allowlist filters out alarms
   that don't match — **except L0/canary alarms, which always
   bypass the filter** (intentional safeguard, can't disable).
3. **VAPID key changed.** If `settings.vapid_keys` was deleted or
   regenerated, every existing subscription stopped working
   silently. Toggle push off + on on each device to re-subscribe.

### "Test alert in /admin/alerts says success but I never received the email"

Sentinel reports success when SMTP accepted the message — the
provider's downstream delivery is out of scope. Check the provider's
delivery logs (Mailgun/SendGrid/SES all have a "recent activity"
view). Likely causes:

- **SPF/DKIM** misconfigured on the From-address domain → recipient
  spam-folder route.
- **Recipient's mail server** is rejecting from your provider's IP
  range → bounces in the provider's log.
- **Provider quota** exceeded → silent throttle.

### "Email arrives but goes to spam"

Set up SPF + DKIM + DMARC on the From-address domain. This is
non-negotiable for production email — most providers will let you
SEND without it, then their downstream peer servers route everything
to spam. The provider's documentation walks through the DNS records.

---

## Detection symptoms

### "A check just went red with `[Errno -5] No address associated with hostname`"

Local DNS resolver flake — Sentinel's side, not the upstream's. The
scheduler's two-path demote should catch it:

- **Raw bubbled exceptions** — `_is_local_dns_error()` walks the
  cause chain.
- **In-check-caught errors** that surface as `status=error` with a
  DNS marker in `summary` — `_maybe_downgrade_for_dns_summary()`
  matches by substring.

If a row stuck on `status=error` with a DNS marker, the in-check
demote didn't fire. Verify the summary string contains one of the
markers in `scheduler._DNS_ERROR_MARKERS` (`gaierror`,
`[errno -2]`, `[errno -3]`, `[errno -5]`, `name or service not
known`, `no address associated with hostname`, `temporary failure
in name resolution`, `dns lookup failed`).

Historical rows already painted red can be batch-demoted via
`/admin/thresholds` → retroactive reprocess, or `python -m
backend.reprocess_dns_errors`.

### "Cascade-demote isn't firing — downstream cells still red during an outage"

The scheduler demotes a check to skip when any `depends_on` ancestor
is unhealthy. If it's not happening:

1. **Check the dependency graph.** `GET /api/checks` → look at each
   downstream check's `depends_on` array. If the parent isn't
   listed (transitively or directly), the demote walk won't find
   it.
2. **Check the in-process status cache.** The scheduler's
   `_latest_status` cache is seeded from the store on startup, then
   updated post-tick. If the parent's status hasn't been written
   since restart, the cache may read stale data. Wait one cadence
   cycle.
3. **Check the await-upstream gate timing.** When a downstream tick
   starts while its parent is still mid-tick, the gate waits up to
   `max_wait_s` (default 10 s). If the parent takes longer, the
   downstream proceeds without the latest status. Look for
   `proceeding without upstream settle` in backend logs.

To verify the dep tree by hand:

```sql
SELECT check_id,
       latest_status,
       payload->>'reason'      AS reason,
       payload->>'suppressed_by' AS suppressed_by
FROM (
  SELECT DISTINCT ON (check_id, target)
         check_id, target, status AS latest_status, payload
  FROM check_runs
  ORDER BY check_id, target, finished_at DESC
) t
WHERE check_id LIKE 'layer%';
```

### "Failures vanished when I switched timeline grain"

`/api/history/timeline` snaps the `until` bucket boundary **UP**
(rounds forward) so the in-flight bucket is included. If you see
failures disappearing on grain switch, `_BUCKETS_S` in
`backend/api/routes/history.py` is wrong or the snap direction
got flipped. Was a real bug prior to 2026-05-18; baked in regression
test would be welcome.

### "L4 image previews show 'Image no longer available'"

Radarca rotates files out of its ~2-hour rolling window. For runs
older than that, the upstream PNG is gone. **Sentinel archives
captured PNGs to a local volume** (`sentinel_archive`,
`data/archive/<sha[:2]>/<sha>.png`), so this only happens for runs
from before the archive feature shipped (v0.1.x onward archives by
default).

### Sparklines look "normal" but data has actually stopped

This used to be a real bug — sparklines plotted by array index, so
30 stale samples looked identical to 30 fresh samples. Fixed in
v0.1.0: sparklines now plot **sample-arrival rate per cadence
bucket**, with x-axis tied to wall-clock time. When data stops, the
trace falls to baseline and the inline label shows `0/<window>`.

If you see this old behavior somewhere new, the call site is
passing the wrong data shape to `<Sparkline>` — it expects
`{ts, value}[]`, not `number[]`.

---

## Data symptoms

### "Login fails with what I'm sure is the right password"

Check the `users` table:

```sql
SELECT id, email, role,
       password_hash IS NOT NULL AS has_pw,
       disabled_at
FROM users
WHERE email = 'you@example.com';
```

- **`has_pw = f`** — user was invited but never set a password.
  Re-issue the reset link via Admin → Users.
- **`disabled_at NOT NULL`** — user is disabled. Re-enable via the
  same page, or in psql.
- **Otherwise** — the password is wrong. `SENTINEL_ADMIN_PASSWORD`
  env vars are ignored once any user exists; they don't reset
  existing passwords.

### "I'm locked out of the admin role entirely"

Recover via psql:

```bash
ssh <prod-host> 'docker exec -it sentinel-postgres psql -U sentinel -d sentinel'
```

```sql
-- Promote yourself to admin + re-enable.
UPDATE users SET role = 'admin', disabled_at = NULL
WHERE email = 'you@example.com';
-- Clear your password to force a reset via the public flow.
UPDATE users SET password_hash = NULL
WHERE email = 'you@example.com';
-- Optional: invalidate every session (force everyone to re-log).
DELETE FROM sessions;
```

Then visit `/login` → Forgot password → follow the email reset link.

### "I wiped `ops/.env.prod` somehow"

The Postgres password is still recoverable from the running
container:

```bash
ssh <prod-host> '
  PG_PASS=$(docker exec sentinel-postgres printenv POSTGRES_PASSWORD)
  echo "POSTGRES_PASSWORD=$PG_PASS" > /srv/sentinel/ops/.env.prod
  # then re-add the remaining vars from ops/.env.prod.example
'
```

The GHCR-pull deploy path never touches the env file, so this
mostly happens on source-tree deploys that forgot to exclude it.

### "I want to undo a threshold edit"

`/admin/audit` records every threshold save with the full
before/after diff. Find the entry, copy the previous value out of
its `details` jsonb, paste back into `/admin/thresholds`, save.

Followed by a [retroactive
reprocess](MAINTENANCE.md#reprocess-historical-data) to re-classify
historical rows under the restored thresholds.

---

## Deployment symptoms

### "Container won't start, logs say POSTGRES_PASSWORD required"

The compose file declares `POSTGRES_PASSWORD:?POSTGRES_PASSWORD
required` as a fast-fail. Either:

- **`.env.prod` is missing or empty** — populate it from
  `.env.prod.example`.
- **Variable name typo** — must be exactly `POSTGRES_PASSWORD`.
- **Wrong `--env-file` path** — make sure your compose invocation
  carries `--env-file ops/.env.prod`.

### "Backend starts then immediately exits"

Check the logs:

```bash
docker logs --tail=50 sentinel-backend
```

Common causes:

- **Postgres not ready yet** — the compose file has
  `depends_on: postgres: condition: service_healthy` so this should
  be impossible, but check the postgres container's
  `healthcheck` status.
- **Schema apply failed** — `schema.sql` is idempotent but if
  manual edits to the DB introduced a state that violates a NOT
  NULL or unique constraint that the migration adds, the apply
  fails. Read the traceback to identify the offending row.
- **Port already in use** — another process is on 8000. `lsof -i
  :8000` to find it.

### "Frontend container won't start"

99% of the time, the backend isn't healthy yet. The frontend's
`depends_on: backend` waits for the backend to expose port 3000
(it doesn't — the backend exposes 8000), so this dependency is
weak. Wait 10 seconds and check `docker compose ps` again.

If the backend really is up and the frontend still fails, check the
build-arg `PUBLIC_SENTINEL_API_BASE` — if you set it to a URL that
isn't actually reachable from the browser, the bundled JS hits a
404 on every API call.

### "Image pull says authentication failed"

The GHCR packages are private and your `~/.docker/config.json`
doesn't have credentials for `ghcr.io`. Re-run:

```bash
echo "$GHCR_PAT" | docker login ghcr.io -u <your-username> --password-stdin
```

The PAT needs the `read:packages` scope (Classic token settings).
If the packages have been flipped to public, no login is required —
verify in the GitHub package settings.

### "I pulled `:latest` and now something is broken"

`SENTINEL_TAG=latest` tracks every push to `main`, including
work-in-progress fixes. Pin to a released version:

```bash
SENTINEL_TAG=0.1.0 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod pull
SENTINEL_TAG=0.1.0 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod up -d
```

See [`MAINTENANCE.md` § Rolling back a release](MAINTENANCE.md#rolling-back-a-release).

---

## Diag harness

The frontend has a `?diag=` URL param that toggles off subsystems on
the Live page. Useful for bisecting freezes and perf issues:

| Flag | What it disables |
|---|---|
| `no-ws` | WebSocket subscription |
| `no-poll` | 5s polling refresh |
| `no-map` | MapView |
| `no-spark` | Sparkline SVGs |
| `no-tick` | 1s clock tick |
| `no-pulse` | Status-dot transition animation |

Combine with commas: `?diag=no-ws,no-map`. A yellow banner shows
which flags are active. The "clear" link in the banner resets to
no flags.

Bisection strategy: start with all flags on (`?diag=no-ws,no-poll,
no-map,no-spark,no-tick,no-pulse`) — that's a static, frozen page.
Drop flags one at a time until the symptom returns; that's your
culprit.

---

## Common pitfalls (things that bit us)

| What | Why | Where |
|---|---|---|
| Source-tree deploys can wipe `ops/.env.prod` | rsync/scp with delete-extraneous flags will remove the gitignored env file if the source doesn't have one. | Prefer the GHCR-pull deploy path; if you must copy source, exclude `ops/.env.prod`. |
| Vite proxy: WS appeared to work but tab slowly leaked memory | Without `ws: true` Vite tangles WS with its HMR socket. | `frontend/vite.config.ts` — keep the `ws: true` flag. |
| Cross-origin cookies fail on plain HTTP LAN | `SameSite=None` requires `Secure` requires HTTPS. | Bearer tokens instead — don't switch back to cookies without putting TLS in front. |
| `credentials: 'include'` + `allow_origins=['*']` | Browser rejects the response — can't combine wildcard with credentials. | Don't add `credentials: 'include'` to fetch calls. Auth flows through `Authorization: Bearer` from `installFetchPrefix()`. |
| Reverting "weird" CSS `contain` rules | `contain: layout style paint` on timeline cells cuts ~80% of display-list rebuilds. | `frontend/src/routes/timeline/+page.svelte` `<style>` block. |
| Mutating `sentinel.rollup.stages` in place | Svelte 5 deep-reactivity tracks property writes; ~30 events/min re-render every subscriber. | Use the atomic-replacement pattern in `flushMerge`. |
| Adding logging to every WS event | Same problem — every event hits ~17 reactive subscribers. | Backend `_maybe_broadcast` only emits run events on status transitions. |
| Reading `config.PRODUCTS[…]` directly from a new check | Bypasses the threshold registry → admin edits don't take effect. | Use `backend.thresholds.get_product/get_radar/get_l4/get_global`. |
| Returning an evaluator's transport error as `status=error` without a DNS-marker summary | DNS-flake demote can't see the exception (caught in-check) nor see it in summary. | Format the original exception into the summary so `_maybe_downgrade_for_dns_summary` can match it. |
| Adding a push/SMS/Slack listener via `engine.add_listener` | Silences gate `engine._process`, NOT listeners. Pages get sent even when silenced. | Fetch `await store.list_active_silences(now)` + call `find_active_silence(payload, now)` before dispatching. |
| Snapping the timeline `until` DOWN to the bucket boundary | Truncates 0..(bucket_s−1) seconds of fresh data → failures vanish on grain switch. | `/api/history/timeline` rounds UP. |
| `<a href>` for navigation inside `MobileDrillDown` | Same-route param-only nav doesn't unmount the drilldown — the click silently no-ops. | Use `<button onclick={() => { detailOpen = false; goto(url); }}>`. |
| Product-pattern allowlist excluding L0 alarms | "I want to be notified about CBAND" excludes "origin is unreachable", the most critical class. | L0 + canary alarms unconditionally bypass per-device pattern filters. Safeguard is permanent. |

---

## Where to go from here

- **[`MAINTENANCE.md`](MAINTENANCE.md)** — backup, restore, perf,
  rollback recipes.
- **[`02-deployment.md`](02-deployment.md)** — every deploy-time
  knob, in case the issue is a config you didn't set right.
- **[`03-administration.md`](03-administration.md)** — admin UI
  walkthroughs.

If you've worked through these and still can't get it: open a
GitHub issue with the relevant `/api/_debug/stats` output, the
backend logs around the failure, and what you've already tried.
