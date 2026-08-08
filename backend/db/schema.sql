-- Sentinel — Postgres 16 schema
-- See radarca-implementation-plan.md §4 for design notes.
-- Run idempotently: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- for gen_random_uuid()

-- =========================================================================
-- CORE: check runs + time-series + alarms
-- =========================================================================

CREATE TABLE IF NOT EXISTS check_runs (
  id            BIGSERIAL PRIMARY KEY,
  check_id      TEXT NOT NULL,
  target        TEXT NOT NULL,
  stage         TEXT NOT NULL,
  status        TEXT NOT NULL,
  started_at    TIMESTAMPTZ NOT NULL,
  finished_at   TIMESTAMPTZ NOT NULL,
  summary       TEXT,
  payload       JSONB,
  artifacts     TEXT[]
);
CREATE INDEX IF NOT EXISTS idx_run_check        ON check_runs(check_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_stage        ON check_runs(stage, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_target       ON check_runs(target, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_payload_gin  ON check_runs USING GIN (payload);
-- Store.latest_per_check() orders by finished_at, NOT started_at. Without a
-- matching index its plan degrades to Seq Scan + full Sort, which spills
-- ~1.4GB of temp files per call at 2M rows — that is what filled the prod
-- disk on 2026-08-01. Keep this in step with that query's ORDER BY.
CREATE INDEX IF NOT EXISTS idx_run_check_finished ON check_runs(check_id, finished_at DESC);
-- Retention sweeps delete by age across the whole table; without this they
-- Seq Scan to find the cutoff.
CREATE INDEX IF NOT EXISTS idx_run_finished     ON check_runs(finished_at);
CREATE INDEX IF NOT EXISTS idx_ms_ts            ON metric_samples(ts);

CREATE TABLE IF NOT EXISTS metric_samples (
  ts            TIMESTAMPTZ NOT NULL,
  check_id      TEXT NOT NULL,
  target        TEXT NOT NULL,
  metric        TEXT NOT NULL,
  value         DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ms_lookup ON metric_samples(check_id, target, metric, ts DESC);

CREATE TABLE IF NOT EXISTS alarms (
  id            BIGSERIAL PRIMARY KEY,
  check_id      TEXT NOT NULL,
  target        TEXT NOT NULL,
  stage         TEXT NOT NULL,
  severity      TEXT NOT NULL,
  opened_at     TIMESTAMPTZ NOT NULL,
  closed_at     TIMESTAMPTZ,
  suppressed_by TEXT,
  message       TEXT NOT NULL,
  payload       JSONB
);
CREATE INDEX IF NOT EXISTS idx_alarms_open   ON alarms(opened_at DESC) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alarms_target ON alarms(target, opened_at DESC);

-- =========================================================================
-- IMAGE ARCHIVE (Tier 0 / cross-cutting)
-- =========================================================================

CREATE TABLE IF NOT EXISTS image_archive (
  sha256        TEXT PRIMARY KEY,
  ext           TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  width         INTEGER,
  height        INTEGER,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at  TIMESTAMPTZ NOT NULL,
  origin_url    TEXT
);

CREATE TABLE IF NOT EXISTS image_observations (
  ts            TIMESTAMPTZ NOT NULL,
  product_id    TEXT NOT NULL,
  scan_ts       TIMESTAMPTZ NOT NULL,
  sha256        TEXT NOT NULL REFERENCES image_archive(sha256),
  PRIMARY KEY (product_id, scan_ts)
);
CREATE INDEX IF NOT EXISTS idx_obs_recent ON image_observations(product_id, ts DESC);

-- Source-path → sha256 lookup. Lets us serve an archived image when the
-- frontend requests it by the same `source` string the L4 check originally
-- captured. Many sources map to one sha256 (dedup via pHash-equal scans).
CREATE TABLE IF NOT EXISTS image_index (
  source        TEXT PRIMARY KEY,
  sha256        TEXT NOT NULL REFERENCES image_archive(sha256),
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_image_index_sha ON image_index(sha256);

-- =========================================================================
-- ALERTING (§14)
-- =========================================================================

CREATE TABLE IF NOT EXISTS alarm_acks (
  alarm_id      BIGINT NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
  acked_by      TEXT NOT NULL,
  acked_at      TIMESTAMPTZ NOT NULL,
  note          TEXT,
  revoked_at    TIMESTAMPTZ,
  PRIMARY KEY (alarm_id, acked_at)
);

CREATE TABLE IF NOT EXISTS notification_log (
  id              BIGSERIAL PRIMARY KEY,
  alarm_id        BIGINT NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
  sent_at         TIMESTAMPTZ NOT NULL,
  receiver        TEXT NOT NULL,
  channel         TEXT NOT NULL,
  escalation_step INTEGER NOT NULL,
  template        TEXT,
  body_excerpt    TEXT,
  delivery_status TEXT NOT NULL,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_alarm ON notification_log(alarm_id, sent_at DESC);

CREATE TABLE IF NOT EXISTS silences (
  id            TEXT PRIMARY KEY,
  matchers      JSONB NOT NULL,
  starts        TIMESTAMPTZ NOT NULL,
  ends          TIMESTAMPTZ NOT NULL,
  reason        TEXT,
  created_at    TIMESTAMPTZ NOT NULL,
  created_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_silences_window ON silences(starts, ends);

CREATE TABLE IF NOT EXISTS ack_tokens (
  token         TEXT PRIMARY KEY,
  alarm_id      BIGINT NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
  issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,
  used_at       TIMESTAMPTZ
);

-- =========================================================================
-- LAYER 4 — TIER 1 BASELINES + TIER 5 CORPUS (§15)
-- =========================================================================

CREATE TABLE IF NOT EXISTS metric_baselines (
  check_id      TEXT NOT NULL,
  target        TEXT NOT NULL,
  metric        TEXT NOT NULL,
  hour_of_day   SMALLINT NOT NULL,
  mean          DOUBLE PRECISION NOT NULL,
  stddev        DOUBLE PRECISION NOT NULL,
  n_samples     INTEGER NOT NULL,
  computed_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (check_id, target, metric, hour_of_day)
);

CREATE TABLE IF NOT EXISTS image_embeddings (
  sha256        TEXT NOT NULL REFERENCES image_archive(sha256) ON DELETE CASCADE,
  model_id      TEXT NOT NULL,
  embedding     BYTEA NOT NULL,
  computed_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (sha256, model_id)
);

CREATE TABLE IF NOT EXISTS image_labels (
  id            BIGSERIAL PRIMARY KEY,
  sha256        TEXT NOT NULL REFERENCES image_archive(sha256) ON DELETE CASCADE,
  label         TEXT NOT NULL,
  confidence    REAL NOT NULL DEFAULT 1.0,
  source        TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL,
  created_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_labels_sha   ON image_labels(sha256);
CREATE INDEX IF NOT EXISTS idx_labels_label ON image_labels(label);

-- =========================================================================
-- AUTH + AUDIT (§17)
-- =========================================================================

CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT,
  role          TEXT NOT NULL DEFAULT 'admin',
  display_name  TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at TIMESTAMPTZ,
  disabled_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sessions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,
  user_agent    TEXT,
  ip            INET
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
  token         TEXT PRIMARY KEY,
  user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,
  used_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS admin_audit (
  id            BIGSERIAL PRIMARY KEY,
  at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_email    TEXT NOT NULL,
  action        TEXT NOT NULL,
  target        TEXT,
  payload       JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_user   ON admin_audit(user_email, at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON admin_audit(action, at DESC);

-- Web Push subscriptions for the mobile PWA. One row per (user, device).
-- The endpoint URL is the unique identifier; iOS, Android, and desktop
-- browsers all produce different endpoints even for the same logical user.
--
-- `routing_config` is a per-device filter blob (see backend/push.py) with:
--   severity_floor:    "info" | "warn" | "critical"
--   product_patterns:  string[]  glob-style match against check_id / target
--   schedule:          group-schedule shape (see backend/groups.py) — when
--                      the device is on-duty
-- Empty object = "match everything" (current behavior pre-Group 11).
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id             BIGSERIAL PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint       TEXT NOT NULL UNIQUE,
  p256dh         TEXT NOT NULL,
  auth           TEXT NOT NULL,
  user_agent     TEXT,
  label          TEXT,                                 -- user-friendly device name (iPhone, etc.)
  routing_config JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);
-- Make the upgrade safe: existing deployments need the new columns.
ALTER TABLE push_subscriptions
  ADD COLUMN IF NOT EXISTS label TEXT,
  ADD COLUMN IF NOT EXISTS routing_config JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Generic settings key→JSON store. Used for SMTP config, alert routing
-- snapshot, future runtime knobs. Read on demand; write via admin API.
CREATE TABLE IF NOT EXISTS settings (
  key          TEXT PRIMARY KEY,
  value        JSONB NOT NULL,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by   TEXT
);

-- =========================================================================
-- GROUPS (Issue plan: /admin/groups)
-- =========================================================================
--
-- A group bundles users + a notification schedule. Schedules can be weekly
-- (active days/hours), biweekly with an anchor date, or always-on; downtime
-- windows can be layered on top. Inheritance: child group can name a
-- parent_group_id and the effective schedule is the union of both. Used
-- by the alert-routing recipient flow to determine whether to dispatch
-- given a wall-clock time.
--
-- See backend/groups.py for the schedule shape + evaluator. JSON is opaque
-- here so future schedule features (rotations, holidays, on-call swaps)
-- don't require migrations.
CREATE TABLE IF NOT EXISTS groups (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  description     TEXT NOT NULL DEFAULT '',
  parent_group_id BIGINT REFERENCES groups(id) ON DELETE SET NULL,
  schedule        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_group_id);

CREATE TABLE IF NOT EXISTS group_members (
  group_id BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  user_id  BIGINT NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
  added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  added_by TEXT,
  PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
