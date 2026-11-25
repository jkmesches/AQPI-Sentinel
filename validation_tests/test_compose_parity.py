"""docker-compose.ghcr.yml must really mirror docker-compose.prod.yml.

Run directly (no DB, no network):

    python validation_tests/test_compose_parity.py

The two files differ in exactly one intended way: prod.yml BUILDS the images,
ghcr.yml PULLS them. Its header has always promised that — "env vars, volumes,
network, ports all match" — so an operator is invited to switch between them by
changing the `-f` argument alone.

It drifted anyway, and the drift was silent and destructive. ghcr.yml
hard-coded `sentinel_archive:/data/archive` with no host-path indirection,
while production points that at an NFS share holding 22 GB of imagery. Running
ghcr.yml on that host would have attached an empty named volume instead: the
stack comes up healthy, the gallery is empty, and new captures land on the
container disk while the real archive sits unreferenced. Nothing errors,
because a fresh archive and a detached one look identical.

It was also missing the retention vars, so a switch would silently stop the
cold-storage offload that keeps the hot database bounded.

Comparing the two by hand is exactly the check nobody performs, which is why
it is here. The failure this prevents is not a crash — it is a deployment that
looks like it worked.
"""
from __future__ import annotations
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROD = ROOT / "ops" / "docker-compose.prod.yml"
GHCR = ROOT / "ops" / "docker-compose.ghcr.yml"
DEPLOY = ROOT / "ops" / "docker-compose.deploy.yml"
ENV_EXAMPLE = ROOT / "ops" / ".env.prod.example"
STORAGE_GROUP = ["${SENTINEL_STORAGE_GID:-1000}"]
RENDER_TIMEOUT_S = 30

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def split_volume(v: str) -> list[str]:
    """Split a short-form volume on ':', ignoring colons inside ${...}.

    A naive str.split(":") is wrong here and quietly so: the interpolation
    form `${SENTINEL_ARCHIVE_HOST_PATH:-sentinel_archive}` contains a colon,
    so the "source" and "target" come out as fragments of the variable. Both
    files were parsed the same wrong way, so the comparison still matched and
    the test passed — while measuring nothing. It only surfaced because a
    mutation removed the braces from one side.
    """
    parts, buf, depth = [], "", 0
    for ch in v:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == ":" and depth == 0:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    parts.append(buf)
    return parts


def volume_targets(svc: dict) -> set[str]:
    """Container-side mount points, ignoring what is bound to them."""
    out = set()
    for v in svc.get("volumes") or []:
        if isinstance(v, str):
            parts = split_volume(v)
            if len(parts) >= 2:
                out.add(parts[1])
        elif isinstance(v, dict) and v.get("target"):
            out.add(v["target"])
    return out


def volume_sources(svc: dict) -> dict[str, str]:
    """target -> source, to catch a hard-coded source where prod uses a var."""
    out = {}
    for v in svc.get("volumes") or []:
        if isinstance(v, str):
            parts = split_volume(v)
            if len(parts) >= 2:
                out[parts[1]] = parts[0]
    return out


def rendered_groups(compose: Path, gid: str | None) -> list[str] | None:
    """Render a compose file and return its backend's group_add.

    Args:
        compose: The compose file.
        gid: SENTINEL_STORAGE_GID for the render, or None to leave it unset.

    Returns:
        The rendered groups, or None when the docker CLI is not installed.
    """
    if shutil.which("docker") is None:
        return None
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SENTINEL_", "POSTGRES_"))}
    # The files refuse to render without a password; this one reaches no database.
    env["POSTGRES_PASSWORD"] = secrets.token_hex(8)
    if gid is not None:
        env["SENTINEL_STORAGE_GID"] = gid
    # An empty env file, so compose does not read a real ops/.env beside the file.
    with tempfile.NamedTemporaryFile("w", suffix=".env") as empty:
        out = subprocess.run(  # noqa: S603  fixed argument list, no shell
            ["docker", "compose", "-f", str(compose), "--env-file", empty.name,
             "config", "--format", "json"],
            env=env, capture_output=True, text=True, timeout=RENDER_TIMEOUT_S, check=True)
    groups = json.loads(out.stdout)["services"]["backend"].get("group_add") or []
    return [str(g) for g in groups]


def main() -> int:
    # The parser first: everything below is meaningless if it mis-splits.
    check("split_volume ignores the colon inside ${VAR:-default}",
          split_volume("${SENTINEL_ARCHIVE_HOST_PATH:-sentinel_archive}:/data/archive")
          == ["${SENTINEL_ARCHIVE_HOST_PATH:-sentinel_archive}", "/data/archive"],
          str(split_volume("${A:-b}:/data/archive")))
    check("split_volume handles a plain named volume",
          split_volume("sentinel_pgdata:/var/lib/postgresql/data")
          == ["sentinel_pgdata", "/var/lib/postgresql/data"])
    check("split_volume keeps a trailing mode flag separate",
          split_volume("${X:-y}:/data/backups:ro")
          == ["${X:-y}", "/data/backups", "ro"])

    prod = yaml.safe_load(PROD.read_text())
    ghcr = yaml.safe_load(GHCR.read_text())

    # --- every documented knob must actually reach a container ------------
    #
    # The checks below compare the two compose files to EACH OTHER, which
    # cannot see a variable missing from both — the same blind spot the
    # parser note above describes. v0.6.0 walked straight into it: it added
    # SENTINEL_PROFILE to ops/.env.prod.example, with a comment explaining
    # that an unknown value refuses to boot, and wired it into neither compose
    # file. Neither has an `env_file:`, so --env-file supplies ${...}
    # interpolation ONLY; a variable reaches the container solely by being
    # named in an `environment:` block. Setting the documented knob therefore
    # did nothing, and an XQPI stack would have come up healthy while
    # monitoring AQPI's product list — the exact failure config.py refuses a
    # bad VALUE to prevent, reached instead through an unset one.
    #
    # So this asserts against the example file, which is what an operator
    # actually reads, rather than against the sibling compose file.
    #
    # Vars that are legitimately interpolation-only: volume SOURCES (a host
    # path or volume name, consumed by the volumes: block, never read by the
    # process) and the image tag.
    INTERPOLATION_ONLY = {
        "SENTINEL_ARCHIVE_HOST_PATH": "volume source for /data/archive",
        "SENTINEL_COLD_HOST_PATH":    "volume source for /data/cold",
        "SENTINEL_BACKUP_HOST_PATH":  "volume source for /data/backups",
        "SENTINEL_BACKEND_HOST_PATH": "volume source for /backend",
        "SENTINEL_SSCB_HOST_PATH":    "volume source for /sscb",
        "SENTINEL_TILT_HOST_PATH":    "volume source for /tilt",
        "SENTINEL_TAG":               "selects the image tag, not process env",
        "SENTINEL_STORAGE_GID":       "the backend's group_add, not process env",
    }
    documented = set(re.findall(r"^#?((?:SENTINEL|POSTGRES)_[A-Z0-9_]+)=",
                                ENV_EXAMPLE.read_text(), re.M))
    check("the example documents the knobs we expect to find",
          len(documented) >= 15, f"{len(documented)} found")
    for label, doc in (("prod.yml", prod), ("ghcr.yml", ghcr)):
        named: set[str] = set()
        for svc in (doc.get("services") or {}).values():
            env = svc.get("environment") or {}
            named |= set(env)
            # a var interpolated INTO an environment value also arrives
            for v in env.values():
                named |= set(re.findall(r"\$\{([A-Z0-9_]+)", str(v)))
        for var in sorted(documented):
            if var in INTERPOLATION_ONLY:
                continue
            check(f"{label}: {var} reaches the container",
                  var in named,
                  "" if var in named else
                  "documented in ops/.env.prod.example but named in no "
                  "environment: block — setting it has no effect")
        # And the converse: an entry can't sit in the allowlist while also
        # being passed, or the allowlist stops describing reality.
        for var, why in sorted(INTERPOLATION_ONLY.items()):
            check(f"{label}: {var} is interpolation-only ({why})",
                  var not in named,
                  "" if var not in named else
                  "now passed to a container — drop it from INTERPOLATION_ONLY")

    # Anchor the parsed result to known-good values, not just to the other
    # file. Comparing the two files alone cannot catch a parser that mangles
    # BOTH the same way — which is exactly what str.split(':') did, and why
    # this suite passed while measuring nothing. If these literals ever need
    # changing, change them deliberately.
    # /backend and /sscb arrived with the LB1/LB2 filesystem checks in v0.5.0
    # (read-only mounts of both archives). They were added to both compose
    # files but not here, so this assertion has been red — and therefore
    # measuring nothing — since that release.
    # /recentfiles arrived in v0.8.2 with the composite input staging checks.
    # It is a THIRD share, not a subtree of /backend, and the release that
    # assumed otherwise reported eight radars healthy against a copy of the
    # tree abandoned in March 2026.
    # /tilt is radar-display's document root, for the per-elevation frames.
    # AQPI leaves it on the empty fallback volume, because that tree is
    # htdocs/ inside the /backend mount and the path derives from it. XQPI is
    # the deployment that needs the bind: FLOW's frames sit under the AQPI
    # root on the main archive, which an XQPI stack does not otherwise mount.
    EXPECTED_BACKEND_MOUNTS = {"/data/archive", "/data/cold", "/data/backups",
                               "/backend", "/sscb", "/recentfiles", "/tilt"}
    for label, doc in (("prod.yml", prod), ("ghcr.yml", ghcr)):
        got = volume_targets(doc["services"]["backend"])
        check(f"{label}: backend mounts parse to real container paths",
              got == EXPECTED_BACKEND_MOUNTS, f"got {sorted(got)}")

    # The backend joins the stores' group on a share through group_add. Unset,
    # it must render to the image user's own group (1000, a no-op), since an
    # empty entry or a name would stop the container from starting.
    deploy = yaml.safe_load(DEPLOY.read_text())
    for label, path, doc in (("prod.yml", PROD, prod), ("ghcr.yml", GHCR, ghcr),
                             ("deploy.yml", DEPLOY, deploy)):
        got_groups = doc["services"]["backend"].get("group_add")
        check(f"{label} · backend group_add as written", got_groups == STORAGE_GROUP,
              f"got {got_groups!r}")
        unset = rendered_groups(path, None)
        if unset is None:
            print(f"  skip  {label} · group_add rendered (no docker CLI)")
            continue
        check(f"{label} · group_add rendered, SENTINEL_STORAGE_GID unset",
              unset == ["1000"], f"got {unset!r}")
        given = rendered_groups(path, "54321")
        check(f"{label} · group_add rendered, SENTINEL_STORAGE_GID=54321",
              given == ["54321"], f"got {given!r}")

    for name in ("postgres", "backend", "frontend"):
        check(f"ghcr.yml defines the {name} service", name in (ghcr.get("services") or {}))
    if failures:
        # Everything below compares the two files to each other, which is only
        # meaningful once the parse and the service set are sound.
        print(f"\n{len(failures)} FAILED before comparison could start: "
              f"{', '.join(failures)}")
        return 1

    for name in ("backend", "frontend", "postgres"):
        p, g = prod["services"][name], ghcr["services"][name]

        pe, ge = set((p.get("environment") or {}).keys()), set((g.get("environment") or {}).keys())
        check(f"{name}: no environment variable is missing from ghcr.yml",
              not (pe - ge), f"missing {sorted(pe - ge)}")
        check(f"{name}: ghcr.yml adds no environment variable prod.yml lacks",
              not (ge - pe), f"extra {sorted(ge - pe)}")

        pv, gv = volume_targets(p), volume_targets(g)
        check(f"{name}: every prod mount point exists in ghcr.yml",
              not (pv - gv), f"missing {sorted(pv - gv)}")

        # The real defect: a mount that prod makes configurable but ghcr pins.
        # An operator switching files would silently detach live data.
        psrc, gsrc = volume_sources(p), volume_sources(g)
        pinned = [t for t in pv & gv
                  if "${" in psrc.get(t, "") and "${" not in gsrc.get(t, "")]
        check(f"{name}: no mount is hard-coded where prod.yml takes a host path",
              not pinned,
              "; ".join(f"{t} pinned to {gsrc[t]!r} but prod uses {psrc[t]!r}" for t in pinned))

        check(f"{name}: ports match", (p.get("ports") or []) == (g.get("ports") or []))
        check(f"{name}: networks match", (p.get("networks") or []) == (g.get("networks") or []))

    # The one intended difference, asserted so it stays the only one.
    check("prod.yml builds the backend, ghcr.yml does not",
          "build" in prod["services"]["backend"] and "build" not in ghcr["services"]["backend"])
    check("ghcr.yml pulls its backend image from GHCR",
          str(ghcr["services"]["backend"].get("image", "")).startswith("ghcr.io/"))
    check("ghcr.yml pulls its frontend image from GHCR",
          str(ghcr["services"]["frontend"].get("image", "")).startswith("ghcr.io/"))

    # Named volumes referenced by a service must be declared, or compose fails
    # at run time rather than here.
    declared = set((ghcr.get("volumes") or {}).keys())
    for name in ("backend", "postgres"):
        for v in ghcr["services"][name].get("volumes") or []:
            if isinstance(v, str):
                src = v.split(":")[0]
                if src.startswith("${") or src.startswith("/") or src.startswith("."):
                    continue
                check(f"named volume {src!r} is declared", src in declared)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall compose-parity assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
