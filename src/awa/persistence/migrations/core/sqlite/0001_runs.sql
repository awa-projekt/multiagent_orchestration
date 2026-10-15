-- @kind additive
-- Process runs and the small state values of schedules. A run's wait keys are a JSON list in
-- process_runs and one row each in process_run_wait_keys (what the GIN index is in PostgreSQL).
CREATE TABLE IF NOT EXISTS process_runs (
  id TEXT PRIMARY KEY, automation TEXT NOT NULL, status TEXT NOT NULL,
  dedup_key TEXT, tenant TEXT, version TEXT,
  created_at TEXT NOT NULL CHECK (created_at LIKE '____-__-__T%Z'),
  updated_at TEXT NOT NULL CHECK (updated_at LIKE '____-__-__T%Z'),
  ready_at TEXT, due_at TEXT,
  lease_owner TEXT, lease_expires_at TEXT,
  wait_keys TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(wait_keys)),
  revision INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL CHECK (json_valid(data)))
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
CREATE TABLE IF NOT EXISTS process_run_wait_keys (
  run_id TEXT NOT NULL REFERENCES process_runs(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  PRIMARY KEY (run_id, key))
-- @statement
CREATE INDEX IF NOT EXISTS process_run_wait_keys_key ON process_run_wait_keys (key)
-- @statement
CREATE TABLE IF NOT EXISTS process_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL,
  updated_at TEXT NOT NULL CHECK (updated_at LIKE '____-__-__T%Z'))
