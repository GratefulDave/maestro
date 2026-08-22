import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { loadFleetDetails, nodeNeedsAttention, runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function GatesPage() {
  const fleet = await loadFleetDetails();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader
          eyebrow="Operate"
          title="Needs attention"
          description="Blocked and cancelled nodes across every loaded run."
        />
        <ApiUnavailable message={fleet.message} />
      </section>
    );
  }

  const flagged = fleet.details.flatMap((run) =>
    run.nodes
      .filter((node) => nodeNeedsAttention(node))
      .map((node) => ({
        source_id: run.source_id,
        source_label: run.source_label,
        run_id: run.run_id,
        node,
      })),
  );

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Operate"
        title="Needs attention"
        description="Nodes with BLOCKED/CANCELLED state, block_reason, or cancel_cause."
      />
      <SourceBanner
        label={`${flagged.length} flagged node${flagged.length === 1 ? "" : "s"}`}
        detail={`${fleet.details.length} run details`}
        tone={flagged.length > 0 ? "warning" : "info"}
      />
      <div className="stat-grid">
        <StatCard label="Flagged nodes" value={flagged.length} />
      </div>
      {flagged.length === 0 ? (
        <EmptyState
          title="Nothing needs attention"
          description="No loaded node has BLOCKED, CANCELLED, block_reason, or cancel_cause."
        />
      ) : (
        <section className="panel section-panel">
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Run</th>
                  <th>Node</th>
                  <th>State</th>
                  <th>block_reason</th>
                  <th>cancel_cause</th>
                </tr>
              </thead>
              <tbody>
                {flagged.map((row) => (
                  <tr key={`${row.source_id}/${row.run_id}/${row.node.node_id}`}>
                    <td>{row.source_label}</td>
                    <td>
                      <Link className="lane-link" href={runHref(row.source_id, row.run_id, "/gates")}>
                        {row.run_id}
                      </Link>
                    </td>
                    <td>
                      <Link
                        className="lane-link"
                        href={`${runHref(row.source_id, row.run_id, "/lanes")}/${encodeURIComponent(row.node.node_id)}`}
                      >
                        {row.node.node_id}
                      </Link>
                    </td>
                    <td>
                      <StatusPill status={row.node.state} />
                    </td>
                    <td>{row.node.block_reason ?? "—"}</td>
                    <td>{row.node.cancel_cause ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </section>
  );
}
