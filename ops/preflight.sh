#!/usr/bin/env bash
# Sentinel pre-deployment check.
#
#   bash ops/preflight.sh [path-to-env-file]     # default: ops/.env
#
# Validates everything that is cheap to check now and expensive to discover
# after `up -d`: a missing password surfaces as a container restart loop, a
# busy port as a proxy that silently never binds, a blank retention setting as
# a full disk three months later.
#
# Exit 0 = safe to deploy. Exit 1 = a blocker. Warnings never fail the run;
# they are judgement calls only the operator can make.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

ENV_FILE="${1:-ops/.env}"
COMPOSE="ops/docker-compose.deploy.yml"
FAIL=0; WARN=0

# Colour only when attached to a terminal, so piping to a file stays readable.
if [ -t 1 ]; then R=$'\033[31m'; Y=$'\033[33m'; G=$'\033[32m'; B=$'\033[1m'; N=$'\033[0m'
else R=""; Y=""; G=""; B=""; N=""; fi

ok()   { printf '  %sok%s    %s\n'   "$G" "$N" "$1"; }
warn() { printf '  %swarn%s  %s\n'   "$Y" "$N" "$1"; WARN=$((WARN+1)); }
bad()  { printf '  %sFAIL%s  %s\n'   "$R" "$N" "$1"; FAIL=$((FAIL+1)); }
hdr()  { printf '\n%s%s%s\n' "$B" "$1" "$N"; }

hdr "Host prerequisites"
if command -v docker >/dev/null 2>&1; then
  ok "docker present ($(docker --version 2>/dev/null | cut -d, -f1))"
  if docker info >/dev/null 2>&1; then ok "docker daemon reachable"
  else bad "docker daemon not reachable — is it running, and are you in the docker group?"; fi
else bad "docker not installed — see https://docs.docker.com/engine/install/"; fi

if docker compose version >/dev/null 2>&1; then
  ok "docker compose v2 present ($(docker compose version --short 2>/dev/null))"
else
  bad "docker compose v2 missing. The legacy 'docker-compose' script will NOT work: this file uses YAML merge keys and depends_on conditions."
fi

hdr "Configuration file"
if [ -f "$ENV_FILE" ]; then
  ok "$ENV_FILE exists"
  perms="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%A' "$ENV_FILE" 2>/dev/null)"
  case "$perms" in
    600|400) ok "permissions $perms (secrets not world-readable)" ;;
    *)       warn "permissions $perms — it holds passwords. chmod 600 $ENV_FILE" ;;
  esac
  # Read values without executing the file: a stray backtick in a password
  # must not run as a command.
  get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- ; }
else
  bad "$ENV_FILE not found — cp ops/.env.deploy.example $ENV_FILE"
  get() { echo ""; }
fi

hdr "Required settings"
PW="$(get POSTGRES_PASSWORD)"
if [ -z "$PW" ]; then bad "POSTGRES_PASSWORD is empty — the stack will not start. Generate one: openssl rand -hex 32"
elif [ "${#PW}" -lt 16 ]; then warn "POSTGRES_PASSWORD is only ${#PW} characters. It is never typed by a human; make it long."
else ok "POSTGRES_PASSWORD set (${#PW} chars)"; fi

AE="$(get SENTINEL_ADMIN_EMAIL)"; AP="$(get SENTINEL_ADMIN_PASSWORD)"
if [ -z "$AE" ] || [ -z "$AP" ]; then
  warn "SENTINEL_ADMIN_EMAIL/PASSWORD blank — no admin account is created and you cannot sign in to acknowledge alarms or reach /admin."
else
  ok "bootstrap admin configured ($AE)"
  [ "${#AP}" -lt 12 ] && warn "admin password is ${#AP} characters; this one IS typed by a human, so make it long rather than clever."
fi

hdr "Reachability"
SITE="$(get SENTINEL_SITE_ADDRESS)"; SITE="${SITE:-http://localhost}"
TLSD="$(get SENTINEL_TLS_DIRECTIVE)"
ok "site address: $SITE"
# A port here binds that port INSIDE the container while compose maps host
# ports to 80/443, so the site becomes unreachable with nothing in any log.
# Caught here because it is invisible at runtime.
case "$SITE" in
  *:80|*:443) : ;;                                  # the container ports; harmless
  http://*:[0-9]*|https://*:[0-9]*|*:[0-9]*)
    bad "SENTINEL_SITE_ADDRESS contains a port ($SITE). Caddy would bind it inside the container while compose maps to 80/443, leaving the site silently unreachable. Drop the port and set HTTP_PORT/HTTPS_PORT instead." ;;
esac
case "$SITE" in
  http://localhost*|http://127.0.0.1*)
    warn "site address is localhost — reachable only from this machine. Set a hostname or IP others can use." ;;
  https://*)
    if [ -z "$TLSD" ]; then
      warn "https:// with no SENTINEL_TLS_DIRECTIVE means Caddy will try Let's Encrypt. That needs public DNS pointing here and ports 80+443 open to the internet. For an internal host use: SENTINEL_TLS_DIRECTIVE=tls internal"
    else ok "TLS: $TLSD"; fi ;;
esac

for spec in "HTTP_PORT 80" "HTTPS_PORT 443"; do
  set -- $spec; var=$1; def=$2
  p="$(get "$var")"; p="${p:-$def}"
  if command -v ss >/dev/null 2>&1;      then busy=$(ss -ltn "sport = :$p" 2>/dev/null | tail -n +2)
  elif command -v netstat >/dev/null 2>&1; then busy=$(netstat -ltn 2>/dev/null | grep -E "[:.]$p ")
  else busy=""; fi
  if [ -n "$busy" ]; then bad "port $p is already in use — the proxy cannot bind. Free it, or set $var to something else."
  else ok "port $p free"; fi
done

UP="$(get SENTINEL_BASE)"; UP="${UP:-https://radarca.engr.colostate.edu}"
if command -v curl >/dev/null 2>&1; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 40 "$UP/api/radar-status/" 2>/dev/null)
  case "$code" in
    200) ok "upstream reachable ($UP)" ;;
    000) warn "could not reach $UP within 40s. Sentinel will start and report the outage, but check egress/DNS if that is unexpected." ;;
    *)   warn "upstream returned HTTP $code from $UP" ;;
  esac
else warn "curl not present; skipped the upstream reachability check"; fi

hdr "Retention and disk"
DBR="$(get SENTINEL_DB_RETENTION_DAYS)"
if [ -z "$DBR" ]; then ok "DB retention unset — compose default of 60 days applies"
elif [ "$DBR" = "0" ]; then bad "SENTINEL_DB_RETENTION_DAYS=0 disables retention entirely. Postgres will grow without bound."
else ok "DB retention: $DBR days"; fi

AR="$(get SENTINEL_ARCHIVE_RETENTION_DAYS)"
if [ -z "$AR" ]; then
  warn "SENTINEL_ARCHIVE_RETENTION_DAYS is blank = keep images forever (~7 GB/month). Fine on a large disk; set a number otherwise."
else ok "archive retention: $AR days"; fi

avail_kb=$(df -Pk /var/lib/docker 2>/dev/null | awk 'NR==2{print $4}')
[ -z "$avail_kb" ] && avail_kb=$(df -Pk / 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$avail_kb" ]; then
  gb=$((avail_kb/1024/1024))
  if   [ "$gb" -lt 20 ]; then bad "only ${gb} GB free where Docker stores volumes. Sentinel needs room for Postgres plus a growing image archive."
  elif [ "$gb" -lt 60 ]; then warn "${gb} GB free. Workable, but point SENTINEL_ARCHIVE_PATH at a larger disk or set an archive retention."
  else ok "${gb} GB free for Docker volumes"; fi
fi

hdr "Container images"
OWNER="$(get SENTINEL_IMAGE_OWNER)"; OWNER="${OWNER:-jkmesches}"
TAG="$(get SENTINEL_TAG)"; TAG="${TAG:-latest}"
[ "$TAG" = "latest" ] && warn "SENTINEL_TAG=latest tracks main and can change under you. Pin a release tag for a stable deployment."
if docker info >/dev/null 2>&1; then
  for img in backend frontend; do
    ref="ghcr.io/$OWNER/sentinel-$img:$TAG"
    if docker manifest inspect "$ref" >/dev/null 2>&1; then
      ok "can pull $ref"
    else
      bad "cannot pull $ref. Check the tag exists (note: the image tag has NO leading v — the git tag v0.4.0 publishes 0.4.0). If the packages are private on this instance, authenticate first: echo \$GHCR_PAT | docker login ghcr.io -u <your-github-username> --password-stdin   (read:packages scope is enough). You can always build from source instead with ops/docker-compose.prod.yml."
    fi
  done
fi

hdr "Compose and proxy config"
if [ -f "$COMPOSE" ]; then
  if out=$(POSTGRES_PASSWORD="${PW:-placeholder}" docker compose -f "$COMPOSE" --env-file "$ENV_FILE" config 2>&1 >/dev/null); then
    ok "compose file is valid"
  else
    bad "compose file did not validate:"; printf '        %s\n' "$(echo "$out" | head -3)"
  fi
else bad "$COMPOSE not found"; fi

if [ -f ops/Caddyfile ] && docker info >/dev/null 2>&1; then
  if docker run --rm -v "$PWD/ops/Caddyfile:/etc/caddy/Caddyfile:ro" \
       -e SENTINEL_SITE_ADDRESS="$SITE" -e SENTINEL_TLS_DIRECTIVE="$TLSD" \
       caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    ok "Caddyfile valid for your site address"
  else
    bad "Caddyfile did not validate with SENTINEL_SITE_ADDRESS=$SITE / SENTINEL_TLS_DIRECTIVE=$TLSD"
  fi
fi

hdr "Result"
if [ "$FAIL" -gt 0 ]; then
  printf '  %s%d blocker(s)%s, %d warning(s). Fix the blockers before deploying.\n' "$R" "$FAIL" "$N" "$WARN"
  exit 1
fi
printf '  %sReady to deploy%s — %d warning(s).\n\n' "$G" "$N" "$WARN"
printf '    docker compose -f %s --env-file %s up -d\n\n' "$COMPOSE" "$ENV_FILE"
exit 0
