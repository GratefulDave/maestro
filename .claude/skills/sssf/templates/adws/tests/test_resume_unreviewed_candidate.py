"""A published candidate nobody read is reviewed, not rebuilt.

`publish_candidate` writes the candidate before the reviewer is
constructed, and the publication is immutable. Anything that kills the
launch in that window leaves the lane owing a descendant of a commit no
reviewer has seen, and the retry cannot pay it: cut from the integration
head, its commit is a sibling of the candidate and is refused "candidate
SHA is equal to or not a proven descendant of its parent" on every
attempt for the life of the run. Cutting it from the candidate instead
makes §7.4's pre-node gate green and blocks the node
GATE_NOT_FALSIFIABLE, which is terminal.

run-9d03105407f440079f3730f1fe4c67b3's `lane-wp6-build` sat on candidate
a551049 in state PUBLISHED across nine retries and two operator resumes,
and its reviewer never once ran.

Real git repositories throughout; nothing stubs subprocess.run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from test_scheduler import SchedulerFixture, _git  # noqa: E402


class _Pass:
    passed = True
    subject_digest = "review-c1"
    findings = ()
    advisories = ()
    unreachable = ()

    def findings_text(self) -> str:
        return ""


class ResumeUnreviewedCandidateTests(SchedulerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.written = {"a": {"a.py": "first candidate\n"}}
        self.reviewed = []
        self.builds = 0

    def _run_once(self, review):
        deps = self.deps(review_attempt=review)
        scheduler = self.schedule([self.agent("a")], deps=deps)
        original = scheduler.deps.run_node

        def counting(*args, **kwargs):
            self.builds += 1
            return original(*args, **kwargs)

        object.__setattr__(scheduler.deps, "run_node", counting)
        scheduler.project()
        scheduler._attempt("a")
        return scheduler

    def _wedge(self):
        """Publish a candidate, then kill the reviewer launch."""

        def exploding(attempt, node, record, base, candidate_sha, _resume):
            raise TypeError(
                "FinalizationWindow.__init__() missing 1 required positional "
                "argument: 'record_reviewer_session'"
            )

        self._run_once(exploding)
        published = self.store.lane_candidates("run1", "a")
        self.assertEqual(len(published), 1)
        return published[0].candidate_sha

    def _accepting(self, attempt, node, record, base, candidate_sha, _resume):
        self.reviewed.append(candidate_sha)
        return _Pass()

    def test_the_wedge_leaves_a_published_candidate_no_reviewer_read(self):
        candidate = self._wedge()
        review = self.store.candidate_review("run1", "a::review", candidate)
        self.assertIsNotNone(review)
        self.assertEqual(review.state, st.CandidateReviewState.PUBLISHED)
        self.assertIsNone(review.verdict)

    def test_the_resume_reviews_that_candidate_and_builds_nothing(self):
        candidate = self._wedge()
        builds_before = self.builds

        self._run_once(self._accepting)

        self.assertEqual(self.reviewed, [candidate])
        self.assertEqual(
            self.builds,
            builds_before,
            "the candidate is already built and gate-verified; the resume "
            "owes it a reader, not another builder",
        )

    def test_no_second_candidate_is_minted_so_descent_is_never_refused(self):
        candidate = self._wedge()
        self._run_once(self._accepting)

        published = self.store.lane_candidates("run1", "a")
        self.assertEqual([c.candidate_sha for c in published], [candidate])
        self.assertEqual(
            self.store.get_node("run1", "a").state, st.NodeState.VERIFIED
        )

    def test_a_terminal_review_falls_through_to_an_ordinary_attempt(self):
        """The resume path is only for a review with no verdict. One that
        reached a verdict is the repair loop's business, not this."""
        candidate = self._wedge()
        scheduler = self.schedule([self.agent("a")], deps=self.deps(
            review_attempt=self._accepting))
        scheduler.project()
        context = sch._AttemptContext(record=None)

        self.assertTrue(scheduler._resume_unreviewed_candidate(
            scheduler.nodes["a"], context))

        review = self.store.candidate_review("run1", "a::review", candidate)
        self.assertEqual(review.state, st.CandidateReviewState.COMPLETED)
        self.assertFalse(scheduler._resume_unreviewed_candidate(
            scheduler.nodes["a"], sch._AttemptContext(record=None)))

    def test_a_lane_with_no_candidate_falls_through(self):
        scheduler = self.schedule([self.agent("a")], deps=self.deps(
            review_attempt=self._accepting))
        scheduler.project()
        self.assertEqual(self.store.lane_candidates("run1", "a"), ())
        self.assertFalse(scheduler._resume_unreviewed_candidate(
            scheduler.nodes["a"], sch._AttemptContext(record=None)))
