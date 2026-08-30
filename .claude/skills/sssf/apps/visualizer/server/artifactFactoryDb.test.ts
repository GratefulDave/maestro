/**
 * Artifact-factory reporting adapter against the runtime SCHEMA.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { ArtifactFactoryDb, publicArtifactBody } from "./artifactFactoryDb.ts";
import { probeKind, resolveSources } from "./sources.ts";
import { discoverMaestroLedger } from "./maestroDb.ts";

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

const PRIVATE_SOURCE = "def test_secret(): assert 'SECRET_LITERAL_NEVER_IN_API' == expected";
const PRIVATE_TOKEN = "SECRET_LITERAL_NEVER_IN_API";

function runtimeSchema(): string {
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
    throw new Error(`could not read SCHEMA from ${LIFECYCLE_PY}: ${result.stderr.toString()}`);
  }
  return result.stdout.toString();
}

let root: string;
let previousRegistry: string | undefined;
const DIGEST = "ab".repeat(32);

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "artifact-factory-report-"));
  previousRegistry = process.env.MAESTRO_REGISTRY;
  process.env.MAESTRO_REGISTRY = join(root, "absent-registry.json");
});

afterAll(() => {
  if (previousRegistry === undefined) delete process.env.MAESTRO_REGISTRY;
  else process.env.MAESTRO_REGISTRY = previousRegistry;
  rmSync(root, { recursive: true, force: true });
});

function factoryLedger(name: string, seed: (db: Database) => void): string {
  const dir = join(root, name);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, "lifecycle.sqlite3");
  const db = new Database(path);
  db.exec("PRAGMA journal_mode=WAL");
  db.exec("PRAGMA foreign_keys=OFF");
  db.exec(runtimeSchema());
  seed(db);
  db.close();
  return path;
}

function legacyLedger(name: string): string {
  const dir = join(root, name);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, "lifecycle.sqlite3");
  const db = new Database(path);
  db.exec(`
    CREATE TABLE runs (run_id TEXT PRIMARY KEY, plan_digest TEXT, created_at TEXT);
    CREATE TABLE dag_nodes (run_id TEXT, node_id TEXT);
    CREATE TABLE node_lifecycle (run_id TEXT, node_id TEXT, state TEXT);
    CREATE TABLE attempts (run_id TEXT, node_id TEXT, attempt_no INTEGER);
  `);
  db.query("INSERT INTO runs (run_id, plan_digest, created_at) VALUES ('legacy-run', ?, ?)")
    .run(DIGEST, "2026-08-01T00:00:00+00:00");
  db.close();
  return path;
}

function insertFactoryRun(
  db: Database,
  runId: string,
  lanes: { id: string; needs: string[]; stage: string }[],
): void {
  const now = "2026-08-29T12:00:00+00:00";
  db.query(
    `INSERT INTO plan_revisions (run_id, plan_revision, plan_digest, parent_revision,
                                 plan_artifact_ref, amendment_artifact_id, created_at)
     VALUES (?, 1, ?, NULL, 'plan-ref', NULL, ?)`,
  ).run(runId, DIGEST, now);
  db.query(
    `INSERT INTO runs (
        run_id, runtime_state_root, runtime_state_fingerprint, plan_digest, plan_revision,
        integration_ref, integration_initial_sha, target_repository_root, target_git_common_dir,
        target_worktree_git_dir, target_object_format, target_repository_fingerprint,
        target_sync_journal_fingerprint, target_initial_main_sha, target_main_ref,
        created_at, updated_at)
     VALUES (?, '/tmp/state', ?, ?, 1, 'refs/heads/integration', ?, '/tmp/product',
             '/tmp/product/.git', '/tmp/product/.git', 'sha1', ?, ?, ?, 'refs/heads/main', ?, ?)`,
  ).run(runId, DIGEST, DIGEST, DIGEST, DIGEST, DIGEST, DIGEST, now, now);
  for (const lane of lanes) {
    db.query(
      `INSERT INTO dag_lanes (run_id, plan_revision, lane_id, needs_json, spec_digest,
                              declared_outputs_json, lane_projection_digest, public_acceptance_json)
       VALUES (?, 1, ?, ?, ?, '["out.py"]', ?, '[]')`,
    ).run(runId, lane.id, JSON.stringify(lane.needs), DIGEST, DIGEST);
    db.query(
      `INSERT INTO lane_state (run_id, lane_id, stage, updated_at) VALUES (?, ?, ?, ?)`,
    ).run(runId, lane.id, lane.stage, now);
  }
}

function insertLaneArtifact(
  db: Database,
  opts: {
    id: string;
    runId: string;
    laneId: string;
    sequence: number;
    stage: string;
    kind: string;
    payload: unknown;
  },
): void {
  db.query(
    `INSERT INTO lane_artifacts (
        artifact_id, run_id, lane_id, sequence, completed_stage, artifact_kind,
        plan_revision, spec_digest, lane_projection_digest, input_digest, output_digest,
        artifact_ref, payload_json, created_at)
     VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, '2026-08-29T12:01:00+00:00')`,
  ).run(
    opts.id,
    opts.runId,
    opts.laneId,
    opts.sequence,
    opts.stage,
    opts.kind,
    DIGEST,
    DIGEST,
    DIGEST,
    DIGEST,
    `ref:${opts.id}`,
    JSON.stringify(opts.payload),
  );
}

function insertRunArtifact(
  db: Database,
  opts: {
    id: string;
    runId: string;
    sequence: number;
    kind: string;
    payload: unknown;
  },
): void {
  db.query(
    `INSERT INTO run_artifacts (
        artifact_id, run_id, sequence, artifact_kind, plan_revision, input_digest,
        output_digest, artifact_ref, payload_json, created_at)
     VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, '2026-08-29T12:02:00+00:00')`,
  ).run(
    opts.id,
    opts.runId,
    opts.sequence,
    opts.kind,
    DIGEST,
    DIGEST,
    `ref:${opts.id}`,
    JSON.stringify(opts.payload),
  );
}

describe("schema probe", () => {
  test("detects artifact-factory and legacy schemas without confusing them", () => {
    const factory = factoryLedger("probe-factory", (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-a", [{ id: "lane-a", needs: [], stage: "PLANNED" }]);
    });
    const legacy = legacyLedger("probe-legacy");
    expect(probeKind(factory)).toBe("artifact-factory");
    expect(probeKind(legacy)).toBe("maestro");
    expect(probeKind(join(root, "missing.sqlite3"))).toBeNull();
  });

  test("a non-sqlite file is skipped", () => {
    const junk = join(root, "junk.db");
    writeFileSync(junk, "not sqlite");
    expect(probeKind(junk)).toBeNull();
  });
});

describe("source identities", () => {
  test("legacy and factory ledgers coexist under deterministic ids", () => {
    const legacy = legacyLedger(join("hist", "FDAdb"));
    const factory = factoryLedger(join("current", "FDAdb"), (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-now", [{ id: "lane-a", needs: [], stage: "BUILDING" }]);
    });
    const forward = resolveSources(["--db", legacy, "--db", factory], root);
    const reverse = resolveSources(["--db", factory, "--db", legacy], root);
    expect(forward.map((s) => [s.kind, s.id, s.info().schema_version])).toEqual([
      ["maestro", "maestro:FDAdb", "legacy-lifecycle"],
      ["artifact-factory", "artifact-factory:FDAdb", "artifact-factory.v1"],
    ]);
    expect(reverse.map((s) => s.id).sort()).toEqual(["artifact-factory:FDAdb", "maestro:FDAdb"]);
    for (const source of [...forward, ...reverse]) source.close();
  });

  test("registry keeps the legacy database and discovers runtime_state_root ledger", () => {
    const repo = join(root, "product-repo");
    mkdirSync(join(repo, "adws"), { recursive: true });
    const stateDir = join(root, "maestro-artifact-factory", "fdadb");
    mkdirSync(stateDir, { recursive: true });
    const plansDir = join(repo, ".maestro", "plans");
    mkdirSync(plansDir, { recursive: true });
    const planName = "registered-factory-plan.v1";
    const planBody = JSON.stringify({
      schema_version: "maestro-plan.artifact-factory.v1",
      lanes: [],
    });
    writeFileSync(join(plansDir, `${planName}.json`), planBody);
    const planDigest = new Bun.CryptoHasher("sha256").update(planBody).digest("hex");
    writeFileSync(
      join(repo, "adws", "maestro.config.yaml"),
      [
        "schema: maestro-config.v1",
        `runtime_state_root: ${stateDir}`,
        "runner_profile: grok",
        "",
      ].join("\n"),
    );
    const factoryPath = join(stateDir, "lifecycle.sqlite3");
    const seed = new Database(factoryPath);
    seed.exec("PRAGMA foreign_keys=OFF");
    seed.exec(runtimeSchema());
    seed.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
    insertFactoryRun(seed, "run-now", [{ id: "lane-a", needs: [], stage: "BUILDING" }]);
    seed.query("UPDATE runs SET plan_digest = ? WHERE run_id = 'run-now'").run(planDigest);
    seed
      .query("UPDATE plan_revisions SET plan_digest = ? WHERE run_id = 'run-now'")
      .run(planDigest);
    seed.close();

    const found = discoverMaestroLedger(repo)!;
    expect(found.db).toBe(factoryPath);
    expect(found.plansDir).toBeNull();

    const legacy = legacyLedger("registry-legacy");
    const registry = join(root, "registry.json");
    writeFileSync(
      registry,
      JSON.stringify({
        installations: [
          {
            repository: repo,
            database: legacy,
            plans_dir: plansDir,
            state: join(root, "legacy-state"),
          },
        ],
      }),
    );
    const previousPlans = process.env.MAESTRO_PLANS;
    process.env.MAESTRO_REGISTRY = registry;
    delete process.env.MAESTRO_PLANS;
    try {
      const sources = resolveSources([], join(root, "elsewhere"));
      expect(sources.map((s) => s.path).sort()).toEqual([legacy, factoryPath].sort());
      expect(sources.some((s) => s.kind === "maestro" && s.path === legacy)).toBe(true);
      const factory = sources.find(
        (source) => source.kind === "artifact-factory" && source.path === factoryPath,
      );
      expect(factory?.artifactFactory?.runs()[0]?.plan_name).toBe(planName);
      for (const source of sources) source.close();
    } finally {
      process.env.MAESTRO_REGISTRY = join(root, "absent-registry.json");
      if (previousPlans === undefined) delete process.env.MAESTRO_PLANS;
      else process.env.MAESTRO_PLANS = previousPlans;
    }
  });
});

describe("run/lane/stage/artifact mapping", () => {
  test("maps durable stage, dependencies, transitions, and never fabricates attempts", () => {
    const path = factoryLedger("mapped", (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-1", [
        { id: "lane-a", needs: [], stage: "MERGED" },
        { id: "lane-b", needs: ["lane-a"], stage: "WRITING_TESTS" },
      ]);
      insertLaneArtifact(db, {
        id: "art-draft",
        runId: "run-1",
        laneId: "lane-b",
        sequence: 1,
        stage: "WRITING_TESTS",
        kind: "TEST_DRAFT",
        payload: {
          source: PRIVATE_SOURCE,
          private_files: { "tests/test_secret.py": PRIVATE_TOKEN },
          selectors: ["test_secret"],
          expected: PRIVATE_TOKEN,
          vault: "vault://hidden",
        },
      });
      insertLaneArtifact(db, {
        id: "art-test-review",
        runId: "run-1",
        laneId: "lane-b",
        sequence: 2,
        stage: "REVIEWING_TESTS",
        kind: "TEST_REVIEW",
        payload: {
          verdict: "REVISE",
          findings: [{ message: `${PRIVATE_TOKEN} failed at line 3` }],
        },
      });
      insertLaneArtifact(db, {
        id: "art-code-review",
        runId: "run-1",
        laneId: "lane-a",
        sequence: 1,
        stage: "REVIEWING_CODE",
        kind: "CODE_REVIEW",
        payload: {
          verdict: "REVISE",
          findings: [
            {
              implementation_area: "declared product outputs",
              observed_behavior: "sealed private tests failed",
              required_behavior: "the candidate must pass every sealed private test",
              violated_requirement: "accepted sealed tests bind the candidate",
            },
          ],
          private_files: { leaked: PRIVATE_TOKEN },
        },
      });
      db.query(
        `INSERT INTO transitions (run_id, lane_id, from_stage, to_stage, artifact_id, reason, created_at)
         VALUES ('run-1', 'lane-b', 'PLANNED', 'WRITING_TESTS', 'art-draft', 'complete_stage',
                 '2026-08-29T12:01:00+00:00')`,
      ).run();
    });

    const db = new ArtifactFactoryDb(path);
    const list = db.runs();
    expect(list).toHaveLength(1);
    expect(list[0]!.state).toBe("RUNNING");
    expect(list[0]!.declared_outcome).toBeNull();
    expect(list[0]!.node_states.map((n) => [n.node_id, n.state])).toEqual([
      ["lane-a", "MERGED"],
      ["lane-b", "WRITING_TESTS"],
    ]);

    const detail = db.run("run-1")!;
    expect(detail.nodes.map((n) => n.attempts)).toEqual([[], []]);
    const laneB = detail.nodes.find((n) => n.node_id === "lane-b")!;
    expect(laneB.lane_phase).toBe("WRITING_TESTS");
    expect(laneB.state).toBe("WRITING_TESTS");
    expect(laneB.needs).toEqual(["lane-a"]);
    expect(laneB.kind).toBeNull();
    expect(laneB.depth).toBe(1);
    expect(detail.run_transitions).toEqual([
      expect.objectContaining({
        node_id: "lane-b",
        from_state: "PLANNED",
        to_state: "WRITING_TESTS",
        reason: "complete_stage",
        actor: null,
      }),
    ]);
    expect(JSON.stringify(detail.results)).not.toContain("TEST_DRAFT");
    const code = detail.results.find((r) => (r.payload as { artifact_id: string }).artifact_id === "art-code-review")!;
    expect(code.adjudication).toBe("REVISE");
    expect(code.payload).toEqual(
      expect.objectContaining({
        artifact_kind: "CODE_REVIEW",
        role: "code-reviewer",
        verdict: "REVISE",
        findings: [
          expect.objectContaining({ implementation_area: "declared product outputs" }),
        ],
      }),
    );
    expect(code.payload).not.toHaveProperty("private_files");
    const testReview = detail.results.find(
      (r) => (r.payload as { artifact_id: string }).artifact_id === "art-test-review",
    )!;
    expect(testReview.payload).not.toHaveProperty("findings");
    expect(testReview.adjudication).toBe("REVISE");
    db.close();
  });

  test("projects final review and publication from run artifacts", () => {
    const pendingPath = factoryLedger("final-review-pending", (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-pending", [{ id: "lane-a", needs: [], stage: "MERGED" }]);
    });
    const reviewedPath = factoryLedger("final-review-pass", (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-reviewed", [{ id: "lane-a", needs: [], stage: "MERGED" }]);
      insertRunArtifact(db, {
        id: "art-final-review",
        runId: "run-reviewed",
        sequence: 1,
        kind: "FINAL_INTEGRATION_REVIEW",
        payload: { verdict: "PASS", findings: [] },
      });
    });
    const publishedPath = factoryLedger("published", (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-published", [{ id: "lane-a", needs: [], stage: "MERGED" }]);
      insertRunArtifact(db, {
        id: "art-publication",
        runId: "run-published",
        sequence: 1,
        kind: "MAIN_PUBLICATION",
        payload: {},
      });
    });

    const pending = new ArtifactFactoryDb(pendingPath);
    const reviewed = new ArtifactFactoryDb(reviewedPath);
    const published = new ArtifactFactoryDb(publishedPath);
    expect(pending.runs()[0]!.state).toBe("INTEGRATION_REVIEW_PENDING");
    expect(reviewed.runs()[0]!.state).toBe("PUBLISHABLE");
    const finalReview = reviewed.run("run-reviewed")!.results.find((result) => {
      const payload = result.payload;
      return (
        payload !== null &&
        typeof payload === "object" &&
        "artifact_kind" in payload &&
        payload.artifact_kind === "FINAL_INTEGRATION_REVIEW"
      );
    })!;
    expect(finalReview.adjudication).toBe("PASS");
    expect(finalReview.payload).toEqual(
      expect.objectContaining({ role: "integration-reviewer", verdict: "PASS" }),
    );
    expect(published.runs()[0]!.state).toBe("COMPLETE");
    pending.close();
    reviewed.close();
    published.close();
  });

  test("resolves a direct artifact-factory plan filename", () => {
    const plans = join(root, "direct-plans");
    mkdirSync(plans, { recursive: true });
    const body = JSON.stringify({
      schema_version: "maestro-plan.artifact-factory.v1",
      lanes: [],
    });
    const name = "fdadb-v2-wp6-geo-layer-r2.v1.json";
    writeFileSync(join(plans, name), body);
    const digest = new Bun.CryptoHasher("sha256").update(body).digest("hex");
    const path = factoryLedger("direct-plan", (ledger) => {
      ledger
        .query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')")
        .run();
      insertFactoryRun(ledger, "run-direct-plan", [
        { id: "lane-a", needs: [], stage: "PLANNED" },
      ]);
      ledger
        .query("UPDATE runs SET plan_digest = ? WHERE run_id = 'run-direct-plan'")
        .run(digest);
      ledger
        .query(
          "UPDATE plan_revisions SET plan_digest = ? WHERE run_id = 'run-direct-plan'",
        )
        .run(digest);
    });

    const db = new ArtifactFactoryDb(path, plans);
    expect(db.runs()[0]!.plan_name).toBe("fdadb-v2-wp6-geo-layer-r2.v1");
    db.close();
  });

  test("private test content never appears in API output", () => {
    const path = factoryLedger("leak", (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-secret", [{ id: "lane-a", needs: [], stage: "REVIEWING_TESTS" }]);
      insertLaneArtifact(db, {
        id: "draft",
        runId: "run-secret",
        laneId: "lane-a",
        sequence: 1,
        stage: "WRITING_TESTS",
        kind: "TEST_DRAFT",
        payload: {
          source: PRIVATE_SOURCE,
          private_files: { "tests/test_secret.py": PRIVATE_TOKEN },
          selectors: ["::test_secret"],
          expected: PRIVATE_TOKEN,
        },
      });
      insertLaneArtifact(db, {
        id: "review",
        runId: "run-secret",
        laneId: "lane-a",
        sequence: 2,
        stage: "REVIEWING_TESTS",
        kind: "TEST_REVIEW",
        payload: { verdict: "REVISE", findings: [{ message: PRIVATE_TOKEN }] },
      });
    });
    const db = new ArtifactFactoryDb(path);
    const dumped = JSON.stringify(db.run("run-secret"));
    expect(dumped).not.toContain(PRIVATE_TOKEN);
    expect(dumped).not.toContain("def test_secret");
    expect(dumped).not.toContain("private_files");
    expect(dumped).not.toContain("selectors");
    expect(dumped).not.toContain(PRIVATE_SOURCE);
    expect(dumped).not.toContain("TEST_DRAFT");
    db.close();
  });

  test("WAITING_FOR_USER is the live state when a lane is waiting", () => {
    const path = factoryLedger("waiting", (db) => {
      db.query("INSERT INTO ledger_meta (schema_version) VALUES ('artifact-factory.v1')").run();
      insertFactoryRun(db, "run-wait", [{ id: "lane-a", needs: [], stage: "WAITING_FOR_USER" }]);
    });
    const db = new ArtifactFactoryDb(path);
    expect(db.runs()[0]!.state).toBe("WAITING_FOR_USER");
    db.close();
  });
});

describe("payload allowlist", () => {
  test("strips non-public keys even when called directly", () => {
    const body = publicArtifactBody("TEST_DRAFT", JSON.stringify({
      source: PRIVATE_SOURCE,
      verdict: "PASS",
    }));
    expect(body).toEqual({ verdict: "PASS", role: "tester" });
  });
});
