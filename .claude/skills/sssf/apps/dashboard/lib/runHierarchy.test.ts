import { describe, expect, test } from "bun:test";
import type { MaestroNode, MaestroRunDetail } from "@/lib/types";
import { projectRunHierarchy } from "@/lib/runHierarchy";

function node(
  node_id: string,
  kind: string | null,
  state: string,
  attempt_no: number,
  needs: string[] = [],
  lane_phase: string | null = null,
): MaestroNode {
  return {
    node_id,
    kind,
    depth: 0,
    needs,
    outputs: [],
    state,
    lane_phase,
    attempt_no,
    block_reason: null,
    cancel_cause: null,
    merge_cause: null,
    output_sha: null,
    granted_extra_attempts: 0,
    updated_at: null,
    attempts: [],
  };
}

describe("run hierarchy projection", () => {
  test("groups a lane's tester, latest builder, and latest reviewer generation", () => {
    const run = {
      run_id: "run-1",
      plan_name: "EPA corpus recovery",
      state: "RUNNING",
      nodes: [
        node("lane-bronze-tests", "tests", "MERGED", 2),
        node("lane-bronze", "agent", "RUNNING", 7, ["lane-bronze-tests"], "REPAIRING"),
        node("lane-bronze::review", "review", "RUNNING", 0, ["lane-bronze"]),
      ],
      actor_sessions: [
        {
          build_node_id: "lane-bronze",
          actor_role: "builder",
          generation: 6,
          state: "CLOSED",
        },
        {
          build_node_id: "lane-bronze",
          actor_role: "builder",
          generation: 7,
          state: "ACTIVE",
        },
        {
          build_node_id: "lane-bronze",
          actor_role: "reviewer",
          generation: 3,
          state: "ACTIVE",
        },
      ],
    } as MaestroRunDetail;

    const hierarchy = projectRunHierarchy(run, "maestro:epa");

    expect(hierarchy.label).toBe("EPA corpus recovery");
    expect(hierarchy.lanes).toHaveLength(1);
    expect(hierarchy.lanes[0]?.label).toBe("lane-bronze");
    expect(hierarchy.lanes[0]?.agents.map(({ label, state }) => [label, state])).toEqual([
      ["tester-a2", "MERGED"],
      ["builder-a7", "ACTIVE"],
      ["reviewer-a3", "ACTIVE"],
    ]);
    expect(hierarchy.lanes[0]?.state).toBe("REPAIRING");
    expect(hierarchy.is_live).toBe(true);
  });

  test("projects typed lane roles and one run-level integration reviewer", () => {
    const run = {
      run_id: "run-factory",
      schema_version: "artifact-factory.v1",
      plan_name: "fdadb-v2-wp6-geo-layer-r2.v3",
      state: "RUNNING",
      nodes: [
        node("lane-wp6-tests", "tests", "REVIEWING_TESTS", 0, [], "REVIEWING_TESTS"),
        node(
          "lane-wp6-build",
          "build",
          "PLANNED",
          0,
          ["lane-wp6-tests"],
          "PLANNED",
        ),
      ],
      actor_sessions: [],
    } as MaestroRunDetail;

    const hierarchy = projectRunHierarchy(run, "artifact-factory:FDAdb");

    expect(hierarchy.lanes.map((lane) => lane.label)).toEqual([
      "lane-wp6-tests",
      "lane-wp6-build",
    ]);
    expect(hierarchy.lanes[0]?.agents.map(({ label, state }) => [label, state])).toEqual([
      ["tester", "COMPLETE"],
      ["test-reviewer", "ACTIVE"],
    ]);
    expect(hierarchy.lanes[1]?.agents.map(({ label, state }) => [label, state])).toEqual([
      ["builder", "WAITING"],
      ["code-reviewer", "WAITING"],
    ]);
    expect(hierarchy.agents.map(({ label, state }) => [label, state])).toEqual([
      ["integration-reviewer", "WAITING"],
    ]);
  });

  test("activates integration review only at the run-level final gate", () => {
    const readyRun = {
      run_id: "run-ready",
      schema_version: "artifact-factory.v1",
      plan_name: "integration",
      state: "RUNNING",
      nodes: [node("lane-a", null, "READY_TO_MERGE", 0, [], "READY_TO_MERGE")],
      actor_sessions: [],
    } as MaestroRunDetail;
    const reviewRun = {
      ...readyRun,
      run_id: "run-final-review",
      state: "INTEGRATION_REVIEW_PENDING",
      nodes: [node("lane-a", null, "MERGED", 0, [], "MERGED")],
    } as MaestroRunDetail;
    const publishableRun = {
      ...reviewRun,
      run_id: "run-publishable",
      state: "PUBLISHABLE",
    } as MaestroRunDetail;

    const ready = projectRunHierarchy(readyRun, "artifact-factory:FDAdb");
    expect(ready.lanes[0]?.agents.map(({ label, state }) => [label, state])).toEqual([
      ["tester", "COMPLETE"],
      ["test-reviewer", "COMPLETE"],
      ["builder", "COMPLETE"],
      ["code-reviewer", "COMPLETE"],
    ]);
    expect(ready.agents[0]?.state).toBe("WAITING");
    expect(
      projectRunHierarchy(reviewRun, "artifact-factory:FDAdb").agents[0]?.state,
    ).toBe("ACTIVE");
    expect(
      projectRunHierarchy(publishableRun, "artifact-factory:FDAdb").agents[0]?.state,
    ).toBe("COMPLETE");
  });

  test("falls back to build node state when a legacy row has no lane phase", () => {
    const run = {
      run_id: "run-legacy",
      plan_name: null,
      state: "BLOCKED",
      nodes: [node("lane-old", "agent", "BLOCKED", 1)],
      actor_sessions: [],
    } as MaestroRunDetail;

    const hierarchy = projectRunHierarchy(run, "maestro:legacy");

    expect(hierarchy.lanes[0]?.state).toBe("BLOCKED");
    expect(hierarchy.is_live).toBe(false);
  });
});
