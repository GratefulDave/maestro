import Link from "next/link";
import { RunChrome, loadRunOrBanner } from "@/components/RunFrame";
import { StatusPill } from "@/components/StatusPill";
import { Timestamp } from "@/components/Timestamp";
import { runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function RunLanesPage({
  params,
}: {
  params: Promise<{ sourceId: string; runId: string }>;
}) {
  const { sourceId, runId } = await params;
  const loaded = await loadRunOrBanner(sourceId, runId);
  if (!loaded.ok) return loaded.page;
  const { run } = loaded;

  return (
    <RunChrome run={run} runId={runId} sourceId={sourceId}>
      <section className="panel section-panel">
        <div className="section-heading">
          <div>
            <h2>Nodes</h2>
            <p>One row per DAG node in this run.</p>
          </div>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Node</th>
                <th>Kind</th>
                <th>State</th>
                <th>Block</th>
                <th>Cancel</th>
                <th>Merge</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {run.nodes.map((node) => (
                <tr key={node.node_id}>
                  <td>
                    <Link
                      className="lane-link"
                      href={`${runHref(sourceId, runId, "/lanes")}/${encodeURIComponent(node.node_id)}`}
                    >
                      {node.node_id}
                    </Link>
                  </td>
                  <td>{node.kind ?? "—"}</td>
                  <td>
                    <StatusPill status={node.state} />
                  </td>
                  <td>{node.block_reason ?? "—"}</td>
                  <td>{node.cancel_cause ?? "—"}</td>
                  <td>{node.merge_cause ?? "—"}</td>
                  <td>
                    <Timestamp value={node.updated_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </RunChrome>
  );
}
