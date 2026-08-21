"""A PENDING write records who put the node back on the frontier (#103).

Three writers share the state and used to be indistinguishable on
`node_lifecycle`:

* `retry()` — `actor=operator`, `reason=retry`, grant delta 0 on a plain retry
* `_reopen_run_cancelled_node` — `reason=resume:run-cancel`
* `fail_attempt` — the scheduler

`granted_extra_attempts` only distinguished a grant, so a plain retry looked
like a scheduler write. The typed `pending_cause` column is the same shape as
`cancel_cause` and `merge_cause`; `granted_extra_attempts` stays the magnitude
of a grant, not the identity of the writer.

§3.6 B15: a field with zero readers is a build failure. The cause is read by
`LifecycleReader.nodes` and by `run status`.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


RUN_ID = "run-pending-cause"
RETRYED = "lane-retried"
RESUMED = "lane-resumed"
FAILED = "lane-failed"
SEEDED = "lane-seeded"


def make_node(node_id: str, depth: int = 0, needs=()) -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=depth,
                       needs=tuple(needs), command=("true",))


def _stored_pending_cause(store: lc.LifecycleStore, node_id: str):
    row = store.conn.execute(
        "SELECT pending_cause FROM node_lifecycle WHERE run_id=? AND node_id=?",
        (RUN_ID, node_id)).fetchone()
    return row[0] if row else None


def _block_then_retry(store: lc.LifecycleStore, node_id: str, *,
                      force: bool = False, grant: int = 0) -> st.NodeLifecycle:
    store.start_attempt(RUN_ID, node_id, base_sha="s1")
    store.mark_blocked(RUN_ID, node_id,
                       st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
    store.declare_outcome(RUN_ID)
    return store.retry(RUN_ID, node_id, force=force, grant=grant)


class PendingCauseVocabularyTests(unittest.TestCase):
    """`pending_cause_label` is the one derivation."""

    def test_the_three_writers_are_three_values(self):
        self.assertNotEqual(st.PendingCause.SCHEDULER,
                            st.PendingCause.OPERATOR_RETRY)
        self.assertNotEqual(st.PendingCause.SCHEDULER,
                            st.PendingCause.OPERATOR_RESUME)
        self.assertNotEqual(st.PendingCause.OPERATOR_RETRY,
                            st.PendingCause.OPERATOR_RESUME)

    def test_a_scheduler_pending_reads_scheduler(self):
        self.assertEqual(
            st.pending_cause_label(st.NodeState.PENDING,
                                   st.PendingCause.SCHEDULER),
            "SCHEDULER")

    def test_an_operator_retry_reads_operator_retry(self):
        self.assertEqual(
            st.pending_cause_label(st.NodeState.PENDING,
                                   st.PendingCause.OPERATOR_RETRY),
            "OPERATOR_RETRY")

    def test_a_resume_reopen_reads_operator_resume(self):
        self.assertEqual(
            st.pending_cause_label(st.NodeState.PENDING,
                                   st.PendingCause.OPERATOR_RESUME),
            "OPERATOR_RESUME")

    def test_a_null_pending_is_not_guessed_as_scheduler(self):
        self.assertIsNone(
            st.pending_cause_label(st.NodeState.PENDING, None))
        self.assertNotEqual(
            st.pending_cause_label(st.NodeState.PENDING, None),
            st.PendingCause.SCHEDULER.value)

    def test_a_node_that_is_not_pending_has_no_provenance(self):
        for state in (st.NodeState.RUNNING, st.NodeState.VERIFIED,
                      st.NodeState.MERGED, st.NodeState.BLOCKED,
                      st.NodeState.CANCELLED):
            self.assertIsNone(
                st.pending_cause_label(state, st.PendingCause.SCHEDULER),
                state)


class PendingCauseIsStoredTests(unittest.TestCase):
    """Each of the three PENDING writers stamps a distinguishable typed cause."""

    def test_fail_attempt_stamps_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(FAILED)])
            store.start_attempt(RUN_ID, FAILED, base_sha="s1")
            store.fail_attempt(RUN_ID, FAILED, st.RetryClass.ENVIRONMENTAL)
            self.assertEqual(_stored_pending_cause(store, FAILED),
                             st.PendingCause.SCHEDULER.value)
            store.close()

    def test_plain_retry_stamps_operator_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(RETRYED)])
            row = _block_then_retry(store, RETRYED)
            self.assertIs(row.state, st.NodeState.PENDING)
            self.assertEqual(row.granted_extra_attempts, 0)
            self.assertEqual(_stored_pending_cause(store, RETRYED),
                             st.PendingCause.OPERATOR_RETRY.value)
            store.close()

    def test_resume_reopen_stamps_operator_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(RESUMED)])
            store.cancel_run(RUN_ID)
            store.declare_outcome(RUN_ID)
            store.resume_run(RUN_ID)
            self.assertEqual(_stored_pending_cause(store, RESUMED),
                             st.PendingCause.OPERATOR_RESUME.value)
            store.close()

    def test_the_three_writers_are_distinguishable_on_one_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run("run-fail", "d" * 64, [make_node(FAILED)])
            store.create_run("run-retry", "d" * 64, [make_node(RETRYED)])
            store.create_run("run-resume", "d" * 64, [make_node(RESUMED)])
            store.start_attempt("run-fail", FAILED, base_sha="s1")
            store.fail_attempt("run-fail", FAILED, st.RetryClass.ENVIRONMENTAL)
            store.start_attempt("run-retry", RETRYED, base_sha="s1")
            store.mark_blocked(
                "run-retry", RETRYED,
                st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
            store.declare_outcome("run-retry")
            store.retry("run-retry", RETRYED)
            store.cancel_run("run-resume")
            store.declare_outcome("run-resume")
            store.resume_run("run-resume")
            causes = {
                FAILED: store.conn.execute(
                    "SELECT pending_cause FROM node_lifecycle"
                    " WHERE run_id=? AND node_id=?",
                    ("run-fail", FAILED)).fetchone()[0],
                RETRYED: store.conn.execute(
                    "SELECT pending_cause FROM node_lifecycle"
                    " WHERE run_id=? AND node_id=?",
                    ("run-retry", RETRYED)).fetchone()[0],
                RESUMED: store.conn.execute(
                    "SELECT pending_cause FROM node_lifecycle"
                    " WHERE run_id=? AND node_id=?",
                    ("run-resume", RESUMED)).fetchone()[0],
            }
            store.close()
            self.assertEqual(causes[FAILED], st.PendingCause.SCHEDULER.value)
            self.assertEqual(causes[RETRYED],
                             st.PendingCause.OPERATOR_RETRY.value)
            self.assertEqual(causes[RESUMED],
                             st.PendingCause.OPERATOR_RESUME.value)
            self.assertEqual(len(set(causes.values())), 3, causes)

    def test_a_grant_does_not_change_the_retry_cause(self):
        """`granted_extra_attempts` is magnitude, not identity (#103)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(RETRYED)])
            row = _block_then_retry(store, RETRYED, force=True)
            self.assertEqual(row.granted_extra_attempts, 1)
            self.assertEqual(_stored_pending_cause(store, RETRYED),
                             st.PendingCause.OPERATOR_RETRY.value)
            store.close()

    def test_seeded_pending_carries_no_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(SEEDED)])
            self.assertIsNone(_stored_pending_cause(store, SEEDED))
            store.close()

    def test_leaving_pending_clears_the_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = lc.LifecycleStore(Path(tmp) / "lifecycle.db")
            store.create_run(RUN_ID, "d" * 64, [make_node(FAILED)])
            store.start_attempt(RUN_ID, FAILED, base_sha="s1")
            store.fail_attempt(RUN_ID, FAILED, st.RetryClass.ENVIRONMENTAL)
            self.assertEqual(_stored_pending_cause(store, FAILED),
                             st.PendingCause.SCHEDULER.value)
            store.start_attempt(RUN_ID, FAILED, base_sha="s2")
            self.assertIsNone(_stored_pending_cause(store, FAILED))
            store.close()


class PendingCauseIsReadTests(unittest.TestCase):
    """B15: the column has a reader. `run status` is that reader."""

    def _stamp_writers(self, store: lc.LifecycleStore) -> None:
        store.create_run("run-fail", "d" * 64, [make_node(FAILED)])
        store.create_run("run-retry", "d" * 64, [make_node(RETRYED)])
        store.create_run("run-resume", "d" * 64, [make_node(RESUMED)])
        store.create_run("run-seed", "d" * 64, [make_node(SEEDED)])
        store.start_attempt("run-fail", FAILED, base_sha="s1")
        store.fail_attempt("run-fail", FAILED, st.RetryClass.ENVIRONMENTAL)
        store.start_attempt("run-retry", RETRYED, base_sha="s1")
        store.mark_blocked(
            "run-retry", RETRYED,
            st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
        store.declare_outcome("run-retry")
        store.retry("run-retry", RETRYED)
        store.cancel_run("run-resume")
        store.declare_outcome("run-resume")
        store.resume_run("run-resume")

    def test_the_read_only_projection_carries_each_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            self._stamp_writers(store)
            store.close()
            reader = lc.LifecycleReader.open(db)
            try:
                by_run = {
                    "run-fail": reader.nodes("run-fail")[0],
                    "run-retry": reader.nodes("run-retry")[0],
                    "run-resume": reader.nodes("run-resume")[0],
                    "run-seed": reader.nodes("run-seed")[0],
                }
            finally:
                reader.close()
            self.assertEqual(by_run["run-fail"].pending_provenance, "SCHEDULER")
            self.assertEqual(by_run["run-retry"].pending_provenance,
                             "OPERATOR_RETRY")
            self.assertEqual(by_run["run-resume"].pending_provenance,
                             "OPERATOR_RESUME")
            self.assertIsNone(by_run["run-seed"].pending_provenance)

    def test_run_status_json_carries_the_cause_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            self._stamp_writers(store)
            store.close()
            reader = lc.LifecycleReader.open(db)
            try:
                causes = {}
                for run_id in ("run-fail", "run-retry", "run-resume", "run-seed"):
                    record = reader.run(run_id)
                    progress = maestro._run_progress(
                        reader, record, SimpleNamespace(plan_digests={}))
                    causes[run_id] = progress["nodes"][0]["pending_cause"]
                    round_tripped = json.loads(
                        json.dumps(progress, sort_keys=True))
                    self.assertEqual(
                        round_tripped["nodes"][0]["pending_cause"],
                        causes[run_id])
            finally:
                reader.close()
            self.assertEqual(causes["run-fail"], "SCHEDULER")
            self.assertEqual(causes["run-retry"], "OPERATOR_RETRY")
            self.assertEqual(causes["run-resume"], "OPERATOR_RESUME")
            self.assertIsNone(causes["run-seed"])

    def test_the_human_render_names_each_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            self._stamp_writers(store)
            store.close()
            reader = lc.LifecycleReader.open(db)
            try:
                rendered = []
                for run_id in ("run-fail", "run-retry", "run-resume"):
                    record = reader.run(run_id)
                    rendered.append(maestro._render_progress(
                        maestro._run_progress(
                            reader, record, SimpleNamespace(plan_digests={}))))
            finally:
                reader.close()
            text = "\n".join(rendered)
            self.assertIn("scheduler retry", text)
            self.assertIn("operator retry", text)
            self.assertIn("operator resume", text)

    def test_the_run_status_verb_prints_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            self._stamp_writers(store)
            store.close()
            args = SimpleNamespace(db=str(db), run_id="run-retry", as_json=True,
                                   plan_digests={}, repository_state=None)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = maestro._run_status(args)
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["nodes"][0]["pending_cause"],
                             "OPERATOR_RETRY")


class LedgerOlderThanTheColumnTests(unittest.TestCase):
    """An existing PENDING row is never displayed as a scheduler write."""

    def test_the_read_only_projection_does_not_refuse_the_old_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            store.create_run(RUN_ID, "d" * 64, [make_node(SEEDED)])
            store.start_attempt(RUN_ID, SEEDED, base_sha="s1")
            store.fail_attempt(RUN_ID, SEEDED, st.RetryClass.ENVIRONMENTAL)
            store.conn.execute(
                "ALTER TABLE node_lifecycle DROP COLUMN pending_cause")
            store.conn.commit()
            store.close()
            reader = lc.LifecycleReader.open(db)
            node = reader.nodes(RUN_ID)[0]
            reader.close()
            self.assertIs(node.state, st.NodeState.PENDING)
            self.assertIsNone(node.pending_cause)
            self.assertIsNone(node.pending_provenance)

    def test_migration_adds_the_column_as_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = lc.LifecycleStore(db)
            store.create_run(RUN_ID, "d" * 64, [make_node(FAILED)])
            store.start_attempt(RUN_ID, FAILED, base_sha="s1")
            store.fail_attempt(RUN_ID, FAILED, st.RetryClass.ENVIRONMENTAL)
            store.conn.execute(
                "ALTER TABLE node_lifecycle DROP COLUMN pending_cause")
            store.conn.commit()
            store.close()
            store = lc.LifecycleStore(db)
            self.assertIn("pending_cause",
                          lc._table_columns(store.conn, "node_lifecycle"))
            stored = store.conn.execute(
                "SELECT pending_cause FROM node_lifecycle"
                " WHERE run_id=? AND node_id=?", (RUN_ID, FAILED)).fetchone()
            store.close()
            self.assertIsNone(stored[0])


class PendingCauseDetailWordingTests(unittest.TestCase):

    def test_each_writer_has_a_detail_phrase(self):
        self.assertEqual(
            maestro._pending_cause_detail(st.PendingCause.SCHEDULER.value),
            "scheduler retry")
        self.assertEqual(
            maestro._pending_cause_detail(st.PendingCause.OPERATOR_RETRY.value),
            "operator retry")
        self.assertEqual(
            maestro._pending_cause_detail(st.PendingCause.OPERATOR_RESUME.value),
            "operator resume")

    def test_a_null_pending_keeps_empty_detail(self):
        self.assertEqual(maestro._pending_cause_detail(None), "")


if __name__ == "__main__":
    unittest.main()
