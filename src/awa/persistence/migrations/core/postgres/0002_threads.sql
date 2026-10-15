-- @kind additive
-- Who owns which thread of a served system, and which paused states were answered.
CREATE TABLE IF NOT EXISTS server_threads (
  system TEXT NOT NULL, thread_id TEXT NOT NULL, tenant TEXT, principal TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (system, thread_id))
-- @statement
CREATE INDEX IF NOT EXISTS server_threads_by_tenant ON server_threads
  (system, tenant, created_at DESC)
-- @statement
CREATE TABLE IF NOT EXISTS server_decisions (
  system TEXT NOT NULL, thread_id TEXT NOT NULL, version TEXT NOT NULL,
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (system, thread_id, version))
