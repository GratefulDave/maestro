"""A bare `run resume` returns a semantically-blocked node to the frontier.

The gap this closes: before it, a node blocked `SEMANTIC_BUDGET_EXHAUSTED` had
no route back to the frontier except a hand-typed `retry --grant`, because the
resume boundary refreshed every other retry class and deliberately skipped that
one.

The incident, from `run-8d1a71f463e4430f92a125a8f8b3731d`:

    1987  lane-routing-chemical  RUNNING->BLOCKED  SEMANTIC_BUDGET_EXHAUSTED
    1988  (run)                  ->BLOCKED         declare-outcome
    1989  lane-routing-chemical  BLOCKED->PENDING  resume:retry-budget
    1993  lane-routing-chemical  RUNNING->BLOCKED  SEMANTIC_BUDGET_EXHAUSTED

1989 to 1993 is thirteen seconds, and it is the first version of this fix
failing in production: the resume reopened the lane, and the lane re-blocked
because the *lane* budget was still counted unfloored.
`TheLaneBudgetActuallyHonoursTheFloor` below is the executed proof of that half.

These cases are built on synthetic fixtures driven through the real store rather
than on that ledger, so they discriminate independently of it.

**The mechanism this needs already existed.** `node_lifecycle.retry_spend_floor`
and `lane_retry_spend_floor` were added by #92 precisely so a budget counts from
a boundary rather than from the beginning of time, and `resume_run` already
raises both for every node in the run. Nothing is deleted or rewritten by it:
the attempt rows, the spend rows and the transitions are the evidence chain, and
the floor moves where counting *starts*. So this change is not a new epoch — a
second durable representation of one fact would be the RC1 shape this design
convicts everywhere else — it is one entry added to the set of block reasons the
existing boundary reopens.

What changed is a policy decision, and it is worth recording that it *was* one.
`_RESUME_REFRESHED_BLOCK_REASONS` deliberately excluded the semantic ceiling on
the grounds that launcher and environmental faults are infrastructure while a
semantic ceiling is an adjudication bound — §7.5's argument that repeated
semantic failure indicates a planning defect rather than bad luck. §16.3 item 16
already records that argument as an assumption rather than a result, and the
operator's recovery as a forced retry. This makes the recovery the ordinary
resume path instead.

  1  a semantically-blocked node makes progress on a bare resume, no grant
  2  resuming twice hands out capacity once
  3  only the blocked frontier is touched, and its capacity is bounded
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_lifecycle import make_node, new_store  # noqa: E402
from test_retry_budget_resume_reset import _block, _spend  # noqa: E402


class TheLaneBudgetActuallyHonoursTheFloor(unittest.TestCase):
    """The half that was inert, driven through the real production method.

    Reopening a node is not the same as giving it budget. `resume_run` raises
    both floors, and the *attempt* budget (`attempts_spent`, keyed on
    `node_lifecycle.retry_spend_floor`) honours it — which is what the rest of
    this file asserts, and why those cases passed while production stayed
    broken. The durable **lane** budget is a second accounting, and
    `Scheduler._lane_retry` read it unfloored for exactly the classes a resume
    now refreshes:

        budget_spends = (
            current_lane_retry_spends(...)      # floor-aware
            if retry_class in (ENVIRONMENTAL, LAUNCHER_TRANSIENT)
            else lane_retry_spends(...)         # UNFLOORED
        )

    SEMANTIC, REVIEW_REJECTION and TEST_REVIEW_REJECTION all took the `else`,
    so a semantically-blocked lane was reopened by the resume and then
    re-blocked on its next spend against a count that still included every
    pre-boundary cycle. Production receipt: floor raised to 12 at 06:43:15,
    `blocked:SEMANTIC_BUDGET_EXHAUSTED` at 06:43:28 — thirteen seconds.

    These cases use a **real `LifecycleStore`** and call the real
    `Scheduler._lane_retry`. A fake store would have asserted the branch back
    at us, which is the failure mode that let this ship.
    """

    RUN_ID = "run-lane-budget"
    NODE_ID = "lane-refund"
    CEILING = 3

    def _probe(self, store):
        """`Scheduler` reduced to the budget seam, over the real store."""
        return SimpleNamespace(
            run_id=self.RUN_ID,
            config=st.SchedulerConfig(
                concurrency=1,
                node_timeout_s=30,
                turn_timeout_s=10,
                final_acceptance_timeout_s=30,
                backstop_t_s=120,
                semantic_ceiling=self.CEILING,
            ),
            deps=SimpleNamespace(store=store),
        )

    def _spend_semantic(self, probe):
        return sch.Scheduler._lane_retry(
            probe,
            SimpleNamespace(node_id=self.NODE_ID),
            st.LaneRetryClass.SEMANTIC,
            candidate_sha=None,
            detail={"reason": "rejected"},
        )

    def test_a_lane_at_its_semantic_ceiling_gets_budget_back_after_a_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run(self.RUN_ID, "d", [make_node(self.NODE_ID, 0)])
            probe = self._probe(store)

            for _ in range(self.CEILING):
                self._spend_semantic(probe)
            self.assertFalse(
                self._spend_semantic(probe),
                "precondition: the lane is over its semantic ceiling",
            )

            store.declare_outcome(self.RUN_ID)
            store.resume_run(self.RUN_ID)

            self.assertTrue(
                self._spend_semantic(probe),
                "a resumed lane must get a usable attempt, not re-block "
                "against spend the boundary already forgave",
            )

    def test_the_spend_accounting_moves_and_the_history_does_not(self):
        """Prove the numbers, not just the boolean.

        "The test passes" was insufficient here once already, so this asserts
        the two counts directly: the floored view resets and the durable rows
        are all still there.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run(self.RUN_ID, "d", [make_node(self.NODE_ID, 0)])
            probe = self._probe(store)
            for _ in range(self.CEILING):
                self._spend_semantic(probe)

            before_all = len(store.lane_retry_spends(self.RUN_ID, self.NODE_ID))
            store.declare_outcome(self.RUN_ID)
            store.resume_run(self.RUN_ID)

            after_all = len(store.lane_retry_spends(self.RUN_ID, self.NODE_ID))
            after_current = len(
                store.current_lane_retry_spends(self.RUN_ID, self.NODE_ID)
            )
            self.assertEqual(
                after_all, before_all, "durable spend rows are the evidence chain"
            )
            self.assertEqual(
                after_current, 0, "the floored view is what a budget must read"
            )
            self.assertGreater(
                store.lane_retry_spend_floor(self.RUN_ID, self.NODE_ID), 0
            )

    def test_the_ceiling_still_bites_after_the_boundary(self):
        """Bounded, not removed — the same proof the review ceiling gets."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run(self.RUN_ID, "d", [make_node(self.NODE_ID, 0)])
            probe = self._probe(store)
            for _ in range(self.CEILING):
                self._spend_semantic(probe)
            store.declare_outcome(self.RUN_ID)
            store.resume_run(self.RUN_ID)

            for _ in range(self.CEILING):
                self._spend_semantic(probe)
            self.assertFalse(
                self._spend_semantic(probe),
                "a fresh allowance is still an allowance, not an off-switch",
            )


class ASemanticCeilingIsRefreshedByABareResume(unittest.TestCase):
    """Regression 1 — the live incident, reproduced against the real store."""

    def test_a_semantically_blocked_node_returns_to_the_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("lane-routing-chemical", 0)])
            _block(
                store,
                "lane-routing-chemical",
                st.RetryClass.SEMANTIC,
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
            )
            store.declare_outcome("run1")

            store.resume_run("run1")

            node = store.get_node("run1", "lane-routing-chemical")
            self.assertIs(node.state, st.NodeState.PENDING)
            self.assertIsNone(node.block_reason)
            self.assertIs(node.pending_cause, st.PendingCause.OPERATOR_RESUME)
            self.assertEqual(
                store.attempts_spent(
                    "run1", "lane-routing-chemical", st.RetryClass.SEMANTIC
                ),
                0,
                "the semantic budget must count from the resume boundary",
            )

    def test_the_transition_is_typed_and_says_why(self):
        """§1.2 — the reopen is keyed on a typed reason, never on prose."""
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

            reason = store.conn.execute(
                "SELECT reason FROM transitions"
                " WHERE run_id=? AND node_id=? ORDER BY id DESC LIMIT 1",
                ("run1", "a"),
            ).fetchone()
            self.assertEqual(reason[0], "resume:retry-budget")

    def test_no_attempt_spend_or_transition_row_is_destroyed(self):
        """The floor moves; the evidence chain does not.

        The requirement is explicit that historical spend and audit evidence
        survive intact, and it is the reason a floor was the right mechanism in
        #92 rather than a decrement.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.SEMANTIC, 3)
            _block(
                store, "a", st.RetryClass.SEMANTIC,
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
            )
            store.declare_outcome("run1")

            def counts():
                return tuple(
                    store.conn.execute(
                        "SELECT (SELECT COUNT(*) FROM attempts WHERE run_id=?),"
                        " (SELECT COUNT(*) FROM transitions WHERE run_id=?)",
                        ("run1", "run1"),
                    ).fetchone()
                )

            before = counts()
            store.resume_run("run1")
            attempts_after, transitions_after = counts()

            self.assertEqual(
                attempts_after, before[0], "attempt rows must survive a resume"
            )
            self.assertGreater(
                transitions_after,
                before[1],
                "the resume itself is a transition and is appended, not replacing",
            )


class ResumingTwiceHandsOutCapacityOnce(unittest.TestCase):
    """Regression 2 — idempotence while the run is active."""

    def test_a_second_resume_does_not_stack_a_second_allowance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.SEMANTIC, 3)
            _block(
                store, "a", st.RetryClass.SEMANTIC,
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
            )
            store.declare_outcome("run1")

            store.resume_run("run1")
            floor_once = store.retry_spend_floor("run1", "a")
            spent_once = store.attempts_spent("run1", "a", st.RetryClass.SEMANTIC)

            store.resume_run("run1")
            floor_twice = store.retry_spend_floor("run1", "a")
            spent_twice = store.attempts_spent("run1", "a", st.RetryClass.SEMANTIC)

            self.assertEqual(
                floor_once,
                floor_twice,
                "with no spend in between, the boundary has nowhere to move",
            )
            self.assertEqual(spent_once, spent_twice)

    def test_spend_after_the_first_resume_is_still_charged_after_the_second(self):
        """Idempotence must not become amnesia.

        The dangerous reading of 'resume refreshes budgets' is that resuming in
        a loop buys unlimited attempts. A second resume moves the floor to the
        spend that has actually happened since the first, which is the same rule
        applied twice rather than a second allowance.
        """
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

            _spend(store, "a", st.RetryClass.SEMANTIC, 2)
            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.SEMANTIC),
                2,
                "spend after the boundary is charged against it",
            )


class OnlyTheBlockedFrontierIsTouched(unittest.TestCase):
    """Regression 3 — capacity goes to the frontier, bounded, and nowhere else."""

    def test_terminal_nodes_are_not_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            nodes = [make_node(n, 0) for n in ("blocked", "merged", "untouched")]
            store.create_run("run1", "d", nodes)
            _block(
                store, "blocked", st.RetryClass.SEMANTIC,
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
            )
            # Driven to MERGED the way production does, so the assertion is
            # about a genuinely terminal node rather than a hand-set column.
            store.start_attempt("run1", "merged", base_sha="s1")
            store.mark_verified("run1", "merged", output_sha="sha1")
            store.mark_merged("run1", "merged")
            store.declare_outcome("run1")

            before = {
                node_id: store.get_node("run1", node_id).state
                for node_id in ("merged", "untouched")
            }
            self.assertIs(before["merged"], st.NodeState.MERGED)

            store.resume_run("run1")

            self.assertIs(
                store.get_node("run1", "blocked").state, st.NodeState.PENDING
            )
            for node_id, state in before.items():
                with self.subTest(node_id=node_id):
                    self.assertIs(
                        store.get_node("run1", node_id).state,
                        state,
                        "a resume must not disturb terminal work",
                    )

    def test_an_unrelated_adjudication_still_stays_blocked(self):
        """The refresh is keyed on the block reason, not on being blocked.

        `CREDENTIAL_REFUSED` is not a budget and no boundary can pay it off, so
        it must survive a resume that reopens the budget-exhausted nodes beside
        it. Without this, 'resume refreshes budgets' would quietly become
        'resume clears every block'.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run(
                "run1", "d", [make_node("semantic", 0), make_node("credential", 0)]
            )
            _block(
                store, "semantic", st.RetryClass.SEMANTIC,
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
            )
            _block(
                store, "credential", st.RetryClass.SEMANTIC,
                st.BlockReason.CREDENTIAL_REFUSED,
            )
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertIs(
                store.get_node("run1", "semantic").state, st.NodeState.PENDING
            )
            credential = store.get_node("run1", "credential")
            self.assertIs(credential.state, st.NodeState.BLOCKED)
            self.assertIs(credential.block_reason, st.BlockReason.CREDENTIAL_REFUSED)

    def test_the_refreshed_capacity_is_a_floor_and_not_an_infinite_ceiling(self):
        """Bounded, asserted by spending it again.

        A refreshed budget that could not be exhausted a second time would be
        an off-switch for §7.5 rather than a boundary, so the proof that it is
        bounded is that the same ceiling still bites after the resume.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.SEMANTIC, 4)
            _block(
                store, "a", st.RetryClass.SEMANTIC,
                st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
            )
            store.declare_outcome("run1")
            store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.SEMANTIC), 0
            )
            _spend(store, "a", st.RetryClass.SEMANTIC, 4)
            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.SEMANTIC),
                4,
                "the ceiling still counts after the boundary; it is not removed",
            )


if __name__ == "__main__":
    unittest.main()
