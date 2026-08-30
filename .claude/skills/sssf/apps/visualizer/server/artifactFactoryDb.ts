/**
 * Schema-versioned read adapter for artifact-factory ledgers.
 *
 * The factory writes `dag_lanes` / `lane_state` / `lane_artifacts` /
 * `run_artifacts` / `transitions`. This module maps those rows onto the
 * existing reporting types. It does not alias the legacy tables, does not
 * invent attempts, and never copies `payload_json` through to the API.
 */
import type { Database } from "bun:sqlite";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join } from "node:path";
import type {
  MaestroIntegration,
  MaestroNode,
  MaestroResult,
  MaestroRunDetail,
  MaestroRunSummary,
  MaestroTransition,
} from "../shared/types.ts";
import { openLedgerReadonly } from "./maestroDb.ts";

export const ARTIFACT_FACTORY_SCHEMA_VERSION = "artifact-factory.v1";

export const ARTIFACT_FACTORY_TABLES = [
  "ledger_meta",
  "runs",
  "dag_lanes",
  "lane_state",
  "plan_revisions",
  "lane_artifacts",
  "run_artifacts",
  "transitions",
];

const PLAN_FILE = "maestro-plan.v1";

const WORKING_STAGES: Record<string, true> = {
  WRITING_TESTS: true,
  REVIEWING_TESTS: true,
  TESTS_SEALED: true,
  BUILDING: true,
  REVIEWING_CODE: true,
  READY_TO_MERGE: true,
};

const PUBLIC_FINDING_KINDS: Record<string, true> = {
  CODE_REVIEW: true,
  FINAL_INTEGRATION_REVIEW: true,
};

const HIDDEN_ARTIFACT_KINDS: Record<string, true> = {
  TEST_DRAFT: true,
};

const ROLE_BY_KIND: Record<string, string> = {
  LANE_PLAN: "planner",
  TEST_DRAFT: "tester",
  TEST_REVIEW: "test-reviewer",
  SEALED_TEST_BUNDLE: "tester",
  BUILDER_OUTPUT: "builder",
  CODE_REVIEW: "code-reviewer",
  INTEGRATION_MERGE: "integration",
  BASE_INVALIDATION: "scheduler",
  USER_WAIT: "operator",
  USER_DECISION: "operator",
  FINAL_INTEGRATION_REVIEW: "integration-reviewer",
  MAIN_PUBLICATION: "publisher",
  PLAN_AMENDMENT: "operator",
};

interface FactoryRunRow {
  run_id: string;
  plan_digest: string;
  plan_revision: number;
  created_at: string | null;
  updated_at: string | null;
  last_transition_at: string | null;
  integration_ref: string | null;
  target_repository_root: string | null;
}

interface FactoryLaneRow {
  lane_id: string;
  needs_json: string;
  declared_outputs_json: string;
  stage: string;
  updated_at: string | null;
}

function parseStringList(raw: string): string[] {
  try {
    const value = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is string => typeof item === "string");
  } catch {
    return [];
  }
}

function parseObject(raw: string): Record<string, unknown> {
  try {
    const value = JSON.parse(raw);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value as Record<string, unknown>;
    }
  } catch {
    /* omit */
  }
  return {};
}

function stringFields(value: unknown): Record<string, string> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const out: Record<string, string> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (typeof item === "string") out[key] = item;
  }
  return out;
}

export function roleForArtifactKind(kind: string): string | null {
  return ROLE_BY_KIND[kind] ?? null;
}

/**
 * Operator-safe artifact body. Explicit allowlist — never `payload_json`.
 */
export function publicArtifactBody(
  kind: string,
  payloadJson: string,
): Record<string, unknown> {
  const payload = parseObject(payloadJson);
  const body: Record<string, unknown> = {};
  if (payload.verdict === "PASS" || payload.verdict === "REVISE") {
    body.verdict = payload.verdict;
  }
  if (PUBLIC_FINDING_KINDS[kind] && Array.isArray(payload.findings)) {
    const findings = payload.findings
      .map(stringFields)
      .filter((item): item is Record<string, string> => item !== null);
    if (findings.length) body.findings = findings;
  }
  const role = roleForArtifactKind(kind);
  if (role) body.role = role;
  return body;
}

interface FactoryRunArtifactRow {
  artifact_kind: string;
  payload_json: string;
}

function fileStamp(path: string): string | null {
  try {
    const stat = statSync(path);
    return `${stat.mtimeMs}:${stat.size}`;
  } catch {
    return null;
  }
}

function liveStateFromStages(
  stages: string[],
  runArtifacts: FactoryRunArtifactRow[],
): string {
  if (runArtifacts.some((artifact) => artifact.artifact_kind === "MAIN_PUBLICATION")) {
    return "COMPLETE";
  }
  if (stages.length > 0 && stages.every((stage) => stage === "MERGED")) {
    const finalReview = runArtifacts.find(
      (artifact) => artifact.artifact_kind === "FINAL_INTEGRATION_REVIEW",
    );
    return parseObject(finalReview?.payload_json ?? "").verdict === "PASS"
      ? "PUBLISHABLE"
      : "INTEGRATION_REVIEW_PENDING";
  }
  if (stages.length === 0) return "EMPTY";
  if (stages.includes("WAITING_FOR_USER")) return "WAITING_FOR_USER";
  if (stages.some((stage) => WORKING_STAGES[stage])) return "RUNNING";
  if (stages.every((stage) => stage === "PLANNED")) return "PENDING";
  if (stages.some((stage) => stage === "PLANNED" || stage === "MERGED")) return "RUNNING";
  return "RUNNING";
}


function laneDepths(lanes: FactoryLaneRow[]): Map<string, number> {
  const needs = new Map(lanes.map((lane) => [lane.lane_id, parseStringList(lane.needs_json)]));
  const depths = new Map<string, number>();
  const visiting = new Set<string>();
  const visit = (id: string): number => {
    const cached = depths.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const deps = needs.get(id) ?? [];
    const depth = deps.length === 0 ? 0 : Math.max(0, ...deps.map(visit)) + 1;
    visiting.delete(id);
    depths.set(id, depth);
    return depth;
  };
  for (const lane of lanes) visit(lane.lane_id);
  return depths;
}

export class ArtifactFactoryDb {
  readonly schemaVersion: string;
  readonly path: string;
  readonly plansDir: string | null;
  readonly journalMode: string;
  private db: Database;
  private immutable: boolean;
  private dbStamp: string | null;
  private walStamp: string | null;
  private planNames = new Map<string, string>();
  private planNamesStamp = 0;

  constructor(path: string, plansDir: string | null = null) {
    if (!existsSync(path)) {
      throw new Error(`lifecycle.sqlite3 not found at ${path}`);
    }
    this.path = path;
    this.plansDir = plansDir;
    const opened = openLedgerReadonly(path);
    this.db = opened.db;
    this.immutable = opened.immutable;
    this.dbStamp = fileStamp(path);
    this.walStamp = fileStamp(`${path}-wal`);
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.journalMode =
      this.db.query<{ journal_mode: string }, []>("PRAGMA journal_mode").get()
        ?.journal_mode ?? "unknown";
    this.schemaVersion =
      this.db.query<{ schema_version: string }, []>(
        "SELECT schema_version FROM ledger_meta LIMIT 1",
      ).get()?.schema_version ?? ARTIFACT_FACTORY_SCHEMA_VERSION;
  }

  close(): void {
    this.db.close();
  }

  private freshDb(): Database {
    const dbStamp = fileStamp(this.path);
    const walStamp = fileStamp(`${this.path}-wal`);
    const changed = this.immutable
      ? dbStamp !== this.dbStamp || walStamp !== this.walStamp
      : walStamp !== this.walStamp;
    if (changed) {
      const opened = openLedgerReadonly(this.path);
      this.db.close();
      this.db = opened.db;
      this.immutable = opened.immutable;
      this.dbStamp = dbStamp;
      this.walStamp = walStamp;
      this.db.exec("PRAGMA busy_timeout = 5000");
    }
    return this.db;
  }

  runCount(): number {
    return this.freshDb().query<{ n: number }, []>("SELECT COUNT(*) AS n FROM runs").get()?.n ?? 0;
  }

  private planNameFor(digest: string): string | null {
    if (!this.plansDir || !existsSync(this.plansDir)) return null;
    const stamp = statSync(this.plansDir).mtimeMs;
    if (stamp !== this.planNamesStamp) {
      const fresh = new Map<string, string>();
      for (const entry of readdirSync(this.plansDir, { withFileTypes: true })) {
        if (entry.isDirectory()) {
          const file = join(this.plansDir, entry.name, PLAN_FILE);
          if (!existsSync(file)) continue;
          const hash = new Bun.CryptoHasher("sha256").update(readFileSync(file)).digest("hex");
          fresh.set(hash, entry.name);
          continue;
        }
        if (!entry.isFile()) continue;
        const file = join(this.plansDir, entry.name);
        const hash = new Bun.CryptoHasher("sha256").update(readFileSync(file)).digest("hex");
        const suffix = extname(entry.name);
        fresh.set(hash, suffix ? entry.name.slice(0, -suffix.length) : entry.name);
      }
      this.planNames = fresh;
      this.planNamesStamp = stamp;
    }
    return this.planNames.get(digest) ?? null;
  }

  private runRows(runId?: string): FactoryRunRow[] {
    const db = this.freshDb();
    const sql = `SELECT r.run_id, r.plan_digest, r.plan_revision, r.created_at, r.updated_at,
                        r.integration_ref, r.target_repository_root,
                        (SELECT MAX(t.created_at) FROM transitions t WHERE t.run_id = r.run_id)
                          AS last_transition_at
                   FROM runs r`;
    return runId
      ? db.query<FactoryRunRow, [string]>(`${sql} WHERE r.run_id = ?`).all(runId)
      : db.query<FactoryRunRow, []>(`${sql} ORDER BY r.created_at DESC, r.run_id DESC`).all();
  }

  private laneRows(runId: string, planRevision: number): FactoryLaneRow[] {
    return this.freshDb()
      .query<FactoryLaneRow, [string, number]>(
        `SELECT d.lane_id, d.needs_json, d.declared_outputs_json, s.stage, s.updated_at
           FROM dag_lanes d
           JOIN lane_state s ON s.run_id = d.run_id AND s.lane_id = d.lane_id
          WHERE d.run_id = ? AND d.plan_revision = ?
          ORDER BY d.lane_id`,
      )
      .all(runId, planRevision);
  }

  private nodesFor(runId: string, planRevision: number): MaestroNode[] {
    const lanes = this.laneRows(runId, planRevision);
    const depths = laneDepths(lanes);
    return lanes
      .map((lane) => ({
        node_id: lane.lane_id,
        kind: null,
        depth: depths.get(lane.lane_id) ?? 0,
        needs: parseStringList(lane.needs_json),
        outputs: parseStringList(lane.declared_outputs_json),
        state: lane.stage,
        lane_phase: lane.stage,
        attempt_no: 0,
        block_reason: null,
        cancel_cause: null,
        merge_cause: null,
        output_sha: null,
        granted_extra_attempts: 0,
        updated_at: lane.updated_at,
        attempts: [],
      }))
      .sort((a, b) => a.depth - b.depth || a.node_id.localeCompare(b.node_id));
  }

  private transitionsFor(runId: string): MaestroTransition[] {
    return this.freshDb()
      .query<
        {
          lane_id: string | null;
          from_stage: string | null;
          to_stage: string | null;
          reason: string;
          created_at: string | null;
          artifact_id: string | null;
        },
        [string]
      >(
        `SELECT lane_id, from_stage, to_stage, reason, created_at, artifact_id
           FROM transitions WHERE run_id = ? ORDER BY id`,
      )
      .all(runId)
      .map((row) => ({
        node_id: row.lane_id,
        from_state: row.from_stage,
        to_state: row.to_stage,
        reason: row.reason,
        actor: null,
        created_at: row.created_at,
        detail: row.artifact_id ? { artifact_id: row.artifact_id } : {},
      }));
  }

  private runArtifactsFor(runId: string): FactoryRunArtifactRow[] {
    return this.freshDb()
      .query<FactoryRunArtifactRow, [string]>(
        `SELECT artifact_kind, payload_json
           FROM run_artifacts WHERE run_id = ? ORDER BY sequence DESC`,
      )
      .all(runId);
  }

  private resultsFor(runId: string): MaestroResult[] {
    const db = this.freshDb();
    const laneRows = db
      .query<
        {
          artifact_id: string;
          lane_id: string;
          artifact_kind: string;
          completed_stage: string;
          plan_revision: number;
          input_digest: string;
          output_digest: string;
          artifact_ref: string;
          payload_json: string;
          created_at: string | null;
        },
        [string]
      >(
        `SELECT artifact_id, lane_id, artifact_kind, completed_stage, plan_revision,
                input_digest, output_digest, artifact_ref, payload_json, created_at
           FROM lane_artifacts WHERE run_id = ? ORDER BY sequence`,
      )
      .all(runId);
    const runRows = db
      .query<
        {
          artifact_id: string;
          artifact_kind: string;
          plan_revision: number;
          input_digest: string;
          output_digest: string;
          artifact_ref: string;
          payload_json: string;
          created_at: string | null;
        },
        [string]
      >(
        `SELECT artifact_id, artifact_kind, plan_revision, input_digest, output_digest,
                artifact_ref, payload_json, created_at
           FROM run_artifacts WHERE run_id = ? ORDER BY sequence`,
      )
      .all(runId);

    const results: MaestroResult[] = [];
    for (const row of laneRows) {
      if (HIDDEN_ARTIFACT_KINDS[row.artifact_kind]) continue;
      const body = publicArtifactBody(row.artifact_kind, row.payload_json);
      results.push({
        node_id: row.lane_id,
        attempt_no: null,
        subject_sha: row.output_digest,
        adjudication: typeof body.verdict === "string" ? body.verdict : null,
        created_at: row.created_at,
        payload: {
          artifact_id: row.artifact_id,
          artifact_kind: row.artifact_kind,
          completed_stage: row.completed_stage,
          plan_revision: row.plan_revision,
          input_digest: row.input_digest,
          output_digest: row.output_digest,
          artifact_ref: row.artifact_ref,
          ...body,
        },
      });
    }
    for (const row of runRows) {
      if (HIDDEN_ARTIFACT_KINDS[row.artifact_kind]) continue;
      const body = publicArtifactBody(row.artifact_kind, row.payload_json);
      results.push({
        node_id: null,
        attempt_no: null,
        subject_sha: row.output_digest,
        adjudication: typeof body.verdict === "string" ? body.verdict : null,
        created_at: row.created_at,
        payload: {
          artifact_id: row.artifact_id,
          artifact_kind: row.artifact_kind,
          plan_revision: row.plan_revision,
          input_digest: row.input_digest,
          output_digest: row.output_digest,
          artifact_ref: row.artifact_ref,
          ...body,
        },
      });
    }
    return results;
  }

  private summaryFrom(row: FactoryRunRow, nodes: MaestroNode[]): MaestroRunSummary {
    const stages = nodes.map((node) => node.state);
    const state = liveStateFromStages(stages, this.runArtifactsFor(row.run_id));
    const last = row.last_transition_at ?? row.updated_at;
    const schedulerWorking = [
      "RUNNING",
      "PENDING",
      "WAITING_FOR_USER",
      "INTEGRATION_REVIEW_PENDING",
      "PUBLISHABLE",
    ].includes(state);
    return {
      run_id: row.run_id,
      schema_version: ARTIFACT_FACTORY_SCHEMA_VERSION,
      plan_name: this.planNameFor(row.plan_digest),
      plan_digest: row.plan_digest,
      state,
      scheduler_liveness: schedulerWorking ? "unknown" : "not_running",
      declared_outcome: null,
      declared_outcome_at: null,
      cancel_cause: null,
      resumable: state !== "COMPLETE",
      cancel_requested: false,
      created_at: row.created_at,
      last_transition_at: last,
      node_count: nodes.length,
      node_states: nodes.map((node) => ({ node_id: node.node_id, state: node.state })),
    };
  }

  runs(): MaestroRunSummary[] {
    return this.runRows().map((row) => this.summaryFrom(row, this.nodesFor(row.run_id, row.plan_revision)));
  }

  run(runId: string): MaestroRunDetail | null {
    const row = this.runRows(runId)[0];
    if (!row) return null;
    const nodes = this.nodesFor(runId, row.plan_revision);
    const summary = this.summaryFrom(row, nodes);
    const transitions = this.transitionsFor(runId);
    const integration: MaestroIntegration | null = row.target_repository_root
      ? {
          path: row.target_repository_root,
          branch: row.integration_ref,
          head: null,
          subject: null,
        }
      : null;
    return {
      ...summary,
      server_now_ms: Date.now(),
      integration,
      nodes,
      actor_sessions: [],
      lane_candidates: [],
      candidate_reviews: [],
      repair_handoffs: [],
      test_strength_contract: "not_recorded",
      test_gate_evidence: [],
      test_pairings: [],
      legacy_test_strength_blocks: [],
      results: this.resultsFor(runId),
      run_transitions: transitions,
    };
  }
}
