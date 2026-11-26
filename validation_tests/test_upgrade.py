"""Run ops/upgrade.sh against fake docker, sudo, crontab, curl and df in a sandbox.

Each case builds a fresh world in the system temp folder: a bare origin one
release ahead of an AQPI checkout, both env files, root's crontab, the backup
locks, the store folders, and the shims in fixtures/upgrade_shims first on
PATH. User namespaces are often refused, so the root cases run a copy of the
script whose EUID test reads ((0 != 0)); the non-root case runs the file
itself. Nothing here touches the real Docker, sudo or crontab. A full run
takes about 75 s.

    python3 -I validation_tests/test_upgrade.py [--keep] [CASE...]
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
# Overridable so a planted fault in a scratch copy can be run against these tests.
SCRIPT = Path(os.environ.get("UPGRADE_SCRIPT") or REPO / "ops" / "upgrade.sh")
SHIMS = HERE / "fixtures" / "upgrade_shims"
RUN_TIMEOUT_S = 60
GIT_ID = ["-c", "user.name=test", "-c", "user.email=test@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
GIT_ENV = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}
SECRETS = ("sekrit-aqpi-123", "sekrit-xqpi-456")
OWNER = pwd.getpwuid(os.getuid()).pw_name
BACKUP_FIXTURE = """#!/bin/sh
# Fixture: note which checkout revision each dump ran from.
here=$(cd "$(dirname "$0")/.." && pwd)
pulls=$(grep -c '"argv": \\["pull"' "$FAKE_STATE/calls.jsonl" 2>/dev/null || true)
echo "backup ${SENTINEL_BACKUP_DIR:-default} home=$HOME caller=${SUDO_USER:-none}" \\
    "late=${LATE:-unset} args=[$*] pulls=${pulls:-0}" \\
    "head=$(git -C "$here" rev-parse --short=7 HEAD)" >>"$FAKE_STATE/backups.log"
[ ! -e "$FAKE_STATE/flags/backup_fail" ] || exit 3
"""


@dataclass
class World:
    """One sandboxed host: paths, the two release commits, and the root copy."""

    root: Path
    c1: str = ""
    c2: str = ""

    @property
    def bin(self) -> Path:
        """Return the shim folder."""
        return self.root / "bin"

    @property
    def state(self) -> Path:
        """Return the fake docker's state folder."""
        return self.root / "state"

    @property
    def checkout(self) -> Path:
        """Return the AQPI checkout."""
        return self.root / "srv" / "aqpi-sentinel"

    @property
    def xqpi_env(self) -> Path:
        """Return XQPI's env file."""
        return self.root / "srv" / "xqpi-sentinel" / ".env.prod"

    @property
    def log(self) -> Path:
        """Return the upgrade log."""
        return self.root / "log" / "upgrade.log"

    @property
    def lock(self) -> Path:
        """Return the upgrade lock file."""
        return self.root / "run" / "upgrade.lock"

    @property
    def root_script(self) -> Path:
        """Return the copy of upgrade.sh that runs as if root."""
        return self.root / "upgrade-root.sh"

    def flag(self, name: str, text: str = "") -> None:
        """Set a fault flag for the fake docker."""
        (self.state / "flags" / name).write_text(text, encoding="utf-8")


@dataclass
class Result:
    """One run of upgrade.sh."""

    rc: int
    out: str
    err: str
    calls: list[dict[str, object]] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Return stdout and stderr together."""
        return self.out + self.err

    def compose(self, sub: str) -> list[dict[str, object]]:
        """Return the compose calls whose subcommand is SUB."""
        return [c for c in self.calls if "stack" in c and c["argv"][0] == sub]  # type: ignore[index]

    def prunes(self) -> list[dict[str, object]]:
        """Return the docker system prune calls."""
        return [c for c in self.calls if c.get("argv", [])[:2] == ["system", "prune"]]  # type: ignore[index]


class Checks:
    """Collect named pass/fail rows per case."""

    def __init__(self) -> None:
        """Start with no rows."""
        self.rows: list[tuple[str, str, bool, str]] = []
        self.case = ""

    def ok(self, name: str, cond: object, detail: str = "") -> None:
        """Record one check of the current case."""
        self.rows.append((self.case, name, bool(cond), detail))


def git(cwd: Path, *args: str) -> str:
    """Run git in CWD with a neutral config and return its stdout."""
    proc = subprocess.run(["git", *GIT_ID, *args], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True, timeout=30)
    return proc.stdout.strip()


def git_steps(cwd: Path, *steps: list[str]) -> None:
    """Run each git command in STEPS in CWD, for their effect only."""
    for step in steps:
        _ = git(cwd, *step)


def build_repo(w: World) -> None:
    """Make the origin, a checkout at the first release, and origin one release on.

    Args:
        w: the world to fill; its c1 and c2 get the two release commits.
    """
    origin, seed = w.root / "origin.git", w.root / "seed"
    git_steps(w.root, ["init", "-q", "--bare", "-b", "main", str(origin)])
    (seed / "ops").mkdir(parents=True)
    git_steps(seed, ["init", "-q", "-b", "main"])
    shutil.copy(REPO / "ops" / "docker-compose.ghcr.yml", seed / "ops")
    (seed / "backend" / "checks").mkdir(parents=True)
    shutil.copy(REPO / "backend" / "checks" / "layer0_selfhost.py", seed / "backend" / "checks")
    (seed / "ops" / "backup.sh").write_text(BACKUP_FIXTURE, encoding="utf-8")
    (seed / "ops" / "backup.sh").chmod(0o755)
    (seed / ".gitignore").write_text("ops/.env.prod\n", encoding="utf-8")
    git_steps(seed, ["add", "-A"], ["commit", "-q", "-m", "release: v0.16.5"],
              ["tag", "v0.16.5"], ["remote", "add", "origin", str(origin)],
              ["push", "-q", "origin", "main", "--tags"])
    w.c1 = git(seed, "rev-parse", "HEAD")
    git_steps(w.root, ["clone", "-q", str(origin), str(w.checkout)])
    shutil.copy(REPO / "ops" / "docker-compose.xqpi.yml", seed / "ops")
    git_steps(seed, ["add", "-A"], ["commit", "-q", "-m", "release: v0.16.6"],
              ["tag", "v0.16.6"], ["push", "-q", "origin", "main", "--tags"])
    w.c2 = git(seed, "rev-parse", "HEAD")


def build_world(sandbox: Path) -> World:
    """Return a fresh sandboxed host.

    Args:
        sandbox: the run's sandbox folder; the world is a new folder inside it.

    Returns:
        The world, with its checkout one release behind origin.
    """
    w = World(Path(tempfile.mkdtemp(prefix="world.", dir=sandbox)))
    for sub in ("bin", "state/flags", "srv/xqpi-sentinel", "backups/aqpi", "backups/xqpi",
                "log", "run", "home", "dockerroot", "share/aqpi/archive", "share/aqpi/cold",
                "share/aqpi/tree", "share/xqpi/tree"):
        (w.root / sub).mkdir(parents=True, exist_ok=True)
    for store in ("share/aqpi/archive", "share/aqpi/cold"):
        (w.root / store / ".sentinel-store").touch()
    for tree in ("share/aqpi/tree", "share/xqpi/tree"):
        (w.root / tree / "index.html").touch()
    for shim in SHIMS.iterdir():
        shutil.copy(shim, w.bin / shim.name)
        (w.bin / shim.name).chmod(0o755)
    build_repo(w)
    (w.checkout / "ops" / ".env.prod").write_text(
        f"POSTGRES_PASSWORD={SECRETS[0]}\nSENTINEL_SOURCE_TAG=K2\nSENTINEL_TAG=latest\n",
        encoding="utf-8")
    w.xqpi_env.write_text(f"POSTGRES_PASSWORD={SECRETS[1]}\nSENTINEL_SOURCE_TAG=TR\n",
                          encoding="utf-8")
    backup = w.checkout / "ops" / "backup.sh"
    w.flag("image_rev", w.c2)
    (w.state / "crontab").write_text(
        f"# m h dom mon dow command\n"
        f"FAKE_STATE={w.state}\n"
        f'PATH="{w.bin}:/usr/bin:/bin"\n'
        f"HOME = {w.root}/home\n"
        f"0 8 * * * {backup} >> {w.root}/log/backup.log 2>&1\n"
        f"#30 8 * * * SENTINEL_BACKUP_DIR=old {backup}\n"
        f"LATE=1\n"
        f"30 8 * * * SENTINEL_BACKUP_DIR={w.root}/backups/xqpi {backup} 'x\\%y'"
        f" >> {w.root}/log/xbackup.log 2>&1\n", encoding="utf-8")
    # A placeholder volume the base file mounts when a path is unset; up may create it.
    w.flag("novolume_xqpi-sentinel_sentinel_sscb_unset")
    for stack in ("aqpi", "xqpi"):
        (w.root / "backups" / stack / ".backup.lock").touch()
    text = SCRIPT.read_text(encoding="utf-8")
    if text.count("((EUID != 0))") != 1:
        raise RuntimeError("upgrade.sh no longer has exactly one ((EUID != 0)) test")
    w.root_script.write_text(text.replace("((EUID != 0))", "((0 != 0))"), encoding="utf-8")
    return w


def env_for(w: World, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment a run sees, with the shims first on PATH.

    Args:
        w: the world to run in.
        extra: variables that replace or add to the defaults.

    Returns:
        The whole environment; nothing else from this process leaks in.
    """
    env = {
        "PATH": f"{w.bin}:{os.environ['PATH']}", "HOME": str(w.root / "home"),
        "LANG": "C.UTF-8", "FAKE_STATE": str(w.state),
        "FAKE_DOCKER_ROOT": str(w.root / "dockerroot"),
        "UPGRADE_AQPI_DIR": str(w.checkout), "UPGRADE_XQPI_ENV": str(w.xqpi_env),
        "UPGRADE_LOG": str(w.log), "UPGRADE_LOCK": str(w.lock),
        "UPGRADE_WORK_ROOT": str(w.root / "run"),
        "UPGRADE_BACKUP_LOCKS": " ".join(str(w.root / "backups" / s / ".backup.lock")
                                         for s in ("aqpi", "xqpi")),
        "UPGRADE_HEALTH_TIMEOUT_S": "3", "UPGRADE_POLL_S": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "SUDO_USER": "tester",
    }
    env.update(extra or {})
    return env


def read_calls(w: World) -> list[dict[str, object]]:
    """Return every call the fake docker recorded."""
    path = w.state / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run(w: World, *args: str, root: bool = True, extra: dict[str, str] | None = None) -> Result:
    """Run upgrade.sh once, bounded by setsid -w timeout."""
    script = w.root_script if root else SCRIPT
    cmd = ["setsid", "-w", "timeout", "-k", "5", str(RUN_TIMEOUT_S), "bash", str(script), *args]
    proc = subprocess.run(cmd, env=env_for(w, extra), cwd=w.root, capture_output=True,
                          text=True, timeout=RUN_TIMEOUT_S + 15, check=False)
    return Result(proc.returncode, proc.stdout, proc.stderr, read_calls(w))


def head(w: World) -> str:
    """Return the checkout's HEAD commit."""
    return git(w.checkout, "rev-parse", "HEAD")


def leftovers(w: World) -> list[Path]:
    """Return work folders a run left behind."""
    return list((w.root / "run").glob("sentinel-upgrade.*"))


def hold_lock(path: Path) -> subprocess.Popen[bytes]:
    """Hold an flock on PATH from another process until it is killed.

    Args:
        path: the lock file.

    Returns:
        The holder, in its own process group, once the lock is taken.

    Raises:
        RuntimeError: when the holder has not taken the lock within 10 s.
    """
    proc = subprocess.Popen(["flock", str(path), "sleep", "30"], start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        probe = subprocess.run(["flock", "-n", str(path), "true"], check=False, timeout=5)
        if probe.returncode != 0:
            return proc
        time.sleep(0.05)
    os.killpg(proc.pid, signal.SIGKILL)
    raise RuntimeError(f"could not take the lock {path}")


def release(proc: subprocess.Popen[bytes]) -> None:
    """Kill a helper's process group and reap it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def unchanged(c: Checks, w: World, r: Result, *, pulled: bool = False) -> None:
    """Check a failed preflight left everything as it was.

    Args:
        c: the checks to record into.
        w: the world the run used.
        r: the run.
        pulled: whether the run got as far as the pull.
    """
    c.ok("exit 1", r.rc == 1, f"rc={r.rc}")
    c.ok("pulled only if expected", bool(r.compose("pull")) == pulled)
    c.ok("no up", not r.compose("up"))
    c.ok("checkout unchanged", head(w) == w.c1)
    c.ok("says nothing changed",
         "The checkout and the running containers were not touched." in r.text)
    c.ok("one failure report", r.text.count("FAILED in step") == 1, r.text[-400:])
    c.ok("no work folder left", not leftovers(w))


def failed_after_up(c: Checks, w: World, r: Result, reason: str) -> None:
    """Check a failure after the recreate gave rollback hints and pruned nothing.

    Args:
        c: the checks to record into.
        w: the world the run used.
        r: the run.
        reason: text the failure message must contain.
    """
    c.ok("exit 1", r.rc == 1, f"rc={r.rc}")
    c.ok("reason given", reason in r.text, r.text[-600:])
    c.ok("both stacks recreated", {x["stack"] for x in r.compose("up")} == {"aqpi", "xqpi"})
    c.ok("no prune", not r.prunes())
    c.ok("rollback hint names the old tag", "SENTINEL_TAG=0.16.5" in r.text)
    c.ok("old image IDs listed", "previously ran image IDs aaaaaaaaaaaa" in r.text)
    c.ok("keeps the checkout for XQPI", "lacks a file below" in r.text)
    c.ok("names the schema risk", "the dumps this run took before the restart" in r.text)
    c.ok("checkout stays at target", head(w) == w.c2)
    c.ok("one failure report", r.text.count("FAILED in step") == 1)


def overlap(calls: list[dict[str, object]]) -> bool:
    """Return whether the two calls ran at the same time."""
    starts = [float(x["t0"]) for x in calls]  # type: ignore[arg-type]
    ends = [float(x["t1"]) for x in calls]  # type: ignore[arg-type]
    return len(calls) == 2 and max(starts) < min(ends)


def case_non_root(c: Checks, w: World) -> None:
    """The file itself refuses a non-root caller before it touches anything."""
    r = run(w, "--check", root=False)
    c.ok("exit 1", r.rc == 1, f"rc={r.rc}")
    c.ok("tells the caller to use sudo", "need root: sudo" in r.err, r.err)
    c.ok("no docker call", not r.calls)
    c.ok("no log written", not w.log.exists())


def case_arguments(c: Checks, w: World) -> None:
    """Help exits 0; a bad option or --ref value exits 2.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    r = run(w, "--help", root=False)
    c.ok("--help exits 0", r.rc == 0 and "--check" in r.out, f"rc={r.rc}")
    for args in (["--ref", "-rf"], ["--ref", "a b"], ["--ref"], ["--bogus"]):
        r = run(w, *args)
        c.ok(f"{' '.join(args)} exits 2", r.rc == 2, f"rc={r.rc} {r.err[-200:]}")
    c.ok("no docker call", not read_calls(w))


def case_check_only(c: Checks, w: World) -> None:
    """--check runs every test and changes nothing.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    r = run(w, "--check")
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("says every test passed",
         "every test passed; no stack, checkout file or image changed" in r.out)
    c.ok("shows the commits ahead", "1 new commits" in r.out and "release: v0.16.6" in r.out)
    c.ok("no pull, up or prune", not (r.compose("pull") or r.compose("up") or r.prunes()))
    c.ok("no dump", not (w.state / "backups.log").exists())
    c.ok("checkout unchanged", head(w) == w.c1)
    c.ok("no work folder left", not leftovers(w))
    c.ok("warns the script is not root's", "should be owned by root" in r.out)
    c.ok("renders read from a pipe", all(str(x["stdout"]).startswith("pipe:")
                                         for x in r.compose("config")))


def case_success(c: Checks, w: World) -> None:
    """A clean run dumps, syncs, pulls and recreates both stacks at once, then prunes.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    r = run(w)
    c.ok("exit 0", r.rc == 0, r.text[-800:])
    c.ok("finished", "upgrade finished" in r.out)
    dumps = (w.state / "backups.log").read_text(encoding="utf-8").splitlines()
    c.ok("both dumps ran after the pulls and before the sync", len(dumps) == 2
         and all(d.endswith(f"pulls=2 head={w.c1[:7]}") for d in dumps), str(dumps))
    c.ok("each dump gets the cron lines above it", len(dumps) == 2
         and "late=unset args=[]" in dumps[0] and "late=1 args=[x%y]" in dumps[1], str(dumps))
    c.ok("compose runs from the checkout's ops/", all(
        x["project_dir"] == str(w.checkout / "ops") for x in r.calls if "stack" in x))
    c.ok("dumps ran in cron's environment", all(f"home={w.root}/home caller=none " in d
                                                for d in dumps)
         and "default home=" in dumps[0] and f"{w.root}/backups/xqpi" in dumps[1], str(dumps))
    c.ok("prune preview before the prune", "the prune takes 0 stopped containers, 2 images"
         in r.out and "example/other-tool:1" in r.out, r.out[-600:])
    pulls, ups = r.compose("pull"), r.compose("up")
    c.ok("pulled both, quietly", {x["stack"] for x in pulls} == {"aqpi", "xqpi"}
         and all("--quiet" in x["argv"] for x in pulls))  # type: ignore[operator]
    c.ok("pulls ran in parallel", overlap(pulls))
    c.ok("up flags", all(x["argv"] == ["up", "-d", "--force-recreate", "--remove-orphans"]
                         for x in ups) and len(ups) == 2)
    c.ok("ups ran in parallel", overlap(ups))
    c.ok("pull before up", max(float(x["t1"]) for x in pulls)  # type: ignore[arg-type]
         <= min(float(x["t0"]) for x in ups))  # type: ignore[arg-type]
    prunes = r.prunes()
    c.ok("pruned once, last", len(prunes) == 1 and r.calls[-1] is prunes[0])
    base = str(w.checkout / "ops" / "docker-compose.ghcr.yml")
    over = str(w.checkout / "ops" / "docker-compose.xqpi.yml")
    files = {x["stack"]: x["files"] for x in ups}
    c.ok("compose files from the checkout", files == {"aqpi": [base], "xqpi": [base, over]},
         str(files))
    c.ok("XQPI uses its env file", all(x["env_file"] == str(w.xqpi_env)
                                      for x in ups if x["stack"] == "xqpi"))
    c.ok("checkout at target", head(w) == w.c2)
    c.ok("reports both versions", "aqpi: 0.16.5 -> 0.16.6" in r.out
         and "xqpi: 0.16.5 -> 0.16.6" in r.out)
    c.ok("log has the run", "upgrade finished" in w.log.read_text(encoding="utf-8"))
    c.ok("no secret in output or log", not any(s in r.text or s in w.log.read_text("utf-8")
                                               for s in SECRETS))
    c.ok("no work folder left", not leftovers(w))
    sudo = (w.state / "sudo.log").read_text(encoding="utf-8").splitlines()
    c.ok("git runs as the checkout owner", sudo and all(
        line.startswith(f"-u {OWNER} -H git -C {w.checkout}") for line in sudo))
    c.ok("no image tag forced", all(x["tag"] is None for x in pulls + ups))


def case_ref_tag(c: Checks, w: World) -> None:
    """--ref vX.Y.Z syncs to the tag and runs that image tag."""
    r = run(w, "--ref", "v0.16.6")
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("tag on pull and up", all(x["tag"] == "0.16.6"
                                   for x in r.compose("pull") + r.compose("up")))
    c.ok("checkout at the tag", head(w) == w.c2)


def case_collide(c: Checks, w: World) -> None:
    """Two stacks naming the same container stop the run in preflight."""
    w.flag("collide")
    r = run(w)
    c.ok("names the shared container", "both stacks claim container sentinel-backend" in r.text,
         r.text[-400:])
    unchanged(c, w, r)


def case_same_project(c: Checks, w: World) -> None:
    """Two stacks with one project name stop the run, since --remove-orphans would cross."""
    w.flag("same_project")
    r = run(w)
    c.ok("names the shared project", "both stacks claim project sentinel" in r.text,
         r.text[-400:])
    unchanged(c, w, r)


def case_render_fail(c: Checks, w: World) -> None:
    """A compose file that does not render stops the run with compose's error."""
    w.flag("render_fail_xqpi")
    r = run(w)
    c.ok("names the stack and error", "the xqpi stack does not render" in r.text
         and "mapping values" in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_dirty(c: Checks, w: World) -> None:
    """A local change to a tracked file stops the run."""
    with (w.checkout / "ops" / "docker-compose.ghcr.yml").open("a", encoding="utf-8") as f:
        f.write("# local edit\n")
    r = run(w)
    c.ok("names the change", "local changes to tracked files" in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_backup_lock(c: Checks, w: World) -> None:
    """A backup holding its lock stops the run before any dump.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    lock = hold_lock(w.root / "backups" / "xqpi" / ".backup.lock")
    try:
        r = run(w)
    finally:
        release(lock)
    c.ok("says a backup runs", "a backup is running" in r.text, r.text[-400:])
    c.ok("no dump", not (w.state / "backups.log").exists())
    unchanged(c, w, r)


def case_backup_fail(c: Checks, w: World) -> None:
    """A failed dump stops the run before the sync."""
    w.flag("backup_fail")
    r = run(w)
    c.ok("names the dump", "backup 1 failed" in r.text, r.text[-400:])
    unchanged(c, w, r, pulled=True)


def case_pull_fail(c: Checks, w: World) -> None:
    """A failed pull puts the checkout back and leaves the containers alone.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("pull_fail_xqpi")
    r = run(w)
    c.ok("exit 1", r.rc == 1)
    c.ok("names the stack", "docker compose pull failed for: xqpi" in r.text, r.text[-400:])
    c.ok("shows compose's error", "xqpi: Error response from daemon: manifest unknown" in r.text)
    c.ok("no up", not r.compose("up"))
    c.ok("checkout untouched", head(w) == w.c1 and "The checkout and the running containers"
         " were not touched." in r.text)
    c.ok("no dump", not (w.state / "backups.log").exists())
    c.ok("one failure report", r.text.count("FAILED in step") == 1)


def case_up_fail(c: Checks, w: World) -> None:
    """A failed up prints the way back and prunes nothing.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("up_fail_aqpi")
    r = run(w)
    c.ok("exit 1", r.rc == 1)
    c.ok("names the stack", "docker compose up failed for: aqpi" in r.text, r.text[-400:])
    c.ok("no prune", not r.prunes())
    c.ok("rollback hint", "SENTINEL_TAG=0.16.5" in r.text)
    c.ok("one failure report", r.text.count("FAILED in step") == 1)


def case_unhealthy(c: Checks, w: World) -> None:
    """An exited container after up fails the run with the way back."""
    w.flag("exited_xqpi-sentinel-backend")
    failed_after_up(c, w, run(w), "xqpi: xqpi-sentinel-backend is exited")


def case_restarts(c: Checks, w: World) -> None:
    """A container that restarted after up fails the run."""
    w.flag("restarts_sentinel-backend")
    failed_after_up(c, w, run(w), "aqpi: sentinel-backend restarted 2 times")


def case_health_timeout(c: Checks, w: World) -> None:
    """A healthcheck that never passes fails the run at the deadline."""
    w.flag("starting_sentinel-postgres")
    failed_after_up(c, w, run(w), "not up after 3s: aqpi")


def case_http_down(c: Checks, w: World) -> None:
    """A backend or frontend that runs but never answers HTTP fails the run at the deadline."""
    w.flag("http_down_xqpi")
    failed_after_up(c, w, run(w), "not up after 3s: xqpi")
    w2 = build_world(w.root.parent)
    w2.flag("frontend_down_aqpi")
    failed_after_up(c, w2, run(w2), "not up after 3s: aqpi")


def case_old_api_down(c: Checks, w: World) -> None:
    """A stack whose API is down before the upgrade still upgrades.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("http_down_before_aqpi")
    r = run(w)
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("version unknown", "aqpi runs an unknown version now" in r.out)
    c.ok("checkout at target", head(w) == w.c2)
    c.ok("no failure report", "FAILED" not in r.text)
    c.ok("reports the new version", "aqpi: unknown -> 0.16.6" in r.out)


def case_slow_start(c: Checks, w: World) -> None:
    """A container that takes two polls to start still passes."""
    w.flag("slowstart_xqpi-sentinel-backend", "2")
    r = run(w)
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("pruned once", len(r.prunes()) == 1)


def case_prune_fail(c: Checks, w: World) -> None:
    """A failed prune after a good upgrade gives a warning and exit 0.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("prune_fail")
    r = run(w)
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("warns with docker's error", "WARNING: docker system prune failed" in r.out
         and "a prune operation is already running" in r.out)
    c.ok("no failure report", "FAILED" not in r.text and "upgrade finished" in r.out)


def case_skips(c: Checks, w: World) -> None:
    """--no-backup and --no-prune skip their steps."""
    r = run(w, "--no-backup", "--no-prune")
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("no dump", not (w.state / "backups.log").exists() and "skipping the dumps" in r.out)
    c.ok("no prune", not r.prunes() and "skipping the prune" in r.out)


def case_tooling(c: Checks, w: World) -> None:
    """An old Compose or a silent daemon stops the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    for name, words in (("compose_old", "older than 2.24.4"), ("daemon_down", "does not answer")):
        w.flag(name)
        r = run(w)
        c.ok(f"{name}: reason", words in r.text, r.text[-300:])
        c.ok(f"{name}: nothing changed", r.rc == 1 and "were not touched." in r.text)
        (w.state / "flags" / name).unlink()


def case_non_ff(c: Checks, w: World) -> None:
    """A rewritten origin history stops the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    seed = w.root / "seed"
    git_steps(seed, ["checkout", "-q", "--orphan", "rewrite"],
              ["commit", "-q", "-m", "rewritten history"],
              ["push", "-q", "-f", "origin", "rewrite:main"])
    r = run(w)
    c.ok("says it cannot fast-forward", "cannot fast-forward" in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_already_there(c: Checks, w: World) -> None:
    """A checkout already at the target still recreates both stacks."""
    git_steps(w.checkout, ["fetch", "-q", "origin"], ["merge", "-q", "--ff-only", "origin/main"])
    r = run(w)
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("says so", "checkout already at" in r.out)
    c.ok("recreated both", len(r.compose("up")) == 2)


def case_lock_held(c: Checks, w: World) -> None:
    """A second run while one holds the lock exits at once.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    lock = hold_lock(w.lock)
    try:
        r = run(w)
    finally:
        release(lock)
    c.ok("exit 1", r.rc == 1)
    c.ok("names the lock", "another run holds" in r.err, r.err)
    c.ok("no docker call", not r.calls)


def case_foreign_container(c: Checks, w: World) -> None:
    """A container name held by another compose project stops the run."""
    w.flag("foreign_xqpi-sentinel-backend")
    r = run(w)
    c.ok("names the owner", "belongs to compose project 'other-project'" in r.text,
         r.text[-400:])
    unchanged(c, w, r)


def case_fresh_host(c: Checks, w: World) -> None:
    """Containers that do not exist yet are not an error."""
    w.flag("fresh_host")
    r = run(w, "--check")
    c.ok("exit 0", r.rc == 0, r.text[-400:])


def case_missing_db_volume(c: Checks, w: World) -> None:
    """A database volume that does not exist stops the run."""
    w.flag("novolume_xqpi_sentinel_pgdata")
    r = run(w)
    c.ok("names the volume", "database volume xqpi_sentinel_pgdata does not exist" in r.text,
         r.text[-400:])
    unchanged(c, w, r)


def case_no_space(c: Checks, w: World) -> None:
    """Too little disk for the new images stops the run."""
    w.flag("huge_images")
    r = run(w)
    c.ok("gives the numbers", "MiB free, and the running images" in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_old_target(c: Checks, w: World) -> None:
    """A target before the XQPI override stops the run."""
    r = run(w, "--ref", "v0.16.5")
    c.ok("names the missing file", "lacks ops/docker-compose.xqpi.yml" in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_missing_env(c: Checks, w: World) -> None:
    """A missing env file stops the run."""
    w.xqpi_env.unlink()
    r = run(w)
    c.ok("names the file", "missing or unreadable env file" in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_log_unwritable(c: Checks, w: World) -> None:
    """A log path that cannot be written stops the run before any step."""
    r = run(w, extra={"UPGRADE_LOG": str(w.root / "nope" / "dir" / "upgrade.log")})
    c.ok("exit 1", r.rc == 1)
    c.ok("names the log", "cannot write the log" in r.err, r.err)
    c.ok("no docker call", not r.calls)


def reap_group(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    """Kill PROC's process group and wait up to 5 s until no member is left.

    Args:
        proc: a process started with start_new_session=True.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    _ = proc.wait(timeout=10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def case_sync_fail(c: Checks, w: World) -> None:
    """A sync that fails half way puts the checkout back before any recreate.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("upstream_fail")
    r = run(w)
    c.ok("exit 1", r.rc == 1, r.text[-400:])
    c.ok("checkout put back", head(w) == w.c1 and "Put the checkout back at" in r.text)
    c.ok("no up", not r.compose("up"))
    c.ok("one failure report", r.text.count("FAILED in step 'sync'") == 1)


def case_first_run_rollback(c: Checks, w: World) -> None:
    """After XQPI's first switch, the way back is the compose file it ran from.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    old = w.root / "srv" / "xqpi-sentinel" / "docker-compose.ghcr.yml"
    old.write_text("name: xqpi-sentinel\n", encoding="utf-8")
    w.flag("old_files_xqpi")
    w.flag("exited_xqpi-sentinel-backend")
    r = run(w)
    c.ok("exit 1", r.rc == 1, r.text[-400:])
    c.ok("XQPI's old command", f"SENTINEL_TAG=0.16.5 docker compose -p xqpi-sentinel -f {old}"
         f" --env-file {w.xqpi_env} up -d --force-recreate" in r.text, r.text[-900:])
    c.ok("AQPI's command", "SENTINEL_TAG=0.16.5 docker compose -p sentinel -f "
         f"{w.checkout}/ops/docker-compose.ghcr.yml --env-file" in r.text)
    c.ok("checkout can go back", f"checkout -B main {w.c1[:7]}" in r.text)
    c.ok("--check names the old file", f"from docker compose -p xqpi-sentinel -f {old}"
         in run(w, "--check").out)


def case_source_tag_shared(c: Checks, w: World) -> None:
    """XQPI labeling its tree with AQPI's source tag stops the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.xqpi_env.write_text(f"POSTGRES_PASSWORD={SECRETS[1]}\nSENTINEL_SOURCE_TAG=K2\n",
                          encoding="utf-8")
    r = run(w)
    c.ok("names the tag", "both stacks set SENTINEL_SOURCE_TAG=K2" in r.text, r.text[-400:])
    c.ok("says which file to edit", f"SENTINEL_SOURCE_TAG in {w.xqpi_env}" in r.text)
    unchanged(c, w, r)


def case_store_volume_missing(c: Checks, w: World) -> None:
    """A named store volume that does not exist stops the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("novolume_xqpi_sentinel_cold")
    r = run(w)
    c.ok("names the volume", "the xqpi store volume xqpi_sentinel_cold does not exist" in r.text,
         r.text[-400:])
    unchanged(c, w, r)


def case_store_bind_no_marker(c: Checks, w: World) -> None:
    """An empty store bind with no marker set stops the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("no_marker_aqpi")
    (w.root / "share/aqpi/cold/.sentinel-store").unlink()
    r = run(w)
    c.ok("names the empty store", f"bind source {w.root}/share/aqpi/cold is empty (a store"
         " may start empty only with a marker)" in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_disk_line(c: Checks, w: World) -> None:
    """A pull that could take the disk past layer0.self.disk's fail line stops the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    st = os.statvfs(w.root / "dockerroot")
    total, avail = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    w.flag("image_size", str(avail - total // 50))
    r = run(w)
    c.ok("names the fail line", "layer0.self.disk fails at 88%, which would stop the run"
         in r.text, r.text[-400:])
    unchanged(c, w, r)


def case_disk_line_reserved(c: Checks, w: World) -> None:
    """Reserved blocks do not count as used, as in the backend's disk check.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    # 1,000,000 B with 50,000 reserved: 86% used after the pull, or 91% if the
    # reserve counted as used.
    w.flag("df_fake", "1000000 850000 100000")
    w.flag("image_size", "10000")
    r = run(w, "--check")
    c.ok("exit 0", r.rc == 0, r.text[-400:])
    c.ok("counts used as the backend does", "stays under 86% used" in r.out, r.out[-400:])


def case_disk_check_skipped(c: Checks, w: World) -> None:
    """A disk check stuck at skip passes with a warning and keeps the old images.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("disk_skip_xqpi")
    r = run(w)
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("warns", "WARNING: xqpi: layer0.self.disk is skip: host offline (still, after 3s)"
         in r.out, r.out[-600:])
    c.ok("no prune", not r.prunes()
         and "keeping the old images: layer0.self.disk could not be graded for xqpi" in r.out)


def case_source_tag_unset(c: Checks, w: World) -> None:
    """An XQPI env with no source tag passes, and an SSCB tag there gets a warning.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.xqpi_env.write_text(f"POSTGRES_PASSWORD={SECRETS[1]}\nSENTINEL_SSCB_SOURCE_TAG=TR\n",
                          encoding="utf-8")
    r = run(w, "--check")
    c.ok("exit 0", r.rc == 0, r.text[-400:])
    c.ok("warns about SSCB", "WARNING: XQPI's env sets SENTINEL_SSCB_SOURCE_TAG" in r.out)


def case_marker_missing(c: Checks, w: World) -> None:
    """A store without its marker stops the run before anything changes.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    (w.root / "share/aqpi/cold/.sentinel-store").unlink()
    r = run(w)
    c.ok("names the store", f"no .sentinel-store in {w.root}/share/aqpi/cold" in r.text,
         r.text[-400:])
    c.ok("says attach it", "a mount above is missing or empty" in r.text)
    unchanged(c, w, r)


def case_bind_empty_or_missing(c: Checks, w: World) -> None:
    """An empty data tree, or a missing one, stops the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    (w.root / "share/aqpi/tree/index.html").unlink()
    shutil.rmtree(w.root / "share/xqpi/tree")
    r = run(w)
    c.ok("names the empty tree", f"bind source {w.root}/share/aqpi/tree is empty" in r.text,
         r.text[-500:])
    c.ok("names the missing tree",
         f"bind source {w.root}/share/xqpi/tree does not exist" in r.text)
    unchanged(c, w, r)


def case_disk_check_fails(c: Checks, w: World) -> None:
    """A backend whose disk check fails after the restart fails the run.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("disk_fail_aqpi")
    failed_after_up(c, w, run(w), "aqpi: layer0.self.disk fails: NOT ATTACHED")


def case_disk_check_stale(c: Checks, w: World) -> None:
    """A disk check that has not run since the restart holds the run until the deadline.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("disk_stale_xqpi")
    failed_after_up(c, w, run(w), "not up after 3s: xqpi")


def case_disk_check_warns(c: Checks, w: World) -> None:
    """A disk check that warns passes with the warning printed.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("disk_warn_xqpi")
    w.flag("disk_absent_aqpi")
    r = run(w)
    c.ok("exit 0", r.rc == 0, r.text[-600:])
    c.ok("prints the warning", "WARNING: xqpi: layer0.self.disk is warn: UNREACHABLE" in r.out)
    c.ok("notes the absent check", "WARNING: aqpi: this version has no layer0.self.disk" in r.out)
    c.ok("pruned", len(r.prunes()) == 1)


def case_cron_lines(c: Checks, w: World) -> None:
    """An @daily backup line runs, and a % in one stops the run in preflight.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    backup = w.checkout / "ops" / "backup.sh"
    crontab = w.state / "crontab"
    base = crontab.read_text(encoding="utf-8")
    crontab.write_text(base + f"@daily SENTINEL_BACKUP_DIR=daily {backup}\n", encoding="utf-8")
    r = run(w, "--no-prune")
    dumps = (w.state / "backups.log").read_text(encoding="utf-8").splitlines()
    c.ok("@daily line ran", r.rc == 0 and len(dumps) == 3 and "backup daily" in dumps[2],
         str(dumps))
    w2 = build_world(w.root.parent)
    crontab2 = w2.state / "crontab"
    crontab2.write_text(crontab2.read_text(encoding="utf-8")
                        + f"0 3 * * * {backup} $(date +%d)\n", encoding="utf-8")
    r2 = run(w2)
    c.ok("% refused", "has a %, which cron and sh read differently" in r2.text, r2.text[-400:])
    unchanged(c, w2, r2)


def case_check_reports(c: Checks, w: World) -> None:
    """--check lists the dumps, the running versions and what a prune takes.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("stopped", "old-job")
    r = run(w, "--check")
    c.ok("exit 0", r.rc == 0, r.text[-400:])
    c.ok("lists the dumps", r.out.count("runs: ") == 2 and "backup 2 runs: SENTINEL_BACKUP_DIR="
         in r.out, r.out[-800:])
    c.ok("names the running setup", "aqpi runs 0.16.5 now, from docker compose -p sentinel"
         in r.out)
    c.ok("lists stopped containers", "stopped containers: old-job" in r.out)
    c.ok("lists unused images", "example/other-tool:1" in r.out)
    c.ok("no dump", not (w.state / "backups.log").exists())


def case_image_notes(c: Checks, w: World) -> None:
    """A Sentinel image built from another commit, or the same digest, gets a note.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("image_rev", "deadbeef" * 5)
    w.flag("same_digest")
    r = run(w, "--no-prune")
    c.ok("exit 0", r.rc == 0, r.text[-400:])
    c.ok("warns on the revision", "was built from deadbee, not" in r.out, r.out[-800:])
    c.ok("notes the same digest", "every pulled image is the one already running" in r.out)
    c.ok("no revision warning for postgres", "postgres image" not in r.out)


def run_signaled(w: World, sig: signal.Signals, ready: Callable[[], bool],
                 extra: dict[str, str] | None = None) -> Result:
    """Start upgrade.sh, send SIG to its shell once READY holds, and wait for it.

    Args:
        w: the world to run in.
        sig: the signal for the script's own shell (not its children).
        ready: polled every 50 ms until it returns True.
        extra: more environment for the run.

    Returns:
        The run, after the script exits and its children are killed.
    """
    out, err = w.root / "signal.out", w.root / "signal.err"
    with out.open("w", encoding="utf-8") as fo, err.open("w", encoding="utf-8") as fe:
        proc = subprocess.Popen(["bash", str(w.root_script)], env=env_for(w, extra),
                                cwd=w.root, stdout=fo, stderr=fe, start_new_session=True)
        try:
            deadline = time.monotonic() + 30
            while not ready() and time.monotonic() < deadline:
                time.sleep(0.05)
            os.kill(proc.pid, sig)
            rc = proc.wait(timeout=RUN_TIMEOUT_S)
        finally:
            reap_group(proc)
    return Result(rc, out.read_text(encoding="utf-8"), err.read_text(encoding="utf-8"),
                  read_calls(w))


def pulls_started(w: World) -> Callable[[], bool]:
    """Return a test for both stacks having started their pull."""
    return lambda: all((w.state / f"started_pull_{s}").exists() for s in ("aqpi", "xqpi"))


def case_sigterm_in_pull(c: Checks, w: World) -> None:
    """SIGTERM during the pull puts the checkout back and explains itself.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("slow_pull", "2")
    r = run_signaled(w, signal.SIGTERM, pulls_started(w))
    c.ok("exit 130", r.rc == 130, f"rc={r.rc} {r.text[-400:]}")
    c.ok("says where it stopped", "INTERRUPTED in step 'pull'" in r.text)
    c.ok("says the pull goes on", "keeps running to its end" in r.text)
    c.ok("checkout untouched", head(w) == w.c1 and "The checkout and the running containers"
         " were not touched." in r.text)
    c.ok("no up", not any("up" in str(x.get("argv")) for x in read_calls(w)))


def case_sigint_in_health(c: Checks, w: World) -> None:
    """SIGINT during the health wait prints the way back and prunes nothing.

    Args:
        c: the checks to record into.
        w: a fresh world.
    """
    w.flag("starting_sentinel-postgres")
    # The first health poll, since the up calls' own files land before STEP moves on.
    polled = (w.state / "health_polled").exists
    r = run_signaled(w, signal.SIGINT, polled, {"UPGRADE_HEALTH_TIMEOUT_S": "30"})
    stops = [line for line in r.text.splitlines()
             if any(word in line for word in ("FAILED", "INTERRUPTED", "trap", "bash:"))]
    c.ok("exit 130", r.rc == 130, f"rc={r.rc} {stops}")
    c.ok("says where it stopped", "INTERRUPTED in step 'health'" in r.text, r.text[-400:])
    c.ok("rollback hint", "SENTINEL_TAG=0.16.5" in r.text)
    c.ok("no prune", not r.prunes())


def case_sighup_in_pull(c: Checks, w: World) -> None:
    """SIGHUP, as from a dropped SSH session, does not stop the run."""
    w.flag("slow_pull", "1")
    r = run_signaled(w, signal.SIGHUP, pulls_started(w))
    c.ok("exit 0", r.rc == 0, f"rc={r.rc} {r.text[-400:]}")
    c.ok("finished", "upgrade finished" in r.out)
    c.ok("log has the end", "upgrade finished" in w.log.read_text(encoding="utf-8"))


CASES: dict[str, Callable[[Checks, World], None]] = {
    name.removeprefix("case_"): fn for name, fn in sorted(globals().items())
    if name.startswith("case_") and callable(fn)
}


def stray_processes(sandbox: Path) -> list[int]:
    """Return the processes left over from a run.

    Args:
        sandbox: the run's sandbox folder.

    Returns:
        The PIDs whose command line or environment names SANDBOX.
    """
    found = []
    needle = str(sandbox).encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            seen = (proc / "cmdline").read_bytes() + (proc / "environ").read_bytes()
            if needle in seen:
                found.append(int(proc.name))
        except OSError:
            continue
    return found


def main(argv: list[str]) -> int:
    """Run the named cases, or all of them, and print one row per check.

    Args:
        argv: --keep to leave the sandbox, then case names.

    Returns:
        0 when every check passed and no process was left, else 1.
    """
    keep = "--keep" in argv
    names = [a for a in argv if a != "--keep"] or list(CASES)
    sandbox = Path(tempfile.mkdtemp(prefix="upgrade-test."))
    checks = Checks()
    try:
        for name in names:
            checks.case = name
            CASES[name](checks, build_world(sandbox))
    finally:
        strays = stray_processes(sandbox)
        for pid in strays:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not keep:
            shutil.rmtree(sandbox, ignore_errors=True)
    failed = [row for row in checks.rows if not row[2]]
    for case, name, ok, detail in checks.rows:
        if not ok:
            print(f"FAIL {case}: {name}\n     {detail[-500:]}")
    print(f"{len(checks.rows) - len(failed)}/{len(checks.rows)} checks passed "
          f"over {len(names)} cases; stray processes: {strays or 'none'}"
          f"{'; kept ' + str(sandbox) if keep else ''}")
    return 0 if not failed and not strays else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

