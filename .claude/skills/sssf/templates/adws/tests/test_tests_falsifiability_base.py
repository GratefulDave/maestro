"""A tests node is proven red where the implementation does not exist.

`attempt.base` is the *mutable measurement* base. Three paths move it onto
an already-published candidate:

  I1  `prepare_descendant_candidate` on every repair round after a review
      rejection (`worktree.py` sets `attempt.base = parent`)
  I2  the sealed / unsealed repair recoveries, which start a new attempt
      from `repair_parent`
  I3  a retry that continues from a candidate no reviewer ever read

A candidate already contains the tests, so collecting the parent nodeids
there proves nothing about them. Only cases added since the previous
candidate are counted new and run red; every case already accepted is
never re-proven; and a tester that strengthens an assertion on an existing
case adds no nodeid at all and is refused `TESTS_NO_NEW_CASES` for doing
the right thing.

`lane-wp6-tests` in run-9d03105407f440079f3730f1fe4c67b3 published three
chained candidates under one attempt at builder generation 1 and merged.
Candidates 2 and 3 were only ever proven red against candidate 1.

Real git repositories and real ledger rows; nothing stubs subprocess.run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402
from test_scheduler import SchedulerFixture, _git  # noqa: E402


class TestsFalsifiabilityBaseTests(SchedulerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.scheduler = self.schedule([self.agent("a")], deps=self.deps())
        self.scheduler.project()
        self.node = self.scheduler.nodes["a"]
        self.head = _git(self.repo, "rev-parse", "HEAD")

    def _worktree(self, base: str, attempt_no: int) -> wt.AttemptWorktree:
        # Left in place: `remove_attempt_worktree` refuses an unproven
        # ancestry by design, and the fixture's temporary directory takes
        # the whole tree at cleanup.
        return wt.create_attempt_worktree(
            self.repo,
            "run1",
            "a",
            attempt_no,
            base,
            self.root / "worktrees",
            self.root / "scratch",
        )

    def _commit_a_candidate(self, attempt: wt.AttemptWorktree, body: str) -> str:
        (attempt.path / "t_a.py").write_text(body)
        _git(attempt.path, "add", "t_a.py")
        _git(attempt.path, "commit", "-qm", "test(a): candidate")
        return _git(attempt.path, "rev-parse", "HEAD")

    def test_a_repair_round_is_still_measured_against_the_lanes_own_start(self):
        # I1. The attempt row is written once; the repair round moves only
        # the in-memory measurement base.
        self.store.start_attempt("run1", "a", self.head)
        attempt = self._worktree(self.head, 1)
        candidate = self._commit_a_candidate(
            attempt, "def test_one():\n    assert False\n"
        )
        attempt.base = candidate

        self.assertNotEqual(candidate, self.head)
        self.assertEqual(
            self.scheduler._pre_candidate_base(self.node, attempt), self.head
        )

    def test_a_new_attempt_cut_from_a_candidate_keeps_the_original_baseline(self):
        # I2 and I3 both start a *new* attempt whose recorded base IS the
        # published candidate. The falsifiability baseline must not follow it.
        self.store.start_attempt("run1", "a", self.head)
        first = self._worktree(self.head, 1)
        candidate = self._commit_a_candidate(
            first, "def test_one():\n    assert False\n"
        )
        self.store.fail_attempt("run1", "a", st.RetryClass.ENVIRONMENTAL)
        self.store.start_attempt("run1", "a", candidate)
        second = self._worktree(candidate, 2)

        self.assertEqual(second.base, candidate)
        self.assertEqual(self.store.get_attempt("run1", "a", 2).base_sha, candidate)
        self.assertEqual(
            self.scheduler._pre_candidate_base(self.node, second), self.head
        )

    def test_the_earliest_attempt_wins_over_every_later_one(self):
        self.store.start_attempt("run1", "a", self.head)
        first = self._worktree(self.head, 1)
        c1 = self._commit_a_candidate(first, "def test_one():\n    assert False\n")
        self.store.fail_attempt("run1", "a", st.RetryClass.ENVIRONMENTAL)
        self.store.start_attempt("run1", "a", c1)
        second = self._worktree(c1, 2)
        c2 = self._commit_a_candidate(second, "def test_two():\n    assert False\n")
        self.store.fail_attempt("run1", "a", st.RetryClass.ENVIRONMENTAL)
        self.store.start_attempt("run1", "a", c2)
        third = self._worktree(c2, 3)

        bases = {row.base_sha for row in self.store.attempts_for("run1", "a")}
        self.assertEqual(bases, {self.head, c1, c2})
        self.assertEqual(
            self.scheduler._pre_candidate_base(self.node, third), self.head
        )

    def test_without_a_recorded_attempt_it_falls_back_to_the_worktree_base(self):
        attempt = self._worktree(self.head, 1)
        self.assertEqual(self.store.attempts_for("run1", "a"), ())
        self.assertEqual(
            self.scheduler._pre_candidate_base(self.node, attempt), self.head
        )


class AgentFalsificationBaseTests(SchedulerFixture):
    """I5. §7.4 reverts an agent node's outputs back to a base and re-asks
    the gate. A repair round already refuses `attempt.base` there and names
    the integration head; the `basis is None` arm did not, and a retry
    continuing from an unreviewed candidate reaches it with `attempt.base`
    already moved onto that candidate. Reverting to the candidate reverts
    the node's own previous output and falsifies nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.written = {"a": {"a.py": "first candidate\n"}}
        self.falsify_bases = []

    def _drive(self, review):
        scheduler = self.schedule([self.agent("a")], deps=self.deps(
            review_attempt=review))
        original = scheduler._falsify_outputs

        def recording(node, attempt, falsify_base):
            self.falsify_bases.append(falsify_base)
            return original(node, attempt, falsify_base)

        scheduler._falsify_outputs = recording
        scheduler.project()
        scheduler._attempt("a")
        return scheduler

    def test_the_retry_falsifies_against_the_lane_start_not_the_candidate(self):
        head = _git(self.repo, "rev-parse", "HEAD")

        def exploding_review(attempt, node, record, base, candidate, _resume):
            raise TypeError("the finalization window is broken")

        self._drive(exploding_review)
        candidate = self.store.lane_candidates("run1", "a")[0].candidate_sha
        self.assertNotEqual(candidate, head)

        self.written = {"a": {"a.py": "second candidate\n"}}
        self._drive(
            lambda attempt, node, record, base, candidate_sha, _resume: _Passed()
        )

        self.assertEqual(self.store.get_attempt("run1", "a", 2).base_sha, candidate)
        self.assertEqual(self.falsify_bases, [head, head])


class _Passed:
    passed = True
    subject_digest = "review-c2"
    findings = ()
    advisories = ()
    unreachable = ()

    def findings_text(self) -> str:
        return ""
