"""Executable proof that a retry budget is counted from a boundary rather than
from the beginning of time (#92, §7.5, §11.3, §1.2).

`attempts_spent` counted every attempt of a class over the node's whole life,
and nothing debited, expired, or reset it. The consequence is not theoretical:
`lane-p5-gap-policy` in `run-2a44d226e75a4be391a14f02b78a6d25` burned three
`LAUNCHER_TRANSIENT` attempts in ten seconds relaunching against a herdr
workspace that had already been destroyed (#79). That defect is fixed and
cannot recur — but the three rows remained, and against `launcher_retries: 2`
the node was permanently over budget: every later launcher failure, including a
genuinely transient one, blocked it on first contact against a debt no amount
of success could pay off. The operator's only route to more room was editing
`execution:` in the deployment's `maestro.config.yaml`, which raises the
ceiling for every node in the run to express a decision about one.

The repair is a floor, not a decrement: the attempt rows are the evidence chain
and are never rewritten, so the count moves where it *starts*. It moves only at
a typed operator boundary — `run resume` for every node in a run, `retry` for
the one node it names — which is what keeps §1.2 true of it. Nothing an agent
says, nothing a process observes, and no passage of time moves it.

  §11.3  a resume refreshes every node's retry budgets
  §11.3  an operator `retry` refreshes the budgets of the node it names, alone
  §7.5   a boundary forgives what was spent *before* it and nothing after it
  §1.2   the floor is written by a transition, and by nothing else

Run with:  uv run adw_test.py -k retry_budget_resume_reset
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_launcher_classification import (agent_node, gone_handle,  # noqa: E402
                                          make_repo, red_gate)
from test_lifecycle import make_node, new_store  # noqa: E402

#: The incident's numbers: three `LAUNCHER_TRANSIENT` attempts against a
#: configured budget of two.
INCIDENT_SPEND = 3
INCIDENT_BUDGET = 2


def _spend(store: lc.LifecycleStore, node_id: str, retry_class: st.RetryClass,
           times: int, run_id: str = "run1") -> None:
    """Spend `times` attempts of `retry_class`, the way the scheduler does:
    each one opened by `start_attempt` and closed by `fail_attempt`."""
    for _ in range(times):
        store.start_attempt(run_id, node_id, base_sha="s1")
        store.fail_attempt(run_id, node_id, retry_class)


def _spend_lane(
    store: lc.LifecycleStore,
    node_id: str,
    retry_class: st.LaneRetryClass,
    times: int,
    run_id: str = "run1",
) -> None:
    """Spend durable retained-lane cycles without rewriting their history."""
    first = len(store.lane_retry_spends(run_id, node_id))
    for offset in range(times):
        store.spend_lane_retry(
            run_id,
            node_id,
            retry_class,
            cycle_seq=first + offset + 1,
            candidate_sha=None,
            detail={"reason": "fixture"},
        )

#: A pid no process holds, so `scheduler_liveness` answers False and an escape
#: against a stranded RUNNING node is legal (§11.3).
DEAD_PID = 2_000_000_000


def _block(store: lc.LifecycleStore, node_id: str,
           retry_class: st.RetryClass = st.RetryClass.LAUNCHER_TRANSIENT,
           reason: st.BlockReason = st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED,
           run_id: str = "run1") -> None:
    """Block a node the way `_settle_failure` does: on an attempt of its own,
    classified in the same transaction as the block."""
    store.start_attempt(run_id, node_id, base_sha="s1")
    store.mark_blocked(run_id, node_id, reason, retry_class=retry_class)


def _blocked_run(store: lc.LifecycleStore, *node_ids: str,
                 run_id: str = "run1") -> None:
    """A declared-BLOCKED run, which is the only state an escape is legal
    against (§7.3). The first node is the blocked one; the rest stay PENDING."""
    store.create_run(run_id, "d", [make_node(n, 0) for n in node_ids])
    _block(store, node_ids[0], run_id=run_id)
    store.declare_outcome(run_id)


class ResumeRefreshesEveryNodesBudget(unittest.TestCase):
    """§11.3 — the resume boundary, which is where #92 asked for it."""

    def test_spend_before_a_resume_is_not_charged_after_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.LAUNCHER_TRANSIENT,
                   INCIDENT_SPEND)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.LAUNCHER_TRANSIENT),
                INCIDENT_SPEND)

            store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.LAUNCHER_TRANSIENT), 0)
            # The rows themselves are untouched: a refreshed budget must not
            # cost the ledger its account of what failed.
            self.assertEqual(
                len([a for a in store.attempts_for("run1", "a")
                     if a.retry_class is st.RetryClass.LAUNCHER_TRANSIENT]),
                INCIDENT_SPEND)

    def test_the_resume_refreshes_every_node_in_the_run(self):
        """The boundary is a property of the run, so no node is left behind by
        one that happened to be the scheduler's last."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d",
                             [make_node("a", 0), make_node("b", 0)])
            _spend(store, "a", st.RetryClass.LAUNCHER_TRANSIENT, 2)
            _spend(store, "b", st.RetryClass.ENVIRONMENTAL, 2)
            store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent(
                    "run1", "a", st.RetryClass.LAUNCHER_TRANSIENT
                ),
                0,
            )
            self.assertEqual(
                store.attempts_spent("run1", "b", st.RetryClass.ENVIRONMENTAL),
                0,
            )
            self.assertEqual(store.retry_spend_floor("run1", "a"), 2)
            self.assertEqual(store.retry_spend_floor("run1", "b"), 2)

    def test_resume_reopens_only_blocks_whose_retry_budget_was_refreshed(self):
        """Refreshing spend must make budget-exhausted nodes schedulable; it
        must not erase an unrelated adjudication merely because the same run
        was resumed."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            cases = (
                (
                    "launcher",
                    st.RetryClass.LAUNCHER_TRANSIENT,
                    st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED,
                ),
                (
                    "environmental",
                    st.RetryClass.ENVIRONMENTAL,
                    st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
                ),
                # Moved out of `terminal` deliberately, and this is the record
                # of the policy change rather than a test bent to fit code.
                # The semantic ceiling used to require an explicit grant on
                # §7.5's argument that repeated semantic failure indicates a
                # planning defect; §16.3 item 16 already called that an
                # assumption, and run-8d1a71f463e4430f92a125a8f8b3731d
                # transitions 1987/1988 are what it cost — one node over its
                # ceiling stopping a run until a human typed a grant. It is a
                # floor and not a ceiling removal, so the same ceiling still
                # bites after the boundary; `test_resume_refreshes_semantic_
                # budget.py` proves that half.
                (
                    "semantic",
                    st.RetryClass.SEMANTIC,
                    st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED,
                ),
                (
                    "review",
                    st.RetryClass.SEMANTIC,
                    st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                ),
            )
            terminal = (
                # `review` moved out of here too, one change after `semantic`
                # did, and for a different reason worth keeping distinct. The
                # semantic ceiling bounds itself — a gate is green or it is
                # not. A reviewer's opinion has no fixed point, so refreshing
                # it needs an explicit bound or it is the unbounded loop §3.6
                # A9 forbids. It has one: `RESUME_REVIEW_REFRESH_CEILING`, a
                # per-run allowance, after which a review block stays blocked.
                # This case sits inside that allowance;
                # `test_resume_refreshes_review_budget.py` proves the ceiling
                # bites once it is spent.
                ("credential", st.BlockReason.CREDENTIAL_REFUSED),
            )
            nodes = [make_node(node_id, 0) for node_id, _, _ in cases]
            nodes.extend(make_node(node_id, 0) for node_id, _ in terminal)
            store.create_run("run1", "d", nodes)
            for node_id, retry_class, reason in cases:
                _block(store, node_id, retry_class, reason)
            for node_id, reason in terminal:
                _block(store, node_id, st.RetryClass.SEMANTIC, reason)
            store.declare_outcome("run1")

            store.resume_run("run1")

            for node_id, _, _ in cases:
                with self.subTest(node_id=node_id):
                    lifecycle = store.get_node("run1", node_id)
                    self.assertIs(lifecycle.state, st.NodeState.PENDING)
                    self.assertIsNone(lifecycle.block_reason)
                    self.assertIs(
                        lifecycle.pending_cause,
                        st.PendingCause.OPERATOR_RESUME,
                    )
                    transition = store.conn.execute(
                        "SELECT reason FROM transitions"
                        " WHERE run_id=? AND node_id=? ORDER BY id DESC LIMIT 1",
                        ("run1", node_id),
                    ).fetchone()
                    self.assertEqual(transition[0], "resume:retry-budget")
            for node_id, reason in terminal:
                lifecycle = store.get_node("run1", node_id)
                self.assertIs(lifecycle.state, st.NodeState.BLOCKED)
                self.assertIs(lifecycle.block_reason, reason)

    def test_every_class_is_refreshed_by_one_boundary(self):
        """The floor is an attempt number, so it orders every class at once —
        a node that exhausted its launcher budget on #79 and its environmental
        budget on #89 must not have to be rescued twice."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.LAUNCHER_TRANSIENT, 2)
            _spend(store, "a", st.RetryClass.ENVIRONMENTAL, 3)

            store.resume_run("run1")

            for retry_class in (st.RetryClass.LAUNCHER_TRANSIENT,
                                st.RetryClass.ENVIRONMENTAL,
                                st.RetryClass.SEMANTIC):
                self.assertEqual(
                    store.attempts_spent("run1", "a", retry_class), 0,
                    retry_class.value)

    def test_the_attempt_the_resume_itself_charges_is_still_charged(self):
        """The floor is the highest attempt *already* classified, so the
        resume's own accounting survives intact: an inherited RUNNING attempt
        that declared nothing is charged ENVIRONMENTAL against the refreshed
        budget rather than being forgiven by the boundary that precedes it."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.ENVIRONMENTAL, 2)
            store.start_attempt("run1", "a", base_sha="s1")
            # The resume charges an inherited attempt only when it recorded
            # evidence of a failure; with no turn, result or sealed output it
            # is released UNCLASSIFIED and there is no charge for the floor to
            # spare. A pane alone is not evidence — a recorded turn is.
            store.record_heartbeat(
                store.get_attempt("run1", "a", 3), turn_count=1, observed_at=1.0
            )

            store.resume_run("run1")

            self.assertEqual(store.retry_spend_floor("run1", "a"), 2)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.ENVIRONMENTAL), 1)

    def test_an_inherited_attempt_that_declared_a_result_still_costs_nothing(self):
        """§9.7's exemption, unchanged by the boundary above it: the attempt is
        closed UNCLASSIFIED, and an unclassified row contributes to nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.record_result("run1", st.ResultRecord(
                node_id="a", attempt_no=1, subject_sha="s1",
                payload={"status": "success"},
                adjudication=st.Adjudication.ACCEPTED))

            store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.ENVIRONMENTAL), 0)


class ResumeRefreshesLaneRetryBudgets(unittest.TestCase):
    """§11.3 applies to retained-lane spends as well as attempt rows."""

    def test_environmental_lane_spend_starts_again_after_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend_lane(store, "a", st.LaneRetryClass.ENVIRONMENTAL, 2)
            _block(
                store,
                "a",
                st.RetryClass.ENVIRONMENTAL,
                st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
            )
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertEqual(len(store.lane_retry_spends("run1", "a")), 2)
            self.assertEqual(store.current_lane_retry_spends("run1", "a"), ())
            self.assertIs(store.get_node("run1", "a").state, st.NodeState.PENDING)

            _spend_lane(store, "a", st.LaneRetryClass.ENVIRONMENTAL, 1)
            current = store.current_lane_retry_spends("run1", "a")
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0].cycle_seq, 3)

class RetryRefreshesTheNodeItNames(unittest.TestCase):
    """§11.3 — the per-node half. A `run resume` flag raises the ceiling for
    every node in the run and edits a deployment-owned file to do it (#91);
    the escape is a decision about one node."""

    def test_the_escape_forgives_the_named_nodes_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.LAUNCHER_TRANSIENT,
                   INCIDENT_SPEND)
            _block(store, "a")
            store.declare_outcome("run1")
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.LAUNCHER_TRANSIENT),
                INCIDENT_SPEND + 1)

            store.retry("run1", "a")

            self.assertEqual(store.get_node("run1", "a").state,
                             st.NodeState.PENDING)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.LAUNCHER_TRANSIENT), 0)

    def test_the_escape_forgives_the_named_nodes_lane_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend_lane(store, "a", st.LaneRetryClass.ENVIRONMENTAL, 2)
            _block(store, "a")
            store.declare_outcome("run1")

            store.retry("run1", "a")

            self.assertEqual(store.current_lane_retry_spends("run1", "a"), ())
            self.assertEqual(len(store.lane_retry_spends("run1", "a")), 2)

    def test_the_escape_leaves_every_other_nodes_budget_alone(self):
        """The granularity that is the point of doing this per node."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _blocked_run(store, "a", "b")
            _spend(store, "b", st.RetryClass.ENVIRONMENTAL, 2)

            store.retry("run1", "a")

            self.assertEqual(
                store.attempts_spent("run1", "b",
                                     st.RetryClass.ENVIRONMENTAL), 2)
            self.assertEqual(store.retry_spend_floor("run1", "b"), 0)

    def test_the_escape_still_charges_the_attempt_it_closes(self):
        """A `retry` against a node stranded RUNNING by a dead scheduler closes
        the live attempt ENVIRONMENTAL in the same transaction. The floor is
        written *before* that close, so the escape does not forgive the very
        failure it is being invoked over."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.ENVIRONMENTAL, 2)
            store.start_attempt("run1", "a", base_sha="s1")
            store.declare_outcome("run1")
            # A dead scheduler, which is what makes RUNNING escapable (§11.3).
            store.conn.execute(
                "UPDATE runs SET scheduler_pid=?, scheduler_host=?"
                " WHERE run_id=?",
                (DEAD_PID, lc.scheduler_host(), "run1"))

            store.retry("run1", "a")

            self.assertEqual(store.retry_spend_floor("run1", "a"), 2)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.ENVIRONMENTAL), 1)

    def test_a_grant_and_a_refreshed_budget_are_different_allowances(self):
        """`--grant N` sizes the review and semantic *ceilings*, which count
        judged work; the retry classes count infrastructure faults. One
        command moves both, and neither reading is derived from the other."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.LAUNCHER_TRANSIENT, 2)
            _block(store, "a")
            store.declare_outcome("run1")

            store.retry("run1", "a", grant=3)

            self.assertEqual(
                store.get_node("run1", "a").granted_extra_attempts, 3)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.LAUNCHER_TRANSIENT), 0)


class TheFloorMovesOnlyAtABoundary(unittest.TestCase):
    """§1.2 — the budget a node is charged against is a function of the
    ledger's transition rows, never of a process's opinion about whether an
    old failure still counts."""

    def test_a_failed_attempt_does_not_move_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.ENVIRONMENTAL, 2)
            self.assertEqual(store.retry_spend_floor("run1", "a"), 0)

            _spend(store, "a", st.RetryClass.ENVIRONMENTAL, 1)

            self.assertEqual(store.retry_spend_floor("run1", "a"), 0)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.ENVIRONMENTAL), 3)

    def test_blocking_a_node_does_not_move_the_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.LAUNCHER_TRANSIENT, 2)
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a",
                               st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED,
                               retry_class=st.RetryClass.LAUNCHER_TRANSIENT)

            self.assertEqual(store.retry_spend_floor("run1", "a"), 0)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.LAUNCHER_TRANSIENT), 3)

    def test_a_ledger_written_before_the_column_counts_as_it_always_did(self):
        """NULL says nobody recorded a boundary, never that one was recorded at
        zero — and both read the same, which is the behaviour that predates the
        column. The migration invents no facts."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            _spend(store, "a", st.RetryClass.ENVIRONMENTAL, 2)
            store.conn.execute(
                "UPDATE node_lifecycle SET retry_spend_floor=NULL"
                " WHERE run_id=? AND node_id=?", ("run1", "a"))

            self.assertEqual(store.retry_spend_floor("run1", "a"), 0)
            self.assertEqual(
                store.attempts_spent("run1", "a",
                                     st.RetryClass.ENVIRONMENTAL), 2)


class ARescuedNodeGetsItsAttemptsBack(unittest.TestCase):
    """The whole complaint, driven through the real scheduler: a node blocked
    on an exhausted launcher budget, handed back by an operator, must get its
    allowance rather than blocking again on first contact.

    Every attempt here fails the same way — the launcher answers with a handle
    it holds no record of, which is the shape `HerdrLauncher.poll` produces for
    `agent_not_found` — so the second run is measured against a node that is
    still failing, never against one the fixture quietly made healthy.
    """

    RUN_ID = "run-rescue"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = lc.LifecycleStore(self.root / "lifecycle.db")
        self.addCleanup(self.store.close)
        self.repo, self.integration, self.branch = make_repo(
            self.root, self.RUN_ID)

    def _config(self) -> st.SchedulerConfig:
        return st.SchedulerConfig(
            concurrency=1, node_timeout_s=30, turn_timeout_s=10,
            final_acceptance_timeout_s=30, backstop_t_s=120,
            semantic_ceiling=2, launcher_retries=INCIDENT_BUDGET)

    def _run_once(self) -> None:
        """One scheduler generation over the shared store, every launch
        failing LAUNCHER_TRANSIENT through the production refusal itself —
        `_require_session_path` against a handle the launcher holds no record
        of, which is the shape `HerdrLauncher.poll` produces for
        `agent_not_found`."""
        def run_node(attempt, node, record, retry_prompt, on_launch, cancelled):
            on_launch(None)
            handle = gone_handle(
                "{}-{}".format(node.node_id, record.attempt_no), attempt.path)
            maestro._require_session_path(handle, node.node_id,
                                          record.attempt_no)
            raise AssertionError("the session-path refusal did not fire")

        deps = sch.SchedulerDeps(
            store=self.store, repo=self.repo,
            integration_path=self.integration, integration_branch=self.branch,
            worktrees_root=self.root / "worktrees",
            scratch_root=self.root / "scratch", run_node=run_node,
            run_gate=lambda *a: red_gate(),
            run_integration_gate=lambda *a: red_gate(),
            quiesce_attempt=lambda record, phase: None,
            kill_attempt=lambda *a: None)
        scheduler = sch.Scheduler(self.RUN_ID, [agent_node()], self._config(),
                                  deps, plan_digest="rescue-digest")
        self.addCleanup(scheduler.shutdown)
        scheduler.run()

    def _attempts(self) -> int:
        return len(self.store.attempts_for(self.RUN_ID, "a"))

    def test_an_operator_retry_buys_the_node_its_budget_again(self):
        self._run_once()
        first = self._attempts()
        self.assertIs(self.store.get_node(self.RUN_ID, "a").block_reason,
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)
        # The configured budget, plus the attempt that spent past it.
        self.assertEqual(first, INCIDENT_BUDGET + 1)

        self.store.retry(self.RUN_ID, "a")
        self._run_once()

        self.assertEqual(
            self._attempts() - first, first,
            "a rescued node blocked again on first contact: the boundary "
            "bought it no attempts")
        # Still failing, and still blocked for the same stated reason — the
        # escape bought attempts, never a different verdict.
        self.assertIs(self.store.get_node(self.RUN_ID, "a").block_reason,
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)
        self.assertEqual(
            {a.retry_class for a in self.store.attempts_for(self.RUN_ID, "a")},
            {st.RetryClass.LAUNCHER_TRANSIENT})

    def test_the_budget_still_bounds_the_rescued_generation(self):
        """A refreshed budget is a budget. The node does not become
        unblockable — it spends its allowance again and blocks again, which is
        what separates this from raising the ceiling."""
        self._run_once()
        self.store.retry(self.RUN_ID, "a")
        self._run_once()

        node = self.store.get_node(self.RUN_ID, "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason,
                      st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED)
        # The allowance the second generation actually spent, sized by the
        # production budget function, plus the attempt that spent past it.
        self.assertEqual(
            self.store.attempts_spent(self.RUN_ID, "a",
                                      st.RetryClass.LAUNCHER_TRANSIENT),
            rp.launcher_retry_budget(self._config(),
                                     rp.LauncherFailure.STARTUP) + 1,
            "the refreshed budget did not bound the rescued generation")


if __name__ == "__main__":
    unittest.main()
