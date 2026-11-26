#!/usr/bin/env bash
# Upgrade the AQPI and XQPI Sentinel stacks on this host together.
#
#   sudo /srv/upgrade.sh                # pull, back up, sync, recreate, wait, prune
#   sudo /srv/upgrade.sh --check        # every preflight test; changes no stack or file
#   sudo /srv/upgrade.sh --ref v0.16.6  # sync to a tag and run that image version
#   sudo /srv/upgrade.sh --no-backup    # skip the dumps taken before the restart
#   sudo /srv/upgrade.sh --no-prune     # keep unused images afterwards
#
# The run syncs the checkout this file comes from, so install a root-owned copy
# outside it and run that:
#   sudo install -o root -g root -m 0750 /srv/aqpi-sentinel/ops/upgrade.sh /srv/upgrade.sh
#
# Both stacks read their compose files from the AQPI checkout. AQPI runs
# ops/docker-compose.ghcr.yml, and XQPI runs the same file with
# ops/docker-compose.xqpi.yml on top and its own env file.
#
# The order keeps both stacks up until their containers restart:
#   1. preflight: root, tools, Compose 2.24.4+, no backup running, a clean
#      checkout that can fast-forward, and both stacks render from the target
#      revision with no shared project, container, volume, network, host port
#      or source tag, each database volume present, each bind-mount source
#      present and filled (store markers included), and room for the images;
#   2. pull every image for both stacks, from the target revision's files;
#   3. dump both databases with root's crontab backup lines, run as cron would;
#   4. sync the checkout to the target;
#   5. recreate both stacks at once (up -d --force-recreate --remove-orphans);
#   6. wait until every container runs without restarting, each healthcheck
#      passes, each backend and frontend answers HTTP, and each backend's
#      layer0.self.disk check has run since the restart without failing;
#   7. docker system prune -af, only when step 6 passed for both stacks.
# Steps 1 to 4 leave the running containers alone, and a failure there puts
# the checkout back where it was. A failure from step 5 on prints the commands
# that go back to the previous versions, and the run never prunes after a
# failure, so the previous images stay on disk. --check fetches from origin,
# and changes no stack, checkout file or image.
#
# Every run is appended to /var/log/sentinel-upgrade.log, and a dropped SSH
# session does not stop a run (it ignores SIGHUP). The UPGRADE_* variables
# below exist for the test suite; sudo clears them, so `sudo /srv/upgrade.sh`
# always runs with the defaults.
set -Eeuo pipefail

AQPI_DIR="${UPGRADE_AQPI_DIR:-/srv/aqpi-sentinel}"
XQPI_ENV="${UPGRADE_XQPI_ENV:-/srv/xqpi-sentinel/.env.prod}"
LOG="${UPGRADE_LOG:-/var/log/sentinel-upgrade.log}"
LOCK="${UPGRADE_LOCK:-/run/sentinel-upgrade.lock}"
WORK_ROOT="${UPGRADE_WORK_ROOT:-/run}"
BACKUP_LOCKS="${UPGRADE_BACKUP_LOCKS:-/var/backups/sentinel/.backup.lock \
/var/backups/xqpi-sentinel/.backup.lock}"
HEALTH_TIMEOUT_S="${UPGRADE_HEALTH_TIMEOUT_S:-300}"
POLL_S="${UPGRADE_POLL_S:-5}"
readonly MIN_COMPOSE=2.24.4 PULL_TIMEOUT_S=1800 UP_TIMEOUT_S=600 BACKUP_TIMEOUT_S=1800
readonly RENDER_TIMEOUT_S=120
readonly STACKS=(aqpi xqpi)
readonly PROJECT_LABEL='{{index .Config.Labels "com.docker.compose.project"}}'
readonly FILES_LABEL='{{index .Config.Labels "com.docker.compose.project.config_files"}}'
readonly REVISION_LABEL='{{index .Config.Labels "org.opencontainers.image.revision"}}'

CHECK_ONLY=0 BACKUP=1 PRUNE=1 REF=""
STEP="preflight" OLD_HEAD="" TARGET="" SYNCED=0 RECREATED=0 HEALTHY=0
WORK="" TEE_PID="" FILES_DIR="" OWNER=""
GIT=()
CMD=()
IMAGE_REFS=()
BACKUP_CMDS=()
BACKUP_ENVN=()
CRON_ENV=()
declare -A OLD_VERSION=() OLD_IMAGES=() OLD_IDS=() OLD_FILES=() OLD_PROJECT=()

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# die MESSAGE...: stop the run. Only the top-level shell explains the state; a
# die inside a command substitution leaves that to the ERR trap above it.
die() {
    log "FAILED in step '$STEP': $*"
    if ((BASH_SUBSHELL == 0)); then explain_state; fi
    exit 1
}

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit "$1"
}

parse_args() {
    while (($#)); do
        case $1 in
            --check) CHECK_ONLY=1 ;;
            --no-backup) BACKUP=0 ;;
            --no-prune) PRUNE=0 ;;
            --ref) (($# >= 2)) || usage 2; REF=$2; shift ;;
            -h|--help) usage 0 ;;
            *) printf 'upgrade.sh: unknown option %s\n' "$1" >&2; usage 2 ;;
        esac
        shift
    done
    if [[ -n $REF && ! $REF =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
        printf 'upgrade.sh: --ref takes a tag, branch or commit, not "%s"\n' "$REF" >&2
        exit 2
    fi
}

# stack_cmd STACK: put the docker compose command for STACK in CMD. FILES_DIR is
# the preflight's export of the target revision until the sync, then the checkout.
# The project directory is the checkout's ops/ in both cases, so a relative
# path in an env file resolves the same in the preflight as in the real run.
stack_cmd() {
    local base="$FILES_DIR/ops/docker-compose.ghcr.yml"
    CMD=(docker compose --project-directory "$AQPI_DIR/ops" -f "$base")
    case $1 in
        aqpi) CMD+=(--env-file "$(env_file aqpi)") ;;
        xqpi) CMD+=(-f "$FILES_DIR/ops/docker-compose.xqpi.yml" --env-file "$(env_file xqpi)") ;;
        *) die "unknown stack $1" ;;
    esac
}

env_file() {
    if [[ $1 == aqpi ]]; then echo "$AQPI_DIR/ops/.env.prod"; else echo "$XQPI_ENV"; fi
}

compose() {
    stack_cmd "$1"
    shift
    "${CMD[@]}" "$@"
}

explain_state() {
    if ((HEALTHY)); then
        log "Both stacks are up and healthy on the new version; only the cleanup failed."
    elif ((RECREATED)); then
        explain_rollback
    elif ((SYNCED)) && [[ -n $OLD_HEAD ]]; then
        if "${GIT[@]}" checkout -q -B main "$OLD_HEAD"; then
            log "Put the checkout back at ${OLD_HEAD:0:7}. The running containers were not touched."
        else
            log "Could not put the checkout back at ${OLD_HEAD:0:7}; check it by hand."
            log "The running containers were not touched."
        fi
    else
        log "The checkout and the running containers were not touched."
    fi
}

# rollback_files STACK: the compose files STACK's backend was started from, by
# its labels, or this script's files when the labels are missing or a file is
# gone. Paths hold no spaces.
rollback_files() {
    local f files=${OLD_FILES[$1]:-}
    for f in $files; do [[ -e $f ]] || files=""; done
    if [[ -z $files || -z ${OLD_PROJECT[$1]:-} ]]; then
        files="$AQPI_DIR/ops/docker-compose.ghcr.yml"
        [[ $1 == aqpi ]] || files+=" $AQPI_DIR/ops/docker-compose.xqpi.yml"
    fi
    echo "$files"
}

# rollback_cmd STACK: the docker compose command that started STACK last.
rollback_cmd() {
    local f cmd="docker compose"
    [[ -z ${OLD_PROJECT[$1]:-} ]] || cmd+=" -p ${OLD_PROJECT[$1]}"
    for f in $(rollback_files "$1"); do cmd+=" -f $f"; done
    echo "$cmd --env-file $(env_file "$1")"
}

# old_head_has_files: whether every checkout file the rollback commands use
# exists at the old revision, so the checkout can go back first.
old_head_has_files() {
    local s f
    for s in "${STACKS[@]}"; do
        for f in $(rollback_files "$s"); do
            [[ $f == "$AQPI_DIR"/* ]] || continue
            "${GIT[@]}" cat-file -e "$OLD_HEAD:${f#"$AQPI_DIR"/}" 2>/dev/null || return 1
        done
    done
}

explain_rollback() {
    log "The run stopped after it began recreating the stacks. Nothing was pruned, so the"
    log "previous images are still on disk. Read the logs first, then run again or go back:"
    log "  sudo docker logs --since 10m sentinel-backend (or xqpi-sentinel-backend)"
    if ((SYNCED)); then
        if old_head_has_files; then
            log "  sudo -u $OWNER git -C $AQPI_DIR checkout -B main ${OLD_HEAD:0:7}"
        else
            log "  (leave the checkout at ${TARGET:0:7}: ${OLD_HEAD:0:7} lacks a file below)"
        fi
    fi
    local s
    for s in "${STACKS[@]}"; do
        if [[ -n ${OLD_VERSION[$s]:-} ]]; then
            log "  sudo SENTINEL_TAG=${OLD_VERSION[$s]} $(rollback_cmd "$s") up -d --force-recreate"
        else
            log "  ($s's previous version is unknown, since its API did not answer before)"
        fi
        [[ -z ${OLD_IMAGES[$s]:-} ]] || log "  $s previously ran image IDs ${OLD_IMAGES[$s]}"
    done
    log "If this release changed the database schema, the previous image may refuse the"
    log "migrated database, and the dumps this run took before the restart are the way back."
}

on_error() {
    # set -E runs this in command substitutions too, and one report is enough.
    ((BASH_SUBSHELL == 0)) || return 0
    log "FAILED in step '$STEP' at line $1: $2"
    explain_state
    exit 1
}

on_signal() {
    trap - ERR INT TERM
    log "INTERRUPTED in step '$STEP'"
    if [[ $STEP == pull || $STEP == recreate ]]; then
        log "A docker compose $STEP that already started keeps running to its end;"
        log "sudo docker ps shows where each stack stands."
    fi
    explain_state
    exit 130
}

cleanup() {
    [[ -z $WORK ]] || rm -rf -- "$WORK"
    if [[ -n $TEE_PID ]]; then
        exec 1>&- 2>&-
        wait "$TEE_PID" 2>/dev/null || true
    fi
}

need_tools() {
    local t missing=() have
    for t in docker git flock curl timeout python3 sudo stat crontab tar df getent env; do
        command -v "$t" >/dev/null 2>&1 || missing+=("$t")
    done
    ((${#missing[@]} == 0)) || die "missing tools: ${missing[*]}"
    timeout 30 docker info >/dev/null 2>&1 || die "the Docker daemon does not answer (docker info)"
    have=$(timeout 30 docker compose version --short 2>/dev/null) \
        || die "the docker compose plugin is not installed"
    have=${have#v}
    if [[ $(printf '%s\n%s\n' "$MIN_COMPOSE" "$have" | sort -V | head -n 1) != "$MIN_COMPOSE" ]]
    then
        die "docker compose $have is older than $MIN_COMPOSE, which the XQPI override needs"
    fi
}

need_files() {
    [[ -d $AQPI_DIR/.git ]] || die "$AQPI_DIR is not a git checkout"
    local f
    for f in "$(env_file aqpi)" "$(env_file xqpi)"; do
        [[ -r $f ]] || die "missing or unreadable env file: $f"
    done
    OWNER=$(stat -c %U "$AQPI_DIR")
    # As the checkout's owner, since git refuses a repository another user owns.
    GIT=(sudo -u "$OWNER" -H git -C "$AQPI_DIR")
    local mode owner
    read -r owner mode < <(stat -c '%u %a' "$0")
    if ((owner != 0)) || (((8#$mode & 8#022) != 0)); then
        log "WARNING: $0 should be owned by root and writable only by root (it is $owner, $mode)"
    fi
}

no_backup_running() {
    local f
    for f in $BACKUP_LOCKS; do
        [[ -e $f ]] || continue
        # Opened for append, so the test never truncates the file backup.sh locks.
        (flock -n 9) 9>>"$f" || die "a backup is running (it holds $f); run again when it ends"
    done
}

# target_revision: fetch, and check the checkout can fast-forward to the target.
target_revision() {
    local target=${REF:-origin/main} dirty
    timeout 120 "${GIT[@]}" fetch -q --tags --force origin || die "git fetch failed"
    [[ $("${GIT[@]}" rev-parse --abbrev-ref HEAD) == main ]] || die "$AQPI_DIR is not on main"
    dirty=$("${GIT[@]}" status --short --untracked-files=no)
    [[ -z $dirty ]] || die "$AQPI_DIR has local changes to tracked files: ${dirty//$'\n'/; }"
    "${GIT[@]}" rev-parse -q --verify "$target^{commit}" >/dev/null \
        || die "no such revision: $target"
    OLD_HEAD=$("${GIT[@]}" rev-parse HEAD)
    TARGET=$("${GIT[@]}" rev-parse "$target^{commit}")
    "${GIT[@]}" merge-base --is-ancestor HEAD "$TARGET" \
        || die "$AQPI_DIR at ${OLD_HEAD:0:7} cannot fast-forward to $target"
    log "checkout ${OLD_HEAD:0:7} -> ${TARGET:0:7} ($target)," \
        "$("${GIT[@]}" rev-list --count "HEAD..$TARGET") new commits"
    "${GIT[@]}" log -n 20 --oneline --no-decorate "HEAD..$TARGET" | sed 's/^/    /'
}

# render_target: render both stacks from the target revision's compose files,
# so a bad file, a collision or a missing mount stops the run before anything
# changes. The renders hold the env files' secrets, so they reach python on
# pipes only, and python stats the mount sources under a timeout, since a stale
# NFS mount hangs a stat.
render_target() {
    WORK=$(mktemp -d "$WORK_ROOT/sentinel-upgrade.XXXXXX")
    if ! "${GIT[@]}" archive "$TARGET" ops/docker-compose.ghcr.yml ops/docker-compose.xqpi.yml \
            | tar -x -C "$WORK"; then
        die "the target revision lacks ops/docker-compose.xqpi.yml, which arrived in v0.16.6"
    fi
    FILES_DIR=$WORK
    local s err rc=0 found
    for s in "${STACKS[@]}"; do
        err=$(compose "$s" config --quiet 2>&1) || die "the $s stack does not render: $err"
    done
    # Problems go to stderr; stdout lists what check_existing looks up.
    found=$(timeout "$RENDER_TIMEOUT_S" python3 - <(compose aqpi config --format json) \
            <(compose xqpi config --format json) <<'PY'
import json, os, sys

def backend(d):
    return (d.get("services") or {}).get("backend") or {}

def store_targets(env):
    return {env.get("SENTINEL_ARCHIVE_ROOT") or "/data/archive",
            env.get("SENTINEL_COLD_ROOT") or "/data/cold"}

def rendered(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    svcs = d.get("services") or {}
    names = {
        "project": {d.get("name")},
        "container": {s.get("container_name") for s in svcs.values()},
        "volume": {v.get("name") for v in (d.get("volumes") or {}).values()},
        "network": {n.get("name") for n in (d.get("networks") or {}).values()},
        "host port": {str(p["published"]) for s in svcs.values() for p in s.get("ports") or []
                      if p.get("published")},
    }
    return d, names

def mount_problems(stack, d):
    env = backend(d).get("environment") or {}
    marker = env.get("SENTINEL_STORAGE_MARKER") or None
    stores = store_targets(env)
    for name, s in (d.get("services") or {}).items():
        for m in s.get("volumes") or []:
            if m.get("type") != "bind":
                continue
            src, store = m.get("source", ""), name == "backend" and m.get("target") in stores
            if not os.path.exists(src):
                yield f"{stack} {name}: bind source {src} does not exist"
            elif store and marker:
                if not os.path.exists(os.path.join(src, marker)):
                    yield f"{stack} {name}: no {marker} in {src}, so the store is not attached"
            elif os.path.isdir(src):
                with os.scandir(src) as it:
                    if next(it, None) is None:
                        hint = " (a store may start empty only with a marker)" if store else ""
                        yield f"{stack} {name}: bind source {src} is empty{hint}"

def found_lines(stack, d, names):
    project = next(iter(names["project"]))
    for c in sorted(n for n in names["container"] if n):
        print("container", stack, project, c)
    tops = d.get("volumes") or {}
    pg = (d.get("services") or {}).get("postgres") or {}
    db = [tops.get(m.get("source"), {}).get("name") for m in pg.get("volumes") or []
          if m.get("type") == "volume" and m.get("target") == "/var/lib/postgresql/data"]
    print("dbvolume", stack, project, (db or ["-"])[0] or "-")
    env = backend(d).get("environment") or {}
    for m in backend(d).get("volumes") or []:
        if m.get("type") == "volume" and m.get("target") in store_targets(env):
            print("storevolume", stack, project, tops.get(m.get("source"), {}).get("name") or "-")
    for name, s in sorted((d.get("services") or {}).items()):
        if s.get("image"):
            print("image", stack, name, s["image"])
    if stack == "xqpi" and env.get("SENTINEL_SSCB_SOURCE_TAG"):
        print("warning", stack, "-", "XQPI's env sets SENTINEL_SSCB_SOURCE_TAG, which reaches"
              " its backend; the SSCB pair belongs to AQPI")

try:
    stacks = {s: rendered(p) for s, p in zip(("aqpi", "xqpi"), sys.argv[1:3])}
except (OSError, ValueError) as e:
    print(f"    cannot read a rendered stack: {e}", file=sys.stderr)
    sys.exit(2)
a, x = stacks["aqpi"][1], stacks["xqpi"][1]
shared = {k: sorted(map(str, (a[k] & x[k]) - {None})) for k in a}
for k, v in shared.items():
    if v:
        print(f"    both stacks claim {k} {', '.join(v)}", file=sys.stderr)
problems = [p for s, (d, _) in stacks.items() for p in mount_problems(s, d)]
for p in problems:
    print(f"    {p}", file=sys.stderr)
tags = {s: (backend(d).get("environment") or {}).get("SENTINEL_SOURCE_TAG") or None
        for s, (d, _) in stacks.items()}
same_tag = tags["aqpi"] is not None and tags["aqpi"] == tags["xqpi"]
if same_tag:
    print(f"    both stacks set SENTINEL_SOURCE_TAG={tags['aqpi']}", file=sys.stderr)
if any(shared.values()):
    sys.exit(1)
if problems:
    sys.exit(3)
if same_tag:
    sys.exit(4)
for s, (d, names) in stacks.items():
    found_lines(s, d, names)
PY
    ) || rc=$?
    case $rc in
        0) ;;
        1) die "the two stacks would share the names above on the Docker host" ;;
        3) die "a mount above is missing or empty; attach it and run again" ;;
        4) die "XQPI's dashboard would show AQPI's source tag; give XQPI its own" \
               "SENTINEL_SOURCE_TAG in $XQPI_ENV (or delete the line there) and run again" ;;
        124) die "checking the mount sources took over ${RENDER_TIMEOUT_S}s (a stale NFS mount?)" ;;
        *) die "could not compare the two rendered stacks" ;;
    esac
    check_existing "$found"
    log "both stacks render from ${TARGET:0:7}, share no names, and have their mounts"
}

# check_existing LINES: each line is "KIND STACK PROJECT-OR-SERVICE VALUE". A
# container with a declared name must belong to the stack's project, or up
# fails on the name after the other stack has restarted. The database volume
# and any named store volume must exist, or up starts the stack on an empty
# one that still passes every healthcheck.
check_existing() {
    local kind s project name owner
    while read -r kind s project name; do
        case $kind in
            container)
                owner=$(docker inspect --type container -f "$PROJECT_LABEL" "$name" 2>/dev/null) \
                    || continue
                [[ $owner != "<no value>" ]] || owner=""
                [[ $owner == "$project" ]] || die "container $name belongs to compose project" \
                    "'${owner:-none}', not $project; stop or rename it, then run again"
                ;;
            dbvolume)
                [[ $name != - ]] || die "the $s stack mounts no named volume for its database"
                docker volume inspect "$name" >/dev/null 2>&1 \
                    || die "the $s database volume $name does not exist, so up would start" \
                        "$s on an empty database"
                ;;
            storevolume)
                docker volume inspect "$name" >/dev/null 2>&1 \
                    || die "the $s store volume $name does not exist, so up would create it" \
                        "empty; if this release adds it, run sudo docker volume create $name"
                ;;
            image) IMAGE_REFS+=("$s $project $name") ;;
            warning) log "WARNING: $name" ;;
            *) ;;
        esac
    done <<<"$1"
}

# need_space: the new images may share no layer with the running ones, so the
# Docker disk must hold their full size again, and stay under the line where
# layer0.self.disk fails, since that failure would stop the run before the
# prune. The line is the target revision's DISK_FAIL_PCT; a value tuned in
# the thresholds table needs an admin login, so it is not read.
need_space() {
    local root ids need=0 size total used avail fail_pct after
    root=$(docker info -f '{{.DockerRootDir}}')
    mapfile -t ids < <(for s in "${STACKS[@]}"; do compose "$s" ps -q 2>/dev/null; done \
        | xargs -r docker inspect -f '{{.Image}}' | sort -u)
    if ((${#ids[@]} > 0)); then
        while read -r size; do need=$((need + size)); done \
            < <(docker image inspect -f '{{.Size}}' "${ids[@]}")
    fi
    # Used as blocks minus free blocks, the way the backend's shutil.disk_usage
    # counts it, so reserved blocks do not count as used.
    read -r total used avail < <(df -B1 --output=size,used,avail "$root" | tail -n 1)
    ((avail > need)) || die "$root has $((avail / 1048576)) MiB free, and the running images" \
        "take $((need / 1048576)) MiB; free space (sudo docker image prune) and run again"
    fail_pct=$("${GIT[@]}" show "$TARGET:backend/checks/layer0_selfhost.py" 2>/dev/null \
        | sed -nE 's/^DISK_FAIL_PCT *= *([0-9]+).*/\1/p') || fail_pct=""
    if [[ $fail_pct =~ ^[0-9]+$ ]] && ((total > 0)); then
        after=$(((used + need) * 100 / total))
        ((after < fail_pct)) || die "the pull could take $root to ${after}% used, and" \
            "layer0.self.disk fails at ${fail_pct}%, which would stop the run before the" \
            "prune; free space (sudo docker image prune -a) and run again"
        log "$root stays under ${after}% used after the pull (layer0.self.disk fails" \
            "at ${fail_pct}%)"
    else
        log "WARNING: no DISK_FAIL_PCT in ${TARGET:0:7}, so the disk line was not tested"
    fi
}

# backup_commands: read root's crontab the way cron does, keeping its
# NAME=value lines for the environment and each backup.sh line's command.
# Cron gives a job only the assignments above it, so BACKUP_ENVN holds how
# many of CRON_ENV each command gets.
backup_commands() {
    local line cmd name value
    BACKUP_CMDS=() BACKUP_ENVN=() CRON_ENV=()
    while IFS= read -r line; do
        [[ ! $line =~ ^[[:space:]]*(#|$) ]] || continue
        if [[ $line =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            name=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
            # cron drops one pair of matching quotes around a value.
            [[ ! $value =~ ^\"(.*)\"$ && ! $value =~ ^\'(.*)\'$ ]] || value=${BASH_REMATCH[1]}
            CRON_ENV+=("$name=$value")
            continue
        fi
        [[ $line == *backup.sh* && ! $line =~ ^[[:space:]]*@reboot ]] || continue
        if [[ $line =~ ^[[:space:]]*@ ]]; then
            cmd=$(sed -E 's/^[[:space:]]*[^[:space:]]+[[:space:]]+//' <<<"$line")
        else
            cmd=$(sed -E 's/^[[:space:]]*([^[:space:]]+[[:space:]]+){5}//' <<<"$line")
        fi
        # cron turns an unescaped % into a newline and feeds the rest to stdin.
        [[ ! $cmd =~ (^|[^\\])% ]] || die "a backup line in root's crontab has a %, which" \
            "cron and sh read differently; take the dumps by hand and use --no-backup"
        # cron hands sh a \% as %.
        BACKUP_CMDS+=("${cmd//\\%/%}")
        BACKUP_ENVN+=("${#CRON_ENV[@]}")
    done < <(crontab -l -u root 2>/dev/null || true)
    local i
    for i in "${!BACKUP_CMDS[@]}"; do log "backup $((i + 1)) runs: ${BACKUP_CMDS[$i]}"; done
    ((${#BACKUP_CMDS[@]} > 0)) || log "WARNING: root's crontab has no backup.sh line, so no dump"
}

warnings() {
    local f hour now
    now=$(date -u +%H)
    for f in "$(env_file aqpi)" "$(env_file xqpi)"; do
        hour=$(grep -E '^SENTINEL_RETENTION_HOUR_UTC=' "$f" | tail -n 1 | cut -d= -f2) || true
        hour=${hour:-9}
        if [[ $hour =~ ^[0-9]+$ ]] && ((10#$now == 10#$hour)); then
            log "WARNING: $f's retention sweep runs this hour; a restart ends it until tomorrow"
        fi
    done
}

# prune_preview: what docker system prune -af takes besides networks and build
# cache: every stopped container, and every image no running container uses.
prune_preview() {
    local stopped used id unused=()
    mapfile -t stopped < <(docker ps -a --filter status=exited --filter status=created \
        --filter status=dead --format '{{.Names}}')
    used=$(docker ps -q | xargs -r docker inspect -f '{{.Image}}' | sort -u)
    while read -r id; do
        [[ -z $id ]] || grep -qxF -- "$id" <<<"$used" || unused+=("$id")
    done < <(docker image ls -q --no-trunc | sort -u)
    log "the prune takes ${#stopped[@]} stopped containers, ${#unused[@]} images no running" \
        "container uses, every unused network and all build cache"
    ((${#stopped[@]} == 0)) || log "  stopped containers: ${stopped[*]}"
    for id in "${unused[@]}"; do
        log "  image ${id#sha256:}" \
            "$(docker image inspect -f '{{join .RepoTags ","}}' "$id" 2>/dev/null || true)"
    done
}

run_backups() {
    STEP="backup"
    no_backup_running
    local i kv shell home envs
    home=$(getent passwd root | cut -d: -f6)
    for i in "${!BACKUP_CMDS[@]}"; do
        envs=("${CRON_ENV[@]:0:${BACKUP_ENVN[$i]}}")
        shell=/bin/sh
        for kv in "${envs[@]}"; do [[ $kv != SHELL=* ]] || shell=${kv#SHELL=}; done
        log "backup $((i + 1)): ${BACKUP_CMDS[$i]%% >>*}"
        # The environment cron gives a job, so the dump matches the nightly one.
        timeout "$BACKUP_TIMEOUT_S" env -i HOME="$home" LOGNAME=root PATH=/usr/bin:/bin \
            SHELL=/bin/sh "${envs[@]}" "$shell" -c "${BACKUP_CMDS[$i]}" \
            || die "backup $((i + 1)) failed; its log is the >> target in root's crontab"
    done
}

sync_checkout() {
    STEP="sync"
    if [[ $OLD_HEAD == "$TARGET" ]]; then
        log "checkout already at ${TARGET:0:7}"
    else
        SYNCED=1
        "${GIT[@]}" checkout -q -B main "$TARGET"
        "${GIT[@]}" branch -q --set-upstream-to=origin/main main
        log "checkout now at $("${GIT[@]}" describe --tags --always)"
    fi
    FILES_DIR=$AQPI_DIR
}

api_version() {
    local port
    port=$(compose "$1" port backend 8000 2>/dev/null) || return 1
    curl -fsS -m 10 "http://127.0.0.1:${port##*:}/api/version" 2>/dev/null \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])' 2>/dev/null
}

# record_running: before anything changes, note each stack's version, image
# IDs, and the project and compose files its backend was started from.
record_running() {
    local line s svc ref ids id labels
    for s in "${STACKS[@]}"; do
        OLD_VERSION[$s]=$(api_version "$s") || OLD_VERSION[$s]=""
        OLD_IMAGES[$s]=""
    done
    for line in "${IMAGE_REFS[@]}"; do
        read -r s svc ref <<<"$line"
        mapfile -t ids < <(compose "$s" ps -q "$svc" 2>/dev/null)
        ((${#ids[@]} > 0)) || continue
        id=$(docker inspect -f '{{.Image}}' "${ids[0]}") || continue
        OLD_IDS[$s/$svc]=$id
        OLD_IMAGES[$s]+="${id:7:12} "
        [[ $svc == backend ]] || continue
        labels=$(docker inspect -f "$PROJECT_LABEL|$FILES_LABEL" "${ids[0]}") || continue
        OLD_PROJECT[$s]=${labels%%|*}
        OLD_FILES[$s]=${labels#*|}
        OLD_FILES[$s]=${OLD_FILES[$s]//,/ }
        [[ ${OLD_FILES[$s]} != "<no value>" ]] || OLD_FILES[$s]=""
    done
    for s in "${STACKS[@]}"; do
        log "$s runs ${OLD_VERSION[$s]:-an unknown version} now, from $(rollback_cmd "$s")"
    done
}

# check_images: say when the pulled images are the ones already running, or
# a Sentinel image was built from another commit than the checkout's target.
check_images() {
    local line s svc ref id rev same=0 total=0
    for line in "${IMAGE_REFS[@]}"; do
        read -r s svc ref <<<"$line"
        IFS='|' read -r id rev < <(docker image inspect -f "{{.Id}}|$REVISION_LABEL" "$ref" \
            2>/dev/null) || continue
        total=$((total + 1))
        [[ $id != "${OLD_IDS[$s/$svc]:-}" ]] || same=$((same + 1))
        if [[ $svc == backend || $svc == frontend ]] \
                && [[ -n $rev && $rev != "<no value>" && $rev != "$TARGET" ]]; then
            log "WARNING: $s $svc image $ref was built from ${rev:0:7}, not ${TARGET:0:7}"
        fi
    done
    if ((total > 0 && same == total)); then
        log "NOTE: every pulled image is the one already running; this run only restarts them"
    fi
}

# in_parallel LIMIT ARGS...: docker compose ARGS for both stacks at once.
in_parallel() {
    local limit=$1 s i pids=() failed=()
    shift
    for s in "${STACKS[@]}"; do
        stack_cmd "$s"
        timeout "$limit" "${CMD[@]}" "$@" >"$WORK/$s.$1.log" 2>&1 &
        pids+=($!)
    done
    for i in "${!pids[@]}"; do
        wait "${pids[$i]}" || failed+=("${STACKS[$i]}")
    done
    for s in "${STACKS[@]}"; do
        sed "s/^/    $s: /" "$WORK/$s.$1.log"
    done
    ((${#failed[@]} == 0)) || die "docker compose $1 failed for: ${failed[*]}"
}

# stack_state STACK: "ok", "warn: why", "wait" or "bad: why", over every
# container of STACK and its backend's own disk and store check.
stack_state() {
    local ids id info name status health restarts
    local fmt='{{.Name}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}'
    mapfile -t ids < <(compose "$1" ps -aq 2>/dev/null)
    ((${#ids[@]} > 0)) || { echo "bad: it has no containers"; return 0; }
    for id in "${ids[@]}"; do
        info=$(docker inspect -f "$fmt|{{.RestartCount}}" "$id" 2>/dev/null) \
            || { echo "wait"; return 0; }
        IFS='|' read -r name status health restarts <<<"$info"
        case $status in
            running) ;;
            created|restarting) echo "wait"; return 0 ;;
            *) echo "bad: ${name#/} is $status"; return 0 ;;
        esac
        ((restarts == 0)) || { echo "bad: ${name#/} restarted $restarts times"; return 0; }
        case $health in
            ""|healthy) ;;
            starting) echo "wait"; return 0 ;;
            *) echo "bad: ${name#/} is $health"; return 0 ;;
        esac
    done
    if http_ok "$1" backend 8000 /api/version && http_ok "$1" frontend 3000 /; then
        disk_state "$1"
    else
        echo "wait"
    fi
}

http_ok() {
    local port
    port=$(compose "$1" port "$2" "$3" 2>/dev/null) || return 1
    [[ -n $port ]] && curl -fsS -m 5 -o /dev/null "http://127.0.0.1:${port##*:}$4" 2>/dev/null
}

# disk_state STACK: the backend's layer0.self.disk result, once the new
# backend container has run it. It fails when a store lost its marker or
# write access, which a backend that answers HTTP does not show.
disk_state() {
    local port ids started
    port=$(compose "$1" port backend 8000 2>/dev/null) || { echo "wait"; return 0; }
    mapfile -t ids < <(compose "$1" ps -q backend 2>/dev/null)
    started=$(docker inspect -f '{{.State.StartedAt}}' "${ids[0]:-none}" 2>/dev/null) \
        || { echo "wait"; return 0; }
    started=$(date -d "$started" +%s.%N 2>/dev/null) || { echo "wait"; return 0; }
    python3 - "$started" <(curl -fsS -m 10 "http://127.0.0.1:${port##*:}/api/status" \
        2>/dev/null) <<'PY' 2>/dev/null || echo "wait"
import json, sys
from datetime import datetime
with open(sys.argv[2], encoding="utf-8") as f:
    stages = json.load(f).get("stages") or {}
rows = [c for cs in stages.values() for c in cs if c.get("check_id") == "layer0.self.disk"]
if not rows:
    print("warn: this version has no layer0.self.disk result")
elif datetime.fromisoformat(rows[0]["finished_at"]).timestamp() < float(sys.argv[1]):
    print("wait")
elif rows[0]["status"] == "fail":
    print("bad: layer0.self.disk fails:", rows[0].get("summary") or "")
elif rows[0]["status"] in ("warn", "error"):
    print(f"warn: layer0.self.disk is {rows[0]['status']}:", rows[0].get("summary") or "")
elif rows[0]["status"] == "pass":
    print("ok")
else:
    # The scheduler marks a check skip while it believes the host is offline.
    print(f"skip: layer0.self.disk is {rows[0]['status']}:", rows[0].get("summary") or "")
PY
}

wait_healthy() {
    STEP="health"
    local deadline=$((SECONDS + HEALTH_TIMEOUT_S)) s state pending
    local -A warned=() skipped=()
    while :; do
        pending=()
        for s in "${STACKS[@]}"; do
            state=$(stack_state "$s")
            unset "skipped[$s]"
            case $state in
                ok) ;;
                warn:*) warned[$s]=${state#warn: } ;;
                skip:*) pending+=("$s"); skipped[$s]=${state#skip: } ;;
                wait) pending+=("$s") ;;
                *) die "$s: ${state#bad: }" ;;
            esac
        done
        ((${#pending[@]} == 0)) && break
        if ((SECONDS >= deadline)); then
            # Only a skipped disk check held them. A skip can hide a detached store
            # while the network is in doubt, so the old images stay for a rollback.
            for s in "${pending[@]}"; do
                [[ -n ${skipped[$s]:-} ]] || die "not up after ${HEALTH_TIMEOUT_S}s: ${pending[*]}"
                warned[$s]="${skipped[$s]} (still, after ${HEALTH_TIMEOUT_S}s)"
            done
            PRUNE=0
            log "keeping the old images: layer0.self.disk could not be graded for ${pending[*]}"
            break
        fi
        sleep "$POLL_S"
    done
    HEALTHY=1
    log "both stacks are up: containers running, healthchecks passing, APIs answering," \
        "layer0.self.disk not failing"
    for s in "${!warned[@]}"; do log "WARNING: $s: ${warned[$s]}"; done
    for s in "${STACKS[@]}"; do
        log "  $s: ${OLD_VERSION[$s]:-unknown} -> $(api_version "$s" || echo unknown)"
    done
}

prune() {
    STEP="prune"
    if ((PRUNE == 0)); then
        log "skipping the prune (--no-prune)"
        return 0
    fi
    prune_preview
    if docker system prune -af >"$WORK/prune.log" 2>&1; then
        tail -n 1 "$WORK/prune.log" | sed 's/^/    /'
    else
        log "WARNING: docker system prune failed, and both stacks are up. Its output:"
        sed 's/^/    /' "$WORK/prune.log"
    fi
}

preflight() {
    need_tools
    need_files
    no_backup_running
    target_revision
    render_target
    need_space
    if ((BACKUP)); then backup_commands; fi
    warnings
}

upgrade() {
    STEP="pull"
    record_running
    in_parallel "$PULL_TIMEOUT_S" pull --quiet
    check_images
    if ((BACKUP)); then run_backups; else log "skipping the dumps (--no-backup)"; fi
    sync_checkout
    STEP="recreate"
    RECREATED=1
    in_parallel "$UP_TIMEOUT_S" up -d --force-recreate --remove-orphans
    wait_healthy
    prune
}

main() {
    parse_args "$@"
    if ((EUID != 0)); then
        printf 'upgrade.sh: the docker commands need root: sudo %s %s\n' "$0" "$*" >&2
        exit 1
    fi
    if ! { exec 8>>"$LOCK"; } 2>/dev/null; then
        printf 'upgrade.sh: cannot open the lock file %s\n' "$LOCK" >&2
        exit 1
    fi
    flock -n 8 || { printf 'upgrade.sh: another run holds %s\n' "$LOCK" >&2; exit 1; }
    if ! { : >>"$LOG"; } 2>/dev/null; then
        printf 'upgrade.sh: cannot write the log %s\n' "$LOG" >&2
        exit 1
    fi
    # Ignored before tee starts, so tee inherits it and keeps writing the log.
    trap '' HUP
    exec > >(tee --output-error=warn -a "$LOG") 2>&1
    TEE_PID=$!
    trap 'on_error $LINENO "$BASH_COMMAND"' ERR
    trap on_signal INT TERM
    trap cleanup EXIT
    local mode=""
    ((CHECK_ONLY == 0)) || mode=" (--check)"
    log "upgrade.sh started by ${SUDO_USER:-root}${REF:+ for $REF}$mode"
    if [[ $REF =~ ^v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
        export SENTINEL_TAG=${BASH_REMATCH[1]}
        log "running image tag $SENTINEL_TAG (from --ref $REF)"
    fi
    preflight
    if ((CHECK_ONLY)); then
        record_running
        prune_preview
        log "--check: every test passed; no stack, checkout file or image changed"
        exit 0
    fi
    upgrade
    log "upgrade finished"
}

main "$@"
