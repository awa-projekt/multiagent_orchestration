-- @kind additive
-- Process runs and the small state values of schedules.
CREATE TABLE IF NOT EXISTS process_runs (
  id TEXT PRIMARY KEY, automation TEXT NOT NULL, status TEXT NOT NULL,
  dedup_key TEXT, tenant TEXT, version TEXT,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  ready_at TIMESTAMPTZ, due_at TIMESTAMPTZ,
  lease_owner TEXT, lease_expires_at TIMESTAMPTZ,
  wait_keys TEXT[] NOT NULL DEFAULT '{}', revision BIGINT NOT NULL DEFAULT 0,
  data JSONB NOT NULL)
-- @statement
CREATE INDEX IF NOT EXISTS process_runs_ready ON process_runs (automation, ready_at)
  WHERE status = 'queued'
-- @statement
CREATE INDEX IF NOT EXISTS process_runs_listing ON process_runs (automation, created_at DESC)
-- @statement
CREATE INDEX IF NOT EXISTS process_runs_dedup ON process_runs (automation, dedup_key)
  WHERE dedup_key IS NOT NULL
-- @statement
CREATE INDEX IF NOT EXISTS process_runs_due ON process_runs (automation, due_at)
  WHERE due_at IS NOT NULL
-- @statement
CREATE INDEX IF NOT EXISTS process_runs_leases ON process_runs (automation, lease_expires_at)
  WHERE status = 'running'
-- @statement
CREATE INDEX IF NOT EXISTS process_runs_waits ON process_runs USING gin (wait_keys)
-- @statement
CREATE TABLE IF NOT EXISTS process_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
