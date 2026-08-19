"""A run an operator stopped with `run cancel` is resumable (§7.3, §7.8, §11.2).

`resume_run` refused `ACCEPTED` and `CANCELLED` under one predicate, and the
two are not alike. `ACCEPTED` is terminal because the run reached its declared
outcome. `CANCELLED` is two outcomes wearing one word: a run the operator
stopped with `run cancel`, where nothing was adjudicated and there is no result
to protect, and a run given up on node by node through `abandon` (§11.3), where
every node was individually adjudicated as work the run should finish without.
The store recorded neither, so the distinction could not be made at resume
time; that missing cause was the defect, and the tests below settle its repair.

  §7.3   the cause of a CANCELLED outcome is stored typed, at run and node level
  §7.8   a run stopped by `run cancel` resumes; its MERGED nodes stay MERGED
  §7.3   an ACCEPTED run is still refused
  §7.3   a run abandoned node by node is still refused
  §7.8   an inherited RUNNING attempt is still failed ENVIRONMENTAL and re-launched
  §11.2  the resume refreshes `last_transition_at` before the backstop starts
  --     the cause survives a store reopen, and an unrecorded cause refuses

Run with:  uv run adw_test.py -k run_cancel_resume
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def make_node(node_id: str, depth: int, needs=()) -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=depth,
                       needs=tuple(needs), command=("true",))


def new_store(tmp_root: Path) -> lc.LifecycleStore:
    return lc.LifecycleStore(tmp_root / "lifecycle.db")


def merge(store: lc.LifecycleStore, run_id: str, node_id: str, sha: str) -> None:
    """Take one node all the way to MERGED, the way the scheduler does."""
    store.start_attempt(run_id, node_id, base_sha="base")
    store.mark_verified(run_id, node_id, output_sha=sha)
    store.mark_merged(run_id, node_id)


class CancelCauseIsRecordedTests(unittest.TestCase):
    """§1.2: the resume predicate keys on a typed record, never on prose or on
    a heuristic read of the node states."""

    def test_operator_cancel_records_run_cancel_at_run_and_node_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0), make_node("b", 0)])
            merge(store, "run1", "a", "sha_a")

            store.cancel_run("run1")
            report = store.declare_outcome("run1")

            self.assertEqual(report.outcome, st.RunOutcome.CANCELLED)
            self.assertEqual(report.cancel_cause, st.CancelCause.RUN_CANCEL)
            self.assertEqual(store.run_cancel_cause("run1"),
                             st.CancelCause.RUN_CANCEL)
            causes = dict(store.conn.execute(
                "SELECT node_id, cancel_cause FROM node_lifecycle WHERE run_id=?",
                ("run1",)).fetchall())
            # The merged node is absolutely terminal, so the cancel never took
            # it and it carries no cause at all.
            self.assertIsNone(causes["a"])
            self.assertEqual(causes["b"], st.CancelCause.RUN_CANCEL.value)

    def test_node_by_node_abandonment_records_abandoned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0), make_node("b", 0)])
            store.start_attempt("run1", "a", base_sha="s")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            store.declare_outcome("run1")
            store.abandon("run1", "a")
            store.abandon("run1", "b")

            report = store.declare_outcome("run1")

            self.assertEqual(report.outcome, st.RunOutcome.CANCELLED)
            self.assertEqual(report.cancel_cause, st.CancelCause.ABANDONED)
            self.assertEqual(store.run_cancel_cause("run1"),
                             st.CancelCause.ABANDONED)
            causes = dict(store.conn.execute(
                "SELECT node_id, cancel_cause FROM node_lifecycle WHERE run_id=?",
                ("run1",)).fetchall())
            self.assertEqual(causes["a"], st.CancelCause.ABANDONED.value)
            self.assertEqual(causes["b"], st.CancelCause.ABANDONED.value)

    def test_the_cause_survives_a_store_reopen(self):
        """The cause is a durable record, not a fact the declaring process
        remembers. A resume is a different process by definition (§7.8)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            store.declare_outcome("run1")
            store.close()

            reopened = new_store(Path(tmp))
            self.assertEqual(reopened.run_cancel_cause("run1"),
                             st.CancelCause.RUN_CANCEL)
            self.assertEqual(
                reopened.conn.execute(
                    "SELECT cancel_cause FROM node_lifecycle"
                    " WHERE run_id=? AND node_id=?", ("run1", "a")).fetchone()[0],
                st.CancelCause.RUN_CANCEL.value)
            # And the read-only projection an operator's `run status` goes
            # through carries it too, so "will resume take this back" is a
            # question the ledger answers rather than one the operator guesses.
            reader = lc.LifecycleReader.open(reopened.db_path)
            try:
                self.assertEqual(reader.run("run1").cancel_cause,
                                 st.CancelCause.RUN_CANCEL)
            finally:
                reader.close()

    def test_a_non_cancelled_declaration_clears_a_stale_cause(self):
        """`runs` holds the latest outcome only (§7.3). A run that was
        cancelled, resumed, and then accepted must not keep the cause of the
        outcome it superseded."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            store.declare_outcome("run1")
            self.assertEqual(store.run_cancel_cause("run1"),
                             st.CancelCause.RUN_CANCEL)

            store.resume_run("run1")
            merge(store, "run1", "a", "sha_a")
            report = store.declare_outcome("run1", acceptance_result=True)

            self.assertEqual(report.outcome, st.RunOutcome.ACCEPTED)
            self.assertIsNone(store.run_cancel_cause("run1"))


class ResumeAfterOperatorCancelTests(unittest.TestCase):

    def test_an_operator_cancelled_run_resumes_keeping_its_merged_nodes(self):
        """The whole point. Twelve lanes cancelled at hour six with eight
        merged must not throw the eight away: `MERGED` is absolutely terminal,
        the cancel never touched those rows, and the resume must not re-run
        them."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [
                make_node("merged", 0), make_node("pending", 0),
                make_node("downstream", 1, needs=("merged",))])
            merge(store, "run1", "merged", "sha_merged")
            store.cancel_run("run1")
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertEqual(store.get_node("run1", "merged").state,
                             st.NodeState.MERGED)
            # Not re-executed: the merged node's attempt count is untouched and
            # its output SHA still names the work that landed.
            self.assertEqual(store.get_node("run1", "merged").attempt_no, 1)
            self.assertEqual(store.get_node("run1", "merged").output_sha,
                             "sha_merged")
            self.assertEqual(store.get_node("run1", "pending").state,
                             st.NodeState.PENDING)
            self.assertEqual(store.get_node("run1", "downstream").state,
                             st.NodeState.PENDING)
            # And they are runnable again: the frontier is PENDING nodes whose
            # deps are all MERGED (§7.1), which is what a resume has to restore.
            self.assertEqual(store.ready_nodes("run1"),
                             ("pending", "downstream"))

    def test_reopened_nodes_carry_no_cause_and_are_charged_nothing(self):
        """`cancel_run` closes the attempt row without classifying it, so a
        node stopped mid-attempt returns to the frontier with the budget it
        had. Nothing about it was adjudicated."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.cancel_run("run1")
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertEqual(store.get_node("run1", "a").state,
                             st.NodeState.PENDING)
            self.assertIsNone(store.conn.execute(
                "SELECT cancel_cause FROM node_lifecycle"
                " WHERE run_id=? AND node_id=?", ("run1", "a")).fetchone()[0])
            for retry_class in st.RetryClass:
                self.assertEqual(
                    store.attempts_spent("run1", "a", retry_class), 0)

    def test_a_resume_leaves_a_separately_abandoned_node_abandoned(self):
        """A run holding both causes at once. Reopening every `CANCELLED` node
        would resurrect the lane the operator gave up on, which is why the
        cause is stored per node rather than per run."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("given-up", 0),
                                           make_node("stopped", 0)])
            store.start_attempt("run1", "given-up", base_sha="s")
            store.mark_blocked("run1", "given-up", st.BlockReason.CREDENTIAL_REFUSED)
            store.declare_outcome("run1")
            store.abandon("run1", "given-up")
            store.cancel_run("run1")
            store.declare_outcome("run1")
            self.assertEqual(store.run_cancel_cause("run1"),
                             st.CancelCause.RUN_CANCEL)

            store.resume_run("run1")

            self.assertEqual(store.get_node("run1", "given-up").state,
                             st.NodeState.CANCELLED)
            self.assertEqual(store.get_node("run1", "stopped").state,
                             st.NodeState.PENDING)

    def test_a_resume_withdraws_the_stop_request_so_the_run_can_declare_again(self):
        """`cancel_requested` is the `CANCELLED` arm's input (§7.3). A resumed
        run that left it set would declare `CANCELLED` again at the quiescence
        it reaches after doing all of its remaining work."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            store.declare_outcome("run1")

            store.resume_run("run1")

            self.assertEqual(
                store.conn.execute("SELECT cancel_requested FROM runs WHERE run_id=?",
                                   ("run1",)).fetchone()[0], 0)
            merge(store, "run1", "a", "sha_a")
            self.assertEqual(
                store.declare_outcome("run1", acceptance_result=True).outcome,
                st.RunOutcome.ACCEPTED)

    def test_a_second_resume_after_a_crash_is_still_legal(self):
        """The resume supersedes the outcome but does not declare a new one,
        so a scheduler that dies before declaring leaves `latest_outcome` at
        `CANCELLED`. The cause must still be readable there or crash recovery
        after a resumed cancel is unreachable."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            store.declare_outcome("run1")

            store.resume_run("run1")
            store.start_attempt("run1", "a", base_sha="s1")  # scheduler dies here
            reclaimed = store.resume_run("run1")

            self.assertEqual(reclaimed, ("a",))
            self.assertEqual(store.get_node("run1", "a").state,
                             st.NodeState.PENDING)

    def test_the_resume_refreshes_last_transition_at_before_reopening(self):
        """§11.2: the silence the backstop measures starts at the resume, not
        at the dead run's last act — and the reopen writes happen after it."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            store.declare_outcome("run1")

            store.resume_run("run1")

            rows = store.conn.execute(
                "SELECT node_id, reason FROM transitions WHERE run_id=? ORDER BY id",
                ("run1",)).fetchall()
            resume_idx = next(i for i, r in enumerate(rows) if r[1] == "resume")
            reopen_idx = next(i for i, r in enumerate(rows)
                              if r[1] == "resume:run-cancel")
            self.assertLess(resume_idx, reopen_idx)
            # And the run row's timer was moved by that same transaction.
            self.assertEqual(
                store.conn.execute(
                    "SELECT last_transition_at >= created_at FROM runs WHERE run_id=?",
                    ("run1",)).fetchone()[0], 1)


class ResumeStillRefusesTests(unittest.TestCase):

    def test_an_accepted_run_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            merge(store, "run1", "a", "sha_a")
            store.declare_outcome("run1", acceptance_result=True)
            self.assertEqual(store.latest_outcome("run1"), st.RunOutcome.ACCEPTED)

            with self.assertRaises(lc.ResumeRefused):
                store.resume_run("run1")

    def test_a_run_abandoned_node_by_node_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0), make_node("b", 0)])
            store.start_attempt("run1", "a", base_sha="s")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            store.declare_outcome("run1")
            store.abandon("run1", "a")
            store.abandon("run1", "b")
            store.declare_outcome("run1")
            self.assertEqual(store.latest_outcome("run1"), st.RunOutcome.CANCELLED)

            with self.assertRaises(lc.ResumeRefused):
                store.resume_run("run1")

            self.assertEqual(store.get_node("run1", "a").state,
                             st.NodeState.CANCELLED)
            self.assertEqual(store.get_node("run1", "b").state,
                             st.NodeState.CANCELLED)

    def test_a_cancelled_run_with_no_recorded_cause_is_refused(self):
        """A ledger written before the column. The migration invents no facts,
        and guessing that an unrecorded cancellation was merely a pause is the
        guess that reopens an adjudicated run."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            store.declare_outcome("run1")
            store.conn.execute(
                "UPDATE runs SET cancel_cause=NULL WHERE run_id=?", ("run1",))

            with self.assertRaises(lc.ResumeRefused):
                store.resume_run("run1")

    def test_an_abandoned_node_stays_absolutely_terminal_at_the_guard(self):
        """The guard's exception is narrow, and this is the conjunct that
        matters: the cause, not the state."""
        with self.assertRaises(lc.IllegalTransition):
            lc._guard_transition(st.NodeState.CANCELLED, st.NodeState.PENDING,
                                 actor="operator",
                                 cancel_cause=st.CancelCause.ABANDONED)
        with self.assertRaises(lc.IllegalTransition):
            lc._guard_transition(st.NodeState.MERGED, st.NodeState.PENDING,
                                 actor="operator",
                                 cancel_cause=st.CancelCause.RUN_CANCEL)
        # And a run-cancelled node may only go back to PENDING, by the operator.
        with self.assertRaises(lc.IllegalTransition):
            lc._guard_transition(st.NodeState.CANCELLED, st.NodeState.RUNNING,
                                 actor="operator",
                                 cancel_cause=st.CancelCause.RUN_CANCEL)
        with self.assertRaises(lc.IllegalTransition):
            lc._guard_transition(st.NodeState.CANCELLED, st.NodeState.PENDING,
                                 actor="scheduler",
                                 cancel_cause=st.CancelCause.RUN_CANCEL)
        lc._guard_transition(st.NodeState.CANCELLED, st.NodeState.PENDING,
                             actor="operator",
                             cancel_cause=st.CancelCause.RUN_CANCEL)


class InheritedRunningAttemptTests(unittest.TestCase):
    """§7.8's existing behaviour, re-asserted on the path this change opens:
    the resume must not have become a way to adopt live work."""

    def test_an_inherited_running_attempt_is_failed_environmental_and_relaunched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")  # scheduler died here
            store.declare_outcome("run1")  # BLOCKED, nothing can progress

            reclaimed = store.resume_run("run1")

            self.assertEqual(reclaimed, ("a",))
            # Re-launchable, never adopted: back to PENDING with the attempt
            # row closed, and charged ENVIRONMENTAL (§7.8).
            self.assertEqual(store.get_node("run1", "a").state,
                             st.NodeState.PENDING)
            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 1)
            self.assertEqual(
                store.conn.execute(
                    "SELECT state FROM attempts WHERE run_id=? AND node_id=?"
                    " AND attempt_no=1", ("run1", "a")).fetchone()[0],
                lc.CLOSED_ATTEMPT_STATE.value)
            # Its pane is recorded so `run status` can name it (§7.8).
            self.assertEqual(len(store.audit_orphans("run1")), 1)
            # And it is genuinely re-launchable.
            self.assertEqual(store.start_attempt("run1", "a", base_sha="s2"), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
