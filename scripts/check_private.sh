#!/usr/bin/env bash
# Refuse to commit internal infrastructure vocabulary to this public repo.
#
# AQPI-Sentinel is public and publishes docs/ to GitHub Pages, so every
# committed byte is world-readable. Internal host names, backend filesystem
# paths and the private wiki's own location belong in the CHILL wiki repo
# instead. Convention alone put `the secondary archive` in 45 files over six months
# before anyone looked.
#
# Install:  ln -sf ../../scripts/check_private.sh .git/hooks/pre-commit
# Audit the whole tree:  scripts/check_private.sh --all
#
# The default mode reads only the lines a commit ADDS, so the 154 names
# already in the tree as of 2026-11-22 do not block unrelated work. Those
# are a separate decision, recorded in the CHILL wiki. Run --all to see them.
#
# A legitimate hit (the deployment docs must name the hosts an operator
# types) is marked on its own line with a trailing `# public-ok` comment,
# or for markdown `<!-- public-ok -->`.
set -euo pipefail

# Shapes rather than names. An earlier version listed every internal host
# here, which made this file the one place in a public repo where the whole
# vocabulary could be read off in order. These match the form such a name
# takes without spelling any of them out.
#
# radarca.engr.colostate.edu is deliberately absent, since it is a public
# website and monitoring it is the point of the project.
PATTERNS=(
  '\bcira-[a-z0-9]+\b'                       # the deployment hosts
  '10\.(25|0|1)\.[0-9]+\.[0-9]+'             # RFC1918 addresses
  '\b[a-z0-9-]+\.local\.[a-z0-9-]+\.(com|net|org)\b'   # internal domains
  '\bcsu-(run|ssh|git|[a-z]+)\b'             # access helpers
  'CHILL/(wiki|backend|records|Agents)'      # paths in the private tree
)

# Names that have no general shape (a machine called after a mountain cannot
# be matched by a pattern) live outside this repo, one extended regex per
# line. Without the file the checks above still run, so a fresh clone is not
# unprotected, it is only less specific.
PRIVATE_PATTERNS="${SENTINEL_PRIVATE_PATTERNS:-$(dirname "$0")/private-patterns.txt}"
if [[ -r "$PRIVATE_PATTERNS" ]]; then
  while IFS= read -r _line; do
    [[ -z "$_line" || "$_line" == \#* ]] && continue
    PATTERNS+=("$_line")
  done < "$PRIVATE_PATTERNS"
fi

# Binary and vendored trees carry no prose and would only produce noise.
SKIP_RE='^(frontend/node_modules|\.venv|frontend/static|.*\.(png|jpg|svg|pdf|ico|woff2?))'

scan_whole_tree() {
  local file pattern line
  while IFS= read -r file; do
    [[ -z "$file" || ! -f "$file" ]] && continue
    [[ "$file" =~ $SKIP_RE ]] && continue
    [[ "$file" == "scripts/check_private.sh" ]] && continue
    for pattern in "${PATTERNS[@]}"; do
      while IFS= read -r line; do
        [[ -z "$line" || "$line" == *public-ok* ]] && continue
        echo "$file:$line"
      done < <(grep -nE "$pattern" "$file" 2>/dev/null || true)
    done
  done < <(git ls-files)
}

# Only the lines a commit adds, so existing text does not block new work.
# `git diff -U0` prefixes an added line with a single `+`, and names the
# file in a `+++ b/path` header.
scan_added_lines() {
  local file="" pattern line
  while IFS= read -r line; do
    case "$line" in
      '+++ b/'*) file="${line#+++ b/}" ;;
      '+'*)
        [[ -z "$file" || "$file" =~ $SKIP_RE ]] && continue
        [[ "$file" == "scripts/check_private.sh" ]] && continue
        [[ "$line" == *public-ok* ]] && continue
        for pattern in "${PATTERNS[@]}"; do
          if grep -qE "$pattern" <<<"${line#+}"; then
            echo "$file: ${line#+}"
            break
          fi
        done
        ;;
    esac
  done < <(git diff --cached -U0 --diff-filter=ACM)
}

main() {
  local -a hits=()
  if [[ "${1:-}" == "--all" ]]; then
    mapfile -t hits < <(scan_whole_tree)
  else
    mapfile -t hits < <(scan_added_lines)
  fi

  if (( ${#hits[@]} == 0 )); then
    echo "check_private: clean"
    return 0
  fi

  echo "check_private: internal vocabulary in a PUBLIC repo" >&2
  printf '  %s\n' "${hits[@]}" >&2
  cat >&2 <<'EOF'

This repo is public and docs/ publishes to GitHub Pages. Move the text to
the CHILL wiki, or if an operator genuinely has to read this host name here,
mark that line `# public-ok` (or `<!-- public-ok -->` in markdown).
EOF
  return 1
}

main "${1:-}"
