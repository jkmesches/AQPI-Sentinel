#!/usr/bin/env bash
# Restart just the backend.
set -e
source "$(dirname "$0")/_common.sh"

ensure_postgres
kill_pid "$BE_PID" "backend"
pkill -f "backend.main" 2>/dev/null || true
sleep 0.5

echo -e "${c_b}starting backend${c_off}"
cd "$ROOT"
("$VENV_PY" -m backend.main > "$BE_LOG" 2>&1 & echo $! > "$BE_PID")

for _ in $(seq 1 25); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/status 2>/dev/null || echo "")
  [ "$code" = "200" ] && break
  sleep 0.4
done

echo -e "${c_g}backend back up${c_off} at http://127.0.0.1:8000"
