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
 * Hide barren runs that are not genuinely in flight.
 * Terminal CANCELLED/BLOCKED with zero merges hide even if needsAttention.
 * Abandoned (not RUNNING) zero-merge runs hide. `?all=1` shows everything.
 */
export function shouldHideBarrenRun(
  run: MaestroRunSummary,
  showAll: boolean,
): boolean {
  if (showAll) return false;
  if (isInFlight(run.state)) return false;
  if (mergedCount(run) !== 0) return false;
  if (run.declared_outcome === "CANCELLED" || run.declared_outcome === "BLOCKED") {
    return true;
  }
  if (needsAttention(run)) return false;
  return true;
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
