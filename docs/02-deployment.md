# Deployment

This doc is the deep dive — every knob, every subsystem, every
trade-off you'll meet at deploy time. Pair it with:

- [`01-getting-started.md`](01-getting-started.md) — the
  fastest-path install. Read that first if you've never run Sentinel.
- [`04-maintenance.md`](MAINTENANCE.md) — day-2 operations
  (logs, backup, rollback, perf). Recurring concerns live there.

The framing remains radarca-specific. Pointing Sentinel at a
different upstream is its own concern, covered in
[`06-porting-to-other-upstreams.md`](#) (roadmap).

---

## 1. Choosing a host

Sentinel is light. A 2-vCPU / 4 GB / 30 GB host comfortably runs the
radarca-scale install (43 checks, ~30 req/min outbound). The
reference deployment runs on an LXC at those specs and rarely tops
1 GB resident — Playwright (used by the L3 overlay check) is the
fattest single dependency, holding a Chromium worker.

**What kind of host:**

| Kind | Works? | Notes |
|---|---|---|
| LXC container | ✓ | Best price/performance for a single-tenant deploy. Make sure `nesting=1` is set if Docker doesn't start. |
| KVM/VM | ✓ | No surprises. |
| Bare metal | ✓ | Overkill for a single install; great if you have spare hardware. |
| Cloud VM (EC2/DO/Hetzner/…) | ✓ | Watch egress bandwidth — pulling images and probing the upstream is the bulk of traffic. |
| Rootless Docker | ✓ | Tested; no special config beyond the usual rootless caveats. |
| Kubernetes | not yet | No Helm chart or Kustomize manifest shipped. The compose file translates straightforwardly but isn't on the roadmap. |

**Disk planning.** Two named Docker volumes hold persistent state:

- `sentinel_pgdata` — Postgres data. Grows roughly with the number
  of `check_runs` you retain. Default policy keeps everything; budget
  ~500 MB/month at radarca cadence.
- `sentinel_archive` — captured PNGs from L4 image checks. Grows
  ~600 MB/day worst case (one PNG per radar per L4 tick).
  `SENTINEL_ARCHIVE_RETENTION_DAYS` caps it; leave blank for
  permanent retention.

On 30 GB total disk, the volumes give you roughly 50 days of image
history before you're tight. Either bump the disk, set a retention,
or pull periodic backups off-host. ZFS and btrfs both work; **turn
off filesystem compression on the archive volume** — PNGs are
already compressed and the CPU spent re-trying is wasted.

**Network egress.** Sentinel needs outbound HTTPS (port 443) to:

- `ghcr.io` — image pulls.
- `radarca.engr.colostate.edu` — the upstream being monitored.
- `tiles.stadiamaps.com` — basemap tiles for the dashboard map.

If you're behind an egress proxy or strict allowlist, those are the
three names to whitelist.

---

## 2. GHCR images and authentication

CI publishes container images to GitHub Container Registry on every
push to `main` and on every `vX.Y.Z` tag (see
[`docker-publish.yml`](https://github.com/jkmesches/AQPI-Sentinel/blob/main/.github/workflows/docker-publish.yml)).
Available image namespaces:

- `ghcr.io/jkmesches/sentinel-backend`
- `ghcr.io/jkmesches/sentinel-frontend`

**Tags published per release:**

| Tag | Set when | Use for |
|---|---|---|
| `vX.Y.Z` (e.g. `v0.1.0`) | semver tag pushed | **Production.** Pin to a specific version for predictable rollback. |
| `X.Y` (e.g. `0.1`) | semver tag pushed | Pin to a minor; receive patch updates. |
| `X` (e.g. `0`) | semver tag pushed | Pin to a major; receive minor+patch updates. |
| `latest` | push to `main` OR semver tag | Tracks the most recent release. Fine for staging; risky for prod. |
| `sha-<short>` | every CI run | Reproducible reference to a specific commit. |

**Pinning strategy.** Prefer `vX.Y.Z` in production. Bumping is a
one-line change in `.env.prod` (`SENTINEL_TAG=v0.2.0`), followed by
`docker compose pull && up -d`. Pulling `latest` to test something,
then forgetting to pin back, is the most common foot-shoot.

### Private vs public packages

By default a package's visibility tracks its repo's. While the
repo is public but the packages are private (the current state),
the prod host needs to authenticate before pulling:

1. Generate a [Personal Access Token](https://github.com/settings/tokens)
   (Classic) with **only** the `read:packages` scope. Save it
   somewhere safe.
2. On the prod host:
   ```bash
   echo "$GHCR_PAT" | docker login ghcr.io \
       -u <your-github-username> --password-stdin
   ```
   Docker stores the credentials in `~/.docker/config.json` (or the
   configured credential helper). Subsequent `docker compose pull`
   calls authenticate automatically.

**Going public** drops the auth step entirely — anonymous pulls
work, multi-host deploys get easier, no PAT rotation. The downside
is no visibility into who pulled what. Flip per-package from
**github.com → your packages → sentinel-backend → Package settings
→ Change visibility → Public** (repeat for `sentinel-frontend`).

**Rotating the PAT.** Generate a new token, run `docker login` again
to overwrite the saved credentials, then revoke the old token. No
service restart needed; Docker re-reads `config.json` on the next
pull.

---

## 3. The compose files

Four compose files ship in `ops/`. They are not interchangeable:

| File | Images | Proxy | Use for |
|---|---|---|---|
| `docker-compose.deploy.yml` | Pulls from GHCR | **Included** | **Start here.** A complete stack: proxy with TLS, Postgres, backend, frontend. |
| `docker-compose.ghcr.yml` | Pulls from GHCR | none | You already run your own proxy and want Sentinel to slot into it. |
| `docker-compose.prod.yml` | Builds from source | none | Branch testing, air-gapped hosts, or anywhere GHCR is unreachable. |
| `docker-compose.dev.yml` | Postgres only | none | Developer workflow (`make dev`). **Never** point this at a prod database. |

### Why `deploy` is the recommended one

It is not merely `ghcr` plus a proxy. Three differences matter:

**It publishes only the proxy's ports.** Postgres, the backend and the
frontend are reachable only on the internal Docker network. That is
also what makes the API same-origin, which in turn means
`PUBLIC_SENTINEL_API_BASE` never has to be set — see §5.

**Retention is on by default.** `SENTINEL_DB_RETENTION_DAYS` defaults
to 60 in the file itself, not just in the example env. An operator who
never opens the env file still gets a database that does not grow
without bound. The other compose files leave it unset. This is not a
hypothetical concern: the reference deployment filled its disk and
crashed because of exactly this.

**Logs are capped and every service has a healthcheck.** Container
logs are the other unbounded disk-fill vector, and `depends_on:
condition: service_healthy` means the proxy does not start routing to
a backend that is still applying migrations.

### First install, start to finish

```bash
cp ops/.env.deploy.example ops/.env
chmod 600 ops/.env
$EDITOR ops/.env                    # POSTGRES_PASSWORD + admin account are required

bash ops/preflight.sh               # validates before anything starts

docker compose -f ops/docker-compose.deploy.yml --env-file ops/.env up -d
docker compose -f ops/docker-compose.deploy.yml --env-file ops/.env ps
```

`preflight.sh` checks the things that are cheap to catch now and
expensive to diagnose later: missing password, busy port, unreachable
GHCR package, a port accidentally left in `SENTINEL_SITE_ADDRESS`,
retention disabled, insufficient disk. Blockers exit non-zero;
judgement calls are warnings.

---

## 4. Environment variables

The full reference is `ops/.env.deploy.example` for the recommended
path (or `ops/.env.prod.example` if you build from source) — both are
annotated and self-documenting. The narrative version below explains
*what each variable controls* and *when you'd change it*.

### Postgres

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `POSTGRES_USER` | no | `sentinel` | DB role. Change only if you're sharing a Postgres instance. |
| `POSTGRES_PASSWORD` | **yes** | — | Used by the backend to connect. **Required**; compose fails fast without it. Use 48+ random chars. |
| `POSTGRES_DB` | no | `sentinel` | Database name. Change if multi-tenanting one Postgres. |

The first start writes these into the Postgres container. **Changing
`POSTGRES_PASSWORD` after first start** requires either restoring
from backup with the new password or manually rotating it inside
Postgres — it's not a no-op restart.

### Backend runtime

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SENTINEL_BASE` | no | `https://radarca.engr.colostate.edu` | Upstream root URL. Change to point Sentinel at a different deployment. |
| `SENTINEL_LOG_LEVEL` | no | `INFO` | Standard Python log levels. `DEBUG` is loud (every probe). Use `WARNING` if you want quieter logs. |
| `SENTINEL_ARCHIVE_ENABLED` | no | `1` | Persist L4 captured images. Disable to save disk if you don't need image history. |
| `SENTINEL_ARCHIVE_ROOT` | no | `/data/archive` | Mount path inside the backend container. Volume-mapped from `sentinel_archive`. |
| `SENTINEL_ARCHIVE_RETENTION_DAYS` | no | (blank = permanent) | Delete archived PNGs older than N days. Blank keeps everything. |

### Admin bootstrap (first start only)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SENTINEL_ADMIN_EMAIL` | yes¹ | — | Email of the first admin user. |
| `SENTINEL_ADMIN_PASSWORD` | yes¹ | — | Password for that user. |
| `SENTINEL_ADMIN_DISPLAY_NAME` | no | — | Shown in the admin UI. |

¹ Only required on the very first start when the `users` table is
empty. **Once any user exists, these are ignored.** You can leave
them set or delete them — Sentinel doesn't care.

### Public URL

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SENTINEL_PUBLIC_URL` | no | — | The URL users actually visit. Embedded in alert emails. Blank = no "Open in dashboard" link in emails. |

This is **not** how the backend decides what port to listen on — it
listens on 8000 inside the container regardless. This is purely the
URL operators see when an email lands.

### Ports

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BACKEND_PORT` | no | `8000` | Host-side port mapped to the backend container. |
| `FRONTEND_PORT` | no | `3000` | Host-side port mapped to the frontend container. |

If your reverse proxy is on the same host and using the standard
ports, you may want to bind these to `127.0.0.1` only. Edit
`ops/docker-compose.ghcr.yml`'s `ports:` block to do that.

### Frontend

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PUBLIC_SENTINEL_API_BASE` | no | (blank → resolve at runtime from `window.location.hostname:8000`) | Bake a backend origin into the frontend image. Set when the frontend and backend live at different origins. |

The dev workflow keeps this blank; the frontend asks the browser
where to find the backend. Set it only when you're serving the
frontend from a different host than the backend.

---

## 5. TLS + reverse proxy

A proxy in front of the stack solves four problems at once:

- **TLS termination** — Let's Encrypt or your internal CA.
- **WebSocket reliability** — Sentinel's live updates ride over
  `/api/ws`. Some networks tear down long-lived WS without TLS;
  HTTPS dodges that.
- **Hostname-based vhosting** — share the host with other services.
- **Single-origin browser security model** — `/api/*` to backend,
  `/` to frontend, same origin, no CORS dance.

**The routing pattern** is the same regardless of proxy:

- `/api/*` → `backend:8000`
- everything else → `frontend:3000`

**WebSocket upgrade headers are mandatory.** Without `Connection:
upgrade` and `Upgrade: websocket` being forwarded, the live tab
falls back to 5s polling, pulse animations stop firing, and alarm
notifications surface ~5 seconds late. Every config below handles
this.

### Caddy — shipped, no config to write

If you use `docker-compose.deploy.yml` you do not write any of this.
`ops/Caddyfile` is included and wired up; you set two variables in
`ops/.env` and it handles routing, WebSocket upgrade and certificates.

| Deployment | `SENTINEL_SITE_ADDRESS` | `SENTINEL_TLS_DIRECTIVE` |
|---|---|---|
| LAN, no TLS — works with no DNS at all | `:80` | *(blank)* |
| Internal hostname, self-signed | `https://sentinel.lab.example.edu` | `tls internal` |
| Public hostname, Let's Encrypt | `https://sentinel.example.edu` | `tls ops@example.edu` |

`:80` matches any hostname or IP, which is why the default install
needs no DNS and no edits.

!!! warning "Never put a port in `SENTINEL_SITE_ADDRESS`"
    Caddy binds whatever port the address names **inside** the
    container, while compose maps host ports to container 80/443.
    Writing `http://host:8080` makes Caddy listen on 8080 internally,
    leaves nothing behind container port 80, and the site becomes
    unreachable **with no error in any log**. To serve on a different
    host port set `HTTP_PORT` / `HTTPS_PORT` instead.
    `preflight.sh` rejects this before you hit it.

With `tls internal`, Caddy issues from its own CA. Browsers warn until
that CA is trusted once; export it with:

```bash
docker cp sentinel-caddy:/data/caddy/pki/authorities/local/root.crt .
```

The `sentinel_caddy_data` volume holds issued certificates and that CA.
Losing it means re-issuing certs — and for `tls internal`, re-trusting
a new CA on every client machine. Include it in backups.

### Rolling your own

The sections below are for slotting Sentinel into a proxy you already
run, using `docker-compose.ghcr.yml`. The routing is the same:
`/api/*` → `backend:8000`, everything else → `frontend:3000`.

### nginx

More explicit, more headers to remember. Save as
`/etc/nginx/sites-available/sentinel`:

```nginx
# Map upgrade header — required for WebSocket forwarding.
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl http2;
    server_name sentinel.example.com;

    # Cert paths — adjust to your ACME tooling.
    ssl_certificate     /etc/letsencrypt/live/sentinel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sentinel.example.com/privkey.pem;

    # Backend: /api/* → backend:8000 (including /api/ws and /api/docs).
    location /api/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        # These two lines are what make WebSocket work.
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        $connection_upgrade;
        proxy_read_timeout 3600s;     # long-lived WS connections
    }

    # Frontend: everything else → frontend:3000.
    location / {
        proxy_pass         http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name sentinel.example.com;
    return 301 https://$host$request_uri;
}
```

Symlink into `sites-enabled`, run `nginx -t`, reload. Watch
`/var/log/nginx/error.log` on the first request to confirm clean
upstream connections.

### Traefik

If Traefik is already managing your Docker stack, add labels to the
backend and frontend services in your override compose:

```yaml
services:
  backend:
    labels:
      - traefik.enable=true
      - traefik.http.routers.sentinel-api.rule=Host(`sentinel.example.com`) && PathPrefix(`/api/`)
      - traefik.http.routers.sentinel-api.entrypoints=websecure
      - traefik.http.routers.sentinel-api.tls.certresolver=letsencrypt
      - traefik.http.services.sentinel-api.loadbalancer.server.port=8000
    networks:
      - traefik
      - sentinel

  frontend:
    labels:
      - traefik.enable=true
      - traefik.http.routers.sentinel-fe.rule=Host(`sentinel.example.com`)
      - traefik.http.routers.sentinel-fe.entrypoints=websecure
      - traefik.http.routers.sentinel-fe.tls.certresolver=letsencrypt
      - traefik.http.services.sentinel-fe.loadbalancer.server.port=3000
      # Lower priority than the /api/ router so /api requests hit the API.
      - traefik.http.routers.sentinel-fe.priority=1
    networks:
      - traefik
      - sentinel

networks:
  traefik:
    external: true
```

Traefik v3 forwards WebSocket upgrade headers transparently when a
client request arrives with them set — no extra middleware needed.
**Trap to avoid**: if you've defined a custom HTTPS middleware that
sets `Connection` (e.g. for HSTS), make sure it's not overwriting
the upgrade header on `/api/ws` paths.

---

## 6. SMTP for email alerts

Email is the older of Sentinel's two notification surfaces (Web Push
is the other). Both are optional; if neither is configured, alarms
still land in the timeline and the dashboard updates live.

**Two config surfaces:**

- **Admin UI** at `/admin/email` — DB-backed, edit on the fly,
  preferred for most deploys.
- **`alerts.yaml`** at the project root — file-based fallback, used
  if the admin row is empty. Survives DB resets but requires source
  access on the prod host.

The admin UI takes precedence when both are populated.

### Provider walkthroughs

**Mailgun.** SMTP host `smtp.mailgun.org`, port `587`, STARTTLS,
SMTP user `postmaster@mg.yourdomain.com`, password from the Mailgun
SMTP credentials page. From-address must be on a verified domain.

**SendGrid.** SMTP host `smtp.sendgrid.net`, port `587`, STARTTLS,
SMTP user literally `apikey`, password = your API key. From-address
needs sender-identity verification in the SendGrid dashboard.

**Amazon SES.** SMTP host `email-smtp.us-east-1.amazonaws.com` (or
your region), port `587`, STARTTLS, SMTP credentials generated from
the SES console (NOT your AWS access keys). Move out of sandbox mode
before pointing at real recipients.

**Postmark.** SMTP host `smtp.postmarkapp.com`, port `587`, STARTTLS,
SMTP user = your server token, password = the same token.

**Generic SMTP relay.** Whatever your provider gave you. Port `587`
with STARTTLS is the modern default; `465` with implicit TLS works
for older providers. Plain `25` should be a last resort and only on
trusted LAN.

### From-address requirements

Most providers reject mail from unverified domains outright; the few
that don't will let you send, then route everything to spam. SPF and
DKIM on the sender domain are non-negotiable for production.

### Testing without blasting everyone

From `/admin/alerts`, pick any routing rule → **Send test alert**.
The test exercises the full pipeline (route → recipient resolution →
group schedule → SMTP) but sends to a single hardcoded scratch
recipient (the admin who clicked the button). Iterate freely.

### Failure modes

| Symptom | Likely cause |
|---|---|
| Connection times out | Provider blocks port 25/465/587 from your IP |
| `STARTTLS not supported` | Wrong port for the provider's TLS mode |
| `Authentication failed` | Wrong user/password — providers use API tokens, not your account password |
| Mail sent, never arrives | SPF/DKIM not set up; recipient's spam folder swallowed it |

Backend logs include the SMTP exchange at `DEBUG`. Bump
`SENTINEL_LOG_LEVEL=DEBUG`, retry, watch the trace.

---

## 7. Web Push (VAPID)

The push subsystem self-bootstraps. On first backend start, it
generates a VAPID keypair and stores it in `settings.vapid_keys`
(jsonb column). The keypair persists across restarts; subscribed
devices keep working as long as that row exists.

**Don't delete the row** unless you intentionally want every device
to silently stop receiving pushes. The frontend re-subscribes on the
next visit, which fixes it — but in the meantime, push is dark.

**Re-generating intentionally.** Two scenarios where this is right:

- Suspected key compromise.
- Moving Sentinel to a new public URL — push subscriptions are
  bound to the URL, so changing `SENTINEL_PUBLIC_URL` invalidates
  existing subscriptions anyway. Re-genning the VAPID keypair forces
  a clean slate.

To regenerate:

```sql
DELETE FROM settings WHERE key = 'vapid_keys';
```

Restart the backend. The next startup generates a fresh pair. Each
user opens `/m/more` on their phone and re-enables notifications.

### iOS Safari install-before-push

iOS only delivers Web Push to PWAs that have been added to the home
screen. Sentinel's mobile shell detects this and disables the
"Enable notifications" button on iOS Safari until the app is
installed. Users who try to enable push without installing get a
clear copy-string telling them what to do — no silent failures.

Android (Chrome / Edge / Samsung Internet) has no such requirement
— push works in the browser tab without installing the PWA. The
"Install app" button shows up if the browser fires
`beforeinstallprompt`, but it's purely a convenience.

---

## 8. Stadia Maps tiles

The dashboard map uses Stadia Maps' free tier with their
`alidade_smooth` (light) and `alidade_smooth_dark` (dark) styles. No
API key is baked into the codebase.

Stadia's free tier works **with a domain allowlist** — they check
the `Referer` header against your registered domains. For
non-production / dev / localhost, this is automatic.

**When you need to configure your own Stadia account:**

- You're deploying on a hostname Stadia doesn't already know about.
- You're hitting their free-tier rate limits.
- Commercial use — read their TOS.

To swap in your own key or a different style:

1. Sign up at [stadiamaps.com](https://stadiamaps.com), get an API
   key.
2. Edit `frontend/src/lib/components/MapView.svelte` (desktop) and
   `frontend/src/lib/components/mobile/MobileStatusMap.svelte`
   (mobile) — search for `STYLE_LIGHT` and `STYLE_DARK` constants.
   Append `?api_key=YOUR_KEY` to the style URLs.
3. Rebuild the frontend image.

**Different basemap provider entirely** (MapTiler, Maptiler Cloud,
Mapbox, self-hosted tile server): swap the style URL constants to
point at the new provider's style JSON. The MapLibre code is
provider-agnostic.

---

## 9. First-time deploy checklist

**Before you start anything**, run `bash ops/preflight.sh`. It covers
the whole pre-boot half of this list — docker present, env file valid
and not world-readable, required secrets set, ports free, images
pullable, retention configured, disk adequate, compose and Caddyfile
both parse. Everything below is the post-boot half, which only a
running system can answer.

Walk through these once before declaring the deploy "done":

- [ ] `docker compose ps` shows every container `running healthy`
      (four with `docker-compose.deploy.yml`: caddy, backend,
      frontend, postgres).
- [ ] Backend logs include `scheduler started with N checks (max
      topological rank M)` where N matches your check count.
- [ ] `/api/_debug/stats → event_loop_lag_ms` < 5 ms.
- [ ] `/api/_debug/stats → network_monitor.online == true` and
      `network_monitor.dns_ok == true`.
- [ ] `/api/_debug/stats → scheduler.tasks_running == tasks_total`.
- [ ] You can log in at `/` with the bootstrap admin credentials.
- [ ] `/api/docs` (Swagger UI) loads and lists routes.
- [ ] The dashboard map shows tiles (not a gray void) — confirms
      Stadia reachability.
- [ ] At least one row in the Site rail is `pass` — confirms the
      backend can reach the upstream.
- [ ] If SMTP is configured: `/admin/alerts → Send test alert`
      delivers an email.
- [ ] If Web Push is set up: at least one device is subscribed and
      a test alert arrives as a notification.
- [ ] Simulate an upstream outage (block egress to the upstream for
      30 seconds): only the root failure cell goes red, downstream
      checks demote to gray with `↑` badges.
- [ ] Confirm retention is actually on:
      `docker exec sentinel-backend printenv SENTINEL_DB_RETENTION_DAYS`
      returns a number. Blank or `0` means the database grows without
      bound, which is a slow failure you will not notice for months.
- [ ] Take one backup and **restore it somewhere else** before you
      need to. An untested backup is a guess — see
      [`MAINTENANCE.md`](MAINTENANCE.md).

If any box doesn't tick, the corresponding subsystem isn't set up
yet — go back to its section above.

---

## 9b. Backups, and moving existing data

### Backups are not automatic

Nothing takes a backup until you install the cron job. On the docker host:

```bash
0 8 * * * SENTINEL_BACKUP_DIR=/var/backups/sentinel \
          /srv/sentinel/ops/backup.sh >> /var/log/sentinel-backup.log 2>&1
```

Validate the configuration first, which writes nothing:

```bash
SENTINEL_BACKUP_DIR=/var/backups/sentinel bash ops/backup.sh --check
```

Schedule it **earlier** than `SENTINEL_RETENTION_HOUR_UTC` (default 09:00 UTC)
so a backup always precedes the sweep that deletes rows. Every setting is
documented in the header of `ops/backup.sh`; the important ones are
`SENTINEL_BACKUP_DIR`, `SENTINEL_BACKUP_KEEP` (must be ≥ 1), and
`SENTINEL_BACKUP_REQUIRE_MOUNT` (refuses to write unless the destination is a
real mountpoint, so a failed network mount cannot quietly fill the local disk).

The script fails safely: it writes to a `.part` file and renames only after
verifying the dump decompresses **and contains the Sentinel schema**, and it
prunes old dumps only after a good one exists. A failed run can never leave
you with fewer backups than you started with.

### Making a silent backup failure loud

A cron backup fails into a log nobody reads, and the first sign is needing
one. `backup.sh` records the outcome of every run — success or failure — to
`status.json`, and the `layer0.self.backup` check reads it and alarms when
backups go stale (warn at 36 h, fail at 72 h) or when the last run failed.

To enable it, point `SENTINEL_BACKUP_PATH` at the same absolute host path the
cron job writes to, so the backend can see it read-only:

```bash
SENTINEL_BACKUP_PATH=/var/backups/sentinel     # in ops/.env
```

Without it the check reports `skip` — "not configured" — which is
deliberately distinct from a failure, so an unconfigured optional feature
never trains anyone to ignore a red cell.

### Moving data from an existing deployment

To carry history over to a new install, export from the old host and import
into the new one:

```bash
# on the OLD host
bash ops/export-data.sh -o /tmp
rsync -av --partial /tmp/sentinel-export-<stamp>/ user@newhost:/tmp/sentinel-export-<stamp>/

# on the NEW host
bash ops/import-data.sh /tmp/sentinel-export-<stamp>
docker compose -f ops/docker-compose.deploy.yml --env-file ops/.env restart backend
```

The bundle carries a `manifest.json` with per-table row counts and a SHA-256
of the dump. `import-data.sh` verifies the checksum **before** touching the
database, then re-counts every table after restoring and compares. A transfer
that lost rows aborts rather than quietly becoming a smaller dataset that
looks fine.

It also refuses to overwrite a database that already holds data unless you
pass `--force` and type the database name. Restoring over populated tables is
not a merge — it collides on primary keys and leaves neither the old data nor
the new. Empty tables from a first boot are cleared automatically, since
nothing is lost.

**The image archive moves separately.** It is content-addressed, so rsync is
the right tool — transfers resume and files already present are skipped:

```bash
rsync -av --partial --info=progress2 /path/to/archive/ user@newhost:/path/to/archive/
```

It is also optional. Sentinel runs fine without it; you lose the ability to
view the images behind past verdicts, not any current monitoring.

---

## 10. Building from source

The GHCR pull path is what you want most of the time. Source builds
are for offline environments, branch testing, or "I want to read
the code while I deploy" days.

**Pre-reqs.** Docker + `docker compose`. **No** local Python or Node
— builds happen inside the Dockerfile build stages.

**Recipe.**

```bash
git clone https://github.com/jkmesches/AQPI-Sentinel.git
cd AQPI-Sentinel
cp ops/.env.prod.example ops/.env.prod    # fill in
docker compose -f ops/docker-compose.prod.yml \
    --env-file ops/.env.prod up -d --build
```

First build is 3-5 minutes (Playwright + Chromium are the bulk of
the backend image). Subsequent rebuilds use the layer cache —
backend rebuilds on Python-code-only changes take ~30s; frontend
rebuilds on Svelte changes take ~45s.

**Caveats vs GHCR pull:**

- No SBOM / image signing (CI builds attach those).
- You're responsible for keeping the source tree current (`git pull`
  before each redeploy).
- Builds run on the prod host's CPU — fine for occasional rebuilds,
  noisy if you redeploy frequently.

---

## 11. Monitoring Sentinel itself

The watcher needs a watcher. The risk profile is bounded — Sentinel
is stateless other than Postgres, and Postgres has its own health
signals — but if the backend wedges silently you'd never know.

**Lightweight self-monitoring options:**

- **External heartbeat.** Hit `/api/_debug/stats` from a tiny cron
  every 5 minutes. Alert if the response is missing or
  `event_loop_lag_ms > 1000`.
- **Postgres uptime.** Any external Postgres monitor pointed at the
  `sentinel-postgres` container.
- **A second Sentinel instance.** Overkill for most deploys, but
  point a small companion at this one's `/api/status` and you get
  the full alarm pipeline for free.

**What to alarm on:**

| Signal | Threshold | Why |
|---|---|---|
| Backend HTTP 5xx rate | > 1% over 5 min | App-layer failure |
| `event_loop_lag_ms` | > 100ms sustained | Backend overloaded |
| `scheduler.tasks_running != tasks_total` | sustained > 1 min | A check task died and didn't restart |
| `asyncpg.in_use == max_size` | sustained > 30s | Pool saturation; tune `max_size` or scale |
| `httpx.in_flight` | sustained > 10 | Upstream slow; not Sentinel's fault but worth knowing |
| Container restart count | > 1/hr | Crash loop |

Push these to whatever monitoring you already run (Prometheus, your
cloud's built-in metrics, etc.).

---

## 12. Where to go from here

- **[`03-administration.md`](#)** — admin UI walkthroughs: users,
  alerts, groups, thresholds, silences, push routing.
- **[`04-maintenance.md`](MAINTENANCE.md)** — day-2 ops: logs,
  backups, rollback, performance tuning, schema migrations.
- **[`05-troubleshooting.md`](#)** — symptom-driven debugging
  recipes for the most common failure modes.
- **[`06-porting-to-other-upstreams.md`](#)** — adapting Sentinel
  to monitor a non-radarca system.
- **[`13-extending-checks.md`](#)** — adding new monitoring checks.

Items linked as `(#)` are on the documentation roadmap and will fill
in as those docs land. See [Changelog](changelog.md) for the
current state.
