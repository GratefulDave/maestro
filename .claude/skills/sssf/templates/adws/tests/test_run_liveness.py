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

import dataclasses
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402


HOST = "test-host"


def node_row(node_id: str, state: st.NodeState) -> lc.NodeRow:
    return lc.NodeRow(
        node_id=node_id, kind="agent", depth=0, needs=(), state=state,
        attempt_no=1, block_reason=None, output_sha=None,
        granted_extra_attempts=0, updated_at="2026-08-18T00:00:00+00:00")


def record(*, pid=None, host=HOST, outcome=None, cancel=False,
           start_epoch=None) -> lc.RunRecord:
    return lc.RunRecord(
        run_id="run-1", plan_digest="d" * 64,
        created_at="2026-08-18T00:00:00+00:00",
        last_transition_at="2026-08-18T00:00:00+00:00",
        latest_outcome=outcome, latest_outcome_at=None,
        cancel_requested=cancel, scheduler_pid=pid,
        scheduler_host=host if pid else None,
        scheduler_claimed_at="2026-08-18T00:00:00+00:00" if pid else None,
        scheduler_start_epoch=start_epoch)


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
        declares the run CANCELLED at quiescence. Mixes of MERGED and
        CANCELLED still read that declaration — the leftover-declaration
        rule must not swallow abandon-by-node. Without it this function
        answered QUIESCENT about a run the scheduler called CANCELLED, and
        the dashboard's copy of this rule answered CANCELLED: two answers
        to one question."""
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

    def test_a_resumed_run_whose_nodes_have_all_merged_is_merged_not_cancelled(self):
        """Resume clears `cancel_requested` and leaves `latest_outcome=CANCELLED`
        until the scheduler declares again. Once every reopened node is MERGED
        the live rows contradict that leftover declaration — this is the
        final-acceptance window, and projecting CANCELLED here is §19 M5's
        stale-outcome defect (issue #39's sibling)."""
        nodes = [node_row("a", st.NodeState.MERGED),
                 node_row("b", st.NodeState.MERGED)]
        rec = record(outcome=st.RunOutcome.CANCELLED, cancel=False)
        self.assertFalse(rec.cancel_requested)
        self.assertIs(rec.latest_outcome, st.RunOutcome.CANCELLED)
        self.assertEqual(derive(rec, nodes, alive=False), "MERGED")

    def test_a_mix_of_merged_and_cancelled_still_reads_the_declaration(self):
        """Abandon-by-node: the outcome function declares CANCELLED and sets
        no `cancel_requested`. All-MERGED must not steal that case."""
        nodes = [node_row("a", st.NodeState.MERGED),
                 node_row("b", st.NodeState.CANCELLED)]
        rec = record(outcome=st.RunOutcome.CANCELLED, cancel=False)
        self.assertEqual(derive(rec, nodes, alive=False), "CANCELLED")

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

    def test_an_fqdn_and_its_short_name_are_the_same_host(self):
        """A DHCP suffix change is not a host change. Live ledgers already
        store FQDNs written by older `gethostname()`; they must keep matching
        without a migration."""
        rec = record(pid=41022, host="Mac.attlocal.net")
        self.assertIs(
            lc.scheduler_liveness(rec, is_alive=lambda _pid: False, host="Mac"),
            False)
        self.assertIs(
            lc.scheduler_liveness(rec, is_alive=lambda _pid: False,
                                  host="MAC"),
            False)

    def test_a_different_short_name_is_still_unknown(self):
        rec = record(pid=41022, host="Mac")
        self.assertIsNone(lc.scheduler_liveness(rec, host="OtherBox"))


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
        # And the identity half (#37): the projection records the process's
        # start epoch beside its pid, so the pid is provably signallable
        # rather than merely present in the process table.
        started = wd.process_start_epoch(os.getpid())
        if started is None:
            self.skipTest("no process start epoch on this platform")
        self.assertEqual(rec.scheduler_start_epoch, started)
        self.assertEqual(lc.scheduler_signal_pid(rec), os.getpid())

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
            old_name = store.conn.execute(
                "SELECT plan_name FROM runs WHERE run_id=?",
                ("old",)).fetchone()[0]
        finally:
            store.close()
        for name, _kind in lc._RUNS_ADDED_COLUMNS:
            self.assertIn(name, columns)
        self.assertIn("plan_name", columns)
        # Migration adds the column and invents no name for a pre-column row.
        self.assertIsNone(old_name)
        reader = lc.LifecycleReader.open(self.db)
        try:
            rec = reader.run("old")
        finally:
            reader.close()
        # The migration invents nothing about a run that predates the column.
        self.assertIsNone(rec.scheduler_pid)
        self.assertIsNone(lc.scheduler_liveness(rec))
        # Including the start epoch (#37). A row written before that column
        # still loads, and reads as *unproven* -- never as proven, which is
        # the direction that would hand a signal to whatever now holds the
        # pid. §19 M21: a schema change must not make a pre-existing row
        # unreadable, nor silently reclassify it.
        self.assertIsNone(rec.scheduler_start_epoch)
        self.assertIsNone(lc.scheduler_signal_pid(rec))

    def test_a_run_recorded_before_the_start_epoch_stays_live_but_unproven(self):
        """The deployed shape #37 has to survive: a ledger that already has
        the ownership columns and a live pid, but no start epoch.

        §19 M21 is the failure to avoid -- a schema change that made a
        previously-optional thing required turned every pre-existing signed
        receipt unparseable, a 110-cell PASS included. So this row must still
        load, and must still read RUNNING with a live scheduler. What it must
        *not* do is read as proven: nothing recorded who that pid was, and
        an unproven pid is not authority to signal.
        """
        store = lc.LifecycleStore(self.db)
        try:
            store.create_run("run1", "d" * 64, [self._node()])
            # Rewind the row to its pre-F9 content: the column exists (the
            # migration added it) but this run was written before anything
            # populated it.
            store.conn.execute(
                "UPDATE runs SET scheduler_start_epoch=NULL WHERE run_id=?",
                ("run1",))
            store.conn.commit()
        finally:
            store.close()
        reader = lc.LifecycleReader.open(self.db)
        try:
            rec = reader.run("run1")
            nodes = reader.nodes("run1")
        finally:
            reader.close()
        self.assertEqual(rec.scheduler_pid, os.getpid())
        self.assertIsNone(rec.scheduler_start_epoch)
        # Still readable, still live, and the derivation is untouched by the
        # missing column: the same answer as the identical row that has one.
        self.assertIs(lc.scheduler_liveness(rec), True)
        with_epoch = dataclasses.replace(rec, scheduler_start_epoch=100.5)
        self.assertEqual(lc.derive_run_state(rec, nodes),
                         lc.derive_run_state(with_epoch, nodes))
        self.assertEqual(lc.derive_run_state(rec, nodes), "PENDING")
        # But not signallable.
        self.assertIsNone(lc.scheduler_signal_pid(rec))

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


def _write_pre_migration_ledger(db: Path) -> None:
    """A ledger that predates `_ADDED_COLUMNS` on both tables the migration
    covers. `SCHEMA` is `CREATE TABLE IF NOT EXISTS`, so opening this path
    is what actually runs the ALTER."""
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY,"
            " plan_digest TEXT NOT NULL, created_at TEXT NOT NULL,"
            " last_transition_at TEXT NOT NULL, latest_outcome TEXT,"
            " latest_outcome_at TEXT,"
            " cancel_requested INTEGER NOT NULL DEFAULT 0)")
        conn.execute(
            "CREATE TABLE node_lifecycle ("
            " run_id TEXT NOT NULL, node_id TEXT NOT NULL,"
            " state TEXT NOT NULL, attempt_no INTEGER NOT NULL DEFAULT 0,"
            " block_reason TEXT, output_sha TEXT,"
            " granted_extra_attempts INTEGER NOT NULL DEFAULT 0,"
            " updated_at TEXT NOT NULL,"
            " PRIMARY KEY (run_id, node_id))")
        conn.execute(
            "INSERT INTO runs VALUES ('old','d','t','t',NULL,NULL,0)")


class ConcurrentCancelCauseMigration(unittest.TestCase):
    """Two processes first-opening one pre-migration ledger must both
    succeed. The check-then-ALTER used to race: both saw the column
    missing, the second died with `duplicate column name`."""

    def test_two_openers_on_a_pre_migration_ledger_both_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.sqlite3"
            _write_pre_migration_ledger(db)
            opener_count = 8
            start = threading.Barrier(opener_count)
            errors: list[str] = []
            stores: list[lc.LifecycleStore] = []
            lock = threading.Lock()

            def open_it() -> None:
                start.wait()
                try:
                    store = lc.LifecycleStore(db)
                except BaseException as error:  # noqa: BLE001
                    errors.append(f"{type(error).__name__}: {error}")
                    return
                with lock:
                    stores.append(store)

            threads = [threading.Thread(target=open_it)
                       for _ in range(opener_count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(
                errors, [],
                "opening a pre-migration ledger must not fail on contention")
            self.assertEqual(len(stores), opener_count)
            try:
                run_columns = lc._table_columns(stores[0].conn, "runs")
                node_columns = lc._table_columns(
                    stores[0].conn, "node_lifecycle")
            finally:
                for store in stores:
                    store.close()
            for name, _kind in lc._RUNS_ADDED_COLUMNS:
                self.assertEqual(run_columns.count(name), 1, name)
            for name, _kind in lc._NODE_LIFECYCLE_ADDED_COLUMNS:
                self.assertEqual(node_columns.count(name), 1, name)


class AResumedAcceptanceWindowIsMerged(unittest.TestCase):
    """Drive resume itself, then derive from the rows it left behind."""

    @staticmethod
    def _node(node_id: str) -> st.PlanNode:
        return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=0,
                           needs=(), command=("true",))

    @staticmethod
    def _merge(store: lc.LifecycleStore, run_id: str, node_id: str,
               sha: str) -> None:
        store.start_attempt(run_id, node_id, base_sha="base")
        store.mark_verified(run_id, node_id, output_sha=sha)
        store.mark_merged(run_id, node_id)

    def test_resume_then_every_node_merged_reads_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.sqlite3"
            store = lc.LifecycleStore(db)
            try:
                store.create_run("run1", "d" * 64,
                                 [self._node("a"), self._node("b")])
                store.cancel_run("run1")
                store.declare_outcome("run1")
                store.resume_run("run1")
                self._merge(store, "run1", "a", "sha_a")
                self._merge(store, "run1", "b", "sha_b")
            finally:
                store.close()
            reader = lc.LifecycleReader.open(db)
            try:
                rec = reader.run("run1")
                nodes = reader.nodes("run1")
            finally:
                reader.close()
            self.assertFalse(rec.cancel_requested)
            self.assertIs(rec.latest_outcome, st.RunOutcome.CANCELLED)
            self.assertEqual(
                {node.node_id: node.state for node in nodes},
                {"a": st.NodeState.MERGED, "b": st.NodeState.MERGED})
            self.assertEqual(lc.derive_run_state(rec, nodes), "MERGED")

    def test_abandon_by_node_still_reads_cancelled_after_a_merge(self):
        """A run holding both causes: resume reopens RUN_CANCEL nodes and
        leaves the abandoned one CANCELLED. After the reopened work merges,
        the leftover declaration must still win — that mix is not the
        all-MERGED final-acceptance window."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.sqlite3"
            store = lc.LifecycleStore(db)
            try:
                store.create_run(
                    "run1", "d" * 64,
                    [self._node("merged"), self._node("stopped"),
                     self._node("given-up")])
                self._merge(store, "run1", "merged", "sha_merged")
                store.start_attempt("run1", "given-up", base_sha="base")
                store.mark_blocked(
                    "run1", "given-up", st.BlockReason.CREDENTIAL_REFUSED)
                store.declare_outcome("run1")
                store.abandon("run1", "given-up")
                store.cancel_run("run1")
                store.declare_outcome("run1")
                store.resume_run("run1")
                self._merge(store, "run1", "stopped", "sha_stopped")
            finally:
                store.close()
            reader = lc.LifecycleReader.open(db)
            try:
                rec = reader.run("run1")
                nodes = reader.nodes("run1")
            finally:
                reader.close()
            self.assertFalse(rec.cancel_requested)
            self.assertIs(rec.latest_outcome, st.RunOutcome.CANCELLED)
            self.assertEqual(
                {node.node_id: node.state for node in nodes},
                {"merged": st.NodeState.MERGED,
                 "stopped": st.NodeState.MERGED,
                 "given-up": st.NodeState.CANCELLED})
            self.assertEqual(lc.derive_run_state(rec, nodes), "CANCELLED")


class SignalPidIdentity(unittest.TestCase):
    """#37 -- a pid alone is not proof of process identity, so the start
    epoch that identifies a process has to be finer than the second a
    pid can be reused within."""

    def test_matching_recorded_start_is_proven(self):
        """The one case that yields a pid: liveness holds *and* the live
        process started at the instant the ledger recorded."""
        rec = record(pid=42, start_epoch=100.5)
        self.assertEqual(
            lc.scheduler_signal_pid(
                rec, is_alive=lambda _pid: True,
                start_epoch=lambda _pid: 100.5, host=HOST),
            42)

    def test_same_second_reuse_is_unproven(self):
        """The case `scheduler_liveness` cannot see. Pid 42 exists, so
        liveness says True, but the process behind it started 767ms after
        the one that claimed the run: a different process wearing a reused
        number. Signalling it would SIGINT a stranger."""
        rec = record(pid=42, start_epoch=100.123)
        self.assertIs(
            lc.scheduler_liveness(rec, is_alive=lambda _pid: True, host=HOST),
            True)
        self.assertIsNone(
            lc.scheduler_signal_pid(
                rec, is_alive=lambda _pid: True,
                start_epoch=lambda _pid: 100.890, host=HOST))
        # And the resolution is what convicts it: truncated to the whole
        # second either side records 100, so a `started <= claimed_at`
        # comparison at second resolution would have called this proven.
        self.assertEqual(int(100.123), int(100.890))

    def test_missing_recorded_start_is_unproven(self):
        """A run whose ledger predates the column records no start, and an
        absent identity is never a proven one."""
        rec = record(pid=42, start_epoch=None)
        self.assertIsNone(
            lc.scheduler_signal_pid(
                rec, is_alive=lambda _pid: True,
                start_epoch=lambda _pid: 100.5, host=HOST))

    def test_a_platform_that_cannot_answer_is_unproven(self):
        """`process_start_epoch` returns None where it cannot read the
        process table. Unknown is not identity."""
        rec = record(pid=42, start_epoch=100.5)
        self.assertIsNone(
            lc.scheduler_signal_pid(
                rec, is_alive=lambda _pid: True,
                start_epoch=lambda _pid: None, host=HOST))

    def test_a_dead_or_unknown_scheduler_is_never_signalled(self):
        """Liveness is a precondition, not the proof -- but a failed
        precondition still short-circuits before any identity question."""
        dead = record(pid=42, start_epoch=100.5)
        self.assertIsNone(
            lc.scheduler_signal_pid(
                dead, is_alive=lambda _pid: False,
                start_epoch=lambda _pid: 100.5, host=HOST))
        elsewhere = record(pid=42, host="other-box", start_epoch=100.5)
        self.assertIsNone(
            lc.scheduler_signal_pid(
                elsewhere, is_alive=lambda _pid: True,
                start_epoch=lambda _pid: 100.5, host=HOST))

    def test_this_processs_own_claim_is_proven_by_the_real_probe(self):
        """End to end against the platform rather than a lambda: the epoch
        the ledger would record for this process is the epoch the probe
        reads back for it."""
        started = wd.process_start_epoch(os.getpid())
        if started is None:
            self.skipTest("no process start epoch on this platform")
        rec = record(pid=os.getpid(), start_epoch=started)
        self.assertEqual(lc.scheduler_signal_pid(rec, host=HOST), os.getpid())

    @unittest.skipUnless(
        sys.platform == "darwin" or sys.platform.startswith("linux"),
        "a process start epoch is only derivable on Darwin and Linux")
    def test_darwin_start_is_not_whole_second_only(self):
        started = wd.process_start_epoch(os.getpid())
        self.assertIsNotNone(started)
        self.assertNotEqual(started, float(int(started)))


class HasThisRunStopped(unittest.TestCase):
    """`run_in_flight` — the yes/no/cannot-say `derive_run_state` withholds.

    Nine derived strings do not partition into stopped and going: BLOCKED and
    PENDING both mean "work is left and nothing is executing it this instant",
    which is the shape of a run whose scheduler declared and exited *and* of a
    live one between two polls. `run convergence` partitioned them by hand, put
    both on the "ended" side, and told an operator watching a live run that it
    had already ended (#107).
    """

    def flight(self, rec, nodes, *, alive=True, host=HOST):
        return lc.run_in_flight(rec, nodes, is_alive=lambda _pid: alive,
                                host=host)

    def test_a_node_running_needs_no_probe_to_be_going(self):
        rec = record(pid=42, outcome=None)
        nodes = [node_row("a", st.NodeState.RUNNING)]
        self.assertEqual(derive(rec, nodes), "RUNNING")
        self.assertIs(self.flight(rec, nodes), True)

    def test_every_node_merged_is_stopped(self):
        rec = record(pid=42, outcome=st.RunOutcome.ACCEPTED)
        nodes = [node_row("a", st.NodeState.MERGED)]
        self.assertEqual(derive(rec, nodes), "MERGED")
        self.assertIs(self.flight(rec, nodes), False)

    def test_a_dead_scheduler_over_a_running_node_is_stopped(self):
        """ABANDONED is a stop, and `derive_run_state` established it from the
        process table before this function was asked anything."""
        rec = record(pid=42, outcome=None)
        nodes = [node_row("a", st.NodeState.RUNNING)]
        self.assertEqual(derive(rec, nodes, alive=False), "ABANDONED")
        self.assertIs(self.flight(rec, nodes, alive=False), False)

    def test_pending_is_decided_by_the_pid_and_not_by_the_node_rows(self):
        """The pair that made the false verdict possible. Identical node rows,
        identical derived state, opposite answers."""
        rec = record(pid=42, outcome=st.RunOutcome.BLOCKED)
        nodes = [node_row("a", st.NodeState.PENDING),
                 node_row("b", st.NodeState.MERGED)]
        self.assertEqual(derive(rec, nodes, alive=True), "PENDING")
        self.assertEqual(derive(rec, nodes, alive=False), "PENDING")
        self.assertIs(self.flight(rec, nodes, alive=True), True)
        self.assertIs(self.flight(rec, nodes, alive=False), False)

    def test_blocked_is_decided_the_same_way(self):
        rec = record(pid=42, outcome=st.RunOutcome.BLOCKED)
        nodes = [node_row("a", st.NodeState.BLOCKED),
                 node_row("b", st.NodeState.PENDING)]
        self.assertEqual(derive(rec, nodes, alive=True), "BLOCKED")
        self.assertIs(self.flight(rec, nodes, alive=True), True)
        self.assertIs(self.flight(rec, nodes, alive=False), False)

    def test_an_unreadable_process_table_answers_neither(self):
        """No pid, or a pid on another host: `None`, and a caller must render
        that as unknown rather than pick the likelier claim."""
        no_pid = record(pid=None, outcome=st.RunOutcome.BLOCKED)
        nodes = [node_row("a", st.NodeState.PENDING)]
        self.assertIsNone(self.flight(no_pid, nodes))
        elsewhere = record(pid=42, host="other-box",
                           outcome=st.RunOutcome.BLOCKED)
        self.assertIsNone(self.flight(elsewhere, nodes, host=HOST))

    def test_a_cancellation_still_reaching_nodes_is_going(self):
        rec = record(pid=42, cancel=True)
        nodes = [node_row("a", st.NodeState.CANCELLED),
                 node_row("b", st.NodeState.PENDING)]
        self.assertEqual(derive(rec, nodes), "CANCELLING")
        self.assertIs(self.flight(rec, nodes), True)

    def test_a_finished_cancellation_is_stopped(self):
        rec = record(pid=42, cancel=True)
        nodes = [node_row("a", st.NodeState.CANCELLED)]
        self.assertEqual(derive(rec, nodes), "CANCELLED")
        self.assertIs(self.flight(rec, nodes), False)

    def test_every_derived_state_is_classified(self):
        """The partition is total. A tenth string added to `derive_run_state`
        without a home here would fall through to the liveness probe and be
        reported as a live run, which is the direction that strands nothing but
        does mislead — so the two sets are asserted against the function's own
        vocabulary rather than left implicit."""
        derived = {"EMPTY", "EMPTY", "MERGED", "CANCELLED", "QUIESCENT",
                   "ABANDONED", "CANCELLING", "RUNNING", "BLOCKED", "PENDING"}
        classified = set(lc.STOPPED_RUN_STATES) | set(lc.LIVE_RUN_STATES)
        self.assertEqual(derived - classified, {"BLOCKED", "PENDING"})
        self.assertEqual(classified - derived, set())

    def test_no_node_at_all_is_stopped(self):
        rec = record(pid=42)
        self.assertEqual(derive(rec, []), "EMPTY")
        self.assertIs(self.flight(rec, []), False)


if __name__ == "__main__":
    unittest.main()
