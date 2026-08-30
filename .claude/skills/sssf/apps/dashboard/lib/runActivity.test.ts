import { describe, expect, test } from "bun:test";
import type { MaestroAttempt, MaestroNode, MaestroRunDetail } from "@/lib/types";
import { runningStat, sourceKindFromId } from "@/lib/runActivity";

function attempt(running: boolean): MaestroAttempt {
  return {
    node_id: "lane-a",
    attempt_no: 1,
    state: "RUNNING",
    base_sha: null,
    turn_count: 0,
    retry_class: null,
    pid: running ? 12 : null,
    started_at_ms: null,
    launched_at_ms: null,
    running,
    liveness: running ? "running" : "not_running",
    model: null,
    vendor: null,
    model_source: "not_recorded",
    vendor_source: "not_recorded",
    declared_config_path: null,
    session_path: null,
    verdict: null,
    transitions: [],
    review_findings: [],
  };
}

function node(
  state: string,
  attempts: MaestroAttempt[] = [],
): MaestroNode {
  return {
    node_id: "lane-a",
    kind: null,
    depth: 0,
    needs: [],
    outputs: [],
    state,
    lane_phase: state,
    attempt_no: 0,
    block_reason: null,
    cancel_cause: null,
    merge_cause: null,
    output_sha: null,
    granted_extra_attempts: 0,
    updated_at: null,
    attempts,
  };
}

function run(nodes: MaestroNode[]): Pick<MaestroRunDetail, "nodes"> {
  return { nodes };
}

describe("runningStat", () => {
  test("legacy pages keep attempt-liveness semantics", () => {
    const stat = runningStat(
      run([
        node("RUNNING", [attempt(true)]),
        node("MERGED", [attempt(false)]),
      ]),
      "maestro",
    );
    expect(stat).toEqual({
      label: "Running",
      value: 1,
      detail: "attempts proven live or sitting in review",
    });
  });

  test("artifact-factory counts working and waiting stages, not attempts", () => {
    const stat = runningStat(
      run([
        node("WRITING_TESTS"),
        node("WAITING_FOR_USER"),
        node("MERGED"),
        node("PLANNED"),
      ]),
      "artifact-factory",
    );
    expect(stat).toEqual({
      label: "Active lanes",
      value: 2,
      detail: "working or waiting stages",
    });
  });

  test("sourceKindFromId distinguishes artifact-factory routes", () => {
    expect(sourceKindFromId("artifact-factory:FDAdb")).toBe("artifact-factory");
    expect(sourceKindFromId("maestro:FDAdb")).toBe("maestro");
  });
});
