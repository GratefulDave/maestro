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
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { MaestroDb, discoverMaestroLedger } from "./maestroDb.ts";
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

function runtimeSchema(): string {
  const source = readFileSync(LIFECYCLE_PY, "utf8");
  const match = source.match(/^SCHEMA = """([\S\s]*?)"""/m);
  if (!match) throw new Error(`no SCHEMA literal in ${LIFECYCLE_PY}`);
  return match[1] as string;
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
    cancel_requested: number;
  }> = {},
) {
  const row = {
    plan_digest: "d".repeat(64),
    created_at: "2026-08-17T06:00:00+00:00",
    last_transition_at: "2026-08-17T06:05:00+00:00",
    latest_outcome: null,
    cancel_requested: 0,
    ...overrides,
  };
  db.query(
    `INSERT INTO runs (run_id, plan_digest, created_at, last_transition_at,
                       latest_outcome, latest_outcome_at, cancel_requested)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    runId,
    row.plan_digest,
    row.created_at,
    row.last_transition_at,
    row.latest_outcome,
    row.latest_outcome === null ? null : row.last_transition_at,
    row.cancel_requested,
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
    output_sha: string | null;
    plan_digest: string;
  }> = {},
) {
  const o = {
    depth: 0,
    needs: [] as string[],
    attempt_no: 0,
    block_reason: null as string | null,
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
    `INSERT INTO node_lifecycle (run_id, node_id, state, attempt_no, block_reason,
                                 output_sha, granted_extra_attempts, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, 0, '2026-08-17T06:05:00+00:00')`,
  ).run(runId, nodeId, state, o.attempt_no, o.block_reason, o.output_sha);
}

function insertAttempt(
  db: Database,
  runId: string,
  nodeId: string,
  attemptNo: number,
  state: string,
  opts: Partial<{ retry_class: string | null; turn_count: number; extra: object }> = {},
) {
  const o = { retry_class: null as string | null, turn_count: 0, extra: {}, ...opts };
  db.query(
    `INSERT INTO attempts (run_id, node_id, attempt_no, base_sha, state, started_at,
                           launched_at, pid, turn_count, retry_class, extra_json)
     VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)`,
  ).run(
    runId,
    nodeId,
    attemptNo,
    "a".repeat(40),
    state,
    1_786_948_000 + attemptNo,
    1_786_948_010 + attemptNo,
    o.turn_count,
    o.retry_class,
    JSON.stringify(o.extra),
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

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "maestro-viz-"));
});
afterAll(() => {
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
    const inFlight = db
      .run("run-dag")!
      .nodes.flatMap((n) => n.attempts)
      .filter((a) => a.running);
    expect(inFlight).toHaveLength(1);
    expect(inFlight[0]!.attempt_no).toBe(2);
    expect(inFlight[0]!.turn_count).toBe(17);
    expect(inFlight[0]!.session_path).toBe("/tmp/a2.jsonl");
    // Seconds in the ledger, milliseconds out of the reader.
    expect(inFlight[0]!.launched_at_ms).toBeGreaterThan(1_700_000_000_000);
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
