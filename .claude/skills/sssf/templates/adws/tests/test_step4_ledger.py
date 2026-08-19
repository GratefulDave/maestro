"""Executable proof of Step 4's ledger (§10.3, §10.5, §10.6, §7.7, §7.8, §7.9).

Step 4 is "lifecycle and audit tables, the partial unique index, immediate
transactions, and the loading discipline that keeps audit rows unvalidated and
undigested". The lifecycle tables and the immediate transactions were built
with the lifecycle store and are proved by `test_lifecycle.py`; what is settled
here is the rest of that sentence:

  §10.3   a partial unique index makes "at most one live attempt per node" a
          declarative constraint that releases when the attempt stops running
  §10.3   one index on the state-filtered lifecycle read
  §10.3   the audit tier's three tables exist: transitions, results, orphans
  §7.7    a result row carries its payload and its adjudication together, and
          the payload cannot be dropped for any of the four adjudications
  §7.8    panes a resumed process cannot reach are recorded in `orphans` and
          reported by `run status`
  §10.5   audit rows are never validated and never digested: read as plain
          dicts, unknown keys ignored, NULL columns absent rather than defaulted
  §10.5   the ledger holds its own connection rather than borrowing the tracer's
  §10.6   one query path — the same reader serves a live run and a finished one,
          and there is no dashboard-only table or view

Run with:  uv run adw_test.py
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def make_node(node_id: str, depth: int = 0, needs=()) -> st.PlanNode:
    """A code node — Step 4 is about rows, not about §7.4's gate."""
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=depth,
                       needs=tuple(needs), command=("true",))


def new_store(tmp_root: Path) -> lc.LifecycleStore:
    return lc.LifecycleStore(tmp_root / "lifecycle.db")


class LedgerFixture(unittest.TestCase):
    """A real sqlite database in a real temporary directory. No mocks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = new_store(self.root)
        self.addCleanup(self.store.close)

    def seed(self, run_id: str = "run1", *node_ids: str) -> None:
        nodes = [make_node(n) for n in (node_ids or ("a",))]
        self.store.create_run(run_id, "digest-1", nodes)

    def attempt_states(self, run_id: str, node_id: str):
        return [row[0] for row in self.store.conn.execute(
            "SELECT state FROM attempts WHERE run_id=? AND node_id=? ORDER BY attempt_no",
            (run_id, node_id)).fetchall()]


# ── §10.3 the partial unique index ──────────────────────────────────────────

class PartialUniqueIndexTests(LedgerFixture):

    def test_a_second_live_attempt_for_one_node_is_refused_by_the_database(self):
        """The constraint is declarative, not a Python check the scheduler can
        forget to call — a second RUNNING attempt row is refused by sqlite."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute(
                "INSERT INTO attempts (run_id, node_id, attempt_no, base_sha, state,"
                " started_at, turn_count, extra_json) VALUES (?,?,?,?,?,?,0,'{}')",
                ("run1", "a", 2, "sha1", st.NodeState.RUNNING.value, 1.0))

    def test_the_constraint_releases_when_the_attempt_stops_being_live(self):
        """"Releases automatically when status changes" (§10.3) — no explicit
        delete, and no second attempt refused after the first one closed."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.fail_attempt("run1", "a", st.RetryClass.ENVIRONMENTAL)
        second = self.store.start_attempt("run1", "a", base_sha="sha1")
        self.assertEqual(second, 2)
        self.assertEqual(self.attempt_states("run1", "a")[1], st.NodeState.RUNNING.value)

    def test_a_failed_attempt_row_stops_being_live(self):
        """The watchdog polls attempt rows whose state is RUNNING (§7.6). An
        attempt returned to PENDING that left its row RUNNING would be watched
        for ever by a watchdog that can never fail it again."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.fail_attempt("run1", "a", st.RetryClass.ENVIRONMENTAL)
        live = [a for a in self.store.attempts_for("run1", "a")
                if a.state is st.NodeState.RUNNING]
        self.assertEqual(live, [])
        self.assertEqual(self.store.attempts_for("run1", "a")[0].retry_class,
                         st.RetryClass.ENVIRONMENTAL)

    def test_cancel_run_closes_every_live_attempt_row(self):
        """§7.8 — a cancelled node's result is rejected "because its attempt is
        no longer running", which is only true if cancellation says so."""
        self.seed("run1", "a", "b")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.start_attempt("run1", "b", base_sha="sha0")
        self.store.cancel_run("run1")
        live = [a for a in self.store.attempts_for("run1")
                if a.state is st.NodeState.RUNNING]
        self.assertEqual(live, [])

    def test_abandon_closes_the_nodes_live_attempt_row(self):
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.declare_outcome("run1", stuck=True)
        self.store.conn.execute(
            "UPDATE runs SET scheduler_pid=?, scheduler_host=? WHERE run_id=?",
            (2_000_000_000, lc.scheduler_host(), "run1"))
        self.store.abandon("run1", "a")
        live = [a for a in self.store.attempts_for("run1", "a")
                if a.state is st.NodeState.RUNNING]
        self.assertEqual(live, [])

    def test_the_constraint_is_per_node_never_per_run(self):
        """§7.2 runs nodes concurrently — one live attempt each is the point."""
        self.seed("run1", "a", "b")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.start_attempt("run1", "b", base_sha="sha0")
        live = [a for a in self.store.attempts_for("run1")
                if a.state is st.NodeState.RUNNING]
        self.assertEqual(sorted(a.node_id for a in live), ["a", "b"])

    def test_many_closed_attempts_for_one_node_are_legal(self):
        """The index is *partial*: it constrains live rows only, so a node's
        attempt history is never capped by it (§7.5's budget does that)."""
        self.seed("run1", "a")
        for _ in range(3):
            self.store.start_attempt("run1", "a", base_sha="sha0")
            self.store.fail_attempt("run1", "a", st.RetryClass.ENVIRONMENTAL)
        self.assertEqual(len(self.store.attempts_for("run1", "a")), 3)

    def test_the_index_is_partial_and_named_in_the_schema(self):
        row = self.store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (lc.LIVE_ATTEMPT_INDEX,)).fetchone()
        self.assertIsNotNone(row, "the partial unique index is missing")
        self.assertIn("UNIQUE", row[0].upper())
        self.assertIn("WHERE", row[0].upper())


# ── §10.3 one index on the state-filtered read ──────────────────────────────

class ReadySetIndexTests(LedgerFixture):

    def test_the_state_filtered_lifecycle_read_is_served_by_an_index(self):
        """§10.3's "one index on the ready-set query". The ready-set *predicate*
        (deps all MERGED) is computed in Python over the projection, so what an
        index can serve is the state-filtered lifecycle read the scheduler and
        resume both issue — `EXPLAIN QUERY PLAN` is the witness."""
        self.seed("run1", "a")
        plan = self.store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT node_id FROM node_lifecycle"
            " WHERE run_id=? AND state=?", ("run1", "RUNNING")).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertIn(lc.READY_SET_INDEX, detail)


# ── §10.3 / §7.7 the audit tier ─────────────────────────────────────────────

class AuditTableTests(LedgerFixture):

    def test_construction_creates_the_three_audit_tables(self):
        names = {row[0] for row in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("transitions", "results", "orphans"):
            self.assertIn(table, names)

    def test_a_result_row_carries_its_payload_and_its_adjudication_together(self):
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.record_result("run1", st.ResultRecord(
            node_id="a", attempt_no=1, subject_sha="sha0",
            payload={"status": "success", "changed_files": ["a.py"]},
            adjudication=st.Adjudication.ACCEPTED))
        rows = self.store.audit_results("run1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adjudication"], "ACCEPTED")
        self.assertEqual(rows[0]["payload"]["changed_files"], ["a.py"])

    def test_the_payload_survives_every_adjudication(self):
        """§7.7 — "the payload is retained in all four outcomes". A rejected
        result that dropped its payload is how a correct FAIL disappeared."""
        self.seed("run1", "a")
        for index, verdict in enumerate(st.Adjudication, start=1):
            self.store.record_result("run1", st.ResultRecord(
                node_id="a", attempt_no=index, subject_sha=f"sha{index}",
                payload={"finding": verdict.value}, adjudication=verdict))
        rows = self.store.audit_results("run1")
        self.assertEqual(len(rows), len(list(st.Adjudication)))
        self.assertEqual({r["payload"]["finding"] for r in rows},
                         {v.value for v in st.Adjudication})

    def test_the_payload_column_is_not_null_in_the_schema(self):
        columns = {row[1]: row for row in
                   self.store.conn.execute("PRAGMA table_info(results)")}
        self.assertEqual(columns["payload_json"][3], 1,
                         "results.payload_json must be NOT NULL (§10.3)")

    def test_a_result_write_that_fails_leaves_no_half_row(self):
        """The audit write is its own `BEGIN IMMEDIATE` (§7.9): an unserialisable
        payload rolls the whole statement back rather than leaving a stub."""
        self.seed("run1", "a")
        with self.assertRaises(TypeError):
            self.store.record_result("run1", st.ResultRecord(
                node_id="a", attempt_no=1, subject_sha="sha0",
                payload={"bad": object()}, adjudication=st.Adjudication.ACCEPTED))
        self.assertEqual(self.store.audit_results("run1"), ())

    def test_resume_records_an_orphan_for_every_pane_it_cannot_reach(self):
        """§7.8 — a resumed run inherits RUNNING attempts belonging to panes the
        new process does not own; none is adopted, and each is recorded."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.mark_launched("run1", "a", 1, pid=4242)
        reclaimed = self.store.resume_run("run1")
        self.assertEqual(reclaimed, ("a",))
        orphans = self.store.audit_orphans("run1")
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["node_id"], "a")
        self.assertEqual(orphans[0]["attempt_no"], 1)
        self.assertEqual(orphans[0]["pid"], 4242)

    def test_a_resume_with_nothing_in_flight_records_no_orphan(self):
        self.seed("run1", "a")
        self.store.resume_run("run1")
        self.assertEqual(self.store.audit_orphans("run1"), ())

    def test_run_status_reports_the_orphans(self):
        """§7.8 — "recorded in `orphans` and reported by `run status`". The
        operator kills the leaked pane by hand, so the report must name it."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        self.store.mark_launched("run1", "a", 1, pid=4242)
        self.store.resume_run("run1")
        scheduler = sch.Scheduler(
            run_id="run1", nodes=[make_node("a")],
            config=st.SchedulerConfig(
                concurrency=2, node_timeout_s=60.0, turn_timeout_s=30.0,
                final_acceptance_timeout_s=60.0, backstop_t_s=600.0,
                semantic_ceiling=2),
            deps=sch.SchedulerDeps(
                store=self.store, repo=self.root, integration_path=self.root,
                integration_branch="integration/run1",
                worktrees_root=self.root / "wt", scratch_root=self.root / "scratch",
                run_node=lambda attempt, node, record, retry_prompt, on_launch,
                                cancel_requested: None,
                run_gate=lambda attempt, node, phase, cancel_requested: None,
                run_integration_gate=lambda path, specs, cancel_requested: None,
                quiesce_attempt=lambda record, phase: None),
            plan_digest="digest-1")
        text = scheduler.status_diagnostic()
        self.assertIn("orphan", text.lower())
        self.assertIn("4242", text)


# ── §10.5 / §5.3 the loading discipline ─────────────────────────────────────

class LoadingDisciplineTests(LedgerFixture):

    def test_audit_rows_load_as_plain_dicts_never_a_validating_model(self):
        """§5.3 — "audit rows are read as `sqlite3.Row`; blobs are `json.loads`ed
        to plain dicts". A pydantic model here reimports digest-revalidation."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        rows = self.store.audit_transitions("run1")
        self.assertTrue(rows)
        for row in rows:
            self.assertIs(type(row), dict)
            self.assertIs(type(row["detail"]), dict)

    def test_an_unknown_key_in_an_audit_blob_is_ignored_not_refused(self):
        """A post-v1 writer adds a key; an older reader must not raise."""
        self.seed("run1", "a")
        self.store.conn.execute(
            "INSERT INTO transitions (run_id, node_id, kind, from_state, to_state,"
            " reason, actor, detail_json, created_at)"
            " VALUES ('run1','a','node','PENDING','RUNNING','x','scheduler',?,'2026-01-01')",
            (json.dumps({"a_key_v1_never_heard_of": 1}),))
        rows = self.store.audit_transitions("run1")
        self.assertEqual(rows[-1]["detail"]["a_key_v1_never_heard_of"], 1)

    def test_a_null_audit_column_is_absent_rather_than_defaulted_on_read(self):
        """§10.5 — "no default applied on read". A run-level transition has no
        node, and the reader must hand back an absent key, never an invented
        value that a later comparison would silently believe."""
        self.seed("run1", "a")
        self.store.acceptance_started("run1")
        run_rows = [r for r in self.store.audit_transitions("run1") if r["kind"] == "run"]
        self.assertTrue(run_rows)
        self.assertNotIn("node_id", run_rows[0])
        self.assertIsNone(run_rows[0].get("node_id"))

    def test_a_post_v1_audit_column_needs_no_migration_of_old_rows(self):
        """Every post-v1 audit column is nullable with no default (§10.5), so
        adding one leaves every historical row readable and unrewritten."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        before = self.store.audit_transitions("run1")
        self.store.conn.execute("ALTER TABLE transitions ADD COLUMN a_v2_column TEXT")
        after = self.store.audit_transitions("run1")
        self.assertEqual(len(before), len(after))
        self.assertNotIn("a_v2_column", after[0])

    def test_no_audit_table_carries_a_digest_hash_or_chain_column(self):
        """§5.3's first permanent prohibition — no digest, hash chain, or
        content-derived identifier over any audit row."""
        forbidden = ("digest", "hash", "checksum", "signature", "prev_", "chain")
        for table in ("transitions", "results", "orphans"):
            for row in self.store.conn.execute("PRAGMA table_info(%s)" % table):
                name = row[1].lower()
                for token in forbidden:
                    self.assertNotIn(token, name,
                                     "%s.%s looks like a content-derived audit "
                                     "identifier (§5.3)" % (table, row[1]))

    def test_the_ledger_holds_its_own_connection(self):
        """§10.5 — the ledger takes its own connection; sharing one would sweep
        an unrelated trace event into a transition's transaction and roll it
        back. The store therefore accepts a path and never a connection."""
        parameters = inspect.signature(lc.LifecycleStore.__init__).parameters
        self.assertNotIn("conn", parameters)
        self.assertNotIn("connection", parameters)
        other = new_store(self.root)
        self.addCleanup(other.close)
        self.assertIsNot(other.conn, self.store.conn)


# ── §10.6 one query path ────────────────────────────────────────────────────

class OneQueryPathTests(LedgerFixture):

    def test_the_same_reader_serves_a_live_run_and_a_finished_one(self):
        """§10.6 — live and historical reads use the same tables and the same
        cursor pattern. Not two readers that can disagree."""
        self.seed("run1", "a")
        self.store.start_attempt("run1", "a", base_sha="sha0")
        live = self.store.audit_transitions("run1")
        self.store.mark_verified("run1", "a", output_sha="sha1")
        self.store.mark_merged("run1", "a")
        self.store.declare_outcome("run1", acceptance_result=True)
        historical = self.store.audit_transitions("run1")
        self.assertEqual(historical[:len(live)], live)
        self.assertGreater(len(historical), len(live))

    def test_there_is_no_dashboard_only_table_or_view(self):
        """§10.6 — no dashboard-only schema and no fixture-only truth."""
        objects = {(row[0], row[1]) for row in self.store.conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table','view')")}
        views = {name for kind, name in objects if kind == "view"}
        self.assertEqual(views, set())
        tables = {name for kind, name in objects
                  if kind == "table" and not name.startswith("sqlite_")}
        self.assertEqual(tables, {"runs", "dag_nodes", "node_lifecycle", "attempts",
                                  "attempt_baselines", "transitions", "results",
                                  "orphans"})


if __name__ == "__main__":
    unittest.main()
