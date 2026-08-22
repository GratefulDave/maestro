import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { loadFleetSummaries, runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function PlansPage() {
  const fleet = await loadFleetSummaries();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader eyebrow="Fleet" title="Plans" description="plan_name and plan_digest from run summaries." />
        <ApiUnavailable message={fleet.message} />
      </section>
    );
  }

  const unresolved = fleet.runs.filter((run) => run.plan_name == null);

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet"
        title="Plans"
        description="Each run carries a plan digest. plan_name is null when that digest matches no installed plan directory."
      />
      <SourceBanner
        label={`${fleet.runs.length} run${fleet.runs.length === 1 ? "" : "s"}`}
        detail={`${unresolved.length} unresolved plan_name`}
        tone={unresolved.length > 0 ? "warning" : "info"}
      />
      <div className="stat-grid">
        <StatCard label="Runs" value={fleet.runs.length} />
        <StatCard label="Unresolved names" value={unresolved.length} />
      </div>
      {fleet.runs.length === 0 ? (
        <EmptyState
          title="No plans"
          description="The run index is empty, so there are no plan digests to show."
        />
      ) : (
        <section className="panel section-panel">
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Run</th>
                  <th>Plan name</th>
                  <th>Digest</th>
                </tr>
              </thead>
              <tbody>
                {fleet.runs.map((run) => (
                  <tr key={`${run.source_id}/${run.run_id}`}>
                    <td>{run.source_label}</td>
                    <td>
                      <Link className="lane-link" href={runHref(run.source_id, run.run_id, "/plan")}>
                        {run.run_id}
                      </Link>
                    </td>
                    <td>{run.plan_name ?? "null"}</td>
                    <td>
                      <code>{run.plan_digest}</code>
                    </td>
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
