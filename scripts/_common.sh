# sourced by the other scripts — shared constants + helpers.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Prefer a venv inside SentinelProject; fall back to one directory up (dev
# layouts where the venv was created alongside the project).
if [ -x "$ROOT/.venv/bin/python" ]; then
  VENV_PY="$ROOT/.venv/bin/python"
else
  VENV_PY="$ROOT/../.venv/bin/python"
fi
BE_LOG=/tmp/sentinel-be.log
FE_LOG=/tmp/sentinel-fe.log
BE_PID=/tmp/sentinel-be.pid
FE_PID=/tmp/sentinel-fe.pid
PG_COMPOSE="$ROOT/ops/docker-compose.dev.yml"

c_dim='\033[2m'; c_b='\033[1m'; c_g='\033[32m'; c_y='\033[33m'; c_r='\033[31m'; c_off='\033[0m'

is_running() {
  local pidfile="$1"
  [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null
}

kill_pid() {
  local pidfile="$1" label="$2"
  if is_running "$pidfile"; then
    local pid; pid=$(cat "$pidfile")
    echo -e "${c_dim}stopping $label (pid $pid)${c_off}"
    kill -INT "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.4
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo -e "${c_y}  forcing $label down${c_off}"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile"
}

ensure_postgres() {
  if ! docker ps --format '{{.Names}}' | grep -q '^sentinel-postgres$'; then
    echo -e "${c_dim}starting postgres…${c_off}"
    docker compose -f "$PG_COMPOSE" up -d >/dev/null
  fi
}
