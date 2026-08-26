/**
 * The Maestro reader, against ledgers built from the runtime's OWN schema.
 *
 * The fixture DDL is not copied here — it is extracted from
 * `templates/adws/adw_modules/lifecycle.py`'s `SCHEMA` literal at test time.
 * A copy would drift the moment the runtime added a column, and the drift
 * would show up as a silently empty dashboard rather than a failing test.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";

import { mkdtempSync, mkdirSync, existsSync, rmSync, writeFileSync } from "node:fs";
import { homedir, hostname, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { MaestroDb, discoverMaestroLedger, mergeProvenance } from "./maestroDb.ts";
import { processStartEpoch } from "./attemptObservation.ts";

import { probeKind, resolveSources } from "./sources.ts";

/** `<visualizer>/../../templates/adws/adw_modules/lifecycle.py`. */
const LIFECYCLE_PY = resolve(
  import.meta.dir,
  "..",
  "..",
  "..",
  "templates",
  "adws",
  "adw_modules",
  "lifecycle.py",
);

/**
 * The runtime's `SCHEMA`, as the runtime itself builds it.
 *
 * It used to be scraped with `/^SCHEMA = """([\S\s]*?)"""/`, which stopped
 * working the moment `SCHEMA` became a concatenation — `SCHEMA = (` followed
 * by a literal, a rendered `candidate_reviews` DDL, and a second literal. The
 * regex then matched nothing and every case in this file failed at setup with
 * `no SCHEMA literal`, which is the opposite of what a drift guard is for:
 * the fixture stopped tracking the runtime and said so in a way that reads as
 * a broken test rather than as a broken schema.
 *
 * Asking Python for the value is the fix, because the value is what these
 * fixtures need. A parser for Python string concatenation written here would
 * be a second implementation of the thing it is checking.
 */
function runtimeSchema(): string {
  // Imported as `adw_modules.lifecycle` from the runtime root, because the
  // module's own imports are relative; loading the file by path outside its
  // package raises `attempted relative import with no known parent package`.
  const runtimeRoot = resolve(LIFECYCLE_PY, "..", "..");
  const result = Bun.spawnSync([
    "python3",
    "-c",
    [
      "import sys",
      `sys.path.insert(0, ${JSON.stringify(runtimeRoot)})`,
      "from adw_modules import lifecycle",
      "sys.stdout.write(lifecycle.SCHEMA)",
    ].join("\n"),
  ]);
  if (result.exitCode !== 0) {
    throw new Error(
      `could not read SCHEMA from ${LIFECYCLE_PY}: ${result.stderr.toString()}`,
    );
  }
  return result.stdout.toString();
}

function runtimeTableSchema(table: string): string {
  const match = runtimeSchema().match(
    new RegExp(`CREATE TABLE IF NOT EXISTS ${table} \\([\\s\\S]*?\\n\\);`),
  );
  if (!match) throw new Error(`no ${table} table in runtime schema`);
  return match[0];
}

let root: string;

/** A ledger with the runtime's schema and whatever rows a case needs. */
function ledger(name: string, seed: (db: Database) => void): string {
  const dir = join(root, name);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, "lifecycle.sqlite3");
  const db = new Database(path);
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(runtimeSchema());
  seed(db);
  db.close();
  return path;
}

function insertRun(
  db: Database,
  runId: string,
  overrides: Partial<{
    plan_digest: string;
    created_at: string;
    last_transition_at: string;
    latest_outcome: string | null;
    cancel_cause: string | null;
    cancel_requested: number;
    scheduler_pid: number | null;
    scheduler_host: string | null;
    scheduler_start_epoch: number | null;
    plan_name: string | null;
  }> = {},
) {
  const row = {
    plan_digest: "d".repeat(64),
    created_at: "2026-08-17T06:00:00+00:00",
    last_transition_at: "2026-08-17T06:05:00+00:00",
    latest_outcome: null as string | null,
    cancel_cause: null as string | null,
    cancel_requested: 0,
    scheduler_pid: null as number | null,
    scheduler_host: null as string | null,
    scheduler_start_epoch: null as number | null,
    plan_name: null as string | null,
    ...overrides,
  };
  db.query(
    `INSERT INTO runs (run_id, plan_digest, created_at, last_transition_at,
                       latest_outcome, latest_outcome_at, cancel_cause,
                       cancel_requested, scheduler_pid, scheduler_host,
                       scheduler_start_epoch, plan_name)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    runId,
    row.plan_digest,
    row.created_at,
    row.last_transition_at,
    row.latest_outcome,
    row.latest_outcome === null ? null : row.last_transition_at,
    row.cancel_cause,
    row.cancel_requested,
    row.scheduler_pid,
    row.scheduler_host,
    row.scheduler_start_epoch,
    row.plan_name,
  );
}

function insertNode(
  db: Database,
  runId: string,
  nodeId: string,
  state: string,
  opts: Partial<{
    depth: number;
    needs: string[];
    attempt_no: number;
    block_reason: string | null;
    cancel_cause: string | null;
    merge_cause: string | null;
    output_sha: string | null;
    lane_phase: string | null;
    plan_digest: string;
  }> = {},
) {
  const o = {
    depth: 0,
    needs: [] as string[],
    attempt_no: 0,
    block_reason: null as string | null,
    cancel_cause: null as string | null,
    merge_cause: null as string | null,
    lane_phase: null as string | null,
    output_sha: null as string | null,
    plan_digest: "d".repeat(64),
    ...opts,
  };
  db.query(
    `INSERT INTO dag_nodes (run_id, node_id, plan_digest, kind, depth,
                            needs_json, outputs_json, specs_json)
     VALUES (?, ?, ?, 'agent', ?, ?, '[]', '[]')`,
  ).run(runId, nodeId, o.plan_digest, o.depth, JSON.stringify(o.needs));
  db.query(
    `INSERT INTO node_lifecycle (run_id, node_id, state, lane_phase, attempt_no,
                                 block_reason, cancel_cause, merge_cause, output_sha,
                                 granted_extra_attempts, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '2026-08-17T06:05:00+00:00')`,
  ).run(runId, nodeId, state, o.lane_phase, o.attempt_no, o.block_reason, o.cancel_cause,
        o.merge_cause, o.output_sha);
}

function insertAttempt(
  db: Database,
  runId: string,
  nodeId: string,
  attemptNo: number,
  state: string,
  opts: Partial<{
    retry_class: string | null;
    turn_count: number;
    extra: object;
    pid: number | null;
    attempt_host: string | null;
    attempt_start_epoch: number | null;
  }> = {},
) {
  const o = {
    retry_class: null as string | null,
    turn_count: 0,
    extra: {},
    pid: null as number | null,
    attempt_host: null as string | null,
    attempt_start_epoch: null as number | null,
    ...opts,
  };
  db.query(
    `INSERT INTO attempts (run_id, node_id, attempt_no, base_sha, state, started_at,
                           launched_at, pid, turn_count, retry_class, extra_json,
                           attempt_host, attempt_start_epoch)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    runId,
    nodeId,
    attemptNo,
    "a".repeat(40),
    state,
    1_786_948_000 + attemptNo,
    1_786_948_010 + attemptNo,
    o.pid,
    o.turn_count,
    o.retry_class,
    JSON.stringify(o.extra),
    o.attempt_host,
    o.attempt_start_epoch,
  );
}

function insertTransition(
  db: Database,
  runId: string,
  nodeId: string | null,
  reason: string,
  detail: object = {},
) {
  db.query(
    `INSERT INTO transitions (run_id, node_id, kind, from_state, to_state, reason,
                              actor, detail_json, created_at)
     VALUES (?, ?, ?, NULL, NULL, ?, 'scheduler', ?, '2026-08-17T06:05:00+00:00')`,
  ).run(runId, nodeId, nodeId === null ? "run" : "node", reason, JSON.stringify(detail));
}

let previousRegistry: string | undefined;

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "maestro-viz-"));
  // Auto-discovery reads the operator's own registry by default, so without
  // this every source-discovery case would return whatever factories this
  // machine happens to have run — passing on a clean checkout and failing
  // the moment someone starts a run. Cases that exercise the registry point
  // it at a file of their own.
  previousRegistry = process.env.MAESTRO_REGISTRY;
  process.env.MAESTRO_REGISTRY = join(root, "absent-registry.json");
});
afterAll(() => {
  if (previousRegistry === undefined) delete process.env.MAESTRO_REGISTRY;
  else process.env.MAESTRO_REGISTRY = previousRegistry;
  rmSync(root, { recursive: true, force: true });
});

describe("schema probe", () => {
  test("a lifecycle ledger is recognised as maestro", () => {
    const path = ledger("probe-maestro", () => {});
    expect(probeKind(path)).toBe("maestro");
  });

  test("a tracer database is recognised as sssf, not maestro", () => {
    const dir = join(root, "probe-sssf");
    mkdirSync(dir, { recursive: true });
    const path = join(dir, "sssf.db");
    const db = new Database(path);
    db.exec("CREATE TABLE sessions (adw_id TEXT)");
    db.exec("CREATE TABLE phases (phase_id TEXT)");
    db.exec("CREATE TABLE events (event_id TEXT)");
    db.close();
    expect(probeKind(path)).toBe("sssf");
  });

  test("a file that is not a database is refused rather than thrown", () => {
    const dir = join(root, "probe-junk");
    mkdirSync(dir, { recursive: true });
    const path = join(dir, "lifecycle.sqlite3");
    writeFileSync(path, "not sqlite at all");
    expect(probeKind(path)).toBeNull();
  });
});

describe("reading a ledger", () => {
  test("an empty ledger reads as no runs", () => {
    const db = new MaestroDb(ledger("empty", () => {}));
    expect(db.runs()).toEqual([]);
    expect(db.runCount()).toBe(0);
    expect(db.run("run-nope")).toBeNull();
    db.close();
  });

  test("a finished ledger reads after its WAL sidecars are gone", () => {
    // Every completed run is this case: SQLite deletes -wal/-shm on the last
    // clean close, and a plain readonly open of what is left fails outright.
    const path = ledger("finished", (db) => {
      insertRun(db, "run-done", { latest_outcome: "ACCEPTED" });
      insertNode(db, "run-done", "lane-one", "MERGED", { attempt_no: 1 });
    });
    for (const sidecar of ["-wal", "-shm"]) {
      rmSync(path + sidecar, { force: true });
    }
    const db = new MaestroDb(path);
    expect(db.runs().map((r) => r.run_id)).toEqual(["run-done"]);
    expect(db.run("run-done")?.declared_outcome).toBe("ACCEPTED");
    db.close();
  });

  test("the reader writes nothing and creates no sidecars", () => {
    const path = ledger("readonly", (db) => insertRun(db, "run-one"));
    for (const sidecar of ["-wal", "-shm"]) rmSync(path + sidecar, { force: true });
    const before = Bun.file(path).size;
    const db = new MaestroDb(path);
    db.runs();
    db.run("run-one");
    expect(() => db["db" as keyof MaestroDb]).not.toThrow();
    db.close();
    expect(Bun.file(path).size).toBe(before);
  });

  test("a missing ledger is an error, not an empty dashboard", () => {
    expect(() => new MaestroDb(join(root, "nope", "lifecycle.sqlite3"))).toThrow();
  });
});

describe("run state", () => {
  test("a live run reads RUNNING even while its declared outcome is stale", () => {
    // The rescue case: `runs.latest_outcome` survives a resume, so the last
    // declared outcome and what the run is doing now are two different facts.
    const db = new MaestroDb(
      ledger("live", (seed) => {
        insertRun(seed, "run-live", { latest_outcome: "BLOCKED" });
        insertNode(seed, "run-live", "lane-one", "RUNNING", { attempt_no: 2 });
      }),
    );
    const run = db.run("run-live");
    expect(run?.state).toBe("RUNNING");
    expect(run?.declared_outcome).toBe("BLOCKED");
    db.close();
  });

  test("a cancel request outranks the node states it has not reached yet", () => {
    const db = new MaestroDb(
      ledger("cancelling", (seed) => {
        insertRun(seed, "run-x", { cancel_requested: 1 });
        insertNode(seed, "run-x", "lane-one", "RUNNING");
      }),
    );
    expect(db.run("run-x")?.state).toBe("CANCELLING");
    db.close();
  });

  test("a cancellation that reached every node reads CANCELLED, not CANCELLING", () => {
    // Issue #39. `cancel_requested` is a REQUEST and only a resume clears it,
    // so checking it first made a finished cancellation render CANCELLING
    // forever — both cancelled runs in the live ledger did exactly that. The
    // node rows already say the run has stopped; that check has to come first,
    // which is the order `lifecycle.derive_run_state` uses.
    const db = new MaestroDb(
      ledger("cancelled", (seed) => {
        insertRun(seed, "run-done", {
          latest_outcome: "CANCELLED",
          cancel_cause: "RUN_CANCEL",
          cancel_requested: 1,
        });
        insertNode(seed, "run-done", "merged-one", "MERGED");
        insertNode(seed, "run-done", "stopped-one", "CANCELLED", {
          cancel_cause: "RUN_CANCEL",
        });
      }),
    );
    const run = db.run("run-done");
    expect(run?.state).toBe("CANCELLED");
    expect(run?.declared_outcome).toBe("CANCELLED");
    db.close();
  });

  test("a run abandoned node by node reads CANCELLED without the flag", () => {
    // `abandon` writes no `cancel_requested`; the scheduler declares CANCELLED
    // at quiescence. The declared outcome is the only thing that says so, and
    // it is trustworthy HERE — a settled run cannot have moved past it.
    const db = new MaestroDb(
      ledger("abandoned", (seed) => {
        insertRun(seed, "run-given-up", {
          latest_outcome: "CANCELLED",
          cancel_cause: "ABANDONED",
        });
        insertNode(seed, "run-given-up", "a", "CANCELLED", {
          cancel_cause: "ABANDONED",
        });
        insertNode(seed, "run-given-up", "b", "CANCELLED", {
          cancel_cause: "ABANDONED",
        });
      }),
    );
    expect(db.run("run-given-up")?.state).toBe("CANCELLED");
    db.close();
  });

  test("a resumed run reads its node states, never its superseded outcome", () => {
    // The control for the case above. Outside the settled branch the declared
    // outcome describes a life the run has moved past, so it must not win.
    const db = new MaestroDb(
      ledger("resumed", (seed) => {
        insertRun(seed, "run-back", {
          latest_outcome: "CANCELLED",
          cancel_cause: "RUN_CANCEL",
          cancel_requested: 0,
        });
        insertNode(seed, "run-back", "merged-one", "MERGED");
        insertNode(seed, "run-back", "working-one", "RUNNING");
      }),
    );
    expect(db.run("run-back")?.state).toBe("RUNNING");
    db.close();
  });

  test("a resumed run whose nodes have all merged is MERGED, not CANCELLED", () => {
    // Resume clears cancel_requested and leaves latest_outcome=CANCELLED
    // until the scheduler declares again. Once every reopened node is
    // MERGED the live rows contradict that leftover declaration — this
    // is the final-acceptance window, and projecting CANCELLED here is
    // §19 M5's stale-outcome defect (issue #39's sibling).
    const db = new MaestroDb(
      ledger("resumed-merged", (seed) => {
        insertRun(seed, "run-accepting", {
          latest_outcome: "CANCELLED",
          cancel_cause: "RUN_CANCEL",
          cancel_requested: 0,
        });
        insertNode(seed, "run-accepting", "merged-one", "MERGED");
        insertNode(seed, "run-accepting", "merged-two", "MERGED");
      }),
    );
    const run = db.run("run-accepting");
    expect(run?.state).toBe("MERGED");
    expect(run?.declared_outcome).toBe("CANCELLED");
    expect(run?.cancel_requested).toBe(false);
    expect(run?.resumable).toBe(false);
    db.close();
  });
});

describe("whether a cancelled run can be resumed", () => {
  test("a RUN_CANCEL run says so, and an ABANDONED one says it cannot", () => {
    const db = new MaestroDb(
      ledger("resumable", (seed) => {
        insertRun(seed, "run-stopped", {
          latest_outcome: "CANCELLED",
          cancel_cause: "RUN_CANCEL",
          cancel_requested: 1,
        });
        insertNode(seed, "run-stopped", "a", "CANCELLED", {
          cancel_cause: "RUN_CANCEL",
        });
        insertRun(seed, "run-given-up", {
          latest_outcome: "CANCELLED",
          cancel_cause: "ABANDONED",
        });
        insertNode(seed, "run-given-up", "a", "CANCELLED", {
          cancel_cause: "ABANDONED",
        });
      }),
    );

    const stopped = db.run("run-stopped");
    expect(stopped?.cancel_cause).toBe("RUN_CANCEL");
    expect(stopped?.resumable).toBe(true);
    expect(stopped?.nodes[0]?.cancel_cause).toBe("RUN_CANCEL");

    const givenUp = db.run("run-given-up");
    expect(givenUp?.cancel_cause).toBe("ABANDONED");
    expect(givenUp?.resumable).toBe(false);
    expect(givenUp?.nodes[0]?.cancel_cause).toBe("ABANDONED");
    db.close();
  });

  test("an unrecorded cause is refused rather than read as a pause", () => {
    // A ledger older than the migration. `resume_run` refuses it, so the
    // dashboard must not offer a resume the CLI will decline.
    const db = new MaestroDb(
      ledger("uncaused", (seed) => {
        insertRun(seed, "run-old", { latest_outcome: "CANCELLED" });
        insertNode(seed, "run-old", "a", "CANCELLED");
      }),
    );
    const run = db.run("run-old");
    expect(run?.cancel_cause).toBeNull();
    expect(run?.resumable).toBe(false);
    db.close();
  });

  test("an ACCEPTED run is not resumable and a BLOCKED one is", () => {
    const db = new MaestroDb(
      ledger("outcomes", (seed) => {
        insertRun(seed, "run-accepted", { latest_outcome: "ACCEPTED" });
        insertNode(seed, "run-accepted", "a", "MERGED");
        insertRun(seed, "run-blocked-2", { latest_outcome: "BLOCKED" });
        insertNode(seed, "run-blocked-2", "a", "BLOCKED", {
          block_reason: "SEMANTIC_BUDGET_EXHAUSTED",
        });
        insertRun(seed, "run-undeclared");
        insertNode(seed, "run-undeclared", "a", "PENDING");
      }),
    );
    expect(db.run("run-accepted")?.resumable).toBe(false);
    expect(db.run("run-blocked-2")?.resumable).toBe(true);
    // A NULL outcome means no scheduler ever declared quiescence, and a rule
    // that refused it would make crash recovery unreachable (§7.3).
    expect(db.run("run-undeclared")?.resumable).toBe(true);
    db.close();
  });

  test("a ledger without the column answers null rather than failing", () => {
    // The deployment ledger this dashboard actually points at has not run the
    // migration. Naming an absent column in a SELECT is an error, not a null,
    // so the query is built against the columns the file has.
    const path = ledger("premigration", (seed) => {
      seed.exec("ALTER TABLE runs DROP COLUMN cancel_cause");
      seed.exec("ALTER TABLE node_lifecycle DROP COLUMN cancel_cause");
      seed.exec("ALTER TABLE node_lifecycle DROP COLUMN lane_phase");
      seed
        .query(
          `INSERT INTO runs (run_id, plan_digest, created_at,
                             last_transition_at, latest_outcome,
                             latest_outcome_at, cancel_requested)
           VALUES (?, ?, '2026-08-17T06:00:00+00:00',
                   '2026-08-17T06:05:00+00:00', 'CANCELLED',
                   '2026-08-17T06:05:00+00:00', 1)`,
        )
        .run("run-old-schema", "d".repeat(64));
      seed
        .query(
          `INSERT INTO dag_nodes (run_id, node_id, plan_digest, kind, depth,
                                  needs_json, outputs_json, specs_json)
           VALUES (?, 'a', ?, 'agent', 0, '[]', '[]', '[]')`,
        )
        .run("run-old-schema", "d".repeat(64));
      seed
        .query(
          `INSERT INTO node_lifecycle (run_id, node_id, state, attempt_no,
                                       granted_extra_attempts, updated_at)
           VALUES (?, 'a', 'CANCELLED', 1, 0, '2026-08-17T06:05:00+00:00')`,
        )
        .run("run-old-schema");
    });
    const db = new MaestroDb(path);
    const run = db.run("run-old-schema");
    expect(run?.state).toBe("CANCELLED");
    expect(run?.cancel_cause).toBeNull();
    expect(run?.resumable).toBe(false);
    expect(run?.nodes[0]?.cancel_cause).toBeNull();
    expect(run?.nodes[0]?.lane_phase).toBeNull();
    db.close();
  });

  test("every node merged reads MERGED; any blocked reads BLOCKED", () => {
    const db = new MaestroDb(
      ledger("states", (seed) => {
        insertRun(seed, "run-merged");
        insertNode(seed, "run-merged", "a", "MERGED");
        insertNode(seed, "run-merged", "b", "MERGED");
        insertRun(seed, "run-blocked");
        insertNode(seed, "run-blocked", "a", "MERGED");
        insertNode(seed, "run-blocked", "b", "BLOCKED", {
          block_reason: "SEMANTIC_BUDGET_EXHAUSTED",
        });
        insertNode(seed, "run-blocked", "c", "PENDING");
      }),
    );
    expect(db.run("run-merged")?.state).toBe("MERGED");
    expect(db.run("run-blocked")?.state).toBe("BLOCKED");
    db.close();
  });
});

describe("the DAG and its attempts", () => {
  // Seeded once: every case in this block reads the same ledger, and reseeding
  // it per test would collide on the run's primary key.
  let dagPath: string | null = null;
  const build = () =>
    (dagPath ??= ledger("dag", (seed) => {
      insertRun(seed, "run-dag");
      insertNode(seed, "run-dag", "lane-schema", "MERGED", {
        depth: 0,
        attempt_no: 1,
        output_sha: "f".repeat(40),
      });
      insertNode(seed, "run-dag", "lane-ingest", "RUNNING", { depth: 0, attempt_no: 2 });
      insertNode(seed, "run-dag", "lane-api", "PENDING", {
        depth: 1,
        needs: ["lane-schema", "lane-ingest"],
      });
      insertAttempt(seed, "run-dag", "lane-schema", 1, "VERIFIED");
      insertAttempt(seed, "run-dag", "lane-ingest", 1, "CANCELLED", {
        retry_class: "SEMANTIC",
        turn_count: 42,
      });
      insertAttempt(seed, "run-dag", "lane-ingest", 2, "RUNNING", {
        turn_count: 17,
        extra: { session_path: "/tmp/a2.jsonl" },
      });
      // The narration the positional join has to file correctly.
      insertTransition(seed, "run-dag", "lane-ingest", "attempt-start");
      insertTransition(seed, "run-dag", "lane-ingest", "retry:SEMANTIC", {
        clause: 4,
        verdict: "the gate stayed red",
      });
      insertTransition(seed, "run-dag", "lane-ingest", "attempt-start");
      insertTransition(seed, "run-dag", null, "declare-outcome");
    }));

  test("nodes carry their depth and their dependency edges", () => {
    const db = new MaestroDb(build());
    const nodes = db.run("run-dag")!.nodes;
    expect(nodes.map((n) => [n.node_id, n.depth])).toEqual([
      ["lane-ingest", 0],
      ["lane-schema", 0],
      ["lane-api", 1],
    ]);
    expect(nodes.find((n) => n.node_id === "lane-api")!.needs).toEqual([
      "lane-schema",
      "lane-ingest",
    ]);
    expect(nodes.find((n) => n.node_id === "lane-schema")!.output_sha).toBe("f".repeat(40));
    db.close();
  });

  test("a verdict lands on the attempt it judged, not on the next one", () => {
    const db = new MaestroDb(build());
    const ingest = db.run("run-dag")!.nodes.find((n) => n.node_id === "lane-ingest")!;
    expect(ingest.attempts.map((a) => a.attempt_no)).toEqual([1, 2]);
    expect(ingest.attempts[0]!.verdict).toBe("the gate stayed red");
    expect(ingest.attempts[0]!.retry_class).toBe("SEMANTIC");
    expect(ingest.attempts[1]!.verdict).toBeNull();
    db.close();
  });

  test("the in-flight attempt is the one marked RUNNING, with its turn count", () => {
    const db = new MaestroDb(build());
    const ingest = db.run("run-dag")!.nodes.find((n) => n.node_id === "lane-ingest")!;
    const inFlight = ingest.attempts.find((a) => a.state === "RUNNING")!;
    expect(inFlight.attempt_no).toBe(2);
    expect(inFlight.turn_count).toBe(17);
    expect(inFlight.session_path).toBe("/tmp/a2.jsonl");
    expect(inFlight.running).toBe(false);
    expect(inFlight.liveness).toBe("not_recorded");
    // Seconds in the ledger, milliseconds out of the reader.
    expect(inFlight.launched_at_ms).toBeGreaterThan(1_700_000_000_000);
    db.close();
  });

  test("run-level transitions stay out of the per-attempt history", () => {
    const db = new MaestroDb(build());
    const detail = db.run("run-dag")!;
    expect(detail.run_transitions.map((t) => t.reason)).toEqual(["declare-outcome"]);
    db.close();
  });

  test("the server stamps its own clock so elapsed time is not the browser's", () => {
    const db = new MaestroDb(build());
    expect(db.run("run-dag")!.server_now_ms).toBeGreaterThan(1_700_000_000_000);
    db.close();
  });
});

describe("attempt liveness uses attempt identity", () => {
  test("a live pid recorded on another host is unknown, not running", () => {
    const path = ledger("foreign-host", (seed) => {
      insertRun(seed, "run-x");
      insertNode(seed, "run-x", "lane-a", "RUNNING", { attempt_no: 1 });
      insertAttempt(seed, "run-x", "lane-a", 1, "RUNNING", {
        pid: process.pid,
        attempt_host: "other-box",
        attempt_start_epoch: 1.0,
      });
    });
    const db = new MaestroDb(path);
    const attempt = db.run("run-x")!.nodes[0]!.attempts[0]!;
    expect(attempt.running).toBe(false);
    expect(attempt.liveness).toBe("unknown");
    db.close();
  });

  test("an old ledger row with no host/epoch is unknown, not dead", () => {
    const path = ledger("old-row", (seed) => {
      insertRun(seed, "run-old");
      insertNode(seed, "run-old", "lane-a", "RUNNING", { attempt_no: 1 });
      insertAttempt(seed, "run-old", "lane-a", 1, "RUNNING", {
        pid: 2_000_000_000,
      });
    });
    const db = new MaestroDb(path);
    const attempt = db.run("run-old")!.nodes[0]!.attempts[0]!;
    expect(attempt.running).toBe(false);
    expect(attempt.liveness).toBe("unknown");
    db.close();
  });

  test("dead builder pid in review (ACCEPTED result) reads alive", () => {
    const path = ledger("review-window", (seed) => {
      insertRun(seed, "run-rev");
      insertNode(seed, "run-rev", "lane-a", "RUNNING", { attempt_no: 1 });
      insertAttempt(seed, "run-rev", "lane-a", 1, "RUNNING", {
        pid: 2_000_000_000,
        attempt_host: "this-box",
        attempt_start_epoch: 1.0,
      });
      seed.query(
        `INSERT INTO results (run_id, node_id, attempt_no, subject_sha,
                              payload_json, adjudication, created_at)
         VALUES (?, ?, ?, ?, '{}', 'ACCEPTED', '2026-08-17T06:05:00+00:00')`,
      ).run("run-rev", "lane-a", 1, "a".repeat(40));
    });
    const db = new MaestroDb(path);
    const attempt = db.run("run-rev")!.nodes[0]!.attempts[0]!;
    expect(attempt.state).toBe("RUNNING");
    expect(attempt.pid).toBe(2_000_000_000);
    expect(attempt.running).toBe(true);
    expect(attempt.liveness).toBe("running");
    db.close();
  });
});

describe("review findings on a merged attempt", () => {
  test("blocking guidance findings reach the attempt; advisories do not", () => {
    const path = ledger("findings", (seed) => {
      insertRun(seed, "run-r7", { latest_outcome: "ACCEPTED" });
      insertNode(seed, "run-r7", "lane-one", "MERGED", { attempt_no: 1 });
      insertAttempt(seed, "run-r7", "lane-one", 1, "VERIFIED", {
        extra: {
          review_rejected: true,
          review_advisory: true,
          guidance: {
            surface: "review",
            subject_digest: "abc",
            findings: [
              {
                check_id: "diff.implements_the_stated_instruction",
                object_id: "diff:c",
                message: "the instruction is not what merged",
                blocking: true,
              },
              {
                check_id: "diff.is_coherent_with_its_surroundings",
                object_id: "src/mod.py:12",
                message: "style only",
                blocking: false,
              },
            ],
          },
        },
      });
    });
    const db = new MaestroDb(path);
    const attempt = db.run("run-r7")!.nodes[0]!.attempts[0]!;
    expect(attempt.review_findings).toEqual([
      {
        check_id: "diff.implements_the_stated_instruction",
        object_id: "diff:c",
        message: "the instruction is not what merged",
        blocking: true,
      },
    ]);
    db.close();
  });

  test("an attempt with no guidance carries an empty list, not null", () => {
    const db = new MaestroDb(
      ledger("findings-empty", (seed) => {
        insertRun(seed, "run-clean", { latest_outcome: "ACCEPTED" });
        insertNode(seed, "run-clean", "lane-one", "MERGED", { attempt_no: 1 });
        insertAttempt(seed, "run-clean", "lane-one", 1, "VERIFIED");
      }),
    );
    expect(db.run("run-clean")!.nodes[0]!.attempts[0]!.review_findings).toEqual([]);
    db.close();
  });
});


describe("plan names", () => {
  test("a run is named by hashing the plan file, and unnamed when it changed", () => {
    const plans = join(root, "plans");
    mkdirSync(join(plans, "my-plan"), { recursive: true });
    const bytes = '{"plan":"stored bytes"}\n';
    writeFileSync(join(plans, "my-plan", "maestro-plan.v1"), bytes);
    const digest = new Bun.CryptoHasher("sha256").update(bytes).digest("hex");

    const db = new MaestroDb(
      ledger("named", (seed) => {
        insertRun(seed, "run-named", { plan_digest: digest });
        insertNode(seed, "run-named", "lane", "MERGED", { plan_digest: digest });
        insertRun(seed, "run-stale", { plan_digest: "0".repeat(64) });
        insertNode(seed, "run-stale", "lane", "MERGED", { plan_digest: "0".repeat(64) });
      }),
      plans,
    );
    const byId = new Map(db.runs().map((r) => [r.run_id, r]));
    expect(byId.get("run-named")!.plan_name).toBe("my-plan");
    // Honest about a plan edited since the run: no name rather than a guess.
    expect(byId.get("run-stale")!.plan_name).toBeNull();
    db.close();
  });

  test("a stored plan_name wins over the digest lookup", () => {
    const plans = join(root, "plans-stored");
    mkdirSync(join(plans, "directory-name"), { recursive: true });
    const bytes = '{"plan":"other bytes"}\n';
    writeFileSync(join(plans, "directory-name", "maestro-plan.v1"), bytes);
    const digest = new Bun.CryptoHasher("sha256").update(bytes).digest("hex");

    const db = new MaestroDb(
      ledger("stored-name", (seed) => {
        insertRun(seed, "run-stored", {
          plan_digest: digest,
          plan_name: "IR title from the ledger",
        });
        insertNode(seed, "run-stored", "lane", "MERGED", { plan_digest: digest });
        insertRun(seed, "run-null", { plan_digest: digest, plan_name: null });
        insertNode(seed, "run-null", "lane", "MERGED", { plan_digest: digest });
      }),
      plans,
    );
    const byId = new Map(db.runs().map((r) => [r.run_id, r]));
    expect(byId.get("run-stored")!.plan_name).toBe("IR title from the ledger");
    expect(byId.get("run-null")!.plan_name).toBe("directory-name");
    expect(db.run("run-stored")!.plan_name).toBe("IR title from the ledger");
    expect(db.run("run-null")!.plan_name).toBe("directory-name");
    db.close();
  });

  test("a ledger without the plan_name column still opens and falls back", () => {
    const plans = join(root, "plans-old-col");
    mkdirSync(join(plans, "my-plan"), { recursive: true });
    const bytes = '{"plan":"old ledger"}\n';
    writeFileSync(join(plans, "my-plan", "maestro-plan.v1"), bytes);
    const digest = new Bun.CryptoHasher("sha256").update(bytes).digest("hex");

    const path = ledger("old-plan-name", (seed) => {
      seed.exec("ALTER TABLE runs DROP COLUMN plan_name");
      seed
        .query(
          `INSERT INTO runs (run_id, plan_digest, created_at,
                             last_transition_at, latest_outcome,
                             latest_outcome_at, cancel_requested)
           VALUES (?, ?, '2026-08-17T06:00:00+00:00',
                   '2026-08-17T06:05:00+00:00', NULL, NULL, 0)`,
        )
        .run("run-old", digest);
      insertNode(seed, "run-old", "lane", "MERGED", { plan_digest: digest });
    });
    const db = new MaestroDb(path, plans);
    expect(db.runs()[0]!.plan_name).toBe("my-plan");
    expect(db.run("run-old")!.plan_name).toBe("my-plan");
    db.close();
  });
});

describe("integration worktree", () => {
  test("the branch and head come from git, and absence is not an error", () => {
    const path = ledger("integration", (seed) => {
      insertRun(seed, "run-int");
      insertNode(seed, "run-int", "lane", "MERGED");
      insertRun(seed, "run-noint");
      insertNode(seed, "run-noint", "lane", "MERGED");
    });
    const worktree = join(root, "integration", "runs", "run-int", "integration");
    mkdirSync(worktree, { recursive: true });
    const git = (...args: string[]) =>
      Bun.spawnSync(["git", "-C", worktree, ...args], { stdout: "pipe", stderr: "pipe" });
    git("init", "-q", "-b", "main");
    writeFileSync(join(worktree, "a.txt"), "hi\n");
    git("add", "a.txt");
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "merged lane");

    const db = new MaestroDb(path);
    const found = db.run("run-int")!.integration!;
    expect(found.branch).toBe("main");
    expect(found.head).toMatch(/^[0-9a-f]{40}$/);
    expect(found.subject).toBe("merged lane");
    expect(db.run("run-noint")!.integration).toBeNull();
    db.close();
  });
});

describe("source discovery", () => {
  test("a repo's ledger is located from its maestro.config.yaml", () => {
    const repo = join(root, "repo");
    mkdirSync(join(repo, "adws"), { recursive: true });
    mkdirSync(join(repo, ".maestro", "plans"), { recursive: true });
    writeFileSync(
      join(repo, "adws", "maestro.config.yaml"),
      "schema: maestro-config.v1\nplans_dir: .maestro/plans\nstate_root: ../repo-state\n",
    );
    // Maestro appends the repository's own directory name under state_root.
    const stateDir = join(root, "repo-state", "repo");
    mkdirSync(stateDir, { recursive: true });
    const db = new Database(join(stateDir, "lifecycle.sqlite3"));
    db.exec(runtimeSchema());
    db.close();

    const found = discoverMaestroLedger(repo)!;
    expect(found.db).toBe(join(stateDir, "lifecycle.sqlite3"));
    expect(found.plansDir).toBe(join(repo, ".maestro", "plans"));

    const sources = resolveSources([], repo);
    expect(sources.map((s) => [s.kind, s.id])).toEqual([["maestro", "maestro:repo"]]);
    for (const source of sources) source.close();
  });

  test("two ledgers with the same directory name still get distinct ids", () => {
    const first = ledger(join("dup", "alpha"), () => {});
    const second = ledger(join("dup2", "alpha"), () => {});
    const sources = resolveSources(["--db", first, "--db", second], root);
    expect(sources.map((s) => s.id)).toEqual(["maestro:alpha", "maestro:alpha-2"]);
    for (const source of sources) source.close();
  });

  test("every factory Maestro has run is served with no arguments", () => {
    const first = ledger("factory-one", (seed) => insertRun(seed, "run-one"));
    const second = ledger("factory-two", (seed) => insertRun(seed, "run-two"));
    const registry = join(root, "registry.json");
    writeFileSync(
      registry,
      JSON.stringify({
        installations: [
          { repository: join(root, "one"), database: first, plans_dir: null },
          { repository: join(root, "two"), database: second, plans_dir: null },
        ],
      }),
    );
    const previous = process.env.MAESTRO_REGISTRY;
    process.env.MAESTRO_REGISTRY = registry;
    try {
      // No --db, no MAESTRO_DB, and a cwd that is not a factory at all.
      const sources = resolveSources([], join(root, "elsewhere"));
      expect(sources.map((s) => s.path)).toEqual([first, second]);
      for (const source of sources) source.close();
    } finally {
      if (previous === undefined) delete process.env.MAESTRO_REGISTRY;
      else process.env.MAESTRO_REGISTRY = previous;
    }
  });

  test("a corrupt registry costs discovery, not the server", () => {
    const registry = join(root, "corrupt-registry.json");
    writeFileSync(registry, "{ not json");
    const previous = process.env.MAESTRO_REGISTRY;
    process.env.MAESTRO_REGISTRY = registry;
    try {
      expect(resolveSources([], join(root, "elsewhere"))).toEqual([]);
    } finally {
      if (previous === undefined) delete process.env.MAESTRO_REGISTRY;
      else process.env.MAESTRO_REGISTRY = previous;
    }
  });

  test("an unreadable database is skipped, never fatal", () => {
    const good = ledger("survivor", (seed) => insertRun(seed, "run-ok"));
    const junk = join(root, "survivor", "junk.db");
    writeFileSync(junk, "not sqlite");
    const sources = resolveSources(["--db", junk, "--db", good], root);
    expect(sources.map((s) => s.path)).toEqual([good]);
    for (const source of sources) source.close();
  });
});

/**
 * A node an operator accepted by hand must not render as one the run merged.
 *
 * The dashboard drew `lane-p5-gap-policy` as MERGED with an output SHA over
 * three attempts reading CANCELLED, CANCELLED, BLOCKED and no verdict on any
 * of them (#93). The display was the honest shape of a skip; nothing on it
 * said an operator had asserted the work, so the reasonable reading was that
 * the run merged it and the attempt list was stale.
 */
describe("merge provenance", () => {
  test("a run-merged node and an operator-accepted one read apart", () => {
    const path = ledger("run-provenance", (seed) => {
      insertRun(seed, "run-prov");
      insertNode(seed, "run-prov", "lane-merged", "MERGED", {
        merge_cause: "SCHEDULER",
        output_sha: "a".repeat(40),
      });
      insertNode(seed, "run-prov", "lane-skipped", "MERGED", {
        merge_cause: "OPERATOR_ACCEPTED",
        output_sha: "b".repeat(40),
      });
    });
    const db = new MaestroDb(path);
    const nodes = db.run("run-prov")?.nodes ?? [];
    const byId = new Map(nodes.map((n) => [n.node_id, n]));
    expect(byId.get("lane-merged")?.state).toBe("MERGED");
    expect(byId.get("lane-skipped")?.state).toBe("MERGED");
    expect(byId.get("lane-merged")?.merge_cause).toBe("SCHEDULER");
    expect(byId.get("lane-skipped")?.merge_cause).toBe("OPERATOR_ACCEPTED");
    db.close();
  });

  test("a MERGED row with no recorded cause reads UNRECORDED, not SCHEDULER", () => {
    const path = ledger("run-unrecorded", (seed) => {
      insertRun(seed, "run-old");
      insertNode(seed, "run-old", "lane", "MERGED", {
        output_sha: "c".repeat(40),
      });
    });
    const db = new MaestroDb(path);
    const node = db.run("run-old")?.nodes?.[0];
    expect(node?.merge_cause).toBe("UNRECORDED");
    expect(node?.merge_cause).not.toBe("SCHEDULER");
    db.close();
  });

  test("a ledger written before the column is read, not refused", () => {
    const path = ledger("run-pre-column", (seed) => {
      insertRun(seed, "run-pre");
      insertNode(seed, "run-pre", "lane", "MERGED", {
        output_sha: "d".repeat(40),
      });
      seed.exec("ALTER TABLE node_lifecycle DROP COLUMN merge_cause");
    });
    const db = new MaestroDb(path);
    const node = db.run("run-pre")?.nodes?.[0];
    expect(node?.merge_cause).toBe("UNRECORDED");
    db.close();
  });

  test("a node that is not MERGED carries no provenance at all", () => {
    const path = ledger("run-not-merged", (seed) => {
      insertRun(seed, "run-live");
      insertNode(seed, "run-live", "lane", "BLOCKED", {
        block_reason: "SEMANTIC_BUDGET_EXHAUSTED",
      });
    });
    const db = new MaestroDb(path);
    expect(db.run("run-live")?.nodes?.[0]?.merge_cause).toBeNull();
    db.close();
  });

  test("mergeProvenance is the one derivation and has four answers", () => {
    expect(mergeProvenance("MERGED", "SCHEDULER")).toBe("SCHEDULER");
    expect(mergeProvenance("MERGED", "OPERATOR_ACCEPTED")).toBe("OPERATOR_ACCEPTED");
    expect(mergeProvenance("MERGED", null)).toBe("UNRECORDED");
    expect(mergeProvenance("BLOCKED", null)).toBeNull();
    expect(mergeProvenance("VERIFIED", null)).toBeNull();
  });
});

describe("run scheduler liveness", () => {
  const host = hostname();
  const liveEpoch = processStartEpoch(process.pid);

  test("dead pid, matching host and epoch, no outcome → abandoned, not RUNNING", () => {
    const path = ledger("sched-dead", (seed) => {
      insertRun(seed, "run-dead", {
        scheduler_pid: 2_000_000_000,
        scheduler_host: host,
        scheduler_start_epoch: 1.0,
      });
      insertNode(seed, "run-dead", "lane-a", "RUNNING");
    });
    const db = new MaestroDb(path);
    const run = db.run("run-dead");
    expect(run?.state).toBe("ABANDONED");
    expect(run?.scheduler_liveness).toBe("abandoned");
    expect(run?.declared_outcome).toBeNull();
    expect(db.runs()[0]?.state).toBe("ABANDONED");
    db.close();
  });

  test("live scheduler still reads RUNNING", () => {
    expect(liveEpoch).not.toBeNull();
    const path = ledger("sched-live", (seed) => {
      insertRun(seed, "run-live", {
        scheduler_pid: process.pid,
        scheduler_host: host,
        scheduler_start_epoch: liveEpoch,
      });
      insertNode(seed, "run-live", "lane-a", "RUNNING");
    });
    const db = new MaestroDb(path);
    const run = db.run("run-live");
    expect(run?.state).toBe("RUNNING");
    expect(run?.scheduler_liveness).toBe("running");
    db.close();
  });

  test("null scheduler fields → unknown, still RUNNING", () => {
    const path = ledger("sched-null", (seed) => {
      insertRun(seed, "run-old");
      insertNode(seed, "run-old", "lane-a", "RUNNING");
    });
    const db = new MaestroDb(path);
    const run = db.run("run-old");
    expect(run?.state).toBe("RUNNING");
    expect(run?.scheduler_liveness).toBe("unknown");
    db.close();
  });


  test("foreign host → unknown, not abandoned", () => {
    const path = ledger("sched-foreign", (seed) => {
      insertRun(seed, "run-x", {
        scheduler_pid: process.pid,
        scheduler_host: "other-box",
        scheduler_start_epoch: 1.0,
      });
      insertNode(seed, "run-x", "lane-a", "RUNNING");
    });
    const db = new MaestroDb(path);
    const run = db.run("run-x");
    expect(run?.state).toBe("RUNNING");
    expect(run?.scheduler_liveness).toBe("unknown");
    db.close();
  });

  test("epoch mismatch → unknown, not abandoned", () => {
    const path = ledger("sched-epoch", (seed) => {
      insertRun(seed, "run-x", {
        scheduler_pid: process.pid,
        scheduler_host: host,
        scheduler_start_epoch: 1.0,
      });
      insertNode(seed, "run-x", "lane-a", "RUNNING");
    });
    const db = new MaestroDb(path);
    const run = db.run("run-x");
    expect(run?.state).toBe("RUNNING");
    expect(run?.scheduler_liveness).toBe("unknown");
    db.close();
  });

  test("pipeline ledger run-774cb… is abandoned, not RUNNING", () => {
    const real = join(
      homedir(),
      "PycharmProjects/.maestro-state/lexgenius-pipeline/lifecycle.sqlite3",
    );
    if (!existsSync(real)) return;
    const db = new MaestroDb(real);
    const run = db.runs().find((row) =>
      row.run_id.startsWith("run-774cb4967117"),
    );
    expect(run).toBeDefined();
    expect(run?.state).toBe("ABANDONED");
    expect(run?.scheduler_liveness).toBe("abandoned");
    expect(run?.declared_outcome).toBeNull();
    db.close();
  });
});

describe("persistent review lifecycle projection", () => {
  test("lane phase, candidates, reviews, findings, and handoffs are authoritative", () => {
    const candidateSha = "a".repeat(40);
    const findings = [{
      check_id: "diff.contract",
      object_id: "src/lane.ts",
      message: "repair the rejected contract",
      blocking: true,
    }];
    const path = ledger("review-lifecycle", (seed) => {
      insertRun(seed, "run-review");
      insertNode(seed, "run-review", "lane-bronze", "RUNNING", {
        attempt_no: 1,
        lane_phase: "REPAIRING",
      });
      insertNode(seed, "run-review", "lane-bronze::review", "RUNNING");
      seed.query("UPDATE dag_nodes SET kind='review' WHERE run_id=? AND node_id=?")
        .run("run-review", "lane-bronze::review");
      seed.query(
        `INSERT INTO lane_candidates
           (run_id, build_node_id, candidate_seq, candidate_sha,
            parent_candidate_sha, builder_generation, created_at)
         VALUES (?, ?, 1, ?, NULL, 2, ?)`,
      ).run("run-review", "lane-bronze", candidateSha, "2026-08-26T00:00:00Z");
      seed.query(
        `INSERT INTO candidate_reviews
           (run_id, review_node_id, candidate_sha, reviewer_generation, state,
            review_digest, receipt_path, findings_json, verdict, completed_at)
         VALUES (?, ?, ?, 3, 'COMPLETED', 'digest', '/receipt.json', ?,
                 'REJECTED', ?)`,
      ).run(
        "run-review",
        "lane-bronze::review",
        candidateSha,
        JSON.stringify(findings),
        "2026-08-26T00:01:00Z",
      );
      seed.query(
        `INSERT INTO repair_handoffs
           (run_id, build_node_id, rejected_candidate_sha, findings_json, state,
            builder_generation, submitted_at, acknowledged_at)
         VALUES (?, ?, ?, ?, 'ACKNOWLEDGED', 2, ?, ?)`,
      ).run(
        "run-review",
        "lane-bronze",
        candidateSha,
        JSON.stringify(findings),
        "2026-08-26T00:02:00Z",
        "2026-08-26T00:03:00Z",
      );
    });

    const db = new MaestroDb(path);
    const run = db.run("run-review")!;

    expect(run.nodes.find((node) => node.node_id === "lane-bronze")?.lane_phase)
      .toBe("REPAIRING");
    expect(run.lane_candidates[0]?.candidate_sha).toBe(candidateSha);
    expect(run.candidate_reviews[0]?.verdict).toBe("REJECTED");
    expect(run.candidate_reviews[0]?.findings).toEqual(findings);
    expect(run.repair_handoffs[0]?.state).toBe("ACKNOWLEDGED");
    expect(run.repair_handoffs[0]?.findings).toEqual(findings);
    db.close();
  });

  test("a live non-immutable reader sees actor and review tables created after first open", () => {
    const path = ledger("late-review-schema", (seed) => {
      insertRun(seed, "run-late");
      insertNode(seed, "run-late", "lane", "RUNNING", { lane_phase: "BUILDING" });
    });
    const writer = new Database(path);
    writer.exec("PRAGMA journal_mode=WAL");
    for (const table of [
      "actor_sessions",
      "repair_handoffs",
      "candidate_reviews",
      "lane_candidates",
    ]) {
      writer.exec(`DROP TABLE ${table}`);
    }
    const db = new MaestroDb(path);
    const before = db.run("run-late")!;
    expect(before.actor_sessions).toEqual([]);
    expect(before.lane_candidates).toEqual([]);
    expect(before.candidate_reviews).toEqual([]);
    expect(before.repair_handoffs).toEqual([]);

    for (const table of [
      "lane_candidates",
      "candidate_reviews",
      "repair_handoffs",
      "actor_sessions",
    ]) {
      writer.exec(runtimeTableSchema(table));
    }
    const candidateSha = "b".repeat(40);
    writer.query(
      `INSERT INTO lane_candidates
         (run_id, build_node_id, candidate_seq, candidate_sha,
          parent_candidate_sha, builder_generation, created_at)
       VALUES ('run-late', 'lane', 1, ?, NULL, 1, '2026-08-26T00:00:00Z')`,
    ).run(candidateSha);
    writer.query(
      // `dispatched_at` is not optional on a DISPATCHED row: the runtime's
      // CHECK requires it, because a dispatch claim with no submission proof
      // is exactly the state that column exists to make unrepresentable.
      `INSERT INTO candidate_reviews
         (run_id, review_node_id, candidate_sha, reviewer_generation, state,
          dispatched_at, review_digest, receipt_path, findings_json, verdict,
          completed_at)
       VALUES ('run-late', 'lane::review', ?, 1, 'DISPATCHED',
               '2026-08-26T00:00:00Z', NULL, NULL, '[]', NULL, NULL)`,
    ).run(candidateSha);
    writer.query(
      `INSERT INTO actor_sessions
         (run_id, build_node_id, actor_role, generation, state, pane_id,
          session_path, correlation_token, updated_at)
       VALUES ('run-late', 'lane', 'reviewer', 1, 'ACTIVE', 'pane-1',
               '/reviewer.jsonl', 'token', '2026-08-26T00:00:00Z')`,
    ).run();

    const after = db.run("run-late")!;
    expect(after.actor_sessions[0]?.actor_role).toBe("reviewer");
    expect(after.lane_candidates[0]?.candidate_sha).toBe(candidateSha);
    expect(after.candidate_reviews[0]?.state).toBe("DISPATCHED");
    expect(after.repair_handoffs).toEqual([]);
    db.close();
    writer.close();
  });
});



describe("actor session projection", () => {
  test("retained Herdr actor generations are exposed with the run", () => {
    const path = ledger("actor-sessions", (seed) => {
      insertRun(seed, "run-actors", { plan_name: "corpus recovery" });
      insertNode(seed, "run-actors", "lane-bronze", "RUNNING", {
        attempt_no: 7,
        lane_phase: "REVIEWING",
      });
      seed.query(
        `INSERT INTO actor_sessions
           (run_id, build_node_id, actor_role, generation, state, pane_id,
            session_path, correlation_token, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).run(
        "run-actors",
        "lane-bronze",
        "builder",
        7,
        "ACTIVE",
        "w1C%2:p1",
        "/sessions/builder.jsonl",
        "builder-token",
        "2026-08-25T07:00:00+00:00",
      );
    });
    const db = new MaestroDb(path);
    expect(db.run("run-actors")?.actor_sessions).toEqual([
      {
        build_node_id: "lane-bronze",
        actor_role: "builder",
        generation: 7,
        state: "ACTIVE",
        pane_id: "w1C%2:p1",
        session_path: "/sessions/builder.jsonl",
        correlation_token: "builder-token",
        updated_at: "2026-08-25T07:00:00+00:00",
      },
    ]);
    db.close();
  });

  test("a pre-actor-session ledger remains readable", () => {
    const path = ledger("no-actor-sessions", (seed) => {
      insertRun(seed, "run-old");
      insertNode(seed, "run-old", "lane", "PENDING");
      seed.exec("DROP TABLE actor_sessions");
    });
    const db = new MaestroDb(path);
    expect(db.run("run-old")?.actor_sessions).toEqual([]);
    db.close();
  });
});
