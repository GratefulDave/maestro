import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { RunCard } from "@/components/RunCard";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { isInFlight, loadFleetSummaries, needsAttention } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function RunsPage() {
  const fleet = await loadFleetSummaries();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader
          eyebrow="Fleet"
          title="Runs"
          description="Every Maestro run across registered lifecycle ledgers."
        />
        <ApiUnavailable message={fleet.message} />
      </section>
    );
  }

  const { maestro, runs } = fleet;
  const active = runs.filter((run) => isInFlight(run.state));
  const attention = runs.filter((run) => needsAttention(run));

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet"
        title="Runs"
        description="Every Maestro run across registered lifecycle ledgers. Progress strips use node_states on the summary."
      />
      <SourceBanner
        label={`${maestro.length} maestro source${maestro.length === 1 ? "" : "s"}`}
        detail={`${runs.length} run${runs.length === 1 ? "" : "s"} via GET /api/sources and GET /api/sources/:id/runs`}
      />
      <div className="stat-grid">
        <StatCard label="Published runs" value={runs.length} />
        <StatCard label="In flight" value={active.length} />
        <StatCard label="Needs attention" value={attention.length} />
        <StatCard label="Sources" value={maestro.length} />
      </div>
      {maestro.length === 0 ? (
        <EmptyState
          title="No Maestro sources"
          description="The Bun API is reachable, but GET /api/sources listed no kind=maestro ledger."
        />
      ) : runs.length === 0 ? (
        <EmptyState
          title="No runs in the ledger"
          description="Registered Maestro sources answered, and their run indexes are empty."
        />
      ) : (
        <>
          {active.length > 0 && (
            <section>
              <div className="section-heading">
                <div>
                  <h2>In flight</h2>
                  <p>RUNNING, CANCELLING, or PENDING.</p>
                </div>
              </div>
              <div className="run-grid">
                {active.map((run) => (
                  <RunCard key={`${run.source_id}/${run.run_id}`} run={run} />
                ))}
              </div>
            </section>
          )}
          <section>
            <div className="section-heading">
              <div>
                <h2>All runs</h2>
                <p>Most recently transitioned first.</p>
              </div>
            </div>
            <div className="run-grid">
              {runs.map((run) => (
                <RunCard key={`${run.source_id}/${run.run_id}`} run={run} />
              ))}
            </div>
          </section>
        </>
      )}
    </section>
  );
}
