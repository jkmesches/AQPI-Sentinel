#!/usr/bin/env bash
set -e
source "$(dirname "$0")/_common.sh"

kill_pid "$FE_PID" "frontend"
kill_pid "$BE_PID" "backend"

# also clean up any vite child shells (npm run dev forks a node child)
pkill -f "vite dev" 2>/dev/null || true
pkill -f "backend.main" 2>/dev/null || true

echo -e "${c_g}sentinel stopped${c_off} (postgres container kept up — 'make pg-stop' to stop it)"
