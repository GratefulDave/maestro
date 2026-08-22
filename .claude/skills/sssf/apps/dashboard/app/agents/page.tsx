import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { elapsedLabel, loadFleetDetails, runHref } from "@/lib/api";
import type { AttemptIdentitySource } from "@/lib/types";

export const dynamic = "force-dynamic";

function IdentityCell({
  value,
  source,
  title,
}: {
  value: string | null;
  source: AttemptIdentitySource;
  title?: string | null;
}) {
  if (!value) {
    return <span className="muted">not recorded</span>;
  }
  return (
    <>
      <code title={title ?? undefined}>{value}</code>
      <span className="muted"> {source.replaceAll("_", " ")}</span>
    </>
  );
}

export default async function AgentsPage() {
  const fleet = await loadFleetDetails();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader
          eyebrow="Fleet"
          title="Agents"
          description="Attempt roster across every registered Maestro ledger."
        />
        <ApiUnavailable message={fleet.message} />
      </section>
    );
  }

  const assignments = fleet.details.flatMap((run) =>
    run.nodes.flatMap((node) =>
      node.attempts.map((attempt) => ({
        source_id: run.source_id,
        source_label: run.source_label,
        run_id: run.run_id,
        server_now_ms: run.server_now_ms,
        node_id: node.node_id,
        attempt,
      })),
    ),
  );
  const live = assignments.filter((row) => row.attempt.running);
  const stale = assignments.filter((row) => row.attempt.liveness === "stale");
  const unknown = assignments.filter((row) => row.attempt.liveness === "unknown");
  const observed = assignments.filter(
    (row) => row.attempt.model_source === "observed" || row.attempt.model_source === "observed_head",
  );
  const declared = assignments.filter((row) => row.attempt.model_source === "declared");

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet"
        title="Agents"
        description="Liveness is a read-side observation, not a transition. Review-window attempts stay live after the builder exits. Model is observed from session jsonl when readable, otherwise declared from maestro.config.yaml."
      />
      <SourceBanner
        label={`${assignments.length} attempt${assignments.length === 1 ? "" : "s"}`}
        detail={`${observed.length} observed · ${declared.length} declared · ${assignments.length - observed.length - declared.length} not recorded`}
      />
      <div className="stat-grid">
        <StatCard
          label="Attempts"
          value={assignments.length}
        />
        <StatCard
          label="Running"
          value={live.length}
          detail="process with this pid exists on this host"
        />
        <StatCard
          label="Stale"
          value={stale.length}
          detail="ledger RUNNING, pid absent here"
        />
        <StatCard
          label="Unknown"
          value={unknown.length}
          detail="foreign host or invalid pid"
        />
        <StatCard
          label="Observed models"
          value={observed.length}
          detail="from session jsonl model_change"
        />
      </div>
      {assignments.length === 0 ? (
        <EmptyState
          title="No attempts"
          description="Loaded run details contain no attempts rows."
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
                  <th>#</th>
                  <th>State</th>
                  <th>Liveness</th>
                  <th>PID</th>
                  <th>Model</th>
                  <th>Vendor</th>
                  <th>Turns</th>
                  <th>Elapsed</th>
                  <th>Session</th>
                </tr>
              </thead>
              <tbody>
                {assignments.map((row) => (
                  <tr key={`${row.source_id}/${row.run_id}/${row.node_id}/${row.attempt.attempt_no}`}>
                    <td>{row.source_label}</td>
                    <td>
                      <Link className="lane-link" href={runHref(row.source_id, row.run_id)}>
                        {row.run_id}
                      </Link>
                    </td>
                    <td>
                      <Link
                        className="lane-link"
                        href={`${runHref(row.source_id, row.run_id, "/lanes")}/${encodeURIComponent(row.node_id)}`}
                      >
                        {row.node_id}
                      </Link>
                    </td>
                    <td>{row.attempt.attempt_no}</td>
                    <td>
                      <StatusPill status={row.attempt.state} />
                    </td>
                    <td>
                      <StatusPill status={row.attempt.liveness} />
                    </td>
                    <td>{row.attempt.pid ?? "—"}</td>
                    <td>
                      <IdentityCell
                        value={row.attempt.model}
                        source={row.attempt.model_source}
                        title={row.attempt.declared_config_path}
                      />
                    </td>
                    <td>
                      <IdentityCell
                        value={row.attempt.vendor}
                        source={row.attempt.vendor_source}
                        title={row.attempt.declared_config_path}
                      />
                    </td>
                    <td>{row.attempt.turn_count}</td>
                    <td>{elapsedLabel(row.attempt.started_at_ms, row.server_now_ms)}</td>
                    <td>
                      <code>{row.attempt.session_path ?? "—"}</code>
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
