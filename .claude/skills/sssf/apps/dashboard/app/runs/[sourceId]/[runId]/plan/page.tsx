import { NodeDagView } from "@/components/NodeDagView";
import { RunChrome, loadRunOrBanner } from "@/components/RunFrame";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";

export const dynamic = "force-dynamic";

export default async function RunPlanPage({
  params,
}: {
  params: Promise<{ sourceId: string; runId: string }>;
}) {
  const { sourceId, runId } = await params;
  const loaded = await loadRunOrBanner(sourceId, runId);
  if (!loaded.ok) return loaded.page;
  const { run } = loaded;
  const edgeCount = run.nodes.reduce((count, node) => count + node.needs.length, 0);

  return (
    <RunChrome run={run} runId={runId} sourceId={sourceId}>
      <SourceBanner
        label={run.plan_name ?? "unresolved plan_name"}
        detail={`digest ${run.plan_digest}`}
        tone={run.plan_name ? "info" : "warning"}
      />
      <div className="stat-grid">
        <StatCard label="Plan name" value={run.plan_name ?? "null"} />
        <StatCard label="Nodes" value={run.nodes.length} />
        <StatCard label="Edges" value={edgeCount} />
        <StatCard label="Digest" value={run.plan_digest.slice(0, 12)} />
      </div>
      {run.plan_name == null && (
        <p className="muted">
          plan_name is null because this digest matches no installed plan directory. The
          filename maestro-plan.v1 is the container; schema_version lives inside the JSON.
        </p>
      )}
      <NodeDagView nodes={run.nodes} runId={runId} sourceId={sourceId} />
    </RunChrome>
  );
}
