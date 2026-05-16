#!/usr/bin/env bash
# Start Sentinel: postgres + backend + frontend. Detached, PIDs written.
set -e
source "$(dirname "$0")/_common.sh"

ensure_postgres

# backend
if is_running "$BE_PID"; then
  echo -e "${c_dim}backend already running (pid $(cat "$BE_PID"))${c_off}"
else
  echo -e "${c_b}starting backend${c_off}"
  cd "$ROOT"
  ("$VENV_PY" -m backend.main > "$BE_LOG" 2>&1 & echo $! > "$BE_PID")
fi

# frontend
if is_running "$FE_PID"; then
  echo -e "${c_dim}frontend already running (pid $(cat "$FE_PID"))${c_off}"
else
  echo -e "${c_b}starting frontend${c_off}"
  cd "$ROOT/frontend"
  (npm run dev > "$FE_LOG" 2>&1 & echo $! > "$FE_PID")
fi

# wait for both to be reachable
echo -e "${c_dim}waiting for ports…${c_off}"
for _ in $(seq 1 30); do
  be_ok=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/status 2>/dev/null || echo "")
  fe_ok=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5173/ 2>/dev/null || echo "")
  [ "$be_ok" = "200" ] && [ "$fe_ok" = "200" ] && break
  sleep 0.6
done

echo
echo -e "${c_b}sentinel up${c_off}"
echo -e "  backend  : ${c_g}http://127.0.0.1:8000${c_off}  (log: $BE_LOG)"
echo -e "  frontend : ${c_g}http://127.0.0.1:5173${c_off}  (log: $FE_LOG)"
echo
echo -e "${c_dim}stop:   make stop${c_off}"
echo -e "${c_dim}logs:   make logs${c_off}"
echo -e "${c_dim}fe:     make restart-fe${c_off}"
echo -e "${c_dim}be:     make restart-be${c_off}"
