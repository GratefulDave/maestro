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
  declared_outcome: null,
  declared_outcome_at: null,
  node_count: 5,
  node_states: [{ node_id: "a", state: "RUNNING" }],
});

const pendingBarren = summary({
  run_id: "pending-zero",
  state: "PENDING",
  declared_outcome: null,
  declared_outcome_at: null,
  node_states: [{ node_id: "a", state: "PENDING" }],
});

const cancellingBarren = summary({
  run_id: "cancelling-zero",
  state: "CANCELLING",
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

describe("shouldHideBarrenRun", () => {
  test("zero-merged finished run is hidden", () => {
    expect(shouldHideBarrenRun(finishedBarren, false)).toBe(true);
  });

  test("zero-merged in-flight run is shown", () => {
    expect(shouldHideBarrenRun(inFlightBarren, false)).toBe(false);
    expect(shouldHideBarrenRun(pendingBarren, false)).toBe(false);
    expect(shouldHideBarrenRun(cancellingBarren, false)).toBe(false);
  });

  test("zero-merged needs-attention run is shown", () => {
    expect(shouldHideBarrenRun(blockedBarren, false)).toBe(false);
    expect(shouldHideBarrenRun(cancelledBarren, false)).toBe(false);
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

  test("default hide drops only finished barren runs", () => {
    expect(
      fleet.filter((run) => shouldHideBarrenRun(run, false)).map((run) => run.run_id),
    ).toEqual(["finished-zero", "finished-barren-2"]);
    expect(fleet.filter((run) => !shouldHideBarrenRun(run, false))).toHaveLength(9);
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
