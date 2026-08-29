"""A published candidate whose review never ran still owns the lane's tip.

`publish_candidate` requires every candidate after the first to be a proven
descendant of the one before it. `_durable_repair_resume` hands the next
attempt a basis only when a reviewer REJECTED the previous candidate. A
candidate published moments before the review machinery itself failed --
an ENVIRONMENTAL retry taken between publication and the reviewer's first
turn -- leaves no basis, so the retry used to be provisioned from
integration HEAD. Its commit was then a *sibling* of the published
candidate, the descent assertion refused it, that refusal classified
ENVIRONMENTAL, and the lane rebuilt forever without ever being reviewed.

Observed in production on `lane-wp6-build`
(run-9d03105407f440079f3730f1fe4c67b3): candidate a551049 was published at
00:00:54, the finalization window raised a TypeError five seconds later,
and every attempt after it died on `candidate SHA is equal to or not a
proven descendant of its parent`.

Real git repositories throughout; nothing stubs subprocess.run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from test_review_repair_basis import _Review  # noqa: E402
from test_scheduler import SchedulerFixture  # noqa: E402


class UnreviewedCandidateBaseTests(SchedulerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.written = {"a": {"a.py": "first candidate\n"}}
        self.reviewed = []

    def _drive(self, review):
        deps = self.deps(review_attempt=review)
        scheduler = self.schedule([self.agent("a")], deps=deps)
        scheduler.project()
        scheduler._attempt("a")
        return scheduler

    def test_a_retry_after_an_unreviewed_candidate_bases_on_that_candidate(self):
        def exploding_review(
            attempt, node, record, base_sha, candidate_sha, _resume_existing
        ):
            self.reviewed.append(candidate_sha)
            raise TypeError(
                "FinalizationWindow.__init__() missing 1 required positional "
                "argument: 'record_reviewer_session'"
            )

        self._drive(exploding_review)

        published = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(published), 1)
        first = published[0].candidate_sha
        self.assertEqual(self.reviewed, [first])

        integration_head = self.store.get_attempt("run1", "a", 1).integration_head
        self.assertNotEqual(first, integration_head)

        # The retry. Its base must be the published candidate, not the
        # integration head the first attempt was cut from.
        self.written = {"a": {"a.py": "second candidate\n"}}
        self.reviewed = []
        scheduler = self._drive(
            lambda attempt, node, record, base_sha, candidate_sha, _resume: (
                self.reviewed.append(candidate_sha) or _Review(True, "review-c2")
            )
        )
        del scheduler

        retry = self.store.get_attempt("run1", "a", 2)
        self.assertEqual(retry.base_sha, first)

        # And the descent assertion that used to refuse every retry now
        # passes, so the second candidate is published and reviewed.
        published = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(published), 2)
        self.assertEqual(published[1].parent_candidate_sha, first)
        self.assertEqual(self.reviewed, [published[1].candidate_sha])
        self.assertEqual(
            [
                spend
                for spend in self.store.lane_retry_spends("run1", "a")
                if spend.retry_class is st.LaneRetryClass.ENVIRONMENTAL
            ][1:],
            [],
        )
