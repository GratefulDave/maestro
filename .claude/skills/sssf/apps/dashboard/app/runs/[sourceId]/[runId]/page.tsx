import { NodeDagView } from "@/components/NodeDagView";
import { RunChrome, loadRunOrBanner } from "@/components/RunFrame";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { Timestamp } from "@/components/Timestamp";
import { nodeNeedsAttention } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function RunPage({
  params,
}: {
  params: Promise<{ sourceId: string; runId: string }>;
}) {
  const { sourceId, runId } = await params;
  const loaded = await loadRunOrBanner(sourceId, runId);
  if (!loaded.ok) return loaded.page;
  const { run } = loaded;
  const blocked = run.nodes.filter((node) => nodeNeedsAttention(node)).length;
  const merged = run.nodes.filter((node) => node.state === "MERGED").length;
  const running = run.nodes.flatMap((node) => node.attempts).filter((attempt) => attempt.running).length;

  return (
    <RunChrome run={run} runId={runId} sourceId={sourceId}>
      <SourceBanner
        label={`live ${run.state}`}
        detail={`scheduler_liveness ${run.scheduler_liveness} · server_now_ms ${run.server_now_ms} · ${run.nodes.length} nodes · digest ${run.plan_digest}`}
      />

      {run.cancel_cause && (
        <SourceBanner
          label={`cancel_cause ${run.cancel_cause}`}
          detail={run.resumable ? "run resume will take this run" : "not resumable"}
          tone="warning"
        />
      )}
      <div className="stat-grid">
        <StatCard label="Nodes" value={run.nodes.length} detail="dag nodes" />
        <StatCard label="Merged" value={merged} detail="nodes" />
        <StatCard
          label="Running"
          value={running}
          detail="attempts proven live or sitting in review"
        />
        <StatCard label="Needs attention" value={blocked} detail="nodes" />
      </div>
      {run.integration && (
        <section className="panel section-panel">
          <div className="section-heading">
            <div>
              <h2>Integration</h2>
              <p>Worktree the scheduler last recorded.</p>
            </div>
          </div>
          <div className="integration-summary">
            <div>
              <span>path</span>
              <code>{run.integration.path}</code>
            </div>
            <div>
              <span>branch</span>
              <code>{run.integration.branch ?? "—"}</code>
            </div>
            <div>
              <span>head</span>
              <code>{run.integration.head ?? "—"}</code>
            </div>
            <div>
              <span>subject</span>
              <code>{run.integration.subject ?? "—"}</code>
            </div>
          </div>
        </section>
      )}
      <NodeDagView nodes={run.nodes} runId={runId} sourceId={sourceId} />
      <section className="panel section-panel">
        <div className="section-heading">
          <div>
            <h2>Run transitions</h2>
            <p>Outcome declarations, resumes, acceptance starts.</p>
          </div>
        </div>
        {run.run_transitions.length === 0 ? (
          <p className="muted">No run-level transitions in the ledger.</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>From</th>
                  <th>To</th>
                  <th>Reason</th>
                  <th>Actor</th>
                </tr>
              </thead>
              <tbody>
                {run.run_transitions.map((row, index) => (
                  <tr key={`${row.created_at ?? "t"}-${index}`}>
                    <td>
                      <Timestamp value={row.created_at} />
                    </td>
                    <td>{row.from_state ?? "—"}</td>
                    <td>{row.to_state ? <StatusPill status={row.to_state} /> : "—"}</td>
                    <td>{row.reason ?? "—"}</td>
                    <td>{row.actor ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </RunChrome>
  );
}
