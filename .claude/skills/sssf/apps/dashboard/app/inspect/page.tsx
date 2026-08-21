import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { Timestamp } from "@/components/Timestamp";
import { loadFleetDetails, runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function InspectPage() {
  const fleet = await loadFleetDetails();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader
          eyebrow="Analyze"
          title="Inspector"
          description="Transitions, results, and adjudications across runs."
        />
        <ApiUnavailable message={fleet.message} />
      </section>
    );
  }

  const transitions = fleet.details.flatMap((run) =>
    run.run_transitions.map((row, index) => ({
      source_id: run.source_id,
      source_label: run.source_label,
      run_id: run.run_id,
      row,
      index,
    })),
  );
  const results = fleet.details.flatMap((run) =>
    run.results.map((row, index) => ({
      source_id: run.source_id,
      source_label: run.source_label,
      run_id: run.run_id,
      row,
      index,
    })),
  );

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Analyze"
        title="Inspector"
        description="Run-level transitions and results from every loaded ledger run."
      />
      <SourceBanner
        label={`${transitions.length} run transitions · ${results.length} results`}
        detail={`${fleet.details.length} run details`}
      />
      <div className="stat-grid">
        <StatCard label="Run transitions" value={transitions.length} />
        <StatCard label="Results" value={results.length} />
      </div>
      {transitions.length === 0 && results.length === 0 ? (
        <EmptyState
          title="No inspect rows"
          description="Loaded run details have empty run_transitions and results."
        />
      ) : (
        <>
          <section className="panel section-panel">
            <div className="section-heading">
              <div>
                <h2>Run transitions</h2>
              </div>
            </div>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Source</th>
                    <th>Run</th>
                    <th>From</th>
                    <th>To</th>
                    <th>Reason</th>
                    <th>Actor</th>
                  </tr>
                </thead>
                <tbody>
                  {transitions.map((item) => (
                    <tr key={`${item.source_id}/${item.run_id}/${item.index}`}>
                      <td>
                        <Timestamp value={item.row.created_at} />
                      </td>
                      <td>{item.source_label}</td>
                      <td>
                        <Link className="lane-link" href={runHref(item.source_id, item.run_id, "/inspect")}>
                          {item.run_id}
                        </Link>
                      </td>
                      <td>{item.row.from_state ?? "—"}</td>
                      <td>{item.row.to_state ? <StatusPill status={item.row.to_state} /> : "—"}</td>
                      <td>{item.row.reason ?? "—"}</td>
                      <td>{item.row.actor ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="panel section-panel">
            <div className="section-heading">
              <div>
                <h2>Results</h2>
              </div>
            </div>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Source</th>
                    <th>Run</th>
                    <th>Node</th>
                    <th>Adjudication</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((item) => (
                    <tr key={`${item.source_id}/${item.run_id}/r${item.index}`}>
                      <td>
                        <Timestamp value={item.row.created_at} />
                      </td>
                      <td>{item.source_label}</td>
                      <td>
                        <Link className="lane-link" href={runHref(item.source_id, item.run_id, "/inspect")}>
                          {item.run_id}
                        </Link>
                      </td>
                      <td>{item.row.node_id ?? "—"}</td>
                      <td>{item.row.adjudication ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </section>
  );
}
