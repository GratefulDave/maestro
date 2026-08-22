import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { loadFleetDetails, runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function LanesPage() {
  const fleet = await loadFleetDetails();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader eyebrow="Fleet" title="Work items" description="DAG nodes across every loaded run." />
        <ApiUnavailable message={fleet.message} />
      </section>
    );
  }

  const nodes = fleet.details.flatMap((run) =>
    run.nodes.map((node) => ({
      source_id: run.source_id,
      source_label: run.source_label,
      run_id: run.run_id,
      node,
    })),
  );

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet"
        title="Work items"
        description="Every DAG node across registered Maestro runs."
      />
      <SourceBanner
        label={`${nodes.length} node${nodes.length === 1 ? "" : "s"}`}
        detail={`${fleet.details.length} run details`}
      />
      <div className="stat-grid">
        <StatCard label="Nodes" value={nodes.length} />
        <StatCard
          label="Running"
          value={nodes.filter((row) => row.node.state === "RUNNING").length}
        />
        <StatCard
          label="Merged"
          value={nodes.filter((row) => row.node.state === "MERGED").length}
        />
      </div>
      {nodes.length === 0 ? (
        <EmptyState
          title="No nodes"
          description="Loaded run details contain no dag_nodes rows."
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
                  <th>Kind</th>
                  <th>State</th>
                  <th>Attempt</th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((row) => (
                  <tr key={`${row.source_id}/${row.run_id}/${row.node.node_id}`}>
                    <td>{row.source_label}</td>
                    <td>
                      <Link className="lane-link" href={runHref(row.source_id, row.run_id)}>
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
                    <td>{row.node.kind ?? "—"}</td>
                    <td>
                      <StatusPill status={row.node.state} />
                    </td>
                    <td>{row.node.attempt_no}</td>
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
