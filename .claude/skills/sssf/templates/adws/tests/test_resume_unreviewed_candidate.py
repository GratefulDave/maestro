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
from adw_modules import worktree as wt  # noqa: E402
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


class _Reject:
    passed = False
    subject_digest = "review-c1"
    advisories = ()
    unreachable = ()
    findings = (
        {
            "check_id": "diff.introduces_no_obvious_defect",
            "grade": "error",
            "message": "a.py:1 does the wrong thing",
            "object_id": "diff:candidate",
        },
    )

    def findings_text(self) -> str:
        return "a.py:1 does the wrong thing"


class ResumedCandidateIsRepairableTests(ResumeUnreviewedCandidateTests):
    """The resumed attempt must be able to *act* on the verdict it fetches.

    Reaching the reviewer is half the debt. A REJECT goes on to
    `wt.prepare_descendant_candidate`, which refuses `HeadMoved` unless the
    worktree HEAD and `refs/heads/maestro/{run}/{node}/a{n}` both already
    hold the candidate -- a repair commits a descendant of the thing that was
    rejected, so it has to start standing on it.

    Cut from the integration head, run-9d03105407f440079f3730f1fe4c67b3's
    `lane-wp6-build` reviewed candidate a551049 correctly and then died on
    its own findings with `HeadMoved: HEAD is 3b1f1beb70, not candidate
    parent a551049c94`, classified ENVIRONMENTAL and retried into the same
    refusal.
    """

    def _capture(self, verdict):
        seen = {}

        def review(attempt, node, record, base, candidate_sha, _resume):
            seen["head"] = _git(attempt.path, "rev-parse", "HEAD").strip()
            seen["ref"] = wt.attempt_ref_commit(
                attempt.repo, attempt.run_id, attempt.node_id, attempt.attempt_no
            )
            seen["candidate"] = candidate_sha
            return verdict

        return seen, review

    def test_the_resumed_worktree_stands_on_the_candidate(self):
        candidate = self._wedge()
        seen, review = self._capture(_Pass())

        self._run_once(review)

        self.assertEqual(seen["candidate"], candidate)
        self.assertEqual(
            seen["head"],
            candidate,
            "prepare_descendant_candidate refuses HeadMoved unless the "
            "worktree HEAD is the candidate a repair must descend from",
        )
        self.assertEqual(
            seen["ref"],
            candidate,
            "prepare_descendant_candidate also compare-and-swaps the "
            "attempt's own ref, so branching it at the head refuses too",
        )

    def test_a_rejected_resumed_candidate_never_reports_headmoved(self):
        """The end the invariant above exists for. `HeadMoved` is raised out
        of the repair round and classified ENVIRONMENTAL, so the lane retries
        into it forever while reporting a broken machine for what is a
        correctly delivered verdict about content."""
        self._wedge()
        _, review = self._capture(_Reject())

        self._run_once(review)

        moved = [
            row[0]
            for row in self.store.conn.execute(
                "SELECT detail_json FROM transitions WHERE run_id=?", ("run1",)
            )
            if "HeadMoved" in (row[0] or "")
        ]
        self.assertEqual(
            moved,
            [],
            "the repair round could not stand on the candidate it was "
            "rejecting",
        )

    def test_a_rejected_candidate_whose_repair_never_started_is_resumed(self):
        """The crash window after the verdict and before the repair.

        `reject_and_create_handoff` writes the findings durably, and the
        repair builder is launched after it. A death in between leaves the
        handoff `PENDING` -- read, rejected, and never repaired. That is the
        unread candidate's debt wearing a later face, and it must not fall to
        `_durable_repair_resume`, which reopens the rejected attempt to
        continue a builder that in this case never ran, finds no accepted
        payload, and raises "late envelope is not usable".

        Written against the ledger rather than by driving a rejection through
        the fixture, because the state under test is the one a *death* leaves:
        the verdict durable, the repair unstarted, and the node still
        resumable.
        """
        candidate = self._wedge()
        self.store.begin_review("run1", "a::review", candidate, reviewer_generation=1)
        self.store.mark_review_dispatched(
            "run1", "a::review", candidate, reviewer_generation=1
        )
        self.store.reject_and_create_handoff(
            "run1",
            "a::review",
            candidate,
            reviewer_generation=1,
            builder_generation=1,
            review_digest="review-digest",
            receipt_path="/dev/null",
            findings=list(_Reject.findings),
        )

        handoff = self.store.repair_handoff("run1", "a", candidate)
        self.assertEqual(
            handoff.state,
            st.RepairHandoffState.PENDING,
            "a handoff a repair builder held would be SUBMITTED",
        )

        scheduler = self.schedule(
            [self.agent("a")], deps=self.deps(review_attempt=self._accepting)
        )
        scheduler.project()
        self.assertTrue(
            scheduler._resume_unreviewed_candidate(
                scheduler.nodes["a"], sch._AttemptContext(record=None)
            ),
            "a rejected candidate whose repair never started still owes a "
            "descendant, and no fresh build can produce one",
        )
