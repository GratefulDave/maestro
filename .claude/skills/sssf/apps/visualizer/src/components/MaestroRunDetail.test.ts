import { describe, expect, test } from 'bun:test'
import type { MaestroNode, MaestroRunDetail } from '../lib/types'
import { candidateLifecycleForNode, nodeAuthorityState } from '../lib/maestroLifecycle'

function buildNode(lane_phase: string | null): MaestroNode {
  return {
    node_id: 'lane-bronze',
    kind: 'agent',
    depth: 0,
    needs: [],
    outputs: [],
    state: 'RUNNING',
    lane_phase,
    attempt_no: 1,
    block_reason: null,
    cancel_cause: null,
    merge_cause: null,
    output_sha: null,
    granted_extra_attempts: 0,
    updated_at: null,
    attempts: [],
  }
}

describe('MaestroRunDetail lifecycle projection', () => {
  test('uses durable lane phase for styling with legacy state fallback', () => {
    expect(nodeAuthorityState(buildNode('REPAIRING'))).toBe('REPAIRING')
    expect(nodeAuthorityState(buildNode(null))).toBe('RUNNING')
  })

  test('joins candidate review findings and repair handoff by exact SHA', () => {
    const candidateSha = 'a'.repeat(40)
    const finding = {
      check_id: 'diff.contract',
      object_id: 'src/lane.ts',
      message: 'repair the rejected contract',
      blocking: true,
    }
    const run = {
      lane_candidates: [
        {
          build_node_id: 'lane-bronze',
          candidate_seq: 1,
          candidate_sha: candidateSha,
          parent_candidate_sha: null,
          builder_generation: 2,
          created_at: '2026-08-26T00:00:00Z',
        },
      ],
      candidate_reviews: [
        {
          review_node_id: 'lane-bronze::review',
          candidate_sha: candidateSha,
          reviewer_generation: 3,
          state: 'COMPLETED',
          review_digest: 'digest',
          receipt_path: '/receipts/review.json',
          findings: [finding],
          verdict: 'REJECTED',
          completed_at: '2026-08-26T00:01:00Z',
        },
      ],
      repair_handoffs: [
        {
          build_node_id: 'lane-bronze',
          rejected_candidate_sha: candidateSha,
          findings: [finding],
          state: 'ACKNOWLEDGED',
          builder_generation: 2,
          submitted_at: '2026-08-26T00:02:00Z',
          acknowledged_at: '2026-08-26T00:03:00Z',
        },
      ],
    } as MaestroRunDetail

    const [projected] = candidateLifecycleForNode(run, buildNode('REPAIRING'))

    expect(projected?.candidate.candidate_sha).toBe(candidateSha)
    expect(projected?.review?.verdict).toBe('REJECTED')
    expect(projected?.review?.findings).toEqual([finding])
    expect(projected?.handoff?.state).toBe('ACKNOWLEDGED')
  })
})
