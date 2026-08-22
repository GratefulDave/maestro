import { EmptyState } from "@/components/EmptyState";
import { RunChrome, loadRunOrBanner } from "@/components/RunFrame";
import { StatusPill } from "@/components/StatusPill";
import { Timestamp } from "@/components/Timestamp";

export const dynamic = "force-dynamic";

export default async function RunInspectPage({
  params,
}: {
  params: Promise<{ sourceId: string; runId: string }>;
}) {
  const { sourceId, runId } = await params;
  const loaded = await loadRunOrBanner(sourceId, runId);
  if (!loaded.ok) return loaded.page;
  const { run } = loaded;
  const nodeTransitions = run.nodes.flatMap((node) =>
    node.attempts.flatMap((attempt) =>
      attempt.transitions.map((row) => ({
        ...row,
        node_id: row.node_id ?? node.node_id,
        attempt_no: attempt.attempt_no,
      })),
    ),
  );

  return (
    <RunChrome run={run} runId={runId} sourceId={sourceId}>
      <section className="panel section-panel">
        <div className="section-heading">
          <div>
            <h2>Run transitions</h2>
          </div>
        </div>
        {run.run_transitions.length === 0 ? (
          <p className="muted">No run-level transitions.</p>
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
                  <tr key={`run-${row.created_at ?? index}`}>
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
      <section className="panel section-panel">
        <div className="section-heading">
          <div>
            <h2>Node transitions</h2>
          </div>
        </div>
        {nodeTransitions.length === 0 ? (
          <p className="muted">No attempt transitions.</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Node</th>
                  <th>Attempt</th>
                  <th>From</th>
                  <th>To</th>
                  <th>Reason</th>
                  <th>Actor</th>
                </tr>
              </thead>
              <tbody>
                {nodeTransitions.map((row, index) => (
                  <tr key={`node-${row.node_id}-${row.created_at ?? index}`}>
                    <td>
                      <Timestamp value={row.created_at} />
                    </td>
                    <td>{row.node_id}</td>
                    <td>{row.attempt_no}</td>
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
      <section className="panel section-panel">
        <div className="section-heading">
          <div>
            <h2>Results / adjudications</h2>
          </div>
        </div>
        {run.results.length === 0 ? (
          <EmptyState
            title="No results rows"
            description="The results table is empty for this run."
          />
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Node</th>
                  <th>Attempt</th>
                  <th>Adjudication</th>
                  <th>Subject</th>
                </tr>
              </thead>
              <tbody>
                {run.results.map((row, index) => (
                  <tr key={`${row.node_id ?? "r"}-${row.created_at ?? index}`}>
                    <td>
                      <Timestamp value={row.created_at} />
                    </td>
                    <td>{row.node_id ?? "—"}</td>
                    <td>{row.attempt_no ?? "—"}</td>
                    <td>{row.adjudication ?? "—"}</td>
                    <td>
                      <code>{row.subject_sha ?? "—"}</code>
                    </td>
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
