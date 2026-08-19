"""D4 — a run's state is derived from durable facts, not remembered by a process.

`runs.latest_outcome` is written by exactly one actor: a live scheduler
declaring quiescence (`LifecycleStore.declare_outcome`, called only from
`Scheduler._declare`). So a scheduler that never gets to declare — crash,
SIGKILL, machine sleep, an operator closing the pane — leaves the column NULL
forever, and nothing in the ledger could contradict a reader that went on
calling the run live. Two shapes were observed on 2026-08-18:

* `run-1907d9c1f9d84def80272cb39b5fc137` — `run cancel` wrote CANCELLED for all
  14 nodes and set `cancel_requested`; the scheduler exited without declaring.
  `_live_state` returned "CANCELLING" from `cancel_requested` alone, before it
  looked at a single node, so the run read CANCELLING permanently. Deleting the
  row was the only way to clear it.
* `run-75dfc6914946487f998453fefb51a0cf` — the scheduler process died with two
  nodes RUNNING, `pid` NULL and `launched_at` NULL. `run list` reported RUNNING
  half an hour later and the visualizer drew live panes for it.

The repair is a durable owner (`runs.scheduler_pid` / `scheduler_host`, written
on projection and on resume) plus one derivation over node rows and process
liveness. The liveness probe is `os.kill(pid, 0)` via `watchdog.process_is_alive`
— a structural question about the process table, never stdout, a log line, or a
pane (§1.2).

Run with:
    python -m pytest tests/test_run_liveness.py -o addopts= -q
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


HOST = "test-host"


def node_row(node_id: str, state: st.NodeState) -> lc.NodeRow:
    return lc.NodeRow(
        node_id=node_id, kind="agent", depth=0, needs=(), state=state,
        attempt_no=1, block_reason=None, output_sha=None,
        granted_extra_attempts=0, updated_at="2026-08-18T00:00:00+00:00")


def record(*, pid=None, host=HOST, outcome=None, cancel=False) -> lc.RunRecord:
    return lc.RunRecord(
        run_id="run-1", plan_digest="d" * 64,
        created_at="2026-08-18T00:00:00+00:00",
        last_transition_at="2026-08-18T00:00:00+00:00",
        latest_outcome=outcome, latest_outcome_at=None,
        cancel_requested=cancel, scheduler_pid=pid,
        scheduler_host=host if pid else None,
        scheduler_claimed_at="2026-08-18T00:00:00+00:00" if pid else None)


def derive(rec, nodes, *, alive=True, host=HOST) -> str:
    return lc.derive_run_state(rec, nodes, is_alive=lambda _pid: alive,
                               host=host)


class ACancelThatFinishedIsCancelled(unittest.TestCase):
    """The first observed shape. No liveness question is involved: the node
    rows already say the cancellation completed."""

    def test_every_node_cancelled_reads_cancelled_not_cancelling(self):
        nodes = [node_row("a", st.NodeState.CANCELLED),
                 node_row("b", st.NodeState.CANCELLED)]
        self.assertEqual(
            derive(record(pid=os.getpid(), cancel=True), nodes, alive=False),
            "CANCELLED")

    def test_a_run_abandoned_node_by_node_reads_cancelled_too(self):
        """`abandon` sets no `cancel_requested`; §7.3's outcome function still
        declares the run CANCELLED at quiescence. Reading that declaration is
        safe in exactly this branch, because a settled run cannot have moved
        past it — a resume returns nodes to PENDING and un-settles it. Without
        it this function answered QUIESCENT about a run the scheduler called
        CANCELLED, and the dashboard's copy of this rule answered CANCELLED:
        two answers to one question."""
        nodes = [node_row("a", st.NodeState.CANCELLED),
                 node_row("b", st.NodeState.CANCELLED)]
        self.assertEqual(
            derive(record(outcome=st.RunOutcome.CANCELLED), nodes,
                   alive=False),
            "CANCELLED")

    def test_a_settled_run_that_declared_something_else_is_unaffected(self):
        """The control. Only the CANCELLED declaration is read here, and only
        to answer the question `cancel_requested` was answering badly."""
        nodes = [node_row("a", st.NodeState.MERGED),
                 node_row("b", st.NodeState.MERGED)]
        self.assertEqual(
            derive(record(outcome=st.RunOutcome.ACCEPTED), nodes, alive=False),
            "MERGED")

    def test_a_resumed_run_is_not_settled_and_ignores_its_declaration(self):
        """The reason the read is confined to the settled branch: a resumed
        run's declaration describes a life it has moved past."""
        nodes = [node_row("a", st.NodeState.MERGED),
                 node_row("b", st.NodeState.PENDING)]
        self.assertEqual(
            derive(record(pid=os.getpid(), outcome=st.RunOutcome.CANCELLED),
                   nodes, alive=True),
            "PENDING")

    def test_it_is_cancelled_even_while_the_scheduler_is_still_alive(self):
        """`cancel_run` writes every node in one transaction, so a live
        scheduler shutting down is looking at a finished cancellation too."""
        nodes = [node_row("a", st.NodeState.CANCELLED)]
        self.assertEqual(
            derive(record(pid=os.getpid(), cancel=True), nodes, alive=True),
            "CANCELLED")

    def test_a_cancellation_still_in_progress_still_reads_cancelling(self):
        """The old answer is still the right one while work remains."""
        nodes = [node_row("a", st.NodeState.CANCELLED),
                 node_row("b", st.NodeState.RUNNING)]
        self.assertEqual(
            derive(record(pid=os.getpid(), cancel=True), nodes, alive=True),
            "CANCELLING")

    def test_a_declared_outcome_is_not_required_to_read_cancelled(self):
        """The whole point: no scheduler ever declared, and the run is still
        reported terminally, because the node rows are enough."""
        rec = record(pid=os.getpid(), cancel=True, outcome=None)
        self.assertIsNone(rec.latest_outcome)
        self.assertEqual(
            derive(rec, [node_row("a", st.NodeState.CANCELLED)], alive=False),
            "CANCELLED")


class ADeadSchedulerIsNotALiveRun(unittest.TestCase):
    """The second observed shape."""

    def test_running_nodes_with_a_dead_scheduler_read_abandoned(self):
        nodes = [node_row("a", st.NodeState.RUNNING),
                 node_row("b", st.NodeState.RUNNING)]
        self.assertEqual(derive(record(pid=424242), nodes, alive=False),
                         "ABANDONED")

    def test_a_null_attempt_pid_does_not_prevent_the_verdict(self):
        """run-75dfc's attempts had `pid` NULL and `launched_at` NULL, so an
        attempt-level liveness check had nothing to read. The run-level owner
        is what makes the case decidable at all."""
        nodes = [node_row("a", st.NodeState.RUNNING)]
        self.assertEqual(derive(record(pid=424242), nodes, alive=False),
                         "ABANDONED")

    def test_pending_work_with_a_dead_undeclared_scheduler_is_abandoned(self):
        """Nothing is running, but nothing will ever start either."""
        nodes = [node_row("a", st.NodeState.PENDING)]
        self.assertEqual(derive(record(pid=424242), nodes, alive=False),
                         "ABANDONED")

    def test_a_declared_run_whose_scheduler_exited_keeps_its_shape(self):
        """A scheduler that declares BLOCKED and exits is *supposed* to be
        gone. Reporting that run as ABANDONED would relabel every normal
        ending as a crash."""
        nodes = [node_row("a", st.NodeState.BLOCKED)]
        rec = record(pid=424242, outcome=st.RunOutcome.BLOCKED)
        self.assertEqual(derive(rec, nodes, alive=False), "BLOCKED")

    def test_a_declared_stuck_run_with_work_in_flight_stops_reading_running(self):
        """§11.2 declares STUCK with work still in flight. Once that scheduler
        is gone nothing owns those attempts, so the operator-visible answer
        must not be RUNNING."""
        nodes = [node_row("a", st.NodeState.RUNNING)]
        rec = record(pid=424242, outcome=st.RunOutcome.STUCK)
        self.assertEqual(derive(rec, nodes, alive=False), "ABANDONED")


class ALiveRunIsNeverCalledDead(unittest.TestCase):
    """The false positive that would matter most: mislabelling a working run
    strands real work and invites an operator to kill it."""

    def test_a_live_scheduler_with_running_nodes_reads_running(self):
        nodes = [node_row("a", st.NodeState.RUNNING),
                 node_row("b", st.NodeState.PENDING)]
        self.assertEqual(derive(record(pid=os.getpid()), nodes, alive=True),
                         "RUNNING")

    def test_this_processs_own_pid_is_seen_as_alive_by_the_real_probe(self):
        """Not the injected stub — the production probe, against a pid that is
        definitely running: this one."""
        rec = record(pid=os.getpid())
        self.assertIs(lc.scheduler_liveness(rec, host=HOST), True)
        self.assertEqual(
            lc.derive_run_state(rec, [node_row("a", st.NodeState.RUNNING)],
                                host=HOST),
            "RUNNING")

    def test_an_unrecorded_pid_is_unknown_and_never_dead(self):
        """A ledger written before the column existed. `None` must not decay
        into `False`, or every historical run reads ABANDONED."""
        rec = record(pid=None)
        self.assertIsNone(lc.scheduler_liveness(rec))
        self.assertEqual(
            derive(rec, [node_row("a", st.NodeState.RUNNING)], alive=False),
            "RUNNING")

    def test_a_pid_from_another_host_is_declined_rather_than_answered(self):
        """Some other machine's pid 41022 is very likely alive here, and just
        as likely to be an unrelated process. The question is refused."""
        rec = record(pid=41022, host="other-machine")
        self.assertIsNone(lc.scheduler_liveness(rec, host=HOST))
        self.assertEqual(
            derive(rec, [node_row("a", st.NodeState.RUNNING)], alive=False,
                   host=HOST),
            "RUNNING")

    def test_pid_reuse_errs_towards_reporting_the_run_as_it_is(self):
        """A recycled pid reads alive. Wrong, and wrong in the safe direction:
        the run is reported exactly as it was before this change."""
        self.assertIs(
            lc.scheduler_liveness(record(pid=424242),
                                  is_alive=lambda _pid: True, host=HOST),
            True)


class TheDerivationIsOtherwiseUnchanged(unittest.TestCase):
    """Every answer the old `_live_state` gave, still given."""

    def test_the_untouched_vocabulary(self):
        cases = [
            ([], "EMPTY"),
            ([st.NodeState.MERGED, st.NodeState.MERGED], "MERGED"),
            ([st.NodeState.RUNNING, st.NodeState.PENDING], "RUNNING"),
            ([st.NodeState.BLOCKED, st.NodeState.PENDING], "BLOCKED"),
            ([st.NodeState.PENDING], "PENDING"),
            ([st.NodeState.VERIFIED], "QUIESCENT"),
            ([st.NodeState.MERGED, st.NodeState.CANCELLED], "QUIESCENT"),
        ]
        for states, expected in cases:
            with self.subTest(states=[s.value for s in states]):
                nodes = [node_row(str(i), state)
                         for i, state in enumerate(states)]
                self.assertEqual(
                    derive(record(pid=os.getpid()), nodes, alive=True),
                    expected)

    def test_every_answer_is_in_the_declared_vocabulary(self):
        for states in ([], [st.NodeState.RUNNING], [st.NodeState.CANCELLED],
                       [st.NodeState.MERGED], [st.NodeState.BLOCKED],
                       [st.NodeState.PENDING], [st.NodeState.VERIFIED]):
            for cancel in (True, False):
                for alive in (True, False):
                    nodes = [node_row("a", s) for s in states]
                    self.assertIn(
                        derive(record(pid=1, cancel=cancel), nodes,
                               alive=alive),
                        lc.RUN_STATES)


class TheOwnerIsWrittenToTheLedger(unittest.TestCase):
    """The durable half: without a recorded owner none of the above is
    decidable, which is exactly why run-75dfc was undecidable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "lifecycle.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _node(node_id="a"):
        return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=0,
                           needs=(), command=("true",))

    def test_projection_records_this_process_as_the_runs_scheduler(self):
        store = lc.LifecycleStore(self.db)
        try:
            store.create_run("run1", "d" * 64, [self._node()])
        finally:
            store.close()
        reader = lc.LifecycleReader.open(self.db)
        try:
            rec = reader.run("run1")
        finally:
            reader.close()
        self.assertEqual(rec.scheduler_pid, os.getpid())
        self.assertEqual(rec.scheduler_host, lc.scheduler_host())
        self.assertIsNotNone(rec.scheduler_claimed_at)
        self.assertIs(lc.scheduler_liveness(rec), True)

    def test_the_scheduler_claims_the_run_when_it_projects_it(self):
        """The production writer. `create_run` stamps the owner on the row it
        inserts, but `Scheduler.project` swallows `RunAlreadyExists` — a second
        process adopting an existing projection would otherwise leave the run
        attributed to the process that is gone."""
        import ast
        from adw_modules import scheduler as sch
        tree = ast.parse(Path(sch.__file__).read_text(encoding="utf-8"))
        project = next(
            item for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "Scheduler"
            for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name == "project")
        calls = {node.func.attr for node in ast.walk(project)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)}
        self.assertIn("claim_run", calls)
        self.assertIn("create_run", calls)

    def test_resume_takes_ownership_from_the_process_that_died(self):
        store = lc.LifecycleStore(self.db)
        try:
            store.create_run("run1", "d" * 64, [self._node()])
            store.claim_run("run1")
            with sqlite3.connect(str(self.db)) as conn:
                conn.execute(
                    "UPDATE runs SET scheduler_pid=424242 WHERE run_id=?",
                    ("run1",))
            store.resume_run("run1")
        finally:
            store.close()
        reader = lc.LifecycleReader.open(self.db)
        try:
            rec = reader.run("run1")
        finally:
            reader.close()
        self.assertEqual(rec.scheduler_pid, os.getpid())

    def test_a_ledger_written_before_the_columns_is_migrated_not_refused(self):
        """`SCHEMA` is `CREATE TABLE IF NOT EXISTS`, so it is inert against an
        existing `runs`. Without the `ALTER`, every deployed ledger would keep
        the old shape and the whole derivation would stay undecidable there."""
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY,"
                " plan_digest TEXT NOT NULL, created_at TEXT NOT NULL,"
                " last_transition_at TEXT NOT NULL, latest_outcome TEXT,"
                " latest_outcome_at TEXT,"
                " cancel_requested INTEGER NOT NULL DEFAULT 0)")
            conn.execute(
                "INSERT INTO runs VALUES ('old','d','t','t',NULL,NULL,0)")
        store = lc.LifecycleStore(self.db)
        try:
            columns = lc._table_columns(store.conn, "runs")
        finally:
            store.close()
        for name, _kind in lc._RUNS_ADDED_COLUMNS:
            self.assertIn(name, columns)
        reader = lc.LifecycleReader.open(self.db)
        try:
            rec = reader.run("old")
        finally:
            reader.close()
        # The migration invents nothing about a run that predates the column.
        self.assertIsNone(rec.scheduler_pid)
        self.assertIsNone(lc.scheduler_liveness(rec))

    def test_a_read_only_reader_tolerates_a_ledger_it_cannot_migrate(self):
        """`LifecycleReader` opens `mode=ro` on purpose, so it must read an
        un-migrated ledger rather than refuse it or try to write."""
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute(
                "CREATE TABLE runs (run_id TEXT PRIMARY KEY,"
                " plan_digest TEXT NOT NULL, created_at TEXT NOT NULL,"
                " last_transition_at TEXT NOT NULL, latest_outcome TEXT,"
                " latest_outcome_at TEXT,"
                " cancel_requested INTEGER NOT NULL DEFAULT 0)")
            conn.execute(
                "INSERT INTO runs VALUES ('old','d','t','t',NULL,NULL,1)")
        reader = lc.LifecycleReader.open(self.db)
        try:
            rec = reader.run("old")
        finally:
            reader.close()
        self.assertTrue(rec.cancel_requested)
        self.assertIsNone(rec.scheduler_pid)


if __name__ == "__main__":
    unittest.main()
