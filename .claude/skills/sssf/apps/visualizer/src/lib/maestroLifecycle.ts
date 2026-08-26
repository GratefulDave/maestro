import type {
  MaestroCandidateReview,
  MaestroLaneCandidate,
  MaestroNode,
  MaestroRepairHandoff,
  MaestroRunDetail,
} from './types'

export interface MaestroCandidateLifecycle {
  candidate: MaestroLaneCandidate
  review: MaestroCandidateReview | null
  handoff: MaestroRepairHandoff | null
}

/** The durable lane phase is the UI authority; legacy nodes fall back to state. */
export function nodeAuthorityState(node: MaestroNode): string {
  return node.lane_phase ?? node.state
}

/** Join the three authoritative review ledgers for one build lane. */
export function candidateLifecycleForNode(
  run: MaestroRunDetail,
  node: MaestroNode,
): MaestroCandidateLifecycle[] {
  if (node.kind !== 'agent') return []
  const reviewNodeId = `${node.node_id}::review`
  return run.lane_candidates
    .filter((candidate) => candidate.build_node_id === node.node_id)
    .map((candidate) => ({
      candidate,
      review:
        run.candidate_reviews.find(
          (review) =>
            review.review_node_id === reviewNodeId &&
            review.candidate_sha === candidate.candidate_sha,
        ) ?? null,
      handoff:
        run.repair_handoffs.find(
          (handoff) =>
            handoff.build_node_id === node.node_id &&
            handoff.rejected_candidate_sha === candidate.candidate_sha,
        ) ?? null,
    }))
}
