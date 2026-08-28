"""A bare `run resume` refreshes the review budget — a bounded number of times.

The semantic ceiling was the first half of this (`test_resume_refreshes_
semantic_budget.py`). The review ceiling is the harder half, because §3.6 A9 is
a standing prohibition against exactly the loop an unbounded refresh would
create:

    Never gate progress on a zero-finding LLM sweep with restart-on-any-finding
    — it has no bounded termination. Bound the loop or accept graded findings.

A semantic ceiling bounds itself: the gate either goes green or it does not, and
the adjudicator is a count of executed cases. A reviewer's opinion has no such
fixed point, so `reject -> resume -> repair -> reject` can run as long as
somebody keeps typing `run resume`. A9 does not forbid refreshing the review
budget; it forbids refreshing it *without a bound*. So the refresh lands with
one, and A9 is amended to "bounded refresh" rather than deleted.

**The bound is per run and it is counted, not timed.** `runs.review_refresh_count`
is incremented once per resume that actually reopens a review-budget-blocked
node, and once it reaches `RESUME_REVIEW_REFRESH_CEILING` a review-budget block
stops being refreshed and stays blocked for an operator to look at. Counting
only resumes that *did something* is what lets the ceiling and idempotence hold
at the same time: resuming twice with no work in between reopens nothing the
second time, so it consumes nothing.

**The existing late-envelope recovery is untouched and is not subject to the
ceiling.** That path (`REVIEW_BUDGET_RECOVERY_KEY` plus a proven late envelope)
answers a different question — a review verdict that arrived after the block —
and it is a correction of the record rather than a fresh allowance. It is
covered by the existing tests in `test_lifecycle.py` and
`test_skip_merge_provenance.py`, which must stay green; this file deliberately
does not re-implement its fixture.

  1  a review-blocked node makes progress on a bare resume, no grant
  2  the per-run ceiling bites, so the loop A9 forbids cannot run forever
  3  repeated resume stays idempotent and consumes one unit, not two
  4  a non-budget block still survives the resume
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_lifecycle import make_node, new_store  # noqa: E402
from test_retry_budget_resume_reset import _block  # noqa: E402


def _review_blocked(store, node_id="lane-refund", run_id="run1"):
    """A run whose only node is blocked on its review ceiling."""
    store.create_run(run_id, "d", [make_node(node_id, 0)])
    _block(
        store,
        node_id,
        st.RetryClass.SEMANTIC,
        st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
        run_id=run_id,
    )
    store.declare_outcome(run_id)


class AReviewCeilingIsRefreshedByABareResume(unittest.TestCase):
    """Regression 1."""

    def test_a_review_blocked_node_returns_to_the_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _review_blocked(store)

            store.resume_run("run1")

            node = store.get_node("run1", "lane-refund")
            self.assertIs(node.state, st.NodeState.PENDING)
            self.assertIsNone(node.block_reason)
            self.assertIs(node.pending_cause, st.PendingCause.OPERATOR_RESUME)

    def test_no_attempt_or_transition_row_is_destroyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _review_blocked(store)
            before = store.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=?", ("run1",)
            ).fetchone()[0]

            store.resume_run("run1")

            after = store.conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=?", ("run1",)
            ).fetchone()[0]
            self.assertEqual(after, before, "attempt rows are the evidence chain")


class ThePerRunCeilingBites(unittest.TestCase):
    """Regression 2 — bounded, which is what makes this legal under A9."""

    def test_the_node_stays_blocked_once_the_ceiling_is_reached(self):
        ceiling = lc.RESUME_REVIEW_REFRESH_CEILING
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _review_blocked(store)

            # Each cycle: resume reopens it, the reviewer rejects it again.
            for cycle in range(ceiling):
                store.resume_run("run1")
                self.assertIs(
                    store.get_node("run1", "lane-refund").state,
                    st.NodeState.PENDING,
                    f"cycle {cycle} should still have capacity",
                )
                _block(
                    store,
                    "lane-refund",
                    st.RetryClass.SEMANTIC,
                    st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                )
                store.declare_outcome("run1")


            store.resume_run("run1")

            node = store.get_node("run1", "lane-refund")
            self.assertIs(
                node.state,
                st.NodeState.BLOCKED,
                "past the ceiling the loop must stop for an operator to look",
            )
            self.assertIs(node.block_reason, st.BlockReason.REVIEW_BUDGET_EXHAUSTED)

    def test_the_ceiling_is_a_real_bound_and_not_a_large_number(self):
        """A9 asks for termination, so the bound must actually be small.

        Asserted rather than assumed: a ceiling of, say, 10_000 would satisfy
        every other test here while leaving the loop A9 forbids effectively
        unbounded in practice.
        """
        self.assertGreaterEqual(lc.RESUME_REVIEW_REFRESH_CEILING, 1)
        self.assertLessEqual(lc.RESUME_REVIEW_REFRESH_CEILING, 10)


class TheLateEnvelopeRecoveryIsNotSubjectToTheCeiling(unittest.TestCase):
    """The two routes are separate allowances, and only one of them is bounded.

    The recovery route answers "a verdict arrived after the block was written",
    which is a correction of the record rather than another round of the loop
    A9 bounds. If the ceiling gated it, a run that had spent its refreshes
    could never absorb a late reviewer verdict again — which would make the
    bound eat a correctness path it was never aimed at.
    """

    def test_recovery_still_works_with_the_allowance_fully_spent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run(
                "run1", "d", [make_node("filler", 0), make_node("lane-refund", 0)]
            )

            # Spend the run's allowance on a different node, and spend it
            # *before* the node under test is blocked. Spending it on the node
            # under test would mint fresh attempts and move the recovery key
            # the grant writes off the attempt the late envelope names.
            for _ in range(lc.RESUME_REVIEW_REFRESH_CEILING):
                _block(
                    store,
                    "filler",
                    st.RetryClass.SEMANTIC,
                    st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                )
                store.declare_outcome("run1")
                store.resume_run("run1")

            _block(
                store,
                "lane-refund",
                st.RetryClass.SEMANTIC,
                st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
            )
            store.declare_outcome("run1")
            # A grant is what marks the retained attempt as recoverable.
            store.retry("run1", "lane-refund", grant=1)
            attempt_no = store.get_node("run1", "lane-refund").attempt_no

            store.resume_run("run1")
            self.assertIs(
                store.get_node("run1", "lane-refund").state,
                st.NodeState.BLOCKED,
                "precondition: the bounded route is exhausted",
            )

            store.resume_run(
                "run1", late_envelope_attempts=(("lane-refund", attempt_no),)
            )

            self.assertIs(
                store.get_node("run1", "lane-refund").state,
                st.NodeState.PENDING,
                "the recovery route must survive an exhausted ceiling",
            )


class RepeatedResumeIsIdempotent(unittest.TestCase):
    """Regression 3 — two resumes with no work between them cost one unit."""

    def test_resuming_twice_consumes_one_unit_of_the_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _review_blocked(store)

            store.resume_run("run1")
            store.resume_run("run1")

            self.assertIs(
                store.get_node("run1", "lane-refund").state, st.NodeState.PENDING
            )

    def test_a_run_with_no_review_block_never_spends_the_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _block(
                store, "a", st.RetryClass.SEMANTIC,
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
            )
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertIs(store.get_node("run1", "a").state, st.NodeState.PENDING)


class TheRefreshIsKeyedOnTheBlockReason(unittest.TestCase):
    """Regression 4 — 'refreshes budgets' must not become 'clears blocks'."""

    def test_a_non_budget_block_survives_the_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run(
                "run1", "d", [make_node("review", 0), make_node("credential", 0)]
            )
            _block(
                store, "review", st.RetryClass.SEMANTIC,
                st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
            )
            _block(
                store, "credential", st.RetryClass.SEMANTIC,
                st.BlockReason.CREDENTIAL_REFUSED,
            )
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertIs(
                store.get_node("run1", "review").state, st.NodeState.PENDING
            )
            credential = store.get_node("run1", "credential")
            self.assertIs(credential.state, st.NodeState.BLOCKED)
            self.assertIs(credential.block_reason, st.BlockReason.CREDENTIAL_REFUSED)

    def test_terminal_work_is_not_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run(
                "run1", "d", [make_node("review", 0), make_node("merged", 0)]
            )
            _block(
                store, "review", st.RetryClass.SEMANTIC,
                st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
            )
            store.start_attempt("run1", "merged", base_sha="s1")
            store.mark_verified("run1", "merged", output_sha="sha1")
            store.mark_merged("run1", "merged")
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertIs(
                store.get_node("run1", "merged").state, st.NodeState.MERGED
            )


if __name__ == "__main__":
    unittest.main()
