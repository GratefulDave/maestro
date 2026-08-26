import { describe, expect, test } from "bun:test";
import { buildDagTextRows } from "./dagText";
import type { MaestroNode } from "./types";

function node(node_id: string, needs: string[] = []): MaestroNode {
  return {
    node_id,
    kind: "agent",
    depth: 0,
    needs,
    outputs: [],
    state: "PENDING",
    lane_phase: null,
    attempt_no: 0,
    block_reason: null,
    cancel_cause: null,
    merge_cause: null,
    output_sha: null,
    granted_extra_attempts: 0,
    updated_at: null,
    attempts: [],
  };
}

describe("buildDagTextRows", () => {
  test("draws one primary tree and preserves every join as off-tree ink", () => {
    const rows = buildDagTextRows([
      node("A"),
      node("B", ["A"]),
      node("C", ["A"]),
      node("D", ["B", "C"]),
    ]);

    expect(
      rows.map(({ node: row, rail, offTreeNeeds }) => [row.node_id, rail, offTreeNeeds]),
    ).toEqual([
      ["A", "", []],
      ["B", "├─ ", []],
      ["D", "│  ╰─ ", ["C"]],
      ["C", "╰─ ", []],
    ]);
  });

  test("renders parallel test and agent lanes as siblings without losing pair edges", () => {
    const rows = buildDagTextRows([
      node("bronze-tests"),
      node("bronze", ["bronze-tests"]),
      node("direct-tests", ["bronze"]),
      node("dmap-tests", ["bronze"]),
      node("direct", ["bronze", "direct-tests"]),
      node("dmap", ["bronze", "dmap-tests"]),
    ]);

    expect(rows.map(({ node: row, rail }) => [row.node_id, rail])).toEqual([
      ["bronze-tests", ""],
      ["bronze", "╰─ "],
      ["direct-tests", "   ├─ "],
      ["dmap-tests", "   ├─ "],
      ["direct", "   ├─ "],
      ["dmap", "   ╰─ "],
    ]);
    expect(rows.find(({ node: row }) => row.node_id === "direct")?.offTreeNeeds).toEqual([
      "direct-tests",
    ]);
    expect(rows.find(({ node: row }) => row.node_id === "dmap")?.offTreeNeeds).toEqual([
      "dmap-tests",
    ]);
  });

  test("terminates malformed cycles and exposes the undisplayed primary edge", () => {
    const rows = buildDagTextRows([node("A", ["B"]), node("B", ["A"])]);

    expect(rows.map(({ node: row }) => row.node_id)).toEqual(["A", "B"]);
    expect(rows[0].offTreeNeeds).toEqual(["B"]);
  });
});
