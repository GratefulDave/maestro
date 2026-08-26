import { StatusPill } from "@/components/StatusPill";
import type { MaestroRunDetail } from "@/lib/types";

export function ReviewLifecycleTable({
  run,
  buildNodeId,
}: {
  run: MaestroRunDetail;
  buildNodeId?: string;
}) {
  const candidates = buildNodeId
    ? run.lane_candidates.filter((candidate) => candidate.build_node_id === buildNodeId)
    : run.lane_candidates;
  const nodes = new Map(run.nodes.map((node) => [node.node_id, node]));
  const reviews = new Map(
    run.candidate_reviews.map((review) => [
      `${review.review_node_id}:${review.candidate_sha}`,
      review,
    ]),
  );
  const handoffs = new Map(
    run.repair_handoffs.map((handoff) => [
      `${handoff.build_node_id}:${handoff.rejected_candidate_sha}`,
      handoff,
    ]),
  );

  return (
    <section className="panel section-panel">
      <div className="section-heading">
        <div>
          <h2>Candidate review lifecycle</h2>
          <p>Immutable candidates, exactly-once reviews, and repair handoffs from the lifecycle ledger.</p>
        </div>
      </div>
      {candidates.length === 0 ? (
        <p className="muted">No review candidate has been published for this lane.</p>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Lane</th>
                <th>Phase</th>
                <th>#</th>
                <th>Candidate SHA</th>
                <th>Review</th>
                <th>Verdict</th>
                <th>Findings</th>
                <th>Handoff</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((candidate) => {
                const review = reviews.get(
                  `${candidate.build_node_id}::review:${candidate.candidate_sha}`,
                );
                const handoff = handoffs.get(
                  `${candidate.build_node_id}:${candidate.candidate_sha}`,
                );
                const findings = review?.findings ?? handoff?.findings ?? [];
                const phase = nodes.get(candidate.build_node_id)?.lane_phase;
                return (
                  <tr key={`${candidate.build_node_id}:${candidate.candidate_sha}`}>
                    <td>{candidate.build_node_id}</td>
                    <td>
                      {phase ? <StatusPill status={phase} /> : <span className="muted">legacy</span>}
                    </td>
                    <td>{candidate.candidate_seq}</td>
                    <td><code>{candidate.candidate_sha}</code></td>
                    <td>
                      <StatusPill status={review?.state ?? "NOT_DISPATCHED"} />
                    </td>
                    <td>{review?.verdict ? <StatusPill status={review.verdict} /> : "—"}</td>
                    <td>
                      {findings.length ? (
                        <details>
                          <summary>{findings.length} blocking findings</summary>
                          <ul>
                            {findings.map((finding, index) => (
                              <li key={`${finding.check_id}:${finding.object_id}:${index}`}>
                                <code>{finding.check_id}</code> · {finding.object_id} — {finding.message}
                              </li>
                            ))}
                          </ul>
                        </details>
                      ) : (
                        <span className="muted">none</span>
                      )}
                    </td>
                    <td>
                      {handoff ? (
                        <>
                          <StatusPill status={handoff.state} />
                          <span className="muted"> builder g{handoff.builder_generation}</span>
                        </>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
