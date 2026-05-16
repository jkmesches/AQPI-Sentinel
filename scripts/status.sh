#!/usr/bin/env bash
set -e
source "$(dirname "$0")/_common.sh"

dot() { is_running "$1" && echo -e "${c_g}●${c_off}" || echo -e "${c_r}○${c_off}"; }

echo -e "$(dot "$BE_PID") backend   $( [ -f "$BE_PID" ] && echo "pid $(cat "$BE_PID")" )   log: $BE_LOG"
echo -e "$(dot "$FE_PID") frontend  $( [ -f "$FE_PID" ] && echo "pid $(cat "$FE_PID")" )   log: $FE_LOG"

if docker ps --format '{{.Names}}' | grep -q '^sentinel-postgres$'; then
  echo -e "${c_g}●${c_off} postgres  $(docker inspect --format '{{.State.Health.Status}}' sentinel-postgres 2>/dev/null)"
else
  echo -e "${c_r}○${c_off} postgres  (not running)"
fi

echo
echo -e "${c_dim}endpoints:${c_off}"
echo "  api    http://127.0.0.1:8000/api/status"
echo "  ui     http://127.0.0.1:5173/"
