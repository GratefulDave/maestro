import type { MaestroRunSummary } from "@/lib/types";

export function isInFlight(state: string): boolean {
  return ["RUNNING", "CANCELLING", "PENDING"].includes(state);
}

export function needsAttention(run: MaestroRunSummary): boolean {
  if (["BLOCKED", "CANCELLED", "STUCK"].includes(run.state)) return true;
  if (run.declared_outcome === "BLOCKED" || run.declared_outcome === "STUCK") return true;
  return run.node_states.some((node) =>
    ["BLOCKED", "CANCELLED"].includes(node.state),
  );
}

export function mergedCount(run: MaestroRunSummary): number {
  return run.node_states.filter((node) => node.state === "MERGED").length;
}

/**
 * Hide finished barren runs from the list. In-flight and needs-attention
 * stay visible even at zero merges. `showAll` is the `?all=1` escape.
 */
export function shouldHideBarrenRun(
  run: MaestroRunSummary,
  showAll: boolean,
): boolean {
  if (showAll) return false;
  if (isInFlight(run.state)) return false;
  if (needsAttention(run)) return false;
  return mergedCount(run) === 0;
}

export function runsListHref(opts: {
  source?: string | null;
  all?: boolean;
} = {}): string {
  const params = new URLSearchParams();
  if (opts.source) params.set("source", opts.source);
  if (opts.all) params.set("all", "1");
  const query = params.toString();
  return query ? `/runs?${query}` : "/runs";
}
