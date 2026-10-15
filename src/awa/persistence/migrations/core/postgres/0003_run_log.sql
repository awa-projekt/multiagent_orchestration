-- @kind additive
-- The run log: steps, attempts and results of process runs in order, approvals, opt-in
-- captured content (with its reads) and the HMAC links from people to their runs.
CREATE TABLE IF NOT EXISTS run_log_head (
  run_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  capture BOOLEAN NOT NULL,
  opened_at TIMESTAMPTZ NOT NULL)
-- @statement
CREATE INDEX IF NOT EXISTS run_log_head_scope ON run_log_head (tenant_id, project_id, opened_at)
-- @statement
CREATE INDEX IF NOT EXISTS run_log_head_opened ON run_log_head (opened_at)
-- @statement
CREATE TABLE IF NOT EXISTS run_step (
  run_id TEXT NOT NULL REFERENCES run_log_head (run_id) ON DELETE CASCADE,
  step_key TEXT NOT NULL CHECK (step_key <> ''),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  status TEXT NOT NULL CHECK (status IN ('pending','started','checking','needs_approval','checked','retrying','failed','skipped','rejected','cancelled')),
  title TEXT NOT NULL DEFAULT '', agent_name TEXT, model TEXT, rule_id TEXT,
  message TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ,
  input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0,
  cache_read_tokens BIGINT NOT NULL DEFAULT 0, cache_write_tokens BIGINT NOT NULL DEFAULT 0,
  cost_micros BIGINT,
  error_type TEXT,
  revision INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, step_key),
  UNIQUE (run_id, ordinal),
  CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at))
-- @statement
CREATE INDEX IF NOT EXISTS run_step_scope ON run_step (tenant_id, project_id, status)
-- @statement
CREATE TABLE IF NOT EXISTS step_attempt (
  run_id TEXT NOT NULL, step_key TEXT NOT NULL,
  attempt INTEGER NOT NULL CHECK (attempt >= 1),
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','cancelled')),
  started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
  model TEXT, agent_run_id TEXT, retry_reason TEXT, error_type TEXT,
  input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0,
  cache_read_tokens BIGINT NOT NULL DEFAULT 0, cache_write_tokens BIGINT NOT NULL DEFAULT 0,
  cost_micros BIGINT,
  PRIMARY KEY (run_id, step_key, attempt),
  FOREIGN KEY (run_id, step_key) REFERENCES run_step (run_id, step_key) ON DELETE CASCADE)
-- @statement
CREATE TABLE IF NOT EXISTS step_result (
  result_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL, step_key TEXT NOT NULL,
  attempt INTEGER NOT NULL CHECK (attempt >= 1),
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  kind TEXT NOT NULL CHECK (kind IN ('intermediate','final')),
  output JSONB,
  answer_length INTEGER NOT NULL DEFAULT 0, answer_ref TEXT,
  tool_calls JSONB NOT NULL DEFAULT '[]',
  checks JSONB NOT NULL DEFAULT '[]',
  created_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY (run_id, step_key) REFERENCES run_step (run_id, step_key) ON DELETE CASCADE)
-- @statement
CREATE INDEX IF NOT EXISTS step_result_run ON step_result (run_id, step_key, attempt, created_at)
-- @statement
CREATE UNIQUE INDEX IF NOT EXISTS step_result_one_final ON step_result (run_id, step_key)
  WHERE kind = 'final'
-- @statement
CREATE TABLE IF NOT EXISTS approval (
  approval_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES run_log_head (run_id) ON DELETE CASCADE,
  step_key TEXT,
  result_id TEXT REFERENCES step_result (result_id) ON DELETE SET NULL,
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  requested_at TIMESTAMPTZ NOT NULL,
  reasons JSONB NOT NULL DEFAULT '[]',
  decision TEXT CHECK (decision IN ('approved','rejected','changes_requested')),
  reviewer TEXT, reason TEXT, decided_at TIMESTAMPTZ,
  CHECK ((decision IS NULL) = (decided_at IS NULL)),
  CHECK (decision IS NULL OR reviewer IS NOT NULL))
-- @statement
CREATE INDEX IF NOT EXISTS approval_run ON approval (run_id, requested_at)
-- @statement
CREATE INDEX IF NOT EXISTS approval_open ON approval (tenant_id, project_id)
  WHERE decision IS NULL
-- @statement
CREATE TABLE IF NOT EXISTS captured_content (
  content_ref TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES run_log_head (run_id) ON DELETE CASCADE,
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  kind TEXT NOT NULL CHECK (kind IN
    ('system','input_messages','output_messages','tool_arguments','tool_result')),
  body JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL)
-- @statement
CREATE INDEX IF NOT EXISTS captured_content_run ON captured_content (run_id)
-- @statement
CREATE TABLE IF NOT EXISTS captured_content_read (
  read_id TEXT PRIMARY KEY,
  content_ref TEXT NOT NULL REFERENCES captured_content (content_ref) ON DELETE CASCADE,
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  reader_hmac TEXT,
  read_at TIMESTAMPTZ NOT NULL)
-- @statement
CREATE INDEX IF NOT EXISTS captured_content_read_ref ON captured_content_read (content_ref)
-- @statement
CREATE TABLE IF NOT EXISTS data_subject_link (
  tenant_id TEXT NOT NULL CHECK (tenant_id <> ''), project_id TEXT NOT NULL CHECK (project_id <> ''),
  subject_hmac TEXT NOT NULL CHECK (length(subject_hmac) = 64),
  key_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL CHECK (ref_kind IN ('run','approval')),
  ref_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES run_log_head (run_id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (tenant_id, project_id, subject_hmac, ref_kind, ref_id))
-- @statement
CREATE INDEX IF NOT EXISTS data_subject_link_run ON data_subject_link (run_id)
