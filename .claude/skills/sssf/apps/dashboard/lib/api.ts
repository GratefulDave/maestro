import "server-only";

import type {
  FleetRun,
  FleetRunDetail,
  HealthResponse,
  MaestroRunDetail,
  MaestroRunSummary,
  SourceInfo,
} from "@/lib/types";

const API_BASE = `http://localhost:${process.env.MAESTRO_API_PORT ?? "4600"}`;

export type ApiFailureKind = "unreachable" | "not_found" | "error";

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; kind: ApiFailureKind; message: string };

async function apiGet<T>(path: string): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { cache: "no-store" });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return {
      ok: false,
      kind: "unreachable",
      message: `Bun API unreachable at ${API_BASE} (${detail})`,
    };
  }

  if (response.status === 404) {
    const body = (await response.json().catch(() => ({ error: "not found" }))) as {
      error?: string;
    };
    return { ok: false, kind: "not_found", message: body.error ?? "not found" };
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => ({ error: response.statusText }))) as {
      error?: string;
    };
    return {
      ok: false,
      kind: "error",
      message: body.error ?? `HTTP ${response.status}`,
    };
  }

  return { ok: true, data: (await response.json()) as T };
}

export function getHealth(): Promise<ApiResult<HealthResponse>> {
  return apiGet<HealthResponse>("/api/health");
}

export function getSources(): Promise<ApiResult<SourceInfo[]>> {
  return apiGet<SourceInfo[]>("/api/sources");
}

export function getRuns(sourceId: string): Promise<ApiResult<MaestroRunSummary[]>> {
  return apiGet<MaestroRunSummary[]>(
    `/api/sources/${encodeURIComponent(sourceId)}/runs`,
  );
}

export function getRun(
  sourceId: string,
  runId: string,
): Promise<ApiResult<MaestroRunDetail>> {
  return apiGet<MaestroRunDetail>(
    `/api/sources/${encodeURIComponent(sourceId)}/runs/${encodeURIComponent(runId)}`,
  );
}

export function maestroSources(sources: SourceInfo[]): SourceInfo[] {
  return sources.filter((source) => source.kind === "maestro");
}

export async function loadFleetSummaries(): Promise<
  | { ok: false; kind: ApiFailureKind; message: string }
  | { ok: true; sources: SourceInfo[]; maestro: SourceInfo[]; runs: FleetRun[] }
> {
  const sourcesResult = await getSources();
  if (!sourcesResult.ok) return sourcesResult;

  const sources = sourcesResult.data;
  const maestro = maestroSources(sources);
  const runGroups = await Promise.all(
    maestro.map(async (source) => {
      const runs = await getRuns(source.id);
      return { source, runs };
    }),
  );

  const failed = runGroups.find((group) => !group.runs.ok);
  if (failed && !failed.runs.ok) {
    return {
      ok: false,
      kind: failed.runs.kind,
      message: failed.runs.message,
    };
  }

  const runs: FleetRun[] = runGroups.flatMap((group) => {
    if (!group.runs.ok) return [];
    return group.runs.data.map((run) => ({
      ...run,
      source_id: group.source.id,
      source_label: group.source.label,
    }));
  });

  runs.sort((a, b) => (b.last_transition_at ?? "").localeCompare(a.last_transition_at ?? ""));
  return { ok: true, sources, maestro, runs };
}

export async function loadFleetDetails(): Promise<
  | { ok: false; kind: ApiFailureKind; message: string }
  | {
      ok: true;
      sources: SourceInfo[];
      maestro: SourceInfo[];
      runs: FleetRun[];
      details: FleetRunDetail[];
    }
> {
  const fleet = await loadFleetSummaries();
  if (!fleet.ok) return fleet;

  const detailGroups = await Promise.all(
    fleet.runs.map(async (run) => {
      const detail = await getRun(run.source_id, run.run_id);
      return { run, detail };
    }),
  );

  const failed = detailGroups.find((group) => !group.detail.ok);
  if (failed && !failed.detail.ok) {
    return {
      ok: false,
      kind: failed.detail.kind,
      message: failed.detail.message,
    };
  }

  const details: FleetRunDetail[] = detailGroups.flatMap((group) => {
    if (!group.detail.ok) return [];
    return [
      {
        ...group.detail.data,
        source_id: group.run.source_id,
        source_label: group.run.source_label,
      },
    ];
  });

  return {
    ok: true,
    sources: fleet.sources,
    maestro: fleet.maestro,
    runs: fleet.runs,
    details,
  };
}

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

export function elapsedLabel(startedAtMs: number | null, serverNowMs: number): string {
  if (startedAtMs == null) return "—";
  const ms = Math.max(0, serverNowMs - startedAtMs);
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

export function runHref(sourceId: string, runId: string, suffix = ""): string {
  return `/runs/${encodeURIComponent(sourceId)}/${encodeURIComponent(runId)}${suffix}`;
}

export function nodeNeedsAttention(node: {
  state: string;
  block_reason: string | null;
  cancel_cause: string | null;
}): boolean {
  return (
    node.state === "BLOCKED" ||
    node.state === "CANCELLED" ||
    Boolean(node.block_reason) ||
    Boolean(node.cancel_cause)
  );
}
