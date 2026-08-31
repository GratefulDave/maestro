import type { MaestroRunDetail, SourceKind } from "@/lib/types";

const FACTORY_ACTIVE_STAGES: Record<string, true> = {
  WRITING_TESTS: true,
  REVIEWING_TESTS: true,
  TESTS_SEALED: true,
  BUILDING: true,
  REVIEWING_CODE: true,
  READY_TO_MERGE: true,
  WAITING_FOR_USER: true,
};

export function sourceKindFromId(sourceId: string): SourceKind {
  if (sourceId.startsWith("artifact-factory:")) return "artifact-factory";
  if (sourceId.startsWith("sssf:")) return "sssf";
  return "maestro";
}

export function runningStat(
  run: Pick<MaestroRunDetail, "nodes">,
  kind: SourceKind,
): { label: string; value: number; detail: string } {
  if (kind === "artifact-factory") {
    return {
      label: "Active lanes",
      value: run.nodes.filter((node) => FACTORY_ACTIVE_STAGES[node.state] === true)
        .length,
      detail: "working or waiting stages",
    };
  }
  return {
    label: "Running",
    value: run.nodes.flatMap((node) => node.attempts).filter((attempt) => attempt.running)
      .length,
    detail: "attempts proven live or sitting in review",
  };
}
