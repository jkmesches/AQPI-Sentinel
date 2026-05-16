#!/usr/bin/env bash
# Kill the vite dev server, blow away its on-disk dep cache + .svelte-kit,
# restart fresh. This is what fixes "Loading failed for the module …" errors
# after big edits.
set -e
source "$(dirname "$0")/_common.sh"

kill_pid "$FE_PID" "frontend"
pkill -f "vite dev" 2>/dev/null || true
sleep 0.5

echo -e "${c_dim}clearing .svelte-kit/ and node_modules/.vite/${c_off}"
rm -rf "$ROOT/frontend/.svelte-kit" "$ROOT/frontend/node_modules/.vite"

echo -e "${c_b}starting frontend${c_off}"
cd "$ROOT/frontend"
(npm run dev > "$FE_LOG" 2>&1 & echo $! > "$FE_PID")

for _ in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5173/ 2>/dev/null || echo "")
  [ "$code" = "200" ] && break
  sleep 0.4
done

echo -e "${c_g}frontend back up${c_off} at http://127.0.0.1:5173  — hard-refresh your tab"
