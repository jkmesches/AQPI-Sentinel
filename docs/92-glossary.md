# Glossary

Vocabulary that shows up across the docs and the UI. If a term in
Sentinel's interface ever surprised you, look here first.

---

## Stages

Sentinel groups checks into five **stages**, ordered from "the
foundation" to "the visible outputs." When a stage at the bottom
fails, every stage above it usually fails too (the [cascade
demote](#cascade-demote) collapses this into a single root-cause
display).

| Stage ID | Descriptor (user-facing) | What it watches |
|---|---|---|
| `L0` | **Connectivity** | The upstream's own reachability — origin, public dashboard page, root URL, TLS certificate. Also "is Sentinel's own internet + DNS OK?" via the Sentinel Internet / Sentinel DNS checks. |
| `L1` | **Product Freshness** | Each upstream product's manifest + latest image + step count. Per-product cadence. |
| `L2` | **Radar Scans** | Per-radar reconciliation: does what the upstream's status flag says agree with what the imagery shows? Detects GHOST_UP, CONFIRMED_DOWN, STUCK_DOWN_FLAG. |
| `L3` | **Map Overlays** | Playwright-driven page-load check of the JS overlay pipeline + cross-product parity (do composite overlays match the underlying products?). |
| `L4-T1T2` | **Image Quality** | Per-radar + per-mosaic image QC. Coverage, autocorrelation, content extent, frozen detection. |

The user-facing descriptors land in the dashboard ("Connectivity"
instead of "L0"); the IDs (`L0`, etc.) are the canonical
identifiers used in routing rules, history filters, and API calls.

---

## Check vs alarm

| Term | Definition |
|---|---|
| **Check** | A class (`backend/checks/*.py`) that knows how to probe one thing. Has an `id`, a `target`, a `cadence_s`, and a `run(ctx)` method. |
| **Check run** | One tick of one check. Stored in the `check_runs` table. Produces a [CheckResult](#checkresult). |
| **Alarm** | A row in the `alarms` table opened when a check returns a non-pass status. Has its own lifecycle (open → acked → closed) independent of the underlying check runs. |

A check can produce many runs without ever opening an alarm (every
run was pass). An alarm corresponds to a span of consecutive
non-pass runs.

---

## CheckResult

The envelope a check returns each tick. Five fields matter for
operators:

| Field | Type | Notes |
|---|---|---|
| `status` | `pass`/`warn`/`fail`/`error`/`skip` | The raw signal — see [Status vs severity](#status-vs-severity). |
| `summary` | string | One-line human-readable description. Surfaced in the dashboard, drilldowns, and email subjects. |
| `payload` | jsonb | Structured data for the drilldown: sub-check verdicts, parsed metrics, source URLs. |
| `metrics` | dict | Numeric metrics (counts, latencies, ages) for sparklines + threshold-based detection. |
| `started_at` / `finished_at` | timestamps | When the run started and ended. |

---

## Status vs severity

These two vocabularies are easy to confuse. They describe different
things:

| Term | Where | Values | Means |
|---|---|---|---|
| **Status** | Returned by a check; stored on `check_runs.status` | `pass` `warn` `fail` `error` `skip` | The raw signal the check produced. |
| **Severity** | Computed on `alarms.severity` | `info` `warn` `critical` | The rolled-up alarm level, used for routing + display. |

The mapping (`backend/alarms/router.py`, v0.1.2+):

| Status | Initial severity | Operational tier |
|---|---|---|
| `warn` | `info` | Attention — degraded but not broken |
| `fail` | `warn` → `critical` after 30 min | Action — broken |
| `error` | `warn` → `critical` after 30 min | Action — check crashed (transport / parse) |
| `pass`/`skip` | — | No alarm opens |

This aligns `severity_floor` with operational priority:

- `info` (or unset) → notify on everything
- `warn` → notify on broken (fail / error) only
- `critical` → notify only on long-running outages (> 30 min open)

**Routing rules can match on either.** The `status` filter sees the
raw signal (5 values); the `severity floor` filter sees the
rolled-up alarm level (3 values). The on-page cheat-sheet at the top
of `/admin/alerts` makes the mapping visible.

---

## Cascade demote

When an upstream dependency is unhealthy, downstream checks would
all fail too — for the same root reason. **Cascade demote**
collapses this cascade so the dashboard shows the actual root cause:

- The unhealthy upstream stays red.
- Every downstream check that would otherwise be red is demoted to
  `status=skip` (gray cell).
- The demoted runs preserve their original status + summary in
  `payload.original_status` and `payload.original_summary` for the
  drilldown.

Implementation: `backend/scheduler.py:_maybe_demote_for_unhealthy_dep`.
Walks the [`depends_on` graph](#depends_on-dependency-graph) at run
time and demotes if any ancestor's latest status is `fail`/`error`.

UI hints:

- Demoted cells get a small `↑` badge on `/m/uptime` so you can
  distinguish "cascade-suppressed" from "intrinsically skipped"
  (e.g. a forecast product whose step-count sub-check legitimately
  doesn't apply).
- The summary reads `Upstream "Origin reachable" unhealthy` (using
  the friendly check label) rather than the raw check_id.

---

## depends_on (dependency graph)

Each check declares its direct dependencies via the
`depends_on: list[str]` class attribute. The graph drives:

1. [**Cascade demote**](#cascade-demote) — downstream fails are
   collapsed to skip when an upstream is unhealthy.
2. **Topological stagger** — at startup, checks fire in
   dependency order (ranks `rank * 2s` apart) so the cascade-demote
   pass has authoritative upstream state on tick 1.
3. **Await-upstream gate** — before each tick runs, the scheduler
   waits (bounded 10s) for any transitive upstream that's mid-run.
4. **Alarm suppression** — `Alarm.suppressed_by` is set at open
   time via `backend/alarms/suppression.py`, gates email
   escalation.

The current L0 / L1 / L2 / L3 / L4 dependency tree is documented in
[`ARCHITECTURE.md`](ARCHITECTURE.md) and visible at runtime via
`GET /api/checks` (each check's `depends_on` field).

---

## Threshold registry

The central store for every detection knob. Lives at
`settings.thresholds` (a single jsonb row in Postgres).

Three-tier fallback when reading a threshold:

1. **DB** — the row at `settings.thresholds`, edited via
   `/admin/thresholds`.
2. **`config.py`** — the defaults shipped with the code.
3. **Hard-coded** — last-resort sentinel values inside the check.

The first non-null hit wins. So a fresh deploy "just works"
without editing thresholds (everything reads from `config.py`); an
admin edit promotes that knob to DB-controlled.

In-process cache lives in `backend/thresholds.py`; an atomic swap
on save means **no restart is needed** to apply threshold changes.

---

## Retroactive reprocess

After a threshold change, historical `check_runs` still carry
verdicts under the OLD threshold. The retroactive reprocess job
(`/admin/thresholds` → bottom panel) walks rows in a chosen time
window and re-classifies each row under the **current** thresholds.

What it does:

- L1 — recomputes `C_freshness`, `E_step_count`, `G_image_size`
  sub-checks from the saved payload.
- L2 — recomputes HEALTHY ↔ GHOST_UP from
  `primary_age_s` vs current `silent_fail_s + hysteresis`.
- L4 — lifts extreme/frozen tier-2 verdicts under current
  `extreme_threshold` / `frozen_min_cov_pct` / etc.

What it preserves:

- Rows where `payload.reason in {local_dns_error, transport_error}`
  — transport failures aren't reclassified as heuristic verdicts.
- Rows whose original status was `skip` or `error`.

Cancellable mid-run; live progress display.

---

## Suppression (alarm-level)

Distinct from [cascade demote](#cascade-demote), which is
status-level.

When an alarm opens, `backend/alarms/suppression.py` walks the
[`depends_on`](#depends_on-dependency-graph) graph and finds the
first unhealthy ancestor. That ancestor's check_id is stored in
`alarms.suppressed_by`. **A suppressed alarm doesn't escalate** —
no email, no push. It still records to the timeline (audit trail
intact), it just doesn't notify.

Cascade demote and alarm suppression are belt-and-suspenders: the
former prevents the alarm from opening at all in most cases; the
latter is the safety net if it does.

---

## Push routing (per device)

Web Push notifications are routed **per device**, not per user. Each
push subscription in `push_subscriptions.routing_config` carries:

| Knob | What it does |
|---|---|
| **Severity floor** | Drop notifications below this level (`info+`/`warn+`/`critical only`). |
| **Match patterns** | Substring patterns (OR-combined) against alarm `check_id`/`target`/`body`. Empty = match everything. |
| **Delay** | Wait N minutes before delivering. Smart-delay: if the alarm self-resolves or is acked during the wait, the notification is dropped. Useful for "page me only if it hasn't fixed itself in N minutes." |
| **On-duty schedule** | Same shape as group schedules — only deliver during this window. |

**Always-pass safeguard:** L0 alarms and any check with `.canary` in
its id **bypass the match-pattern filter** unconditionally. The
severity floor + schedule still apply. This prevents an
over-narrow allowlist from silencing the most-critical class of
alarm (origin down).

UI at `/m/push-settings/edit/<id>` (mobile) and `/settings/devices`
(desktop) — same backing data.

---

## Group, schedule, downtime

A **group** is a named bundle of users plus a notification
**schedule**. When an alarm dispatches, the recipient's `group_ids`
are expanded to currently-on-duty members per the schedule.

| Concept | Definition |
|---|---|
| **Schedule** | `always` / `weekly` / `biweekly` (with anchor date). Defines on-duty windows. |
| **Time window** | A start/end pair (e.g. `09:00 → 17:00`). Overnight wraps midnight. |
| **One-off downtime** | A specific date range that suppresses notifications regardless of schedule. Vacations, planned absence. |
| **Recurring downtime** | A weekly-repeating quiet window. Nightly, lunch hours, weekends. |
| **Parent group** | A group can reference a parent. Both schedules apply (AND-merged). |

Off-duty members are silently skipped at dispatch time. **Dedup
across overlapping groups + direct recipients** ensures one
notification per email address per step.

---

## Sparklines

Each row on the dashboard has a small inline trace showing recent
data-arrival rate.

- **X axis** — wall-clock time within a [cadence](#cadence)-derived
  window (30m / h / 3h / 6h / 12h, auto-picked per check).
- **Y axis** — **sample-arrival rate** per bucket, NOT the raw
  metric value. When data stops flowing, the trace falls to zero.
- **Inline label** — `N/window`, e.g. `16/h` or `0/6h`.

A trace at baseline means "no data arrived in the displayed
window." Distinct from "absent" (empty SVG) — empty traces happen
when there's no historical data at all (fresh deploy).

---

## Cadence

How often a check is scheduled to run. Set on each check class as
`cadence_s` (seconds). Examples:

- L0 connectivity: 60 s.
- L1 product (radar-derived): 60 s.
- L2 radar: 120 s.
- L1 forecast: 1800 s (30 min).
- L1 max-water: 3600 s (1 h).

Sparkline window auto-adapts to the cadence
([`format.ts:windowFromCadence`](https://github.com/jkmesches/AQPI-Sentinel/blob/main/frontend/src/lib/format.ts)).
The sparkline plots ~30 cadence intervals at a glance, snapping the
window to a natural unit so the inline label reads cleanly.

---

## Silence

A temporary mute rule. While active, **alarms matching the silence
still record but don't notify**. Used for known-flaky windows,
planned maintenance, ongoing investigations.

A silence has:

- **Matchers** — key=value pairs (stage / check / target /
  severity / suppressed_by / ...). AND-combined; matches an alarm
  if every pair matches.
- **Time window** — start + end (UTC, with admin-UI input toggle
  for local entry).
- **Reason** — free-text audit string.

UI: `/admin/silences`. Presets cover the common cases (All / All
Radars / All Products / All Website) one-click.

---

## Stale skip vs cascade skip vs intrinsic skip

Three reasons a check can be `status=skip`. They look identical in
the timeline grid — the drilldown's `payload.reason` distinguishes
them:

| `payload.reason` | Means | Effect on an open alarm |
|---|---|---|
| `upstream_unhealthy` | [Cascade demote](#cascade-demote) — an ancestor was unhealthy at this run's `finished_at`. | **None** — inconclusive |
| `local_dns_error` | Local DNS resolution flake. Detected via `_is_local_dns_error` or `_summary_looks_like_dns`. | **None** — inconclusive |
| `local_network_offline` | Network monitor said the host's own internet was down. | **None** — inconclusive |
| (not set) | Intrinsic skip — the check legitimately couldn't assess (forecast products skipping step-count sub-checks, etc.). | **Closes it** |

The first three are **inconclusive**: the check declined to judge, so
the previous verdict still stands and an open alarm is left alone. The
fourth is a real observation that nothing is wrong. Treating an
inconclusive skip as recovery makes a permanently-down target
re-alert every few hours — see
[Inconclusive skips](16-alarm-engine.md#inconclusive-skips-are-not-recovery).
The set lives in `alarms.suppression.INCONCLUSIVE_SKIP_REASONS`.

On `/m/uptime`, cascade-demoted cells get a small `↑` badge to
distinguish them from intrinsic skips at a glance.

On the bucketed timeline, a cell is drawn from its **composition**, not just
its worst status: the defect band at the bottom, then skips in grey, then the
share that really passed. `/api/history/timeline` sends `n_fail`, `n_error`,
`n_warn` and `n_skip` alongside `n` to make that possible. Before v0.3.0 the
remainder above a defect band was assumed to be `pass`, and any bucket whose
worst status was `pass` was drawn as one flat green block — so a bucket where
we had stopped being able to see anything looked exactly like a healthy one.
Measured over 24 h at 1 h grain, 59 of 1,093 buckets were affected.

---

## Ack, unack, close

The alarm lifecycle:

- **Open** — alarm is fresh, not yet acknowledged. Default
  notification cadence applies.
- **Acked** — a user clicked the ack button. Repeats stop firing
  even if the alarm is still open. Tracked in `alarm_acks` (one
  row per ack event, supports unack-and-re-ack).
- **Unacked** — a previously-acked alarm got un-acked. Repeats
  resume. Used when an ack was premature.
- **Closed** — the underlying check returned to `pass` or an
  *intrinsic* skip. The alarm's `closed_at` is set. Closed alarms
  keep their history visible in `/history` but stop participating in
  routing. An inconclusive skip does not close an alarm.

The row records `acked_by`, but the ack is **not** scoped to that
user: `alarm_acks` is keyed on `alarm_id` alone, so one ack silences
the alarm for everyone. That is intentional — the operator who acks
is "owning" it.

An ack suppresses every channel. `engine._process` checks `is_acked`
before it resolves a route, so an acked alarm dispatches nothing on
email, console or webhook at any severity, `repeat_interval` re-sends
included. Web push fires only on `alarm_open` so it never repeats;
its deferred send re-checks the ack (and, since v0.2.1, respects a
revocation, so un-acking really does restore paging).

!!! warning "An ack binds to the alarm row, not to the check"
    Anything that closes and re-opens an alarm discards the ack, and
    the replacement pages again. This is why acking a
    known-down target used to have no lasting effect: a cascade
    demote closed the alarm within hours and the next one arrived
    clean. Fixed in v0.2.1. To suppress something for a *planned*
    window regardless of alarm churn, use a
    [silence](#silence) instead.

---

## VAPID

The protocol that authenticates push notifications. Sentinel
auto-generates a VAPID keypair on first start, stores it in
`settings.vapid_keys` (jsonb), and uses it to sign every push.

**Subscriptions are bound to the VAPID public key.** Regenerating
the keypair invalidates every existing subscription — each device
silently stops receiving pushes until the user re-subscribes via
`/m/more`.

Re-generation scenarios: suspected compromise, moving Sentinel to
a new public URL.

---

## Stage descriptor

The user-facing name for a stage. Different from the canonical ID:

| Canonical ID | Descriptor |
|---|---|
| `L0` | Connectivity |
| `L1` | Product Freshness |
| `L2` | Radar Scans |
| `L3` | Map Overlays |
| `L4-T1T2` | Image Quality |

Stored in `backend/stages.py:stage_descriptor()` and mirrored in
`frontend/src/lib/format.ts:stageLabel()`. Edit both surfaces if
you ever rename a stage.

---

## Where to go from here

- **[`01-getting-started.md`](01-getting-started.md)** — first
  install, if you're new to Sentinel.
- **[`03-administration.md`](03-administration.md)** — the
  admin-UI walkthroughs that this glossary supports.
- **[`05-troubleshooting.md`](05-troubleshooting.md)** — symptom
  recipes for when the system isn't behaving.
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — under-the-hood
  walkthrough of how a check flows end-to-end.
