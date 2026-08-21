import Link from "next/link";
import { EmptyState } from "@/components/EmptyState";
import { RunChrome, loadRunOrBanner } from "@/components/RunFrame";
import { StatusPill } from "@/components/StatusPill";
import { nodeNeedsAttention, runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function RunGatesPage({
  params,
}: {
  params: Promise<{ sourceId: string; runId: string }>;
}) {
  const { sourceId, runId } = await params;
  const loaded = await loadRunOrBanner(sourceId, runId);
  if (!loaded.ok) return loaded.page;
  const { run } = loaded;
  const flagged = run.nodes.filter((node) => nodeNeedsAttention(node));

  return (
    <RunChrome run={run} runId={runId} sourceId={sourceId}>
      {flagged.length === 0 ? (
        <EmptyState
          title="No blocked or cancelled nodes"
          description="block_reason and cancel_cause are empty on every node in this run."
        />
      ) : (
        <section className="panel section-panel">
          <div className="section-heading">
            <div>
              <h2>Needs attention</h2>
              <p>BLOCKED / CANCELLED nodes, plus any with block_reason or cancel_cause.</p>
            </div>
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>State</th>
                  <th>block_reason</th>
                  <th>cancel_cause</th>
                </tr>
              </thead>
              <tbody>
                {flagged.map((node) => (
                  <tr key={node.node_id}>
                    <td>
                      <Link
                        className="lane-link"
                        href={`${runHref(sourceId, runId, "/lanes")}/${encodeURIComponent(node.node_id)}`}
                      >
                        {node.node_id}
                      </Link>
                    </td>
                    <td>
                      <StatusPill status={node.state} />
                    </td>
                    <td>{node.block_reason ?? "—"}</td>
                    <td>{node.cancel_cause ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </RunChrome>
  );
}
