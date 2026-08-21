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
  MaestroReviewFinding,
  MaestroRunDetail,
  MaestroRunSummary,
  MaestroTransition,
} from "../shared/types.ts";

/** The tables that identify a ledger as Maestro's rather than the tracer's. */
export const MAESTRO_TABLES = ["runs", "dag_nodes", "node_lifecycle", "attempts"];

/** The plan file every named plan directory holds. Its bytes are its identity. */
const PLAN_FILE = "maestro-plan.v1";

/** A readonly ledger connection, plus whether it had to be opened `immutable=1`. */
export interface LedgerConnection {
  db: Database;
  /**
   * `immutable=1` tells SQLite the file will never change, which is only true
   * for as long as nothing else writes to it. A row deleted or added by
   * another process afterwards is invisible to this connection — or worse,
   * `SQLITE_CORRUPT` — because it stops checking the file for changes at all.
   * Callers that hold one of these across requests must reopen it once the
   * file's mtime moves; see `MaestroDb.freshDb()`.
   */
  immutable: boolean;
}

/**
 * Open a ledger read-only, tolerating a cleanly-closed WAL database.
 *
 * Kept as a free function so the schema probe can borrow it without
 * constructing a reader for a database that may turn out to be an sssf.db.
 */
export function openLedgerReadonly(path: string): LedgerConnection {
  try {
    return { db: probed(new Database(path, { readonly: true })), immutable: false };
  } catch {
    const flags = constants.SQLITE_OPEN_READONLY | constants.SQLITE_OPEN_URI;
    return {
      db: probed(new Database(`file:${encodeURI(path)}?mode=ro&immutable=1`, flags)),
      immutable: true,
    };
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
  cancel_cause: string | null;
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
  cancel_cause: string | null;
  merge_cause: string | null;
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

function reviewFindingsFromExtra(extra: Record<string, unknown>): MaestroReviewFinding[] {
  const guidance = extra.guidance;
  if (guidance == null || typeof guidance !== "object" || Array.isArray(guidance)) {
    return [];
  }
  const payload = guidance as Record<string, unknown>;
  if (payload.surface !== "review") return [];
  const raw = payload.findings;
  if (!Array.isArray(raw)) return [];
  const findings: MaestroReviewFinding[] = [];
  for (const item of raw) {
    if (item == null || typeof item !== "object" || Array.isArray(item)) continue;
    const row = item as Record<string, unknown>;
    if (row.blocking !== true) continue;
    findings.push({
      check_id: typeof row.check_id === "string" ? row.check_id : "",
      object_id: typeof row.object_id === "string" ? row.object_id : "",
      message: typeof row.message === "string" ? row.message : "",
      blocking: true,
    });
  }
  return findings;
}


export class MaestroDb {
  readonly path: string;
  /** `<state_root>/<repo>/runs` — where each run's integration worktree lives. */
  readonly runsDir: string;
  /** The repository's plans directory, when it could be located. */
  readonly plansDir: string | null;
  readonly journalMode: string;
  private db: Database;
  private immutable: boolean;
  /** File mtime at the moment `db` was opened; used to detect a stale immutable handle. */
  private dbStamp: number;
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
    const opened = openLedgerReadonly(path);
    this.db = opened.db;
    this.immutable = opened.immutable;
    this.dbStamp = statSync(path).mtimeMs;
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

  /**
   * The connection to read through for this call, reopened first if it was
   * opened `immutable=1` and the file has moved since.
   *
   * A live WAL writer's readonly connection already sees fresh commits
   * through the shared WAL index, so it is left alone. An `immutable=1`
   * connection — the only way to open a *finished* run's ledger at all,
   * because its `-wal`/`-shm` sidecars are gone — explicitly tells SQLite the
   * file will never change and stops checking; once something does write to
   * it (the maestro CLI, a direct `sqlite3` edit, a resumed scheduler), that
   * connection either keeps serving its boot-time snapshot or throws
   * `SQLITE_CORRUPT`. Reopening on a moved mtime is what makes a per-request
   * query actually observe a per-request state of the file.
   */
  private freshDb(): Database {
    if (!this.immutable) return this.db;
    const stamp = statSync(this.path).mtimeMs;
    if (stamp === this.dbStamp) return this.db;
    const opened = openLedgerReadonly(this.path);
    this.db.close();
    this.db = opened.db;
    this.immutable = opened.immutable;
    this.dbStamp = stamp;
    this.db.exec("PRAGMA busy_timeout = 5000");
    // A reopened file may be a different schema version from the one this
    // object booted against, so the column map goes with the connection.
    this.columnCache.clear();
    return this.db;
  }

  /**
   * The columns a table actually has, per connection.
   *
   * `cancel_cause` arrived by migration, and a ledger written before it — or
   * by a deployment copy that has not caught up — simply does not have it.
   * Naming an absent column in a SELECT is an error, not a null, so the query
   * is built against what is there. This is a *schema* probe, never a value
   * default: an absent column yields `null`, which the dashboard renders as
   * "not recorded" rather than as a cause it invented (§19.2).
   */
  private columnCache = new Map<string, Set<string>>();

  private columns(db: Database, table: string): Set<string> {
    const cached = this.columnCache.get(table);
    if (cached) return cached;
    const rows = db
      .query<{ name: string }, []>(`PRAGMA table_info(${table})`)
      .all();
    const names = new Set(rows.map((row) => row.name));
    this.columnCache.set(table, names);
    return names;
  }

  /** `<alias>.<column>` when the table has it, else a typed NULL under the
   * same name, so every row shape is the same whatever the ledger's age. */
  private optionalColumn(
    db: Database,
    table: string,
    alias: string,
    column: string,
  ): string {
    return this.columns(db, table).has(column)
      ? `${alias}.${column}`
      : `NULL AS ${column}`;
  }

  runCount(): number {
    return this.freshDb().query<{ n: number }, []>("SELECT COUNT(*) AS n FROM runs").get()?.n ?? 0;
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
    const db = this.freshDb();
    const cause = this.optionalColumn(db, "runs", "runs", "cancel_cause");
    const sql =
      `SELECT run_id, plan_digest, created_at, last_transition_at,
              latest_outcome, latest_outcome_at, ${cause}, cancel_requested
         FROM runs`;
    return runId
      ? db.query<RunRow, [string]>(`${sql} WHERE run_id = ?`).all(runId)
      : db.query<RunRow, []>(`${sql} ORDER BY created_at DESC, rowid DESC`).all();
  }

  private nodeRows(runId: string): NodeRow[] {
    const db = this.freshDb();
    const cause = this.optionalColumn(
      db, "node_lifecycle", "l", "cancel_cause");
    // Same treatment, and for the same reason: a ledger written before the
    // column selects NULL, which `mergeProvenance` reads as UNRECORDED
    // rather than as SCHEDULER. Guessing the other way would have every
    // pre-existing MERGED node claim an evidence chain nobody checked.
    const merged = this.optionalColumn(
      db, "node_lifecycle", "l", "merge_cause");
    return db
      .query<NodeRow, [string]>(
        `SELECT d.node_id, d.kind, d.depth, d.needs_json, d.outputs_json,
                l.state, l.attempt_no, l.block_reason, ${cause}, ${merged},
                l.output_sha,
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
      const state = liveState(row, nodes);
      return {
        run_id: row.run_id,
        plan_name: this.planNameFor(row.plan_digest),
        plan_digest: row.plan_digest,
        state,
        declared_outcome: row.latest_outcome,
        declared_outcome_at: row.latest_outcome_at,
        cancel_cause: row.cancel_cause,
        resumable: isResumable(row, state),
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
    // Same open-or-reopened connection nodeRows() just settled on: mtime
    // cannot have moved again between these calls in this one request.
    const db = this.freshDb();
    const attemptRows = db
      .query<AttemptRow, [string]>(
        `SELECT node_id, attempt_no, base_sha, state, started_at, launched_at,
                pid, turn_count, retry_class, extra_json
           FROM attempts WHERE run_id = ? ORDER BY node_id, attempt_no`,
      )
      .all(runId);
    const transitionRows = db
      .query<TransitionRow, [string]>(
        `SELECT node_id, kind, from_state, to_state, reason, actor,
                detail_json, created_at
           FROM transitions WHERE run_id = ? ORDER BY id`,
      )
      .all(runId);
    const resultRows = db
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
        review_findings: reviewFindingsFromExtra(extra),
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
      cancel_cause: node.cancel_cause,
      merge_cause: mergeProvenance(node.state, node.merge_cause),
      output_sha: node.output_sha,
      granted_extra_attempts: node.granted_extra_attempts ?? 0,
      updated_at: node.updated_at,
      attempts: attemptsByNode.get(node.node_id) ?? [],
    }));

    const state = liveState(row, nodeRows);
    return {
      run_id: row.run_id,
      plan_name: this.planNameFor(row.plan_digest),
      plan_digest: row.plan_digest,
      state,
      declared_outcome: row.latest_outcome,
      declared_outcome_at: row.latest_outcome_at,
      cancel_cause: row.cancel_cause,
      resumable: isResumable(row, state),
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

/** §7.3's absolutely-terminal node states: nothing automatic leaves them. */
const ABSOLUTELY_TERMINAL = new Set(["MERGED", "CANCELLED"]);

/**
 * What a MERGED row is showing about how it got there: the stored
 * `merge_cause`, or `UNRECORDED` where the column is absent or NULL on a
 * MERGED row — a ledger written before the column existed. `null` for any
 * other state, where the question does not arise.
 *
 * The twin of `lifecycle.merge_cause_label` on the Python side, and the one
 * place this dashboard derives the pair. `derive_run_state` and `liveState`
 * being one rule in two languages with nothing comparing them is how the
 * dashboard once rendered CANCELLING for a run whose every node was terminal
 * (issue #39); keeping this to a single named function on each side is the
 * cheapest thing that keeps the two readable against each other.
 */
export function mergeProvenance(
  state: string,
  mergeCause: string | null,
): string | null {
  if (state !== "MERGED") return null;
  return mergeCause ?? "UNRECORDED";
}

/**
 * What a run is doing NOW, which is usually not what it last declared.
 *
 * `runs.latest_outcome` is the last quiescence a scheduler declared and it
 * survives a resume, so a run that blocked, was rescued and is working again
 * still reads BLOCKED there. The dashboard shows both, and this is the one
 * that answers "is it moving".
 *
 * Settled node rows are read before `cancel_requested` — issue #39. The flag
 * is a *request*, cleared only by a resume, so it never says the stop is
 * still in progress. The node rows say that.
 *
 * `latest_outcome` is consulted only inside the settled branch, and only
 * when the live rows do not already contradict it. A resume leaves
 * `latest_outcome=CANCELLED` standing until the scheduler declares again.
 * Once every reopened node is MERGED the rows say MERGED; treating the
 * leftover declaration as the live state is §19 M5's stale-outcome
 * projection during the final-acceptance window. Mixes of MERGED and
 * CANCELLED still read the declaration so an abandon-by-node run stays
 * CANCELLED (no `cancel_requested`, and the declaration is the typed
 * statement that it is).
 */
function liveState(row: RunRow, nodes: { state: string }[]): string {
  if (nodes.length === 0) return "EMPTY";
  const states = nodes.map((node) => node.state);
  if (states.every((state) => ABSOLUTELY_TERMINAL.has(state))) {
    const allMerged = states.every((state) => state === "MERGED");
    if (allMerged && !row.cancel_requested) return "MERGED";
    if (row.cancel_requested || row.latest_outcome === "CANCELLED") {
      return "CANCELLED";
    }
    return "QUIESCENT";
  }
  if (row.cancel_requested) return "CANCELLING";
  if (states.includes("RUNNING")) return "RUNNING";
  if (states.includes("BLOCKED")) return "BLOCKED";
  if (states.includes("PENDING")) return "PENDING";
  return "QUIESCENT";
}

/**
 * Whether `run resume` will take this run.
 *
 * `resume_run` refuses ACCEPTED, refuses a CANCELLED whose cause is
 * ABANDONED, refuses a CANCELLED with no recorded cause — a ledger older than
 * the column, where reading an unrecorded cancellation as a pause is the guess
 * that reopens an adjudicated run — and accepts a CANCELLED caused by
 * RUN_CANCEL. An operator looking at a cancelled run could not tell those
 * apart, which is what this answers.
 *
 * The displayed live state wins when it already names the window resume
 * would refuse: a run whose nodes are all MERGED is in final acceptance,
 * even if `latest_outcome` is still the CANCELLED a resume left behind.
 *
 * Everything else is resumable, including the run that declared nothing: a
 * NULL latest outcome means no scheduler ever declared quiescence, and a
 * legality rule that refused it would make crash recovery unreachable (§7.3).
 */
function isResumable(row: RunRow, state: string): boolean {
  if (state === "MERGED") return false;
  if (row.latest_outcome === "ACCEPTED") return false;
  if (row.latest_outcome === "CANCELLED") {
    return row.cancel_cause === "RUN_CANCEL";
  }
  return true;
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
