/**
 * SQLite reader over a Maestro lifecycle store.
 *
 * Maestro's DAG runtime does not write the SSSF tracer schema. It writes its
 * own ledger — `runs`, `dag_nodes`, `node_lifecycle`, `attempts`, plus the
 * audit tables `transitions` / `results` / `orphans` — at
 * `<state_root>/<repo>/lifecycle.sqlite3`. That ledger is the authority the
 * scheduler transacts against, so this file only ever reads it. Nothing here
 * writes, and there is deliberately no equivalent of SssfDb's `setArchived`:
 * a lifecycle row is run authority, not review triage.
 *
 * Two shapes of the same schema have to be readable:
 *
 *  - a LIVE run, whose scheduler holds the database open in WAL mode; and
 *  - a FINISHED run, whose last connection closed cleanly and therefore
 *    DELETED the `-wal`/`-shm` sidecars.
 *
 * A plain readonly open of the second one fails with "unable to open database
 * file", because read-only WAL access needs the shared-memory index. When the
 * sidecars are gone there is provably no live writer, which is exactly the
 * condition under which SQLite's `immutable=1` is safe — so the fallback
 * cannot read behind a writer, and it is the only way to open a finished run's
 * ledger at all.
 */
import { Database, constants } from "bun:sqlite";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import type {
  MaestroAttempt,
  MaestroIntegration,
  MaestroNode,
  MaestroRunDetail,
  MaestroRunSummary,
  MaestroTransition,
} from "../shared/types.ts";

/** The tables that identify a ledger as Maestro's rather than the tracer's. */
export const MAESTRO_TABLES = ["runs", "dag_nodes", "node_lifecycle", "attempts"];

/** The plan file every named plan directory holds. Its bytes are its identity. */
const PLAN_FILE = "maestro-plan.v1";

/**
 * Open a ledger read-only, tolerating a cleanly-closed WAL database.
 *
 * Kept as a free function so the schema probe can borrow it without
 * constructing a reader for a database that may turn out to be an sssf.db.
 */
export function openLedgerReadonly(path: string): Database {
  try {
    return probed(new Database(path, { readonly: true }));
  } catch {
    const flags = constants.SQLITE_OPEN_READONLY | constants.SQLITE_OPEN_URI;
    return probed(new Database(`file:${encodeURI(path)}?mode=ro&immutable=1`, flags));
  }
}

/**
 * Force the open, so a missing-`-shm` refusal surfaces at the point that
 * chooses between the two read-only modes.
 *
 * `new Database(...)` does not reliably fail on construction — the error can
 * arrive at the first statement instead, which is long after the only place
 * that could have retried.
 */
function probed(db: Database): Database {
  try {
    db.query("SELECT 1 FROM sqlite_master LIMIT 1").all();
  } catch (error) {
    db.close();
    throw error;
  }
  return db;
}

interface RunRow {
  run_id: string;
  plan_digest: string;
  created_at: string | null;
  last_transition_at: string | null;
  latest_outcome: string | null;
  latest_outcome_at: string | null;
  cancel_requested: number | null;
}

interface NodeRow {
  node_id: string;
  kind: string | null;
  depth: number | null;
  needs_json: string | null;
  outputs_json: string | null;
  state: string;
  attempt_no: number | null;
  block_reason: string | null;
  output_sha: string | null;
  granted_extra_attempts: number | null;
  updated_at: string | null;
}

interface AttemptRow {
  node_id: string;
  attempt_no: number;
  base_sha: string | null;
  state: string;
  started_at: number | null;
  launched_at: number | null;
  pid: number | null;
  turn_count: number | null;
  retry_class: string | null;
  extra_json: string | null;
}

interface TransitionRow {
  node_id: string | null;
  kind: string | null;
  from_state: string | null;
  to_state: string | null;
  reason: string | null;
  actor: string | null;
  detail_json: string | null;
  created_at: string | null;
}

interface ResultRow {
  node_id: string | null;
  attempt_no: number | null;
  subject_sha: string | null;
  payload_json: string | null;
  adjudication: string | null;
  created_at: string | null;
}

function parseJson<T>(raw: string | null | undefined, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    // A payload a newer runtime wrote in a shape we cannot parse is still a
    // row worth showing; it just contributes no structure.
    return fallback;
  }
}

export class MaestroDb {
  readonly path: string;
  /** `<state_root>/<repo>/runs` — where each run's integration worktree lives. */
  readonly runsDir: string;
  /** The repository's plans directory, when it could be located. */
  readonly plansDir: string | null;
  readonly journalMode: string;
  private readonly db: Database;
  /** digest → plan name, rebuilt whenever the plans directory changes on disk. */
  private planNames = new Map<string, string>();
  private planNamesStamp = 0;

  constructor(path: string, plansDir: string | null = null) {
    if (!existsSync(path)) {
      throw new Error(`lifecycle.sqlite3 not found at ${path}`);
    }
    this.path = path;
    this.runsDir = resolve(dirname(path), "runs");
    this.plansDir = plansDir;
    this.db = openLedgerReadonly(path);
    // A readonly connection cannot set journal_mode; take the busy_timeout so a
    // transacting scheduler never turns a poll into a failed request.
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.journalMode =
      this.db.query<{ journal_mode: string }, []>("PRAGMA journal_mode").get()
        ?.journal_mode ?? "unknown";
  }

  close(): void {
    this.db.close();
  }

  runCount(): number {
    return this.db.query<{ n: number }, []>("SELECT COUNT(*) AS n FROM runs").get()?.n ?? 0;
  }

  /**
   * digest → plan name.
   *
   * The ledger stores a plan DIGEST and has never heard of a plan name, so
   * this map is the whole bridge between the two. It is recomputed whenever
   * the plans directory's mtime moves, because a plan edited between runs
   * changes its digest and a stale entry would label a run with the wrong
   * plan. A run whose plan bytes have since changed simply has no name here,
   * which is the honest answer rather than a guess.
   */
  private planNameFor(digest: string): string | null {
    if (!this.plansDir || !existsSync(this.plansDir)) return null;
    const stamp = statSync(this.plansDir).mtimeMs;
    if (stamp !== this.planNamesStamp) {
      const fresh = new Map<string, string>();
      for (const entry of readdirSync(this.plansDir, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        const file = join(this.plansDir, entry.name, PLAN_FILE);
        if (!existsSync(file)) continue;
        const hash = new Bun.CryptoHasher("sha256")
          .update(readFileSync(file))
          .digest("hex");
        fresh.set(hash, entry.name);
      }
      this.planNames = fresh;
      this.planNamesStamp = stamp;
    }
    return this.planNames.get(digest) ?? null;
  }

  private runRows(runId?: string): RunRow[] {
    const sql =
      `SELECT run_id, plan_digest, created_at, last_transition_at,
              latest_outcome, latest_outcome_at, cancel_requested
         FROM runs`;
    return runId
      ? this.db.query<RunRow, [string]>(`${sql} WHERE run_id = ?`).all(runId)
      : this.db
          .query<RunRow, []>(`${sql} ORDER BY created_at DESC, rowid DESC`)
          .all();
  }

  private nodeRows(runId: string): NodeRow[] {
    return this.db
      .query<NodeRow, [string]>(
        `SELECT d.node_id, d.kind, d.depth, d.needs_json, d.outputs_json,
                l.state, l.attempt_no, l.block_reason, l.output_sha,
                l.granted_extra_attempts, l.updated_at
           FROM dag_nodes d
           JOIN node_lifecycle l
             ON l.run_id = d.run_id AND l.node_id = d.node_id
          WHERE d.run_id = ?
          ORDER BY d.depth, d.node_id`,
      )
      .all(runId);
  }

  /**
   * Run rows for the index, newest first, each carrying just enough node state
   * to draw a progress strip without a request per card.
   */
  runs(): MaestroRunSummary[] {
    return this.runRows().map((row) => {
      const nodes = this.nodeRows(row.run_id);
      return {
        run_id: row.run_id,
        plan_name: this.planNameFor(row.plan_digest),
        plan_digest: row.plan_digest,
        state: liveState(row, nodes),
        declared_outcome: row.latest_outcome,
        declared_outcome_at: row.latest_outcome_at,
        cancel_requested: Boolean(row.cancel_requested),
        created_at: row.created_at,
        last_transition_at: row.last_transition_at,
        node_count: nodes.length,
        node_states: nodes.map((node) => ({
          node_id: node.node_id,
          state: node.state,
        })),
      } satisfies MaestroRunSummary;
    });
  }

  /** One run, whole: DAG shape, node lifecycle, every attempt, every verdict. */
  run(runId: string): MaestroRunDetail | null {
    const row = this.runRows(runId)[0];
    if (!row) return null;

    const nodeRows = this.nodeRows(runId);
    const attemptRows = this.db
      .query<AttemptRow, [string]>(
        `SELECT node_id, attempt_no, base_sha, state, started_at, launched_at,
                pid, turn_count, retry_class, extra_json
           FROM attempts WHERE run_id = ? ORDER BY node_id, attempt_no`,
      )
      .all(runId);
    const transitionRows = this.db
      .query<TransitionRow, [string]>(
        `SELECT node_id, kind, from_state, to_state, reason, actor,
                detail_json, created_at
           FROM transitions WHERE run_id = ? ORDER BY id`,
      )
      .all(runId);
    const resultRows = this.db
      .query<ResultRow, [string]>(
        `SELECT node_id, attempt_no, subject_sha, payload_json, adjudication,
                created_at
           FROM results WHERE run_id = ? ORDER BY id`,
      )
      .all(runId);

    const history = attemptHistory(transitionRows);
    const attemptsByNode = new Map<string, MaestroAttempt[]>();
    for (const attempt of attemptRows) {
      const entries = history.get(`${attempt.node_id}#${attempt.attempt_no}`) ?? [];
      const extra = parseJson<Record<string, unknown>>(attempt.extra_json, {});
      const projected: MaestroAttempt = {
        node_id: attempt.node_id,
        attempt_no: attempt.attempt_no,
        state: attempt.state,
        base_sha: attempt.base_sha,
        turn_count: attempt.turn_count ?? 0,
        retry_class: attempt.retry_class,
        pid: attempt.pid,
        // The ledger stores these as epoch SECONDS (time.time()); the UI works
        // in milliseconds, and converting once here keeps that seam in one place.
        started_at_ms: attempt.started_at == null ? null : attempt.started_at * 1000,
        launched_at_ms: attempt.launched_at == null ? null : attempt.launched_at * 1000,
        running: attempt.state === "RUNNING",
        session_path:
          typeof extra.session_path === "string" ? extra.session_path : null,
        verdict: firstVerdict(entries),
        transitions: entries,
      };
      const list = attemptsByNode.get(attempt.node_id);
      if (list) list.push(projected);
      else attemptsByNode.set(attempt.node_id, [projected]);
    }

    const nodes: MaestroNode[] = nodeRows.map((node) => ({
      node_id: node.node_id,
      kind: node.kind,
      depth: node.depth ?? 0,
      needs: parseJson<string[]>(node.needs_json, []),
      outputs: parseJson<string[]>(node.outputs_json, []),
      state: node.state,
      attempt_no: node.attempt_no ?? 0,
      block_reason: node.block_reason,
      output_sha: node.output_sha,
      granted_extra_attempts: node.granted_extra_attempts ?? 0,
      updated_at: node.updated_at,
      attempts: attemptsByNode.get(node.node_id) ?? [],
    }));

    return {
      run_id: row.run_id,
      plan_name: this.planNameFor(row.plan_digest),
      plan_digest: row.plan_digest,
      state: liveState(row, nodeRows),
      declared_outcome: row.latest_outcome,
      declared_outcome_at: row.latest_outcome_at,
      cancel_requested: Boolean(row.cancel_requested),
      created_at: row.created_at,
      last_transition_at: row.last_transition_at,
      // Wall clock as the SERVER sees it, so the browser can render elapsed
      // times against the same clock the scheduler stamped them with.
      server_now_ms: Date.now(),
      integration: this.integration(runId),
      nodes,
      results: resultRows.map((result) => ({
        node_id: result.node_id,
        attempt_no: result.attempt_no,
        subject_sha: result.subject_sha,
        adjudication: result.adjudication,
        created_at: result.created_at,
        payload: parseJson<unknown>(result.payload_json, null),
      })),
      run_transitions: transitionRows
        .filter((transition) => transition.node_id === null)
        .map(projectTransition),
    };
  }

  /**
   * The run's integration worktree, as git reports it.
   *
   * Every node's work lands on this branch and every attempt is based on its
   * head, so "which commit is integration on" is the single most load-bearing
   * fact about a run that the ledger does not store. Read with a plain `git`
   * invocation because the worktree is the record — nothing mirrors it.
   */
  private integration(runId: string): MaestroIntegration | null {
    const path = join(this.runsDir, runId, "integration");
    if (!existsSync(path)) return null;
    const read = (...args: string[]): string | null => {
      const proc = Bun.spawnSync(["git", "-C", path, ...args], {
        stdout: "pipe",
        stderr: "pipe",
      });
      return proc.exitCode === 0 ? proc.stdout.toString().trim() : null;
    };
    return {
      path,
      branch: read("rev-parse", "--abbrev-ref", "HEAD"),
      head: read("rev-parse", "HEAD"),
      subject: read("log", "-1", "--format=%s"),
    };
  }
}

/**
 * Each node transition filed under the attempt it happened during.
 *
 * `transitions` carries no attempt number, so the join is positional: the
 * scheduler writes exactly one `attempt-start` per attempt, in order, so the
 * nth `attempt-start` for a node opens attempt n. Anything before the first
 * one belongs to no attempt and is dropped rather than guessed at.
 */
function attemptHistory(rows: TransitionRow[]): Map<string, MaestroTransition[]> {
  const open = new Map<string, number>();
  const history = new Map<string, MaestroTransition[]>();
  for (const row of rows) {
    if (!row.node_id) continue;
    if (row.reason === "attempt-start") {
      open.set(row.node_id, (open.get(row.node_id) ?? 0) + 1);
      continue;
    }
    const attemptNo = open.get(row.node_id);
    if (attemptNo === undefined) continue;
    const key = `${row.node_id}#${attemptNo}`;
    const entry = projectTransition(row);
    const list = history.get(key);
    if (list) list.push(entry);
    else history.set(key, [entry]);
  }
  return history;
}

function projectTransition(row: TransitionRow): MaestroTransition {
  return {
    node_id: row.node_id,
    from_state: row.from_state,
    to_state: row.to_state,
    reason: row.reason,
    actor: row.actor,
    created_at: row.created_at,
    detail: parseJson<Record<string, unknown>>(row.detail_json, {}),
  };
}

/** The sentence explaining why an attempt ended, when the scheduler wrote one. */
function firstVerdict(entries: MaestroTransition[]): string | null {
  for (const entry of entries) {
    const verdict = entry.detail?.verdict;
    if (typeof verdict === "string" && verdict) return verdict;
  }
  return null;
}

/**
 * What a run is doing NOW, which is not what it last declared.
 *
 * `runs.latest_outcome` is the last quiescence a scheduler declared and it
 * survives a resume, so a run that blocked, was rescued and is working again
 * still reads BLOCKED there. The dashboard shows both, and this is the one
 * that answers "is it moving".
 */
function liveState(row: RunRow, nodes: { state: string }[]): string {
  if (row.cancel_requested) return "CANCELLING";
  if (nodes.length === 0) return "EMPTY";
  const states = nodes.map((node) => node.state);
  if (states.includes("RUNNING")) return "RUNNING";
  if (states.every((state) => state === "MERGED")) return "MERGED";
  if (states.includes("BLOCKED")) return "BLOCKED";
  if (states.includes("PENDING")) return "PENDING";
  return "QUIESCENT";
}

/**
 * Locate a repository's lifecycle ledger from its `adws/maestro.config.yaml`.
 *
 * The config states `state_root` relative to the repo, and Maestro appends the
 * repository's own directory name — so the ledger is
 * `<state_root>/<repo name>/lifecycle.sqlite3`. Deriving it means the operator
 * runs the visualizer from the repo, exactly as they run every other verb,
 * instead of pasting an absolute path into a flag.
 *
 * Only `state_root` and `plans_dir` are read, by line, because a YAML parser
 * is a dependency this process does not otherwise need and the two keys are
 * top-level scalars in a schema-versioned file.
 */
export function discoverMaestroLedger(
  repo: string,
): { db: string; plansDir: string | null } | null {
  const config = join(repo, "adws", "maestro.config.yaml");
  if (!existsSync(config)) return null;
  const text = readFileSync(config, "utf8");
  const scalar = (key: string): string | null => {
    const match = text.match(new RegExp(`^\\s*"?${key}"?\\s*:\\s*"?([^"\\n#]+?)"?\\s*,?$`, "m"));
    return match ? (match[1] ?? "").trim() : null;
  };
  const stateRoot = scalar("state_root");
  if (!stateRoot) return null;
  const db = resolve(repo, stateRoot, basename(resolve(repo)), "lifecycle.sqlite3");
  if (!existsSync(db)) return null;
  const plans = scalar("plans_dir");
  return { db, plansDir: plans ? resolve(repo, plans) : null };
}
