import { getRun } from "@/lib/api";
import { projectRunHierarchy } from "@/lib/runHierarchy";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ sourceId: string; runId: string }> },
) {
  const { sourceId, runId } = await params;
  const result = await getRun(sourceId, runId);
  if (!result.ok) {
    const status = result.kind === "not_found" ? 404 : 502;
    return Response.json({ error: result.message }, { status });
  }
  return Response.json(projectRunHierarchy(result.data, sourceId));
}
