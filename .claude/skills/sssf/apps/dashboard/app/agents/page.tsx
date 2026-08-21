import Link from "next/link";
import { ApiUnavailable } from "@/components/ApiUnavailable";
import { EmptyState } from "@/components/EmptyState";
import { PageHeader } from "@/components/PageHeader";
import { SourceBanner } from "@/components/SourceBanner";
import { StatCard } from "@/components/StatCard";
import { StatusPill } from "@/components/StatusPill";
import { elapsedLabel, loadFleetDetails, runHref } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function AgentsPage() {
  const fleet = await loadFleetDetails();
  if (!fleet.ok) {
    return (
      <section className="page-stack">
        <PageHeader
          eyebrow="Fleet"
          title="Agents"
          description="Attempts as agent assignments. The ledger has no vendor or model identity per attempt."
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

  return (
    <section className="page-stack">
      <PageHeader
        eyebrow="Fleet"
        title="Agents"
        description="pid, liveness, session_path, and turn_count from attempts. Vendor and model identity are not in the ledger."
      />
      <SourceBanner
        label={`${assignments.length} attempt${assignments.length === 1 ? "" : "s"}`}
        detail="No vendor or model column exists on attempts; that roster is EmptyState, not invented."
        tone="warning"
      />
      <div className="stat-grid">
        <StatCard label="Attempts" value={assignments.length} />
        <StatCard label="Running" value={live.length} />
        <StatCard label="Vendor / model" value="absent" detail="not a ledger field" />
      </div>
      <EmptyState
        title="No vendor or model identity per attempt"
        description="MaestroAttempt publishes pid, running, session_path, and turn_count. It does not publish a vendor or model. Filling that later is a data change."
      />
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
                  <th>Running</th>
                  <th>PID</th>
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
                    <td>{row.attempt.running ? "yes" : "no"}</td>
                    <td>{row.attempt.pid ?? "—"}</td>
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
