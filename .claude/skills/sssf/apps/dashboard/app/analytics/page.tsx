import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { FleetAnalyticsCharts, type FleetChartDatum } from "@/components/FleetAnalyticsCharts";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { loadFleetDetails, runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

function chartData(counts: Map<string, number>): FleetChartDatum[] {
  return [...counts.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([label, value]) => ({ label, value }));
}

export default async function AnalyticsPage() {
  const fleet = await loadFleetDetails();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader
          eyebrow="Fleet analytics"
          title="Analytics"
          description="Counts derived from nodes, attempts, and transitions."
        />
        <ApiUnavailable message={fleet.message} />
      </section>
    );
  }

  const liveCounts = new Map<string, number>();
  const nodeCounts = new Map<string, number>();
  let attemptCount = 0;
  let transitionCount = 0;

  for (const run of fleet.details) {
    liveCounts.set(run.state, (liveCounts.get(run.state) ?? 0) + 1);
    transitionCount += run.run_transitions.length;
    for (const node of run.nodes) {
      nodeCounts.set(node.state, (nodeCounts.get(node.state) ?? 0) + 1);
      attemptCount += node.attempts.length;
      for (const attempt of node.attempts) {
        transitionCount += attempt.transitions.length;
      }
    }
  }

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet analytics"
        title="Analytics"
        description="Counts derived from nodes, attempts, and transitions. No usage or cost fields exist."
      />
      <SourceBanner
        label={`${fleet.details.length} run details`}
        detail={`${attemptCount} attempts · ${transitionCount} transitions`}
      />
      <div className="stat-grid">
        <StatCard label="Runs" value={fleet.details.length} />
        <StatCard
          label="Nodes"
          value={[...nodeCounts.values()].reduce((sum, value) => sum + value, 0)}
        />
        <StatCard label="Attempts" value={attemptCount} />
        <StatCard label="Transitions" value={transitionCount} />
      </div>
      {fleet.details.length === 0 ? (
        <EmptyState
          title="No runs to count"
          description="The ledger answered; there are no run details to aggregate."
        />
      ) : (
        <>
          <FleetAnalyticsCharts
            liveStateData={chartData(liveCounts)}
            nodeStateData={chartData(nodeCounts)}
          />
          <section className="panel section-panel">
            <div className="section-heading">
              <div>
                <h2>Runs</h2>
                <p>Each counted run opens its report.</p>
              </div>
            </div>
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Run</th>
                    <th>State</th>
                    <th>Nodes</th>
                  </tr>
                </thead>
                <tbody>
                  {fleet.details.map((run) => (
                    <tr key={`${run.source_id}/${run.run_id}`}>
                      <td>{run.source_label}</td>
                      <td>
                        <Link className="lane-link" href={runHref(run.source_id, run.run_id)}>
                          {run.run_id}
                        </Link>
                      </td>
                      <td>
                        <StatusPill status={run.state} />
                      </td>
                      <td>{run.nodes.length}</td>
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
