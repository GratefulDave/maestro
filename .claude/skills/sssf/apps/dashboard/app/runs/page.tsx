import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { RunCard } from "@/components/RunCard";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { decodeRouteParam, loadFleetSummaries } from "@/lib/api";
import {
  isInFlight,
  needsAttention,
  runsListHref,
  shouldHideBarrenRun,
} from "@/lib/runVisibility";

export const dynamic = "force-dynamic";

export default async function RunsPage({
  searchParams,
}: {
  searchParams: Promise<{ source?: string; all?: string }>;
}) {
  const { source, all } = await searchParams;
  const sourceFilter = source ? decodeRouteParam(source) : null;
  const showAll = all === "1";

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

  const { sources, maestro, runs: allRuns } = fleet;
  const listed = sourceFilter
    ? allRuns.filter((run) => run.source_id === sourceFilter)
    : allRuns;
  const hidden = listed.filter((run) => shouldHideBarrenRun(run, false));
  const runs = listed.filter((run) => !shouldHideBarrenRun(run, showAll));
  const active = listed.filter((run) => isInFlight(run.state));
  const attention = listed.filter((run) => needsAttention(run));

  const filteredSource = sourceFilter
    ? sources.find((item) => item.id === sourceFilter)
    : null;
  const notMaestro = Boolean(
    filteredSource && filteredSource.kind !== "maestro" && filteredSource.kind !== "artifact-factory",
  );
  const unknownSource = Boolean(sourceFilter && !filteredSource);

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet"
        title="Runs"
        description="Every Maestro run across registered lifecycle ledgers. Progress strips use node_states on the summary."
      />
      <SourceBanner
        label={
          sourceFilter
            ? sourceFilter
            : `${maestro.length} maestro source${maestro.length === 1 ? "" : "s"}`
        }
        detail={`${listed.length} run${listed.length === 1 ? "" : "s"} via GET /api/sources and GET /api/sources/:id/runs`}
      />
      {hidden.length > 0 && (
        <SourceBanner
          tone={showAll ? "info" : "warning"}
          label={`${hidden.length} run${hidden.length === 1 ? "" : "s"} with no merged nodes ${showAll ? "shown" : "hidden"}`}
          detail={
            <Link
              className="lane-link"
              href={runsListHref({ source: sourceFilter, all: !showAll })}
            >
              {showAll ? "Hide them" : "Show them"}
            </Link>
          }
        />
      )}
      <div className="stat-grid">
        <StatCard label="Published runs" value={listed.length} />
        <StatCard label="In flight" value={active.length} />
        <StatCard label="Needs attention" value={attention.length} />
        <StatCard label="Sources" value={maestro.length} />
      </div>

      {notMaestro ? (
        <EmptyState
          title="Not a Maestro ledger"
          description={`${sourceFilter} is registered as kind=${filteredSource?.kind}. /runs only lists maestro sources.`}
        />
      ) : unknownSource ? (
        <EmptyState
          title="Unknown source"
          description={`${sourceFilter} is not in GET /api/sources.`}
        />
      ) : maestro.length === 0 ? (
        <EmptyState
          title="No Maestro sources"
          description="The Bun API is reachable, but GET /api/sources listed no kind=maestro ledger."
        />
      ) : listed.length === 0 ? (
        <EmptyState
          title="No runs in the ledger"
          description={
            sourceFilter
              ? `${sourceFilter} is a maestro source, and its run index is empty.`
              : "Registered Maestro sources answered, and their run indexes are empty."
          }
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
