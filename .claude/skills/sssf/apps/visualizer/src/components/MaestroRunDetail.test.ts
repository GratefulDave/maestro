import { describe, expect, test } from 'bun:test'
import type { MaestroNode, MaestroRunDetail } from '../lib/types'
import {
  acceptedTestCandidate,
  candidateLifecycleForNode,
  nodeAuthorityState,
  testBytesLocation,
  testStrengthPhase,
} from '../lib/maestroLifecycle'

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

function testsNode(overrides: Partial<MaestroNode> = {}): MaestroNode {
  return { ...buildNode(null), node_id: 'lane-tests', kind: 'tests', ...overrides }
}

describe('the test-strength lifecycle the dashboard renders', () => {
  const candidateSha = 'c'.repeat(40)

  function runWith(strong: boolean, verdict: string): MaestroRunDetail {
    return {
      test_gate_evidence: [
        {
          tests_node_id: 'lane-tests',
          candidate_sha: candidateSha,
          runner: 'pytest',
          selector: 'tests/test_manifest.py',
          strong,
          refusal: strong ? null : 'TEST_STRENGTH_REQUIREMENT_UNCOVERED',
          evidence: {},
          created_at: '2026-08-26T00:00:00Z',
        },
      ],
      candidate_reviews: [
        {
          review_node_id: 'lane-tests::review',
          candidate_sha: candidateSha,
          reviewer_generation: 1,
          state: 'COMPLETED',
          review_digest: 'digest',
          receipt_path: '/receipts/review.json',
          findings: [],
          verdict,
          completed_at: '2026-08-26T00:01:00Z',
        },
      ],
      test_pairings: [],
    } as unknown as MaestroRunDetail
  }

  test('a tests node has a candidate lifecycle at all', () => {
    const run = {
      lane_candidates: [
        {
          build_node_id: 'lane-tests',
          candidate_seq: 1,
          candidate_sha: candidateSha,
          parent_candidate_sha: null,
          builder_generation: 1,
          created_at: '2026-08-26T00:00:00Z',
        },
      ],
      candidate_reviews: [],
      repair_handoffs: [],
    } as unknown as MaestroRunDetail
    expect(candidateLifecycleForNode(run, testsNode()).length).toBe(1)
  })

  test('acceptance needs strong evidence and a passed review, together', () => {
    expect(acceptedTestCandidate(runWith(true, 'PASS'), 'lane-tests')).toBe(candidateSha)
    expect(acceptedTestCandidate(runWith(false, 'PASS'), 'lane-tests')).toBeNull()
    expect(acceptedTestCandidate(runWith(true, 'REJECTED'), 'lane-tests')).toBeNull()
  })

  test('a private acceptance is not rendered as merged', () => {
    const node = testsNode({ state: 'VERIFIED', lane_phase: 'ACCEPTED' })
    expect(testStrengthPhase(node, { accepted: true, paired: false })).toBe('TEST_ACCEPTED')
    expect(testBytesLocation(node, candidateSha)).toBe('staged')
  })

  test('an unreviewed candidate reads as reviewing, never as accepted', () => {
    const node = testsNode({ state: 'RUNNING', lane_phase: 'REVIEWING' })
    expect(testStrengthPhase(node, { accepted: false, paired: false })).toBe('TEST_REVIEWING')
    expect(testBytesLocation(node, null)).toBe('private')
  })

  test('an implementation is PAIRED_MERGED only with its pairing', () => {
    const node = buildNode('ACCEPTED')
    node.state = 'MERGED'
    expect(testStrengthPhase(node, { accepted: false, paired: true })).toBe('PAIRED_MERGED')
    expect(testStrengthPhase(node, { accepted: false, paired: false })).toBe(
      'IMPLEMENTATION_ACCEPTED',
    )
  })

  test('a code node gets no phase rather than an invented one', () => {
    const node = testsNode({ kind: 'code', state: 'MERGED' })
    expect(testStrengthPhase(node, { accepted: false, paired: false })).toBeNull()
  })
})
