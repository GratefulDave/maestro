"""Attempt pid liveness is declinable: unknown is never dead and never alive.

`attempts` used to record a pid with no host and no start epoch, so neither
reader could prove the pid was that attempt's own process. A foreign pid
absent here convicted a live attempt (watchdog). A reused pid present here
blocked salvage forever. `attempt_liveness` is the one three-answer
predicate both readers consume.

Run with:
    PYTHONPATH=. python -m pytest tests/test_attempt_liveness.py -o addopts= -q
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


ADWS = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import salvage  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402


HOST = "test-host"
EPOCH = 100.5
PID = 4242
DEAD_SCHEDULER_PID = 2_000_000_000


def attempt(*, pid=PID, host=HOST, epoch=EPOCH, launched=True) -> st.AttemptRecord:
    return st.AttemptRecord(
        run_id="run-1", node_id="node-1", attempt_no=1, base_sha="b" * 40,
        state=st.NodeState.RUNNING, started_at=0.0,
        launched_at=1.0 if launched else None, pid=pid,
        attempt_host=host, attempt_start_epoch=epoch)


def make_config() -> st.SchedulerConfig:
    return st.SchedulerConfig(
        concurrency=2, node_timeout_s=1000.0, turn_timeout_s=1000.0,
        final_acceptance_timeout_s=50.0, backstop_t_s=1_000_000.0,
        semantic_ceiling=3)


class Probe:
    """Records every process-table consultation. Empty means not consulted."""

    def __init__(self, alive=False, epoch=EPOCH):
        self.alive_calls = []
        self.epoch_calls = []
        self._alive = alive
        self._epoch = epoch

    def is_alive(self, pid):
        self.alive_calls.append(pid)
        return self._alive

    def start_epoch(self, pid):
        self.epoch_calls.append(pid)
        return self._epoch


class AttemptLivenessContract(unittest.TestCase):
    """The production predicate, driven without spawning a process."""

    def test_no_pid_is_unknown(self):
        self.assertIsNone(lc.attempt_liveness(attempt(pid=None)))

    def test_a_pre_migration_row_is_unknown(self):
        self.assertIsNone(lc.attempt_liveness(attempt(host=None, epoch=None)))
        self.assertIsNone(lc.attempt_liveness(attempt(host=None, epoch=EPOCH)))
        self.assertIsNone(lc.attempt_liveness(attempt(host=HOST, epoch=None)))

    def test_a_pre_migration_row_does_not_consult_the_process_table(self):
        probe = Probe(alive=False)
        self.assertIsNone(
            lc.attempt_liveness(
                attempt(host=None, epoch=None),
                is_alive=probe.is_alive, start_epoch=probe.start_epoch,
                host=HOST))
        self.assertEqual(probe.alive_calls, [])
        self.assertEqual(probe.epoch_calls, [])

    def test_a_foreign_host_is_unknown_and_does_not_consult_the_process_table(self):
        probe = Probe(alive=True)
        self.assertIsNone(
            lc.attempt_liveness(
                attempt(host="other-machine"),
                is_alive=probe.is_alive, start_epoch=probe.start_epoch,
                host=HOST))
        self.assertEqual(probe.alive_calls, [])
        self.assertEqual(probe.epoch_calls, [])

    def test_an_fqdn_and_its_short_name_are_the_same_host(self):
        self.assertIs(
            lc.attempt_liveness(
                attempt(host="Mac.attlocal.net"),
                is_alive=lambda _pid: False, start_epoch=lambda _pid: EPOCH,
                host="Mac"),
            False)

    def test_matching_and_alive_is_true(self):
        self.assertIs(
            lc.attempt_liveness(
                attempt(),
                is_alive=lambda _pid: True, start_epoch=lambda _pid: EPOCH,
                host=HOST),
            True)

    def test_matching_and_dead_is_false(self):
        probe = Probe(alive=False)
        self.assertIs(
            lc.attempt_liveness(
                attempt(),
                is_alive=probe.is_alive, start_epoch=probe.start_epoch,
                host=HOST),
            False)
        self.assertEqual(probe.alive_calls, [PID])
        self.assertEqual(probe.epoch_calls, [])

    def test_reused_pid_is_unknown(self):
        self.assertIsNone(
            lc.attempt_liveness(
                attempt(),
                is_alive=lambda _pid: True, start_epoch=lambda _pid: 100.890,
                host=HOST))

    def test_this_processs_own_pid_is_proven_alive_by_the_real_probe(self):
        started = wd.process_start_epoch(os.getpid())
        if started is None:
            self.skipTest("no process start epoch on this platform")
        rec = attempt(pid=os.getpid(), host=lc.scheduler_host(), epoch=started)
        self.assertIs(lc.attempt_liveness(rec), True)


class WatchdogFailsOpen(unittest.TestCase):
    """Instance 1: None must not stall. Only proven-dead may."""

    def setUp(self):
        self.kills = []
        self.fails = []

    def _watchdog(self, rec, probe):
        return wd.Watchdog(
            config=make_config(),
            attempts_provider=lambda: [rec],
            write_heartbeat=lambda *args: None,
            kill=lambda a: self.kills.append(a),
            fail_attempt=lambda a, retry, reason: self.fails.append(
                (a, retry, reason)),
            process_alive=probe.is_alive,
            start_epoch=probe.start_epoch,
            host=HOST,
            time_source=lambda: 0.001)

    def _stalled_process_dead(self):
        return [reason for _, _, reason in self.fails
                if reason == wd.StallReason.PROCESS_DEAD.value]

    def test_absent_identity_does_not_stall(self):
        probe = Probe(alive=False)
        rec = attempt(host=None, epoch=None)
        self._watchdog(rec, probe).check_once()
        self.assertEqual(self._stalled_process_dead(), [])
        self.assertEqual(probe.alive_calls, [])
        self.assertEqual(probe.epoch_calls, [])

    def test_matching_dead_stalls(self):
        probe = Probe(alive=False)
        self._watchdog(attempt(), probe).check_once()
        self.assertEqual(len(self._stalled_process_dead()), 1)
        self.assertEqual(self.fails[0][1], st.RetryClass.ENVIRONMENTAL)

    def test_matching_alive_does_not_stall(self):
        probe = Probe(alive=True, epoch=EPOCH)
        self._watchdog(attempt(), probe).check_once()
        self.assertEqual(self._stalled_process_dead(), [])

    def test_foreign_host_does_not_stall_and_does_not_consult_the_table(self):
        probe = Probe(alive=False)
        self._watchdog(attempt(host="other-machine"), probe).check_once()
        self.assertEqual(self._stalled_process_dead(), [])
        self.assertEqual(probe.alive_calls, [])
        self.assertEqual(probe.epoch_calls, [])

    def test_reused_pid_does_not_stall(self):
        probe = Probe(alive=True, epoch=100.890)
        self._watchdog(attempt(), probe).check_once()
        self.assertEqual(self._stalled_process_dead(), [])


class SalvageFailsClosed(unittest.TestCase):
    """Instance 2: None still refuses, under a distinct code from live."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = lc.LifecycleStore(root / "lifecycle.db")
        node = st.PlanNode(
            node_id="node-1", kind=st.NodeKind.CODE, depth=0,
            command=("true",))
        self.store.create_run("run-1", "d" * 64, [node])
        self.store.start_attempt("run-1", "node-1", base_sha="b" * 40)
        self.store.declare_outcome("run-1")
        self.store.conn.execute(
            "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
            (DEAD_SCHEDULER_PID, lc.scheduler_host(), "run-1"))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _refuse(self, rec, probe):
        salvage._refuse_if_live(
            self.store, rec,
            is_alive=probe.is_alive, start_epoch=probe.start_epoch,
            host=HOST)

    def test_absent_identity_refuses_unknown(self):
        probe = Probe(alive=False)
        rec = attempt(host=None, epoch=None)
        with self.assertRaises(salvage.SalvageRefused) as caught:
            self._refuse(rec, probe)
        self.assertEqual(
            caught.exception.outcome, "SALVAGE_ATTEMPT_LIVENESS_UNKNOWN")
        self.assertEqual(probe.alive_calls, [])
        self.assertEqual(probe.epoch_calls, [])

    def test_matching_dead_proceeds(self):
        probe = Probe(alive=False)
        self._refuse(attempt(), probe)

    def test_matching_alive_refuses_live(self):
        probe = Probe(alive=True, epoch=EPOCH)
        with self.assertRaises(salvage.SalvageRefused) as caught:
            self._refuse(attempt(), probe)
        self.assertEqual(caught.exception.outcome, "SALVAGE_ATTEMPT_LIVE")

    def test_foreign_host_refuses_unknown_and_does_not_consult_the_table(self):
        probe = Probe(alive=True)
        rec = attempt(host="other-machine")
        with self.assertRaises(salvage.SalvageRefused) as caught:
            self._refuse(rec, probe)
        self.assertEqual(
            caught.exception.outcome, "SALVAGE_ATTEMPT_LIVENESS_UNKNOWN")
        self.assertEqual(probe.alive_calls, [])
        self.assertEqual(probe.epoch_calls, [])

    def test_reused_pid_refuses_unknown(self):
        probe = Probe(alive=True, epoch=100.890)
        with self.assertRaises(salvage.SalvageRefused) as caught:
            self._refuse(attempt(), probe)
        self.assertEqual(
            caught.exception.outcome, "SALVAGE_ATTEMPT_LIVENESS_UNKNOWN")


class MarkLaunchedWritesIdentity(unittest.TestCase):

    def test_a_pid_is_recorded_with_this_host_and_start_epoch(self):
        started = wd.process_start_epoch(os.getpid())
        if started is None:
            self.skipTest("no process start epoch on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            try:
                node = st.PlanNode(
                    node_id="n", kind=st.NodeKind.CODE, depth=0,
                    command=("true",))
                store.create_run("run1", "d" * 64, [node])
                no = store.start_attempt("run1", "n", base_sha="b" * 40)
                store.mark_launched("run1", "n", no, os.getpid())
                rec = store.get_attempt("run1", "n", no)
                listed = store.attempts_for("run1", "n")[0]
            finally:
                store.close()
        self.assertEqual(rec.pid, os.getpid())
        self.assertEqual(rec.attempt_host, lc.scheduler_host())
        self.assertEqual(rec.attempt_start_epoch, started)
        self.assertEqual(listed.attempt_host, rec.attempt_host)
        self.assertEqual(listed.attempt_start_epoch, rec.attempt_start_epoch)
        self.assertIs(lc.attempt_liveness(rec), True)

    def test_no_pid_records_no_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            try:
                node = st.PlanNode(
                    node_id="n", kind=st.NodeKind.CODE, depth=0,
                    command=("true",))
                store.create_run("run1", "d" * 64, [node])
                no = store.start_attempt("run1", "n", base_sha="b" * 40)
                store.mark_launched("run1", "n", no, None)
                rec = store.get_attempt("run1", "n", no)
            finally:
                store.close()
        self.assertIsNone(rec.pid)
        self.assertIsNone(rec.attempt_host)
        self.assertIsNone(rec.attempt_start_epoch)


class AttemptsIdentityMigration(unittest.TestCase):

    def test_pre_migration_columns_arrive_nullable_and_existing_rows_read_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            with sqlite3.connect(str(db)) as conn:
                conn.execute(
                    "CREATE TABLE attempts ("
                    " run_id TEXT NOT NULL, node_id TEXT NOT NULL,"
                    " attempt_no INTEGER NOT NULL, base_sha TEXT NOT NULL,"
                    " state TEXT NOT NULL, started_at REAL, launched_at REAL,"
                    " pid INTEGER, turn_count INTEGER NOT NULL DEFAULT 0,"
                    " retry_class TEXT, extra_json TEXT NOT NULL DEFAULT '{}',"
                    " PRIMARY KEY (run_id, node_id, attempt_no))")
                conn.execute(
                    "INSERT INTO attempts (run_id, node_id, attempt_no,"
                    " base_sha, state, started_at, launched_at, pid,"
                    " extra_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("old", "n", 1, "b" * 40, st.NodeState.RUNNING.value,
                     1.0, 1.0, 41022, "{}"))
            store = lc.LifecycleStore(db)
            try:
                columns = {
                    row[1]: row for row in
                    store.conn.execute("PRAGMA table_info(attempts)").fetchall()}
                rec = store.get_attempt("old", "n", 1)
            finally:
                store.close()
        for name, kind in lc._ATTEMPTS_ADDED_COLUMNS:
            self.assertIn(name, columns)
            _cid, _name, sql_kind, notnull, default, _pk = columns[name]
            self.assertEqual(sql_kind, kind)
            self.assertEqual(notnull, 0)
            self.assertIsNone(default)
        self.assertEqual(rec.pid, 41022)
        self.assertIsNone(rec.attempt_host)
        self.assertIsNone(rec.attempt_start_epoch)
        self.assertIsNone(lc.attempt_liveness(rec))
