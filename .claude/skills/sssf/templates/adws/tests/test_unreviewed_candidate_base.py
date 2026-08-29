"""A retry is never cut from a published candidate.

`publish_candidate` requires every candidate after the first to be a proven
descendant of the one before it, so a retry taken after a candidate was
published -- and before any reviewer read it -- mints a sibling and is
refused `candidate SHA is equal to or not a proven descendant of its
parent` on every attempt for the life of the run.

Continuing the retry *from* that candidate looks like the fix and is not.
A candidate contains the implementation, and §7.4's pre-node clause
requires the attempt's starting tree to be one where the gate is RED.
Cutting the attempt from a candidate makes the pre-node gate green and
blocks the node `GATE_NOT_FALSIFIABLE`, which is terminal and
non-retryable -- strictly worse than the descent refusal, which at least
left the lane retryable.

Observed on `lane-wp6-build` (run-9d03105407f440079f3730f1fe4c67b3):
attempt 13 was cut from candidate a551049, launched no builder at all,
and blocked GATE_NOT_FALSIFIABLE 27 seconds later.

This test pins the constraint that rules the shortcut out, so the next
reader of the descent refusal does not reach for it again. The descent
refusal itself is answered by reviewing the published candidate, not by
building another one on top of it.

Real git repositories throughout; nothing stubs subprocess.run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from test_scheduler import SchedulerFixture, _git  # noqa: E402


class RetryBaseIsNeverACandidateTests(SchedulerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.written = {"a": {"a.py": "first candidate\n"}}

    def test_a_retry_after_an_unreviewed_candidate_stays_on_integration_head(self):
        head = _git(self.repo, "rev-parse", "HEAD")

        def exploding_review(
            attempt, node, record, base_sha, candidate_sha, _resume_existing
        ):
            raise TypeError(
                "FinalizationWindow.__init__() missing 1 required positional "
                "argument: 'record_reviewer_session'"
            )

        deps = self.deps(review_attempt=exploding_review)
        scheduler = self.schedule([self.agent("a")], deps=deps)
        scheduler.project()
        scheduler._attempt("a")

        published = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(published), 1)
        candidate = published[0].candidate_sha
        self.assertNotEqual(candidate, head)

        self.written = {"a": {"a.py": "second candidate\n"}}
        scheduler = self.schedule([self.agent("a")], deps=deps)
        scheduler.project()
        scheduler._attempt("a")

        retry = self.store.get_attempt("run1", "a", 2)
        self.assertNotEqual(
            retry.base_sha,
            candidate,
            "an attempt cut from a candidate cannot satisfy §7.4's pre-node "
            "clause: the implementation is already present, so the gate that "
            "must be red is green and the node blocks GATE_NOT_FALSIFIABLE",
        )
        self.assertEqual(retry.base_sha, head)
