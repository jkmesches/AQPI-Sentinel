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

# Host names and paths that exist only inside the CSU and JPL networks.
# radarca.engr.colostate.edu is deliberately absent: it is a public website
# and the whole project is about monitoring it.
PATTERNS=(
  '\bthe C-band producer host\b'
  '\bthe CIRA deployment\b'
  '\bthe CIRA deployment\b'
  '\bthe CIRA deployment\b'
  '\bthe QPE tooling host\b'
  'the main archive/projects'
  '\bthe access helper\b'
  '\bthe access helper\b'
  '\bthe access helper\b'
  '\bthe test deployment\b'
  '10\.25\.[0-9]'
  'CHILL/wiki'
)

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
