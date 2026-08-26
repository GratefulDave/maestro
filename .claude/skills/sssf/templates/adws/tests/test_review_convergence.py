"""Candidate-review convergence is derived without attempt-extra authority."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import review_convergence as rc
from adw_modules import scheduler_types as st


C1 = "1" * 40
C2 = "2" * 40
C3 = "3" * 40


def _node(node_id="build", *, state=st.NodeState.RUNNING, block_reason=None, granted=0):
    return SimpleNamespace(
        node_id=node_id,
        state=state,
        block_reason=block_reason,
        granted_extra_attempts=granted,
    )


def _review_node(node_id="build::review"):
    return SimpleNamespace(
        node_id=node_id,
        state=st.NodeState.PENDING,
        block_reason=None,
        granted_extra_attempts=0,
    )


def _candidate(seq, sha, node_id="build"):
    return st.LaneCandidate(
        run_id="run1",
        build_node_id=node_id,
        candidate_seq=seq,
        candidate_sha=sha,
        parent_candidate_sha=None if seq == 1 else C1,
        builder_generation=1,
        created_at="2026-08-25T00:00:00+00:00",
    )


def _review(sha, verdict, findings=(), node_id="build::review"):
    return st.CandidateReview(
        run_id="run1",
        review_node_id=node_id,
        candidate_sha=sha,
        reviewer_generation=1,
        state=st.CandidateReviewState.COMPLETED,
        review_digest="digest-" + sha[:1],
        receipt_path="/receipts/" + sha[:1],
        findings=tuple(findings),
        verdict=verdict,
        completed_at="2026-08-25T00:00:01+00:00",
    )


def _findings(count):
    return tuple(
        {"check_id": "check-{}".format(index), "message": "finding"}
        for index in range(count)
    )


def _profile(*, node=None, candidates=(), reviews=(), in_flight=False, ceiling=3):
    node = node or _node(state=st.NodeState.MERGED)
    return rc.run_convergence(
        "run1",
        (node, _review_node()),
        candidates,
        reviews,
        review_ceiling=ceiling,
        in_flight=in_flight,
    )


class CandidateConvergenceTest(unittest.TestCase):
    def test_rejections_and_pass_are_ordered_by_candidate_sequence(self):
        profile = _profile(
            candidates=(_candidate(1, C1), _candidate(2, C2), _candidate(3, C3)),
            reviews=(
                _review(C3, st.ReviewVerdict.PASS),
                _review(C1, st.ReviewVerdict.REJECTED, _findings(3)),
                _review(C2, st.ReviewVerdict.REJECTED, _findings(1)),
            ),
        )

        lane = profile.lanes[0]
        self.assertIs(lane.outcome, rc.Outcome.CONVERGED)
        self.assertEqual(
            [
                (item.candidate_seq, item.candidate_sha, item.findings)
                for item in lane.findings_per_candidate
            ],
            [(1, C1, 3), (2, C2, 1)],
        )
        self.assertEqual(lane.passed_at_candidate, 3)
        self.assertEqual(lane.passed_candidate_sha, C3)
        self.assertEqual(lane.convergence_length, 3)
        self.assertTrue(lane.descending)

    def test_rejected_lane_uses_durable_block_reason(self):
        profile = _profile(
            node=_node(
                state=st.NodeState.BLOCKED,
                block_reason=st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
            ),
            candidates=(_candidate(1, C1),),
            reviews=(_review(C1, st.ReviewVerdict.REJECTED, _findings(2)),),
        )

        lane = profile.lanes[0]
        self.assertIs(lane.outcome, rc.Outcome.NOT_CONVERGED)
        self.assertIs(lane.cause, rc.Cause.REVIEW_CEILING_REACHED)

    def test_live_rejected_lane_is_not_reported_as_ended(self):
        profile = _profile(
            node=_node(),
            candidates=(_candidate(1, C1),),
            reviews=(_review(C1, st.ReviewVerdict.REJECTED, _findings(1)),),
            in_flight=True,
        )
        lane = profile.lanes[0]
        self.assertIs(lane.cause, rc.Cause.RUN_IN_FLIGHT)
        self.assertIn("run still in flight", rc.render(profile))

    def test_dispatched_review_is_not_invented_as_a_verdict(self):
        dispatched = st.CandidateReview(
            run_id="run1",
            review_node_id="build::review",
            candidate_sha=C1,
            reviewer_generation=1,
            state=st.CandidateReviewState.DISPATCHED,
            review_digest=None,
            receipt_path=None,
            findings=(),
            verdict=None,
            completed_at=None,
        )
        lane = _profile(
            node=_node(),
            candidates=(_candidate(1, C1),),
            reviews=(dispatched,),
            in_flight=True,
        ).lanes[0]
        self.assertIs(lane.outcome, rc.Outcome.NO_REVIEW)
        self.assertEqual(lane.findings_per_candidate, ())

    def test_attempt_shaped_objects_cannot_change_the_projection(self):
        profile = rc.run_convergence(
            "run1",
            (_node(), _review_node()),
            (_candidate(1, C1),),
            (_review(C1, st.ReviewVerdict.PASS),),
            in_flight=False,
        )
        self.assertIs(profile.lanes[0].outcome, rc.Outcome.CONVERGED)
        lane_payload = profile.as_dict()["lanes"][0]
        self.assertNotIn("findings_per_attempt", lane_payload)
        self.assertNotIn("passed_at_attempt", lane_payload)


class ConvergenceSummaryTest(unittest.TestCase):
    def test_longest_and_warning_use_candidate_review_count(self):
        profile = _profile(
            candidates=(_candidate(1, C1), _candidate(2, C2), _candidate(3, C3)),
            reviews=(
                _review(C1, st.ReviewVerdict.REJECTED, _findings(3)),
                _review(C2, st.ReviewVerdict.REJECTED, _findings(1)),
                _review(C3, st.ReviewVerdict.PASS),
            ),
            ceiling=2,
        )
        self.assertEqual(profile.longest.node_id, "build")
        self.assertIn("3 candidate reviews", profile.ceiling_warning)
        payload = profile.as_dict()
        self.assertEqual(
            payload["lanes"][0]["findings_per_candidate"][0]["candidate_sha"], C1
        )
        self.assertNotIn("findings_per_attempt", payload["lanes"][0])

    def test_only_authored_parents_of_derived_review_nodes_are_lanes(self):
        profile = rc.run_convergence(
            "run1",
            (_node("build"), _review_node("build::review"), _node("plain")),
            (),
            (),
        )
        self.assertEqual([lane.node_id for lane in profile.lanes], ["build"])


if __name__ == "__main__":
    unittest.main()
