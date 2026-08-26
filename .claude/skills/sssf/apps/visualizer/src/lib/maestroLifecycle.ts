import type {
  MaestroCandidateReview,
  MaestroLaneCandidate,
  MaestroNode,
  MaestroRepairHandoff,
  MaestroRunDetail,
  MaestroTestBytesLocation,
  MaestroTestStrengthPhase,
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
  // A tests node owns a candidate/review lifecycle exactly as a build lane
  // does. Excluding it here rendered a tests node's candidates, reviews and
  // repair handoffs as nothing at all — the display half of the defect that
  // let one reach MERGED unread.
  if (node.kind !== 'agent' && node.kind !== 'tests') return []
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


/**
 * Where a node sits in the test-strength lifecycle, or `null` for a kind the
 * lifecycle does not describe.
 *
 * The same projection the runtime computes, from the same inputs, so the CLI,
 * the dashboard and `run status` cannot describe one node three ways. Nothing
 * stores this: a fourth durable state machine beside `state` and `lane_phase`
 * would be two representations of one fact.
 *
 * `TEST_ACCEPTED` is reported whether or not the bytes reached the
 * integration branch, because acceptance and integration are different facts
 * and `testBytesLocation` reports the second one. Collapsing them is what
 * made a private acceptance render as `tester MERGED`.
 */
export function testStrengthPhase(
  node: MaestroNode,
  options: { accepted: boolean; paired: boolean },
): MaestroTestStrengthPhase | null {
  if (node.kind === 'tests') {
    if (node.state === 'BLOCKED') return 'TEST_BLOCKED'
    if (options.accepted) return 'TEST_ACCEPTED'
    // Integrated with no evidence attributable to its exact candidate: the
    // rollout's classification, rendered. `TEST_BUILDING` here would be
    // false and `TEST_ACCEPTED` would be the claim this whole contract
    // exists to stop a surface from making.
    if (node.state === 'MERGED' || node.state === 'ACCEPTED') {
      return 'TEST_LEGACY_UNPROVEN'
    }
    if (node.lane_phase === 'CANDIDATE_READY') return 'TEST_CANDIDATE_READY'
    if (node.lane_phase === 'REVIEWING') return 'TEST_REVIEWING'
    if (node.lane_phase === 'REPAIR_HANDOFF') return 'TEST_REJECTED'
    if (
      node.lane_phase === 'REPAIRING' ||
      node.lane_phase === 'WAITING_FOR_NEW_CANDIDATE'
    ) {
      return 'TEST_REPAIRING'
    }
    return 'TEST_BUILDING'
  }
  if (node.kind === 'agent') {
    if (node.state === 'MERGED') {
      return options.paired ? 'PAIRED_MERGED' : 'IMPLEMENTATION_ACCEPTED'
    }
    if (node.lane_phase === 'ACCEPTED') return 'IMPLEMENTATION_ACCEPTED'
    if (
      node.lane_phase === 'REVIEWING' ||
      node.lane_phase === 'CANDIDATE_READY' ||
      node.lane_phase === 'REPAIR_HANDOFF'
    ) {
      return 'IMPLEMENTATION_REVIEWING'
    }
    if (node.state === 'PENDING') return 'IMPLEMENTATION_PENDING'
    return 'IMPLEMENTATION_BUILDING'
  }
  return null
}

/**
 * The one test candidate carrying both halves of acceptance — strong measured
 * evidence and a passed independent review, bound to the same immutable sha.
 *
 * Asking only one of the two questions is the gap that let a tests node reach
 * MERGED on a case count, so the join lives here rather than at each caller.
 */
export function acceptedTestCandidate(
  run: MaestroRunDetail,
  testsNodeId: string,
): string | null {
  const reviewNodeId = `${testsNodeId}::review`
  for (const item of run.test_gate_evidence ?? []) {
    if (item.tests_node_id !== testsNodeId || !item.strong) continue
    const passed = (run.candidate_reviews ?? []).some(
      (review) =>
        review.review_node_id === reviewNodeId &&
        review.candidate_sha === item.candidate_sha &&
        review.state === 'COMPLETED' &&
        review.verdict === 'PASS',
    )
    if (passed) return item.candidate_sha
  }
  return null
}

/** Where a tests node's accepted bytes are, which `MERGED` alone cannot say. */
export function testBytesLocation(
  node: MaestroNode,
  accepted: string | null,
): MaestroTestBytesLocation | null {
  if (node.kind !== 'tests') return null
  if (accepted === null) return 'private'
  return node.state === 'MERGED' ? 'integrated' : 'staged'
}
