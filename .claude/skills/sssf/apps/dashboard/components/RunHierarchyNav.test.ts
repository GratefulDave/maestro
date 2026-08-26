import { describe, expect, test } from "bun:test";
import {
  HIERARCHY_LIVE_REFRESH_MS,
  HIERARCHY_TERMINAL_REFRESH_MS,
  hierarchyRefreshDelay,
} from "./RunHierarchyNav";
import type { RunHierarchy } from "@/lib/runHierarchy";

function hierarchy(is_live: boolean): RunHierarchy {
  return {
    label: "plan",
    is_live,
    href: "/runs/source/run",
    lanes: [],
  };
}

describe("RunHierarchyNav refresh policy", () => {
  test("retries a transient initial API failure at the live cadence", () => {
    expect(hierarchyRefreshDelay(null)).toBe(HIERARCHY_LIVE_REFRESH_MS);
  });

  test("keeps live hierarchy snapshots fresh", () => {
    expect(hierarchyRefreshDelay(hierarchy(true))).toBe(HIERARCHY_LIVE_REFRESH_MS);
  });

  test("backs off after the authoritative run state becomes terminal", () => {
    expect(hierarchyRefreshDelay(hierarchy(false))).toBe(HIERARCHY_TERMINAL_REFRESH_MS);
    expect(HIERARCHY_TERMINAL_REFRESH_MS).toBeGreaterThan(HIERARCHY_LIVE_REFRESH_MS);
  });
});
