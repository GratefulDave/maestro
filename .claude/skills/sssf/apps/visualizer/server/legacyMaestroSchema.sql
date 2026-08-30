CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  plan_digest TEXT,
  plan_name TEXT,
  created_at TEXT,
  last_transition_at TEXT,
  latest_outcome TEXT,
  latest_outcome_at TEXT,
  cancel_cause TEXT,
  cancel_requested INTEGER,
  scheduler_pid INTEGER,
  scheduler_host TEXT,
  scheduler_start_epoch INTEGER
);
CREATE TABLE IF NOT EXISTS dag_nodes (
  run_id TEXT,
  node_id TEXT,
  plan_digest TEXT,
  kind TEXT,
  depth INTEGER,
  needs_json TEXT,
  outputs_json TEXT,
  specs_json TEXT
);
CREATE TABLE IF NOT EXISTS node_lifecycle (
  run_id TEXT,
  node_id TEXT,
  state TEXT,
  lane_phase TEXT,
  attempt_no INTEGER,
  block_reason TEXT,
  cancel_cause TEXT,
  merge_cause TEXT,
  output_sha TEXT,
  granted_extra_attempts INTEGER,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
  run_id TEXT,
  node_id TEXT,
  attempt_no INTEGER,
  base_sha TEXT,
  state TEXT,
  started_at REAL,
  launched_at REAL,
  pid INTEGER,
  attempt_host TEXT,
  attempt_start_epoch REAL,
  turn_count INTEGER,
  retry_class TEXT,
  extra_json TEXT
);
CREATE TABLE IF NOT EXISTS lane_candidates (
  run_id TEXT,
  build_node_id TEXT,
  candidate_seq INTEGER,
  candidate_sha TEXT,
  parent_candidate_sha TEXT,
  builder_generation INTEGER,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS candidate_reviews (
  run_id TEXT,
  review_node_id TEXT,
  candidate_sha TEXT,
  reviewer_generation INTEGER,
  state TEXT,
  dispatched_at TEXT,
  review_digest TEXT,
  receipt_path TEXT,
  findings_json TEXT,
  verdict TEXT,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS repair_handoffs (
  run_id TEXT,
  build_node_id TEXT,
  rejected_candidate_sha TEXT,
  findings_json TEXT,
  state TEXT,
  builder_generation INTEGER,
  submitted_at TEXT,
  acknowledged_at TEXT
);
CREATE TABLE IF NOT EXISTS actor_sessions (
  run_id TEXT,
  build_node_id TEXT,
  actor_role TEXT,
  generation INTEGER,
  state TEXT,
  pane_id TEXT,
  tab_id TEXT,
  session_path TEXT,
  correlation_token TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  node_id TEXT,
  kind TEXT,
  from_state TEXT,
  to_state TEXT,
  reason TEXT,
  actor TEXT,
  detail_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  node_id TEXT,
  attempt_no INTEGER,
  subject_sha TEXT,
  payload_json TEXT,
  adjudication TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS orphans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  node_id TEXT,
  attempt_no INTEGER,
  pid INTEGER,
  handle TEXT,
  reason TEXT,
  created_at TEXT
);
