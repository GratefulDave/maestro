import { EmptyState } from "@/components/EmptyState";
import { RunChrome, loadRunOrBanner } from "@/components/RunFrame";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { Timestamp } from "@/components/Timestamp";
import { elapsedLabel } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function RunLanePage({
  params,
}: {
  params: Promise<{ sourceId: string; runId: string; lane: string }>;
}) {
  const { sourceId, runId, lane } = await params;
  const loaded = await loadRunOrBanner(sourceId, runId);
  if (!loaded.ok) return loaded.page;
  const { run } = loaded;
  const node = run.nodes.find((item) => item.node_id === lane);

  if (!node) {
    return (
      <RunChrome run={run} runId={runId} sourceId={sourceId}>
        <EmptyState
          title="Node not in this run"
          description={`No dag_nodes row named ${lane} on run ${runId}.`}
        />
      </RunChrome>
    );
  }

  return (
    <RunChrome run={run} runId={runId} sourceId={sourceId}>
      <SourceBanner
        label={`${node.node_id} · ${node.state}`}
        detail={[
          node.block_reason && `block_reason ${node.block_reason}`,
          node.cancel_cause && `cancel_cause ${node.cancel_cause}`,
          node.merge_cause && `merge_cause ${node.merge_cause}`,
        ]
          .filter(Boolean)
          .join(" · ") || `attempt ${node.attempt_no}`}
        tone={node.state === "BLOCKED" || node.state === "CANCELLED" ? "warning" : "info"}
      />
      <div className="stat-grid">
        <StatCard label="Kind" value={node.kind ?? "—"} />
        <StatCard label="Attempt" value={node.attempt_no} />
        <StatCard label="Outputs" value={node.outputs.length} />
        <StatCard label="Needs" value={node.needs.length} />
      </div>
      <section className="panel section-panel">
        <div className="section-heading">
          <div>
            <h2>Attempts</h2>
            <p>pid, liveness, session_path, turn_count. No vendor or model identity is in the ledger.</p>
          </div>
        </div>
        {node.attempts.length === 0 ? (
          <p className="muted">No attempts recorded for this node.</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>State</th>
                  <th>Running</th>
                  <th>PID</th>
                  <th>Turns</th>
                  <th>Elapsed</th>
                  <th>Retry</th>
                  <th>Verdict</th>
                  <th>Session</th>
                </tr>
              </thead>
              <tbody>
                {node.attempts.map((attempt) => (
                  <tr key={attempt.attempt_no}>
                    <td>{attempt.attempt_no}</td>
                    <td>
                      <StatusPill status={attempt.state} />
                    </td>
                    <td>{attempt.running ? "yes" : "no"}</td>
                    <td>{attempt.pid ?? "—"}</td>
                    <td>{attempt.turn_count}</td>
                    <td>{elapsedLabel(attempt.started_at_ms, run.server_now_ms)}</td>
                    <td>{attempt.retry_class ?? "—"}</td>
                    <td>{attempt.verdict ?? "—"}</td>
                    <td>
                      <code>{attempt.session_path ?? "—"}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="panel section-panel">
        <div className="section-heading">
          <div>
            <h2>Outputs</h2>
          </div>
        </div>
        {node.outputs.length === 0 ? (
          <p className="muted">No declared outputs.</p>
        ) : (
          <ul className="dependency-list">
            {node.outputs.map((output) => (
              <li className="dependency-chip" key={output}>
                {output}
              </li>
            ))}
          </ul>
        )}
      </section>
      {node.output_sha && (
        <p className="muted">
          output_sha <code>{node.output_sha}</code>
        </p>
      )}
      <p className="muted">
        updated <Timestamp value={node.updated_at} />
      </p>
    </RunChrome>
  );
}
