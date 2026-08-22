import Link from "next/link";
import { mergedCount, runHref } from "@/lib/api";
import type { FleetRun } from "@/lib/types";
import { StatusPill } from "./StatusPill";
import { Timestamp } from "./Timestamp";

export function RunCard({ run }: { run: FleetRun }) {
  const complete = mergedCount(run);
  const laneCount = run.node_count;
  return (
    <Link className="run-card panel" href={runHref(run.source_id, run.run_id)}>
      <div className="run-card-head">
        <span className="run-repo">{run.source_label}</span>
        <StatusPill status={run.state} />
        <StatusPill status={run.scheduler_liveness} />
      </div>

      <strong className="run-id">{run.plan_name ?? run.plan_digest}</strong>
      <div className="run-progress" aria-label={`${complete} of ${laneCount} nodes merged`}>
        <span style={{ width: laneCount ? `${(complete / laneCount) * 100}%` : "0%" }} />
      </div>
      <div className="run-card-meta">
        <span>
          {complete}/{laneCount} merged
        </span>
        <span className="mono">{run.run_id}</span>
        <Timestamp value={run.last_transition_at} />
      </div>
    </Link>
  );
}
