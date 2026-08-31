import { describe, expect, test } from "bun:test";
import type { MaestroRunSummary } from "@/lib/types";
import { runsListHref, shouldHideBarrenRun } from "@/lib/runVisibility";

function summary(
  overrides: Partial<MaestroRunSummary> & Pick<MaestroRunSummary, "run_id">,
): MaestroRunSummary {
  return {
    plan_name: null,
    plan_digest: "digest",
    state: "SUCCEEDED",
    scheduler_liveness: "not_running",
    declared_outcome: "SUCCEEDED",
    declared_outcome_at: "2026-08-22T00:00:00Z",
    cancel_cause: null,
    resumable: false,
    cancel_requested: false,
    created_at: "2026-08-22T00:00:00Z",
    last_transition_at: "2026-08-22T00:00:00Z",
    node_count: overrides.node_states?.length ?? 1,
    node_states: [{ node_id: "n1", state: "READY" }],
    ...overrides,
  };
}

const finishedBarren = summary({
  run_id: "finished-zero",
  node_count: 5,
  node_states: [
    { node_id: "a", state: "READY" },
    { node_id: "b", state: "READY" },
  ],
});

const inFlightBarren = summary({
  run_id: "inflight-zero",
  state: "RUNNING",
  scheduler_liveness: "running",
  declared_outcome: null,
  declared_outcome_at: null,
  node_count: 5,
  node_states: [{ node_id: "a", state: "RUNNING" }],
});

const pendingBarren = summary({
  run_id: "pending-zero",
  state: "PENDING",
  scheduler_liveness: "running",
  declared_outcome: null,
  declared_outcome_at: null,
  node_states: [{ node_id: "a", state: "PENDING" }],
});

const cancellingBarren = summary({
  run_id: "cancelling-zero",
  state: "CANCELLING",
  scheduler_liveness: "running",
  declared_outcome: null,
  declared_outcome_at: null,
  node_states: [{ node_id: "a", state: "RUNNING" }],
});

const blockedBarren = summary({
  run_id: "blocked-zero",
  state: "BLOCKED",
  declared_outcome: "BLOCKED",
  node_states: [{ node_id: "a", state: "BLOCKED" }],
});

const cancelledBarren = summary({
  run_id: "cancelled-zero",
  state: "CANCELLED",
  declared_outcome: "CANCELLED",
  node_states: [{ node_id: "a", state: "CANCELLED" }],
});

const abandonedBarren = summary({
  run_id: "abandoned-zero",
  state: "ABANDONED",
  scheduler_liveness: "abandoned",
  declared_outcome: null,
  declared_outcome_at: null,
  node_states: [{ node_id: "a", state: "RUNNING" }],
});

const unknownStillRunning = summary({
  run_id: "unknown-running",
  state: "RUNNING",
  scheduler_liveness: "unknown",
  declared_outcome: null,
  declared_outcome_at: null,
  node_states: [{ node_id: "a", state: "RUNNING" }],
});

const nodeBlockedBarren = summary({
  run_id: "node-blocked-zero",
  state: "SUCCEEDED",
  declared_outcome: "SUCCEEDED",
  node_states: [{ node_id: "a", state: "BLOCKED" }],
});

const mergedOnce = summary({
  run_id: "one-merged",
  node_states: [
    { node_id: "a", state: "MERGED" },
    { node_id: "b", state: "READY" },
  ],
});

const fleet: MaestroRunSummary[] = [
  finishedBarren,
  inFlightBarren,
  pendingBarren,
  cancellingBarren,
  blockedBarren,
  cancelledBarren,
  nodeBlockedBarren,
  mergedOnce,
  summary({
    run_id: "two-merged",
    node_states: [
      { node_id: "a", state: "MERGED" },
      { node_id: "b", state: "MERGED" },
    ],
  }),
  summary({
    run_id: "failed-merged",
    state: "FAILED",
    declared_outcome: "FAILED",
    node_states: [{ node_id: "a", state: "MERGED" }],
  }),
  summary({
    run_id: "finished-barren-2",
    node_states: [{ node_id: "a", state: "READY" }],
  }),
];

/** Shapes from lexgenius-pipeline 2026-08-22 after D1 observation. */
const pipelineLedger: MaestroRunSummary[] = [
  summary({
    run_id: "run-c0523695712b495eac9b1f4b311e9d50",
    state: "MERGED",
    declared_outcome: "ACCEPTED",
    node_states: Array.from({ length: 6 }, (_, i) => ({
      node_id: `n${i}`,
      state: "MERGED",
    })),
  }),
  summary({
    run_id: "run-9f76fa05879f49fb98199da59fd5848e",
    state: "BLOCKED",
    declared_outcome: "BLOCKED",
    node_states: Array.from({ length: 6 }, (_, i) => ({
      node_id: `n${i}`,
      state: "PENDING",
    })),
  }),
  summary({
    run_id: "run-3fcd8c7517a54d07ae4265205166bde6",
    state: "MERGED",
    declared_outcome: "ACCEPTED",
    node_states: Array.from({ length: 5 }, (_, i) => ({
      node_id: `n${i}`,
      state: "MERGED",
    })),
  }),
  summary({
    run_id: "run-c8910572828c4f5bb5c60c0582dd4be5",
    state: "ABANDONED",
    scheduler_liveness: "abandoned",
    declared_outcome: null,
    declared_outcome_at: null,
    node_states: Array.from({ length: 5 }, (_, i) => ({
      node_id: `n${i}`,
      state: i === 0 ? "RUNNING" : "PENDING",
    })),
  }),
  summary({
    run_id: "run-774cb49671174be9a6862de721da1394",
    state: "ABANDONED",
    scheduler_liveness: "abandoned",
    declared_outcome: null,
    declared_outcome_at: null,
    node_states: Array.from({ length: 5 }, (_, i) => ({
      node_id: `n${i}`,
      state: i === 0 ? "RUNNING" : "PENDING",
    })),
  }),
  summary({
    run_id: "run-7034bdf98d5342acafc61c439c2caa58",
    state: "BLOCKED",
    declared_outcome: "BLOCKED",
    node_states: Array.from({ length: 5 }, (_, i) => ({
      node_id: `n${i}`,
      state: "PENDING",
    })),
  }),
  summary({
    run_id: "run-fb9973646d344400a9e4f4d7818d00f2",
    state: "CANCELLED",
    declared_outcome: "CANCELLED",
    node_states: Array.from({ length: 5 }, (_, i) => ({
      node_id: `n${i}`,
      state: "CANCELLED",
    })),
  }),
  summary({
    run_id: "run-2a44d226e75a4be391a14f02b78a6d25",
    state: "ABANDONED",
    scheduler_liveness: "abandoned",
    declared_outcome: "BLOCKED",
    node_states: [
      ...Array.from({ length: 7 }, (_, i) => ({ node_id: `m${i}`, state: "MERGED" })),
      ...Array.from({ length: 5 }, (_, i) => ({ node_id: `r${i}`, state: "RUNNING" })),
    ],
  }),
  summary({
    run_id: "run-75b96fd1f01e46989671771645ee6acc",
    state: "CANCELLED",
    declared_outcome: "CANCELLED",
    node_states: Array.from({ length: 12 }, (_, i) => ({
      node_id: `n${i}`,
      state: "CANCELLED",
    })),
  }),

  summary({
    run_id: "run-9e9ac412669140039ae078601048f6c7",
    state: "CANCELLED",
    declared_outcome: "CANCELLED",
    node_states: [
      { node_id: "m0", state: "MERGED" },
      ...Array.from({ length: 12 }, (_, i) => ({
        node_id: `n${i}`,
        state: "CANCELLED",
      })),
    ],
  }),
  summary({
    run_id: "run-0120c32064d144c2aa55c344087e0b0a",
    state: "BLOCKED",
    declared_outcome: "BLOCKED",
    node_states: [
      { node_id: "m0", state: "MERGED" },
      ...Array.from({ length: 13 }, (_, i) => ({
        node_id: `n${i}`,
        state: "BLOCKED",
      })),
    ],
  }),
];

describe("shouldHideBarrenRun", () => {
  test("zero-merged finished run is hidden", () => {
    expect(shouldHideBarrenRun(finishedBarren, false)).toBe(true);
  });

  test("zero-merged in-flight run is shown", () => {
    expect(shouldHideBarrenRun(inFlightBarren, false)).toBe(false);
    expect(shouldHideBarrenRun(pendingBarren, false)).toBe(false);
    expect(shouldHideBarrenRun(cancellingBarren, false)).toBe(false);
    expect(
      shouldHideBarrenRun(
        summary({
          run_id: "waiting-zero",
          state: "WAITING_FOR_USER",
          scheduler_liveness: "unknown",
          declared_outcome: null,
          node_states: [{ node_id: "a", state: "WAITING_FOR_USER" }],
        }),
        false,
      ),
    ).toBe(false);
  });

  test("unknown liveness that still reads RUNNING is shown", () => {
    expect(shouldHideBarrenRun(unknownStillRunning, false)).toBe(false);
  });

  test("zero-merged terminal CANCELLED/BLOCKED is hidden", () => {
    expect(shouldHideBarrenRun(blockedBarren, false)).toBe(true);
    expect(shouldHideBarrenRun(cancelledBarren, false)).toBe(true);
  });

  test("zero-merged abandoned run is hidden", () => {
    expect(shouldHideBarrenRun(abandonedBarren, false)).toBe(true);
  });

  test("zero-merged needs-attention without a terminal outcome is shown", () => {
    expect(shouldHideBarrenRun(nodeBlockedBarren, false)).toBe(false);
  });

  test("a run with one merged node is shown", () => {
    expect(shouldHideBarrenRun(mergedOnce, false)).toBe(false);
  });

  test("?all=1 shows all 11", () => {
    expect(fleet).toHaveLength(11);
    expect(fleet.every((run) => !shouldHideBarrenRun(run, true))).toBe(true);
    expect(fleet.filter((run) => !shouldHideBarrenRun(run, true))).toHaveLength(11);
  });

  test("default hide drops finished and terminal barren runs", () => {
    expect(
      fleet.filter((run) => shouldHideBarrenRun(run, false)).map((run) => run.run_id),
    ).toEqual([
      "finished-zero",
      "blocked-zero",
      "cancelled-zero",
      "finished-barren-2",
    ]);
  });
});

describe("pipeline ledger shapes", () => {
  test("hides the six barren runs and keeps the five that merged or are live", () => {
    const hidden = pipelineLedger
      .filter((run) => shouldHideBarrenRun(run, false))
      .map((run) => run.run_id.slice(0, 12));
    const shown = pipelineLedger
      .filter((run) => !shouldHideBarrenRun(run, false))
      .map((run) => run.run_id.slice(0, 12));
    expect(hidden).toEqual([
      "run-9f76fa05",
      "run-c8910572",
      "run-774cb496",
      "run-7034bdf9",
      "run-fb997364",
      "run-75b96fd1",
    ]);
    expect(shown).toEqual([
      "run-c0523695",
      "run-3fcd8c75",
      "run-2a44d226",
      "run-9e9ac412",
      "run-0120c320",
    ]);
    expect(pipelineLedger.filter((run) => !shouldHideBarrenRun(run, true))).toHaveLength(11);
  });
});

describe("runsListHref", () => {
  test("toggles all without dropping source", () => {
    expect(runsListHref()).toBe("/runs");
    expect(runsListHref({ all: true })).toBe("/runs?all=1");
    expect(runsListHref({ source: "maestro:lexgenius-pipeline" })).toBe(
      "/runs?source=maestro%3Alexgenius-pipeline",
    );
    expect(runsListHref({ source: "maestro:lexgenius-pipeline", all: true })).toBe(
      "/runs?source=maestro%3Alexgenius-pipeline&all=1",
    );
  });
});
