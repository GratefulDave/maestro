"""Exactly-once candidate review and merge-gating regression tests."""

from __future__ import annotations

import sys
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from test_review_repair_basis import _Review  # noqa: E402
from test_scheduler import SchedulerFixture  # noqa: E402


class CandidateReviewAuthorityTests(SchedulerFixture):
    def test_completed_candidate_review_is_never_redispatched(self):
        scheduler = self.schedule(
            [self.agent("a")],
            deps=self.deps(
                review_attempt=lambda *args: self.fail("review callback must not run"),
                receipt_path_for=lambda digest: str(self.root / "receipts" / digest),
            ),
        )
        scheduler.project()
        attempt_no = self.store.start_attempt("run1", "a", "a" * 40)
        self.assertEqual(attempt_no, 1)
        candidate = self.store.publish_candidate(
            "run1", "a", "b" * 40, builder_generation=1
        ).candidate
        first = self.store.begin_review(
            "run1", "a::review", candidate.candidate_sha, reviewer_generation=1
        )
        self.assertTrue(first.should_dispatch)
        completed = self.store.complete_review(
            "run1",
            "a::review",
            candidate.candidate_sha,
            reviewer_generation=1,
            verdict=st.ReviewVerdict.PASS,
            review_digest="review-c1",
            receipt_path=str(self.root / "receipts" / "review-c1"),
            findings=(),
        )
        self.assertTrue(completed.completed)

        resumed = self.schedule(
            [self.agent("a")],
            deps=self.deps(
                review_attempt=lambda *args: self.fail("completed review redispatched"),
                receipt_path_for=lambda digest: str(self.root / "receipts" / digest),
            ),
        )
        resumed.project()
        replay = self.store.begin_review(
            "run1", "a::review", candidate.candidate_sha, reviewer_generation=1
        )
        self.assertFalse(replay.should_dispatch)
        self.assertIs(replay.review.verdict, st.ReviewVerdict.PASS)

    def test_rejected_candidate_cannot_merge_without_a_matching_pass(self):
        self.written = {"a": {"a.py": "candidate one\n"}}
        reviewed = []
        closed = []

        def review(attempt, node, record, base_sha, candidate_sha, _resume_existing):
            reviewed.append(candidate_sha)
            return _Review(
                False,
                "review-c1",
                (
                    {
                        "check_id": "correct",
                        "object_id": "a.py",
                        "message": "must be fixed",
                    },
                ),
            )

        scheduler = self.schedule(
            [self.agent("a")],
            config=self.config(review_ceiling=1),
            deps=self.deps(
                review_attempt=review,
                close_review=closed.append,
                receipt_path_for=lambda digest: str(self.root / "receipts" / digest),
            ),
        )

        report = scheduler.run()

        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(closed, ["a"])
        self.assertEqual(self.store.get_node("run1", "a").state, st.NodeState.BLOCKED)
        self.assertEqual(
            self.store.get_node("run1", "a::review").state, st.NodeState.PENDING
        )
        self.assertNotIn(
            "MERGED", [row.state for row in self.store.node_records("run1")]
        )
