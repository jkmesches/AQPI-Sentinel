#!/usr/bin/env bash
# Tail both logs side-by-side, prefixed.
set -e
source "$(dirname "$0")/_common.sh"

trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM
( tail -F "$BE_LOG" 2>/dev/null | sed -u $'s/^/\033[36mbe\033[0m  /' ) &
( tail -F "$FE_LOG" 2>/dev/null | sed -u $'s/^/\033[35mfe\033[0m  /' ) &
wait
