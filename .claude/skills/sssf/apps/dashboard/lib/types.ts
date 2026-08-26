/**
 * Maestro ledger shapes as published by the Bun visualizer API.
 * Copied from apps/visualizer/shared/types.ts so this app never imports
 * bun:sqlite or the Vue server.
 */

export type SourceKind = "sssf" | "maestro";

export interface SourceInfo {
  id: string;
  kind: SourceKind;
  path: string;
  label: string;
  journal_mode: string;
  count: number;
}

export type SourcesResponse = SourceInfo[];

export type MaestroNodeState = string;
export type MaestroRunOutcome = string;
export type MaestroLiveState = string;
export type RunLiveness =
  | "running"
  | "abandoned"
  | "unknown"
  | "not_running";
export type MaestroCancelCause = string;

export type MaestroMergeCause = string;

export interface MaestroTransition {
  node_id: string | null;
  from_state: string | null;
  to_state: string | null;
  reason: string | null;
  actor: string | null;
  created_at: string | null;
  detail: Record<string, unknown>;
}

export interface MaestroReviewFinding {
  check_id: string;
  object_id: string;
  message: string;
  blocking: boolean;
}

export type MaestroLanePhase = string;

export interface MaestroLaneCandidate {
  build_node_id: string;
  candidate_seq: number;
  candidate_sha: string;
  parent_candidate_sha: string | null;
  builder_generation: number;
  created_at: string | null;
}

export interface MaestroCandidateReview {
  review_node_id: string;
  candidate_sha: string;
  reviewer_generation: number;
  state: string;
  review_digest: string | null;
  receipt_path: string | null;
  findings: MaestroReviewFinding[];
  verdict: string | null;
  completed_at: string | null;
}

export interface MaestroRepairHandoff {
  build_node_id: string;
  rejected_candidate_sha: string;
  findings: MaestroReviewFinding[];
  state: string;
  builder_generation: number;
  submitted_at: string | null;
  acknowledged_at: string | null;
}

export type AttemptLiveness =
  | "running"
  | "stale"
  | "not_recorded"
  | "not_running"
  | "unknown";
export type AttemptIdentitySource =
  | "observed"
  | "observed_head"
  | "declared"
  | "not_recorded";

export interface MaestroAttempt {
  node_id: string;
  attempt_no: number;
  state: MaestroNodeState;
  base_sha: string | null;
  turn_count: number;
  retry_class: string | null;
  pid: number | null;
  started_at_ms: number | null;
  launched_at_ms: number | null;
  /**
   * Proven live or sitting in review. Unknown host/epoch, a foreign
   * host, or a reused pid is false — unprovable, not dead.
   */
  running: boolean;
  liveness: AttemptLiveness;
  model: string | null;
  vendor: string | null;
  model_source: AttemptIdentitySource;
  vendor_source: AttemptIdentitySource;
  declared_config_path: string | null;
  session_path: string | null;
  verdict: string | null;
  transitions: MaestroTransition[];
  review_findings: MaestroReviewFinding[];
}

export interface MaestroNode {
  node_id: string;
  kind: string | null;
  depth: number;
  needs: string[];
  outputs: string[];
  state: MaestroNodeState;
  lane_phase: MaestroLanePhase | null;
  attempt_no: number;
  block_reason: string | null;
  cancel_cause: MaestroCancelCause | null;
  merge_cause: MaestroMergeCause | null;
  output_sha: string | null;
  granted_extra_attempts: number;
  updated_at: string | null;
  attempts: MaestroAttempt[];
}

export interface MaestroIntegration {
  path: string;
  branch: string | null;
  head: string | null;
  subject: string | null;
}

export interface MaestroResult {
  node_id: string | null;
  attempt_no: number | null;
  subject_sha: string | null;
  adjudication: string | null;
  created_at: string | null;
  payload: unknown;
}

export interface MaestroRunSummary {
  run_id: string;
  plan_name: string | null;
  plan_digest: string;
  state: MaestroLiveState;
  scheduler_liveness: RunLiveness;

  declared_outcome: MaestroRunOutcome | null;
  declared_outcome_at: string | null;
  cancel_cause: MaestroCancelCause | null;
  resumable: boolean;
  cancel_requested: boolean;
  created_at: string | null;
  last_transition_at: string | null;
  node_count: number;
  node_states: { node_id: string; state: MaestroNodeState }[];
}

export interface MaestroActorSession {
  build_node_id: string;
  actor_role: string;
  generation: number;
  state: string;
  pane_id: string | null;
  session_path: string | null;
  correlation_token: string | null;
  updated_at: string | null;
}

export interface MaestroRunDetail {
  run_id: string;
  plan_name: string | null;
  plan_digest: string;
  state: MaestroLiveState;
  scheduler_liveness: RunLiveness;

  declared_outcome: MaestroRunOutcome | null;
  declared_outcome_at: string | null;
  cancel_cause: MaestroCancelCause | null;
  resumable: boolean;
  cancel_requested: boolean;
  created_at: string | null;
  last_transition_at: string | null;
  server_now_ms: number;
  integration: MaestroIntegration | null;
  nodes: MaestroNode[];
  actor_sessions: MaestroActorSession[];
  lane_candidates: MaestroLaneCandidate[];
  candidate_reviews: MaestroCandidateReview[];
  repair_handoffs: MaestroRepairHandoff[];
  results: MaestroResult[];
  run_transitions: MaestroTransition[];
}

export interface HealthResponse {
  ok: boolean;
  db: string;
  journal_mode: string;
  sessions: number;
  sources: SourceInfo[];
}

export interface ApiError {
  error: string;
}

export type FleetRun = MaestroRunSummary & {
  source_id: string;
  source_label: string;
};

export type FleetRunDetail = MaestroRunDetail & {
  source_id: string;
  source_label: string;
};
