"""Executable proof of the authority-tier lifecycle store (§7.1, §7.3, §7.8, §7.9, §8.7, §11.2, §11.3).

Step 6's lifecycle store owns node state, attempt rows, and transitions — never
which agent to launch, never how to classify a failure, never liveness. The
tests below settle:

  §7.9    every transition is one BEGIN IMMEDIATE writing lifecycle + audit + runs together
  §7.3    MERGED/CANCELLED are absolutely terminal; BLOCKED is operator-terminal only
  §7.1    the ready set is PENDING nodes whose deps are all MERGED, sorted (depth, node_id)
  §8.7    UPSTREAM_BLOCKED is derived and the cascade is reversible with no un-cascade rule
  §7.3    the run outcome function is total over the terminal state
  §7.3    outcome is a record (latest + timestamp), never a stored history; NULL means undeclared
  §7.8    cancellation writes CANCELLED for every non-terminal node in one transaction
  §11.3   every stored block_reason's escapes actually execute and leave BLOCKED
  --      the store is safe under a real ThreadPoolExecutor

Run with:  uv run adw_test.py -k lifecycle
"""

from __future__ import annotations

import itertools
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402


def make_node(node_id: str, depth: int, needs=()) -> st.PlanNode:
    """A code node — no gate needed, so tests stay focused on lifecycle, not §7.4."""
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.CODE,
        depth=depth,
        needs=tuple(needs),
        command=("true",),
    )


def make_agent_node(node_id: str, depth: int = 0, needs=()) -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.AGENT,
        depth=depth,
        needs=tuple(needs),
        instruction=f"Build {node_id}.",
        gate_command=("pytest",),
        gate_selector=f"tests/test_{node_id}.py",
    )


def new_store(tmp_root: Path) -> lc.LifecycleStore:
    return lc.LifecycleStore(tmp_root / "lifecycle.db")


def _node_cancel_cause(store: lc.LifecycleStore, run_id: str, node_id: str):
    """The stored cause on a node row. Read from the column, not from a
    projection — the column is what `_guard_transition` reads."""
    row = store.conn.execute(
        "SELECT cancel_cause FROM node_lifecycle WHERE run_id=? AND node_id=?",
        (run_id, node_id),
    ).fetchone()
    return row[0] if row else None


def _init_git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _orphan_sha(root: Path) -> str:
    """A commit that exists but is not an ancestor of HEAD — the negative skip case."""
    original_branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(root), "checkout", "-q", "--orphan", "not-merged"], check=True
    )
    (root / "orphan.txt").write_text("y")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "orphan"], check=True)
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    # Restore HEAD to the original branch — `skip`'s ancestry check below is
    # always against *this repo's* HEAD, and this orphan commit must stay
    # unreachable from it for the negative case to mean anything.
    subprocess.run(
        ["git", "-C", str(root), "checkout", "-q", original_branch], check=True
    )
    return out.stdout.strip()


# ── §5.3 / store construction ───────────────────────────────────────────────


class StoreConstructionTests(unittest.TestCase):
    def test_construction_creates_the_persistent_review_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            names = {
                row[0]
                for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for table in (
                "runs",
                "dag_nodes",
                "node_lifecycle",
                "attempts",
                "transitions",
                "lane_candidates",
                "candidate_reviews",
                "repair_handoffs",
                "legacy_review_migration_blocks",
                "lane_retry_spend",
                "actor_sessions",
            ):
                self.assertIn(table, names)

    def test_dag_nodes_carry_the_plan_digest_on_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "digest-abc", [make_node("a", 0)])
            row = store.conn.execute(
                "SELECT plan_digest FROM dag_nodes WHERE run_id=? AND node_id=?",
                ("run1", "a"),
            ).fetchone()
            self.assertEqual(row[0], "digest-abc")

    def test_create_run_writes_the_plan_name(self):
        """The INSERT is the only writer. A name that never reaches this
        statement is the §16.3 item 61 reverse-hash forever."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run(
                "run1", "digest-abc", [make_node("a", 0)], plan_name="Phase 1 freeze"
            )
            row = store.conn.execute(
                "SELECT plan_name FROM runs WHERE run_id=?", ("run1",)
            ).fetchone()
            self.assertEqual(row[0], "Phase 1 freeze")
            store.create_run("run2", "digest-def", [make_node("b", 0)])
            unnamed = store.conn.execute(
                "SELECT plan_name FROM runs WHERE run_id=?", ("run2",)
            ).fetchone()
            self.assertIsNone(unnamed[0])

    def test_create_run_seeds_every_node_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run(
                "run1", "d", [make_node("a", 0), make_node("b", 1, needs=("a",))]
            )
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            self.assertEqual(store.get_node("run1", "b").state, st.NodeState.PENDING)


# ── persistent review lifecycle ─────────────────────────────────────────────


def _review_lane(store: lc.LifecycleStore) -> None:
    store.create_run(
        "run1",
        "d",
        [make_agent_node("build", 0), make_node("downstream", 2, needs=("build",))],
    )
    store.ensure_derived_review_node(
        "run1", "build", depth=1, downstream_needs=("downstream",)
    )


class PersistentReviewLifecycleTests(unittest.TestCase):
    sha_a = "a" * 40
    sha_b = "b" * 40

    def test_migrates_legacy_dag_and_lifecycle_rows_without_inventing_a_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle.db"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE runs (
                  run_id TEXT PRIMARY KEY, plan_digest TEXT NOT NULL,
                  created_at TEXT NOT NULL, last_transition_at TEXT NOT NULL,
                  latest_outcome TEXT, latest_outcome_at TEXT,
                  cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE dag_nodes (
                  run_id TEXT NOT NULL, node_id TEXT NOT NULL, plan_digest TEXT NOT NULL,
                  kind TEXT NOT NULL, depth INTEGER NOT NULL, needs_json TEXT NOT NULL,
                  outputs_json TEXT NOT NULL, specs_json TEXT NOT NULL,
                  PRIMARY KEY (run_id, node_id)
                );
                CREATE TABLE node_lifecycle (
                  run_id TEXT NOT NULL, node_id TEXT NOT NULL, state TEXT NOT NULL,
                  attempt_no INTEGER NOT NULL DEFAULT 0, block_reason TEXT,
                  output_sha TEXT, granted_extra_attempts INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL, PRIMARY KEY (run_id, node_id)
                );
            """)
            conn.close()
            store = lc.LifecycleStore(path)
            dag_columns = {
                row[1] for row in store.conn.execute("PRAGMA table_info(dag_nodes)")
            }
            lifecycle_columns = {
                row[1]
                for row in store.conn.execute("PRAGMA table_info(node_lifecycle)")
            }
            self.assertIn("review_of", dag_columns)
            self.assertIn("lane_phase", lifecycle_columns)
            self.assertIn(
                "lane_candidates",
                {
                    row[0]
                    for row in store.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                },
            )
            self.assertIsNotNone(store.review_migration_backup_path)
            self.assertTrue(store.review_migration_backup_path.is_file())

    def test_review_schema_failure_restores_the_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, plan_digest TEXT,"
                    " created_at TEXT, last_transition_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO runs VALUES ('legacy', 'digest', 'then', 'then')"
                )
            original_migrate = lc._migrate
            try:

                def fail_after_review_ddl(_conn):
                    raise sqlite3.OperationalError("forced legacy conversion failure")

                lc._migrate = fail_after_review_ddl
                with self.assertRaises(lc.ReviewSchemaMigrationFailed):
                    lc.LifecycleStore(path)
            finally:
                lc._migrate = original_migrate
            with sqlite3.connect(path) as conn:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertEqual(names, {"runs"})
                self.assertEqual(
                    conn.execute("SELECT run_id FROM runs").fetchone()[0], "legacy"
                )
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )

    def test_derived_review_projection_is_idempotent_and_rewires_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            review = store.ensure_derived_review_node(
                "run1", "build", depth=1, downstream_needs=("downstream",)
            )
            self.assertEqual(review.state, st.NodeState.PENDING)
            row = store.conn.execute(
                "SELECT kind, review_of, needs_json FROM dag_nodes"
                " WHERE run_id=? AND node_id=?",
                ("run1", "build::review"),
            ).fetchone()
            self.assertEqual((row[0], row[1]), (st.NodeKind.REVIEW.value, "build"))
            self.assertEqual(row[2], '["build"]')
            downstream = store.conn.execute(
                "SELECT needs_json FROM dag_nodes WHERE run_id=? AND node_id=?",
                ("run1", "downstream"),
            ).fetchone()
            self.assertEqual(downstream[0], '["build::review"]')
            with self.assertRaises(lc.LifecycleError):
                store.ensure_derived_review_node(
                    "run1", "build", depth=9, downstream_needs=("downstream",)
                )

    def test_authored_review_remains_refused_and_a_tests_node_owns_a_review(self):
        """An authored review node stays unrepresentable; a tests node owns one.

        The second half of this used to assert the opposite — that a tests
        node could not own a derived review — and that was the ledger half of
        the defect run-8d1a71f463e4430f92a125a8f8b3731d exposed: a tests node
        reached MERGED with no independent reader, because there was nothing
        for one to be recorded against. A code node still cannot own one; its
        acceptance is its command's exit code (§6.2).

        The run is pinned `STRENGTH_V1` because that is the contract under
        which a tests node is reviewable at all. Written before §19 M42, this
        left the pin unset, which reads as LEGACY — and under LEGACY the same
        call is refused on purpose, which is asserted by
        `test_a_legacy_pinned_run_refuses_a_tests_owner_for_a_derived_review`
        rather than by weakening anything here.
        """
        with self.assertRaises(ValueError):
            st.PlanNode(node_id="authored-review", kind=st.NodeKind.REVIEW, depth=0)
        tests = st.PlanNode(
            node_id="tests",
            kind=st.NodeKind.TESTS,
            depth=0,
            gate_command=("true",),
            gate_selector="tests/test_example.py",
            instruction="add a focused test",
        )
        code = st.PlanNode(
            node_id="code",
            kind=st.NodeKind.CODE,
            depth=0,
            command=("true",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run(
                "run1",
                "d",
                [tests, code],
                test_strength_contract=st.TestStrengthContract.STRENGTH_V1,
            )
            row = store.conn.execute(
                "SELECT kind, review_of FROM dag_nodes WHERE run_id=? AND node_id=?",
                ("run1", "tests"),
            ).fetchone()
            self.assertEqual(row, (st.NodeKind.TESTS.value, None))
            store.ensure_derived_review_node(
                "run1", "tests", depth=1, downstream_needs=()
            )
            derived = store.conn.execute(
                "SELECT kind, review_of FROM dag_nodes WHERE run_id=? AND node_id=?",
                ("run1", "tests::review"),
            ).fetchone()
            self.assertEqual(derived, (st.NodeKind.REVIEW.value, "tests"))
            with self.assertRaises(lc.LifecycleError):
                store.ensure_derived_review_node(
                    "run1", "code", depth=1, downstream_needs=()
                )

    def test_a_legacy_pinned_run_refuses_a_tests_owner_for_a_derived_review(self):
        """§19 M42 — the pin decides, not what this runtime can now do.

        A tests node in a legacy-pinned run was admitted on its case count
        under rules that had no reviewer in them. Projecting a review for it
        now would not merely add a row: it rewires every direct dependant's
        `needs_json` to the review and lifts the depth of everything below,
        reopening dependency decisions of nodes that may already be terminal.
        The scheduler stopped asking; this refusal is what stops a future
        caller reintroducing it by another route.

        The same fixture as the test above, differing only in the pin — which
        is the whole claim.
        """
        tests = st.PlanNode(
            node_id="tests",
            kind=st.NodeKind.TESTS,
            depth=0,
            gate_command=("true",),
            gate_selector="tests/test_example.py",
            instruction="add a focused test",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            # No `test_strength_contract`: the legacy pin, recorded explicitly.
            store.create_run("run1", "d", [tests])
            self.assertIs(
                store.test_strength_contract("run1"),
                st.TestStrengthContract.LEGACY,
            )

            with self.assertRaises(lc.LifecycleError) as caught:
                store.ensure_derived_review_node(
                    "run1", "tests", depth=1, downstream_needs=()
                )
            self.assertIn("cannot own a derived review node", str(caught.exception))
            # And the refusal left no half-written row behind.
            self.assertIsNone(
                store.conn.execute(
                    "SELECT 1 FROM dag_nodes WHERE run_id=? AND node_id=?",
                    ("run1", "tests::review"),
                ).fetchone()
            )

    def test_derived_review_rejects_generic_verified_and_merged_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            with self.assertRaises(lc.LifecycleError):
                store.mark_verified("run1", "build::review", self.sha_a)
            with self.assertRaises(lc.LifecycleError):
                store.mark_merged("run1", "build::review")
            self.assertIs(
                store.get_node("run1", "build::review").state,
                st.NodeState.PENDING,
            )

    def test_candidate_publication_replays_once_and_requires_proven_descent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            first = store.publish_candidate(
                "run1", "build", self.sha_a, builder_generation=0
            )
            replay = store.publish_candidate(
                "run1", "build", self.sha_a, builder_generation=0
            )
            self.assertTrue(first.created)
            self.assertFalse(replay.created)
            self.assertEqual(replay.candidate.candidate_seq, 1)
            with self.assertRaises(lc.LifecycleError):
                store.publish_candidate(
                    "run1",
                    "build",
                    self.sha_b,
                    parent_candidate_sha=self.sha_a,
                    builder_generation=0,
                    ancestry_validator=lambda _parent, _child: False,
                )
            second = store.publish_candidate(
                "run1",
                "build",
                self.sha_b,
                parent_candidate_sha=self.sha_a,
                builder_generation=0,
                ancestry_validator=lambda parent, child: (
                    parent == self.sha_a and child == self.sha_b
                ),
            )
            self.assertTrue(second.created)
            self.assertEqual(second.candidate.candidate_seq, 2)
            with self.assertRaises(lc.LifecycleError):
                store.publish_candidate(
                    "run1",
                    "build",
                    self.sha_a,
                    parent_candidate_sha=self.sha_a,
                    builder_generation=0,
                )

    def test_lane_retry_spend_is_replay_safe_without_a_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            first = store.spend_lane_retry(
                "run1",
                "build",
                st.LaneRetryClass.REVIEW_REJECTION,
                cycle_seq=1,
                candidate_sha=self.sha_a,
                detail={"reason": "review rejected"},
            )
            replay = store.spend_lane_retry(
                "run1",
                "build",
                st.LaneRetryClass.REVIEW_REJECTION,
                cycle_seq=1,
                candidate_sha=self.sha_a,
                detail={"reason": "review rejected"},
            )
            self.assertTrue(first.created)
            self.assertFalse(replay.created)
            self.assertEqual(len(store.lane_retry_spends("run1", "build")), 1)
            with self.assertRaises(lc.LifecycleError):
                store.spend_lane_retry(
                    "run1",
                    "build",
                    st.LaneRetryClass.SEMANTIC,
                    cycle_seq=1,
                    candidate_sha=self.sha_a,
                    detail={"reason": "different debit"},
                )

    def test_published_review_dispatches_once_and_completes_only_after_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=0)

            started = store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            self.assertTrue(started.created)
            self.assertTrue(started.should_dispatch)
            self.assertIs(started.review.state, st.CandidateReviewState.PUBLISHED)
            self.assertIsNone(started.review.dispatched_at)
            resumed = store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=9
            )
            self.assertFalse(resumed.created)
            self.assertTrue(resumed.should_dispatch)
            self.assertEqual(resumed.review.reviewer_generation, 0)

            with self.assertRaises(lc.LifecycleError):
                store.complete_review(
                    "run1",
                    "build::review",
                    self.sha_a,
                    reviewer_generation=0,
                    verdict=st.ReviewVerdict.PASS,
                    review_digest="before-dispatch",
                    receipt_path="/receipts/before-dispatch",
                    findings=(),
                )

            dispatched = store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            replay = store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            self.assertIs(dispatched.state, st.CandidateReviewState.DISPATCHED)
            self.assertIsNotNone(dispatched.dispatched_at)
            self.assertEqual(replay, dispatched)
            self.assertFalse(
                store.begin_review(
                    "run1", "build::review", self.sha_a, reviewer_generation=0
                ).should_dispatch
            )

            completed = store.complete_review(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=0,
                verdict=st.ReviewVerdict.PASS,
                review_digest="digest-a",
                receipt_path="/receipts/a",
                findings=(),
            )
            late = store.complete_review(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=9,
                verdict=st.ReviewVerdict.PASS,
                review_digest="digest-late",
                receipt_path="/receipts/late",
                findings=(),
            )
            self.assertTrue(completed.completed)
            self.assertEqual(completed.review.dispatched_at, dispatched.dispatched_at)
            self.assertFalse(late.completed)
            self.assertEqual(late.review.verdict, st.ReviewVerdict.PASS)
            self.assertEqual(len(store.candidate_reviews("run1", "build::review")), 1)

    def test_recovery_advances_generation_and_republishes_unfinished_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=0)
            store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=4
            )
            store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=4
            )

            recovered = store.recover_review_dispatch(
                "run1",
                "build::review",
                self.sha_a,
                expected_reviewer_generation=4,
                reviewer_generation=5,
            )

            self.assertFalse(recovered.created)
            self.assertTrue(recovered.should_dispatch)
            self.assertEqual(recovered.review.reviewer_generation, 5)
            self.assertIs(recovered.review.state, st.CandidateReviewState.PUBLISHED)
            self.assertIsNone(recovered.review.dispatched_at)

    def test_rejection_and_handoff_are_one_durable_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=0)
            store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            rejected = store.reject_and_create_handoff(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=0,
                builder_generation=2,
                review_digest="digest-r",
                receipt_path="/receipts/r",
                findings=({"check_id": "C1", "message": "repair me"},),
            )
            self.assertTrue(rejected.completed)
            self.assertTrue(rejected.created)
            self.assertEqual(rejected.review.verdict, st.ReviewVerdict.REJECTED)
            self.assertEqual(rejected.handoff.state, st.RepairHandoffState.PENDING)
            self.assertEqual(
                store.candidate_review("run1", "build::review", self.sha_a).verdict,
                st.ReviewVerdict.REJECTED,
            )
            self.assertEqual(
                store.repair_handoff("run1", "build", self.sha_a).findings[0][
                    "check_id"
                ],
                "C1",
            )

    def test_handoff_acknowledgement_is_sha_and_current_generation_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=0)
            store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.reject_and_create_handoff(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=0,
                builder_generation=2,
                review_digest="digest-r",
                receipt_path="/receipts/r",
                findings=(),
            )
            store.register_actor_session(
                "run1",
                "build",
                "builder",
                generation=2,
                pane_id="pane-2",
                session_path="/sessions/builder-2",
                correlation_token="token-2",
            )
            self.assertTrue(
                store.mark_handoff_submitted(
                    "run1", "build", self.sha_a, builder_generation=2
                ).submitted
            )
            self.assertFalse(
                store.acknowledge_handoff(
                    "run1", "build", self.sha_a, builder_generation=1
                ).acknowledged
            )
            store.recover_actor_session(
                "run1",
                "build",
                "builder",
                expected_generation=2,
                generation=3,
                pane_id="pane-3",
                session_path="/sessions/builder-3",
                correlation_token="token-3",
            )
            self.assertFalse(
                store.acknowledge_handoff(
                    "run1", "build", self.sha_a, builder_generation=2
                ).acknowledged
            )
            with self.assertRaises(lc.LifecycleError):
                store.acknowledge_handoff(
                    "run1", "build", self.sha_b, builder_generation=3
                )

    def test_proven_absent_builder_rebinds_unfinished_handoff_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=0)
            store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.reject_and_create_handoff(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=0,
                builder_generation=2,
                review_digest="digest-r",
                receipt_path="/receipts/r",
                findings=(),
            )
            store.register_actor_session(
                "run1",
                "build",
                "builder",
                generation=2,
                pane_id="pane-2",
                session_path="/sessions/builder-2",
                correlation_token="token-2",
            )
            self.assertTrue(
                store.mark_handoff_submitted(
                    "run1", "build", self.sha_a, builder_generation=2
                ).submitted
            )

            recovered = store.recover_builder_handoff(
                "run1",
                "build",
                self.sha_a,
                expected_generation=2,
                generation=3,
                pane_id="pane-3",
                session_path="/sessions/builder-3",
                correlation_token="token-3",
            )

            self.assertTrue(recovered.recovered)
            self.assertEqual(recovered.session.generation, 3)
            handoff = store.repair_handoff("run1", "build", self.sha_a)
            self.assertEqual(handoff.builder_generation, 3)
            self.assertEqual(handoff.state, st.RepairHandoffState.PENDING)
            self.assertIsNone(handoff.submitted_at)
            sessions = store.actor_sessions("run1", "build", actor_role="builder")
            self.assertEqual(
                [(row.generation, row.state) for row in sessions],
                [(2, st.ActorSessionState.CLOSED), (3, st.ActorSessionState.ACTIVE)],
            )

    def test_closed_builder_rebinds_acknowledged_handoff_after_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=0)
            store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.reject_and_create_handoff(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=0,
                builder_generation=2,
                review_digest="digest-r",
                receipt_path="/receipts/r",
                findings=(),
            )
            store.register_actor_session(
                "run1",
                "build",
                "builder",
                generation=2,
                pane_id="pane-2",
                session_path="/sessions/builder-2",
                correlation_token="token-2",
            )
            store.mark_handoff_submitted(
                "run1", "build", self.sha_a, builder_generation=2
            )
            store.acknowledge_handoff("run1", "build", self.sha_a, builder_generation=2)
            store.close_actor_session("run1", "build", "builder", generation=2)

            recovered = store.recover_builder_handoff(
                "run1",
                "build",
                self.sha_a,
                expected_generation=2,
                generation=3,
                pane_id="pane-3",
                session_path="/sessions/builder-3",
                correlation_token="token-3",
            )

            self.assertTrue(recovered.recovered)
            handoff = store.repair_handoff("run1", "build", self.sha_a)
            self.assertEqual(handoff.builder_generation, 3)
            self.assertEqual(handoff.state, st.RepairHandoffState.PENDING)
            self.assertIsNone(handoff.submitted_at)
            self.assertIsNone(handoff.acknowledged_at)
            sessions = store.actor_sessions("run1", "build", actor_role="builder")
            self.assertEqual(
                [(row.generation, row.state) for row in sessions],
                [(2, st.ActorSessionState.CLOSED), (3, st.ActorSessionState.ACTIVE)],
            )

    def test_resume_reader_returns_persistent_review_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            self.assertTrue(
                store.set_lane_phase("run1", "build", st.LanePhase.BUILDING)
            )
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=0)
            store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=0
            )
            store.complete_review(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=0,
                verdict=st.ReviewVerdict.PASS,
                review_digest="digest-a",
                receipt_path="/receipts/a",
                findings=(),
            )
            reader = lc.LifecycleReader.open(store.db_path)
            try:
                self.assertEqual(
                    reader.nodes("run1")[0].lane_phase, st.LanePhase.BUILDING
                )
                self.assertEqual(len(reader.lane_candidates("run1", "build")), 1)
                self.assertEqual(
                    reader.candidate_reviews("run1", "build::review")[0].verdict,
                    st.ReviewVerdict.PASS,
                )
                self.assertEqual(reader.repair_handoffs("run1", "build"), ())
                self.assertEqual(reader.legacy_review_migrations("run1"), ())
            finally:
                reader.close()

    def test_unique_legacy_terminal_reviews_migrate_without_redispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.conn.execute(
                "UPDATE node_lifecycle SET output_sha=? WHERE run_id=? AND node_id=?",
                (self.sha_b, "run1", "build"),
            )
            evidence = (
                lc.LegacyReviewEvidence(
                    build_node_id="build",
                    candidate_seq=1,
                    candidate_sha=self.sha_a,
                    base_sha="c" * 40,
                    review_digest="d" * 64,
                    receipt_path="/receipts/legacy-a",
                    verdict=st.ReviewVerdict.REJECTED,
                    findings=({"check_id": "C1", "message": "repair"},),
                ),
                lc.LegacyReviewEvidence(
                    build_node_id="build",
                    candidate_seq=2,
                    candidate_sha=self.sha_b,
                    base_sha="c" * 40,
                    review_digest="e" * 64,
                    receipt_path="/receipts/legacy-b",
                    verdict=st.ReviewVerdict.PASS,
                    findings=(),
                ),
            )
            migrated = store.migrate_legacy_inline_reviews(
                "run1",
                evidence,
                evidence_validator=lambda _item: True,
                ancestry_validator=lambda parent, child: (
                    parent == self.sha_a and child == self.sha_b
                ),
            )
            self.assertTrue(migrated[0].migrated)
            self.assertFalse(migrated[0].blocked)
            self.assertEqual(
                [candidate.candidate_sha for candidate in migrated[0].candidates],
                [self.sha_a, self.sha_b],
            )
            self.assertEqual(
                store.repair_handoff("run1", "build", self.sha_a).state,
                st.RepairHandoffState.PENDING,
            )
            resumed = store.begin_review(
                "run1", "build::review", self.sha_b, reviewer_generation=8
            )
            self.assertFalse(resumed.should_dispatch)
            self.assertEqual(resumed.review.verdict, st.ReviewVerdict.PASS)
            replayed = store.migrate_legacy_inline_reviews(
                "run1",
                evidence,
                evidence_validator=lambda _item: True,
                ancestry_validator=lambda parent, child: (
                    parent == self.sha_a and child == self.sha_b
                ),
            )
            self.assertFalse(replayed[0].migrated)
            self.assertFalse(replayed[0].blocked)

    def test_canonical_review_ledger_supersedes_stale_legacy_attempt_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.publish_candidate("run1", "build", self.sha_a, builder_generation=30)
            store.begin_review(
                "run1", "build::review", self.sha_a, reviewer_generation=30
            )
            store.mark_review_dispatched(
                "run1", "build::review", self.sha_a, reviewer_generation=30
            )
            store.complete_review(
                "run1",
                "build::review",
                self.sha_a,
                reviewer_generation=30,
                verdict=st.ReviewVerdict.PASS,
                review_digest="canonical-review",
                receipt_path="/receipts/canonical",
                findings=(),
            )
            store.conn.execute(
                "INSERT INTO legacy_review_migration_blocks"
                " (run_id, build_node_id, reason, detail_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    "run1",
                    "build",
                    "LEGACY_REVIEW_LEDGER_CONFLICT",
                    "{}",
                    "2026-08-25T00:00:00+00:00",
                ),
            )

            resumed = store.migrate_legacy_inline_reviews(
                "run1",
                (
                    lc.LegacyReviewEvidence(
                        "build",
                        1,
                        self.sha_b,
                        "c" * 40,
                        "d" * 64,
                        "/receipts/stale",
                        st.ReviewVerdict.REJECTED,
                        (),
                    ),
                ),
                evidence_validator=lambda _item: self.fail(
                    "canonical ledgers must not revalidate legacy evidence"
                ),
            )

            self.assertFalse(resumed[0].migrated)
            self.assertFalse(resumed[0].blocked)
            self.assertEqual(resumed[0].candidates[0].candidate_sha, self.sha_a)
            self.assertIs(resumed[0].reviews[0].verdict, st.ReviewVerdict.PASS)
            self.assertEqual(store.legacy_review_migrations("run1"), ())

    def test_duplicate_or_mismatched_legacy_evidence_blocks_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.conn.execute(
                "UPDATE node_lifecycle SET output_sha=? WHERE run_id=? AND node_id=?",
                (self.sha_a, "run1", "build"),
            )
            duplicate = store.migrate_legacy_inline_reviews(
                "run1",
                (
                    lc.LegacyReviewEvidence(
                        "build",
                        1,
                        self.sha_a,
                        "c" * 40,
                        "d" * 64,
                        "/receipts/a",
                        st.ReviewVerdict.PASS,
                        (),
                    ),
                    lc.LegacyReviewEvidence(
                        "build",
                        2,
                        self.sha_a,
                        "c" * 40,
                        "e" * 64,
                        "/receipts/a-again",
                        st.ReviewVerdict.PASS,
                        (),
                    ),
                ),
                evidence_validator=lambda _item: True,
                ancestry_validator=lambda _parent, _child: True,
            )
            self.assertTrue(duplicate[0].blocked)
            self.assertEqual(duplicate[0].reason, "LEGACY_CANDIDATE_DUPLICATE")
            self.assertEqual(store.lane_candidates("run1", "build"), ())
            with self.assertRaises(lc.LegacyReviewMigrationBlocked):
                store.begin_review(
                    "run1", "build::review", self.sha_a, reviewer_generation=0
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.conn.execute(
                "UPDATE node_lifecycle SET output_sha=? WHERE run_id=? AND node_id=?",
                (self.sha_a, "run1", "build"),
            )
            mismatch = store.migrate_legacy_inline_reviews(
                "run1",
                (
                    lc.LegacyReviewEvidence(
                        "build",
                        1,
                        self.sha_b,
                        "c" * 40,
                        "d" * 64,
                        "/receipts/b",
                        st.ReviewVerdict.PASS,
                        (),
                    ),
                ),
                evidence_validator=lambda _item: True,
            )
            self.assertTrue(mismatch[0].blocked)
            self.assertEqual(mismatch[0].reason, "LEGACY_CANDIDATE_SHA_MISMATCH")
            self.assertEqual(
                store.legacy_review_migrations("run1")[0].build_node_id, "build"
            )

    def test_unproven_legacy_candidate_ancestry_blocks_the_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            store.conn.execute(
                "UPDATE node_lifecycle SET output_sha=? WHERE run_id=? AND node_id=?",
                (self.sha_b, "run1", "build"),
            )
            blocked = store.migrate_legacy_inline_reviews(
                "run1",
                (
                    lc.LegacyReviewEvidence(
                        "build",
                        1,
                        self.sha_a,
                        "c" * 40,
                        "d" * 64,
                        "/receipts/a",
                        st.ReviewVerdict.REJECTED,
                        (),
                    ),
                    lc.LegacyReviewEvidence(
                        "build",
                        2,
                        self.sha_b,
                        "c" * 40,
                        "e" * 64,
                        "/receipts/b",
                        st.ReviewVerdict.PASS,
                        (),
                    ),
                ),
                evidence_validator=lambda _item: True,
                ancestry_validator=lambda _parent, _child: False,
            )
            self.assertTrue(blocked[0].blocked)
            self.assertEqual(blocked[0].reason, "LEGACY_CANDIDATE_ANCESTRY_UNPROVEN")
            self.assertEqual(store.candidate_reviews("run1", "build::review"), ())


# ── §7.9 one transaction per transition ─────────────────────────────────────


class TransitionTransactionTests(unittest.TestCase):
    def test_start_attempt_writes_lifecycle_attempt_transition_and_runs_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            before = store.conn.execute(
                "SELECT last_transition_at FROM runs WHERE run_id=?", ("run1",)
            ).fetchone()[0]
            attempt_no = store.start_attempt("run1", "a", base_sha="deadbeef")

            self.assertEqual(attempt_no, 1)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.RUNNING)
            attempt_row = store.conn.execute(
                "SELECT base_sha FROM attempts WHERE run_id=? AND node_id=? AND attempt_no=?",
                ("run1", "a", 1),
            ).fetchone()
            self.assertEqual(attempt_row[0], "deadbeef")
            transition_row = store.conn.execute(
                "SELECT from_state, to_state, actor FROM transitions"
                " WHERE run_id=? AND node_id=? ORDER BY id DESC LIMIT 1",
                ("run1", "a"),
            ).fetchone()
            self.assertEqual(transition_row, ("PENDING", "RUNNING", "scheduler"))
            after = store.conn.execute(
                "SELECT last_transition_at FROM runs WHERE run_id=?", ("run1",)
            ).fetchone()[0]
            # ISO-8601 strings sort lexicographically; >= tolerates two writes
            # landing in the same millisecond while still proving the refresh ran.
            self.assertGreaterEqual(after, before)

    def test_start_attempt_advances_past_retained_cancelled_rows(self):
        """Recovery may move the pointer back; durable attempt ids never do."""
        for pointer, durable_max, expected in ((1, 2, 3), (3, 4, 5)):
            with self.subTest(pointer=pointer, durable_max=durable_max):
                with tempfile.TemporaryDirectory() as tmp:
                    store = new_store(Path(tmp))
                    store.create_run("run1", "d", [make_node("a", 0)])
                    for attempt_no in range(1, durable_max + 1):
                        store.conn.execute(
                            "INSERT INTO attempts "
                            "(run_id,node_id,attempt_no,base_sha,state,"
                            " started_at,turn_count,extra_json)"
                            " VALUES (?,?,?,?,?,?,0,'{}')",
                            (
                                "run1",
                                "a",
                                attempt_no,
                                "old",
                                st.NodeState.CANCELLED.value,
                                time.time(),
                            ),
                        )
                    store.conn.execute(
                        "UPDATE node_lifecycle SET attempt_no=?"
                        " WHERE run_id=? AND node_id=?",
                        (pointer, "run1", "a"),
                    )

                    attempt_no = store.start_attempt("run1", "a", base_sha="new")

                    self.assertEqual(attempt_no, expected)
                    self.assertEqual(
                        store.get_attempt("run1", "a", expected).base_sha, "new"
                    )

    def test_mark_launched_preserves_transcript_metadata_across_rearming(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            attempt_no = store.start_attempt("run1", "a", base_sha="deadbeef")
            store.mark_launched(
                "run1",
                "a",
                attempt_no,
                None,
                extra={wd.SESSION_PATH_KEY: "/tmp/session.jsonl"},
            )
            store.mark_launched("run1", "a", attempt_no, None)

            attempt = store.attempts_for("run1", "a")[0]
            self.assertEqual(attempt.extra[wd.SESSION_PATH_KEY], "/tmp/session.jsonl")
            self.assertIsNotNone(attempt.launched_at)

    def test_a_refused_transition_writes_nothing(self):
        """The guard fires before any write — a failed transition leaves no residue."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            with self.assertRaises(lc.LifecycleError):
                store.mark_merged(
                    "run1", "a"
                )  # PENDING -> MERGED is not legal directly
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            count = store.conn.execute(
                "SELECT COUNT(*) FROM transitions WHERE run_id=?", ("run1",)
            ).fetchone()[0]
            self.assertEqual(count, 0)


# ── §7.3 the legal transition guard ─────────────────────────────────────────


class LegalTransitionGuardTests(unittest.TestCase):
    def _run_to_merged(self, store, run_id="run1", node_id="a"):
        store.create_run(run_id, "d", [make_node(node_id, 0)])
        store.start_attempt(run_id, node_id, base_sha="s1")
        store.mark_verified(run_id, node_id, output_sha="sha1")
        store.mark_merged(run_id, node_id)
        return store

    def test_merged_is_absolutely_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._run_to_merged(new_store(Path(tmp)))
            with self.assertRaises(lc.LifecycleError):
                store.mark_blocked("run1", "a", st.BlockReason.MERGE_CONFLICT)
            # Exercise the terminal guard itself, isolated from the escape-legality
            # gate (a MERGED-only run already declares ACCEPTED, which refuses
            # escapes for its own reason — both refusals agree MERGED cannot move).
            with self.assertRaises(lc.IllegalTransition):
                store._transition_node(
                    "run1",
                    "a",
                    st.NodeState.CANCELLED,
                    actor="operator",
                    reason="abandon",
                )

    def test_cancelled_is_absolutely_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.declare_outcome("run1")  # quiesce so the escape is legal (§7.3)
            store.abandon("run1", "a")
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.CANCELLED)
            with self.assertRaises(lc.LifecycleError):
                store.start_attempt("run1", "a", base_sha="s2")

    def test_blocked_admits_no_automatic_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            # An automatic (scheduler-actor) attempt to leave BLOCKED is refused —
            # only the operator escapes (exercised below) may.
            with self.assertRaises(lc.LifecycleError):
                store._transition_node(
                    "run1",
                    "a",
                    st.NodeState.PENDING,
                    actor="scheduler",
                    reason="not-a-real-escape",
                )

    def test_blocked_admits_the_operator_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked(
                "run1", "a", st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED
            )
            store.declare_outcome("run1")  # quiesce so the escape is legal (§7.3)
            store.retry("run1", "a")
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)


# ── §7.1, §8.7 ready set and the reversible cascade ─────────────────────────


class ReadySetAndCascadeTests(unittest.TestCase):
    def _diamond(self, store):
        # a -> b, a -> c, {b, c} -> d
        store.create_run(
            "run1",
            "d",
            [
                make_node("a", 0),
                make_node("b", 1, needs=("a",)),
                make_node("c", 1, needs=("a",)),
                make_node("d", 2, needs=("b", "c")),
            ],
        )

    def test_ready_set_is_pending_nodes_whose_deps_are_all_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self._diamond(store)
            self.assertEqual(store.ready_nodes("run1"), ("a",))
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged("run1", "a")
            self.assertEqual(
                store.ready_nodes("run1"), ("b", "c")
            )  # sorted (depth, node_id)

    def test_ready_set_predicate_is_merged_never_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self._diamond(store)
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            # a is VERIFIED, not MERGED yet — its dependents must not be ready.
            self.assertEqual(store.ready_nodes("run1"), ())

    def test_cascade_is_derived_and_reversible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self._diamond(store)
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged("run1", "a")
            store.start_attempt("run1", "b", base_sha="sha_a")
            store.mark_blocked("run1", "b", st.BlockReason.MERGE_CONFLICT)

            blocked_descendants = store.upstream_blocked("run1")
            self.assertIn(
                "d", blocked_descendants
            )  # d needs b (and c) — derived-unready

            store.declare_outcome("run1")
            store.retry(
                "run1", "b"
            )  # rescue the origin — no un-cascade rule exists anywhere
            self.assertNotIn("d", store.upstream_blocked("run1"))


# ── §7.3 the total run outcome function ─────────────────────────────────────


class RunOutcomeTotalityTests(unittest.TestCase):
    def test_outcome_is_total_over_an_enumerated_grid(self):
        """Every combination of two nodes' states, stuck, cancel_requested, and
        acceptance_result must produce exactly one of the four outcomes — never
        raise, never return anything else. This includes combinations nobody
        designed for (§7.3's residual-class claim)."""
        states = list(st.NodeState)
        for s1, s2, stuck, cancel_requested, acceptance in itertools.product(
            states, states, (True, False), (True, False), (None, True, False)
        ):
            node_states = [("a", s1, None), ("b", s2, None)]
            report = lc.total_run_outcome(
                node_states,
                stuck=stuck,
                cancel_requested=cancel_requested,
                acceptance_result=acceptance,
            )
            self.assertIsInstance(report, lc.OutcomeReport)
            self.assertIn(report.outcome, set(st.RunOutcome))

    def test_stuck_wins_even_with_work_in_flight(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.RUNNING, None)],
            stuck=True,
            cancel_requested=False,
            acceptance_result=None,
        )
        self.assertEqual(report.outcome, st.RunOutcome.STUCK)

    def test_cancel_requested_yields_cancelled(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None)],
            stuck=False,
            cancel_requested=True,
            acceptance_result=None,
        )
        self.assertEqual(report.outcome, st.RunOutcome.CANCELLED)

    def test_every_node_cancelled_yields_cancelled_without_the_flag(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.CANCELLED, None), ("b", st.NodeState.CANCELLED, None)],
            stuck=False,
            cancel_requested=False,
            acceptance_result=None,
        )
        self.assertEqual(report.outcome, st.RunOutcome.CANCELLED)

    def test_accepted_requires_a_merge_no_stragglers_and_a_green_acceptance(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None), ("b", st.NodeState.CANCELLED, None)],
            stuck=False,
            cancel_requested=False,
            acceptance_result=True,
        )
        self.assertEqual(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(report.abandoned_nodes, ("b",))
        self.assertIs(report.acceptance_result, True)

    def test_accepted_records_the_green_acceptance_result(self):
        """G3 — ACCEPTED keys on `acceptance_result is True`, then used to
        construct the report without that field, so the ledger persisted
        null. The r7 `acceptance_result: null` is this, not a skipped gate."""
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None)],
            stuck=False,
            cancel_requested=False,
            acceptance_result=True,
        )
        self.assertEqual(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertIs(report.acceptance_result, True)

    def test_accepted_refused_when_acceptance_is_not_green(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None)],
            stuck=False,
            cancel_requested=False,
            acceptance_result=False,
        )
        self.assertEqual(report.outcome, st.RunOutcome.BLOCKED)

    def test_some_merged_some_blocked_is_blocked_not_undefined(self):
        report = lc.total_run_outcome(
            [
                ("a", st.NodeState.MERGED, None),
                ("b", st.NodeState.BLOCKED, st.BlockReason.MERGE_CONFLICT),
            ],
            stuck=False,
            cancel_requested=False,
            acceptance_result=None,
        )
        self.assertEqual(report.outcome, st.RunOutcome.BLOCKED)
        self.assertEqual(report.blocked_nodes, ("b",))
        self.assertEqual(report.block_reasons["b"], st.BlockReason.MERGE_CONFLICT)

    def test_blocked_is_the_residual_class_for_the_empty_run(self):
        report = lc.total_run_outcome(
            [], stuck=False, cancel_requested=False, acceptance_result=None
        )
        self.assertEqual(report.outcome, st.RunOutcome.BLOCKED)


# ── §7.3 outcome as a record; NULL, resume, and escape legality ────────────


class OutcomeRecordAndLegalityTests(unittest.TestCase):
    def test_latest_outcome_is_null_until_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            self.assertIsNone(store.latest_outcome("run1"))

    def test_declaring_twice_keeps_only_the_latest_and_the_history_lives_in_transitions(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            store.declare_outcome("run1")
            self.assertEqual(store.latest_outcome("run1"), st.RunOutcome.BLOCKED)
            store.retry("run1", "a")
            store.start_attempt("run1", "a", base_sha="s2")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged("run1", "a")
            store.declare_outcome("run1", acceptance_result=True)
            self.assertEqual(store.latest_outcome("run1"), st.RunOutcome.ACCEPTED)
            # Only one outcome row on `runs` — the audit history is in `transitions`.
            declared = store.conn.execute(
                "SELECT COUNT(*) FROM transitions WHERE run_id=? AND to_state IN"
                " ('BLOCKED','ACCEPTED','CANCELLED','STUCK') AND node_id IS NULL",
                ("run1",),
            ).fetchone()[0]
            self.assertEqual(declared, 2)

    def test_declare_outcome_persists_the_green_acceptance_on_accepted(self):
        """G3 — `declare_outcome` writes `report.acceptance_result` into the
        transition detail. Before the ACCEPTED arm carried the field, this
        was null on a green run."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged("run1", "a")
            report = store.declare_outcome("run1", acceptance_result=True)
            self.assertEqual(report.outcome, st.RunOutcome.ACCEPTED)
            self.assertIs(report.acceptance_result, True)
            declared = [
                row
                for row in store.audit_transitions("run1")
                if row.get("reason") == "declare-outcome"
            ]
            self.assertEqual(len(declared), 1)
            self.assertIs(declared[0]["detail"]["acceptance_result"], True)

    def test_resume_is_legal_against_blocked_stuck_and_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.resume_run("run1")  # NULL — nobody ever declared

            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            store.declare_outcome("run1")
            store.resume_run("run1")  # BLOCKED

    def test_resume_is_refused_against_accepted_and_against_abandonment(self):
        """The two runs that reached a result. `ACCEPTED` reached the run's
        declared outcome; a run abandoned node by node had every node
        individually adjudicated as work it should finish without. A run
        stopped by `run cancel` adjudicated nothing and is resumable —
        tests/test_run_cancel_resume.py owns that half."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged("run1", "a")
            store.declare_outcome("run1", acceptance_result=True)
            with self.assertRaises(lc.LifecycleError):
                store.resume_run("run1")

            store2 = new_store(Path(tmp) / "second")
            store2.create_run("run1", "d", [make_node("a", 0)])
            store2.start_attempt("run1", "a", base_sha="s1")
            store2.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            store2.declare_outcome("run1")
            store2.abandon("run1", "a")
            store2.declare_outcome("run1")
            self.assertEqual(store2.latest_outcome("run1"), st.RunOutcome.CANCELLED)
            with self.assertRaises(lc.LifecycleError):
                store2.resume_run("run1")

    def test_escapes_are_refused_against_a_null_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            # No declare_outcome() call — the scheduler may still be alive.
            with self.assertRaises(lc.LifecycleError):
                store.retry("run1", "a")
            with self.assertRaises(lc.LifecycleError):
                store.abandon("run1", "a")

    def test_resume_refreshes_last_transition_at_before_touching_inherited_attempts(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt(
                "run1", "a", base_sha="s1"
            )  # left RUNNING — a crashed scheduler
            # A recorded turn, so this exercises the charged closure. Without
            # evidence the attempt is released UNCLASSIFIED and the reason
            # below is `resume:no-evidence-recorded` rather than `retry:`.
            store.record_heartbeat(
                store.get_attempt("run1", "a", 1), turn_count=1, observed_at=1.0
            )
            reclaimed = store.resume_run("run1")
            self.assertEqual(reclaimed, ("a",))
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            rows = store.conn.execute(
                "SELECT node_id, reason FROM transitions WHERE run_id=? ORDER BY id",
                ("run1",),
            ).fetchall()
            # The resume transition (node_id NULL) precedes the reclaimed attempt's transition.
            resume_idx = next(i for i, r in enumerate(rows) if r[1] == "resume")
            reclaim_idx = next(
                i
                for i, r in enumerate(rows)
                if r[0] == "a" and i > 0 and r[1].startswith("retry:")
            )
            self.assertLess(resume_idx, reclaim_idx)

    def test_resume_retains_an_inherited_attempt_that_declared_a_result(self):
        """A durable result resumes completion on the same attempt.

        The attempt already finished its authored turn. Closing it and
        creating another generation repeats work; late-envelope recovery
        instead re-enters candidate/review/handoff reconciliation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.record_result(
                "run1",
                st.ResultRecord(
                    node_id="a",
                    attempt_no=1,
                    subject_sha="s1",
                    payload={"status": "success"},
                    adjudication=st.Adjudication.ACCEPTED,
                ),
            )

            reclaimed = store.resume_run("run1")

            self.assertEqual(reclaimed, ("a",))
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 0
            )
            attempt = store.get_attempt("run1", "a", 1)
            self.assertEqual(attempt.state, lc.CLOSED_ATTEMPT_STATE)
            self.assertIsNone(attempt.retry_class)
            self.assertIs(attempt.extra[lc.LATE_ENVELOPE_RECOVERY_KEY], True)
            self.assertEqual(len(store.audit_results("run1", "a")), 1)
            self.assertEqual(len(store.audit_orphans("run1")), 1)

    def test_late_envelope_claim_opens_a_fresh_watchdog_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.record_result(
                "run1",
                st.ResultRecord(
                    node_id="a",
                    attempt_no=1,
                    subject_sha="s1",
                    payload={"status": "success"},
                    adjudication=st.Adjudication.ACCEPTED,
                ),
            )
            store.resume_run("run1")
            store.conn.execute(
                "UPDATE attempts SET started_at=1, launched_at=2, pid=3,"
                " attempt_host='old', attempt_start_epoch=4, turn_count=5"
                " WHERE run_id='run1' AND node_id='a' AND attempt_no=1"
            )

            store.claim_late_envelope_attempt("run1", "a", 1)

            attempt = store.get_attempt("run1", "a", 1)
            self.assertGreater(attempt.started_at, 1)
            self.assertIsNone(attempt.launched_at)
            self.assertIsNone(attempt.pid)
            self.assertIsNone(attempt.attempt_host)
            self.assertIsNone(attempt.attempt_start_epoch)
            self.assertEqual(attempt.turn_count, 0)

    def test_resume_still_charges_an_attempt_that_took_a_turn_and_then_died(self):
        """The inverse, and the backstop this change must not remove: an agent
        that took a turn and then died is a fact about the environment, so it
        is charged ENVIRONMENTAL exactly as it always was. The budget still
        protects against a genuinely broken environment."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_launched("run1", "a", 1, pid=None)
            store.record_heartbeat(
                store.get_attempt("run1", "a", 1), turn_count=1, observed_at=1.0
            )

            store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 1
            )
            self.assertIs(
                store.get_attempt("run1", "a", 1).retry_class,
                st.RetryClass.ENVIRONMENTAL,
            )

    def test_resume_releases_an_attempt_that_never_acquired_launch_identity(self):
        """§7.5, §16.3 item 136 — an operator's restart is not a fact about
        the environment.

        `mark_launched` never fired for this attempt, so it has no
        `launched_at`, no pid, no host and no start epoch — and therefore no
        turn, no result and no sealed output either. Missing identity is not
        the predicate any more, but it is still sufficient for it: an attempt
        that never launched cannot have recorded evidence. Charging it
        ENVIRONMENTAL records a judgement about a failure nobody saw, and
        spends a budget on it. The row is still closed — that part is not
        optional — but UNCLASSIFIED.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            self.assertIsNone(store.get_attempt("run1", "a", 1).launched_at)

            reclaimed = store.resume_run("run1")

            self.assertEqual(reclaimed, ("a",))
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            # The charge that must not happen.
            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 0
            )
            attempt = store.get_attempt("run1", "a", 1)
            self.assertIsNone(attempt.retry_class)
            # ...and the closure that must still happen (§10.3, §7.6, §7.7).
            self.assertEqual(attempt.state, lc.CLOSED_ATTEMPT_STATE)
            # The pane is still recorded before the row is closed (§7.8).
            self.assertEqual(len(store.audit_orphans("run1")), 1)
            # And the node is genuinely re-launchable.
            self.assertEqual(store.start_attempt("run1", "a", base_sha="s2"), 2)

    def test_the_release_is_a_typed_transition_naming_why_it_cost_nothing(self):
        """§1.2 — the durable record says which costless closure this was, so
        the reason an attempt carries no class is readable from the ledger
        rather than inferred from an absent column."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")

            store.resume_run("run1")

            reasons = [
                row[0]
                for row in store.conn.execute(
                    "SELECT reason FROM transitions"
                    " WHERE run_id=? AND node_id=? ORDER BY id",
                    ("run1", "a"),
                ).fetchall()
            ]
            self.assertIn("resume:no-evidence-recorded", reasons)
            self.assertEqual([r for r in reasons if r.startswith("retry:")], [])

    def test_no_evidence_resume_clears_a_terminal_lane_phase(self):
        """Second live hang: resume:no-evidence-recorded left lane_phase
        BLOCKED, so the fresh attempt's BUILDING CAS stranded RUNNING.
        Without the clear, `set_lane_phase(BUILDING)` returns False."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            self.assertTrue(
                store.set_lane_phase("run1", "a", st.LanePhase.BLOCKED)
            )
            store.resume_run("run1")
            self.assertIsNone(store.get_node("run1", "a").lane_phase)
            store.start_attempt("run1", "a", base_sha="s2")
            self.assertTrue(
                store.set_lane_phase("run1", "a", st.LanePhase.BUILDING)
            )

    def test_repeated_restarts_cannot_exhaust_a_budget_they_did_not_spend(self):
        """The incident, in miniature: `lane-wp6-tests` in
        `run-8a200af7f9044ce7a11a51b6908f37e3` reached 2 of 2 environmental
        spend where half the spend was the operator restarting the scheduler.
        Three restarts over an attempt that never launched must leave the
        budget untouched, not exhaust it."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            for base in ("s1", "s2", "s3"):
                store.start_attempt("run1", "a", base_sha=base)
                store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 0
            )
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)

    def test_charging_and_releasing_are_told_apart_by_recorded_evidence_alone(self):
        """The discriminator, stated as one comparison: two runs identical in
        every respect — both launched, both with a pane and a transcript path —
        except that one attempt took a turn. Only that one is charged.

        Launch identity is held constant precisely because it is no longer the
        discriminating fact. Were it still the predicate, both columns would
        read 1 and this comparison could not tell them apart at all."""
        with tempfile.TemporaryDirectory() as tmp:
            spent = {}
            for name, took_turn in (("took-a-turn", True), ("no-evidence", False)):
                root = Path(tmp) / name
                root.mkdir()
                store = new_store(root)
                self.addCleanup(store.close)
                store.create_run("run1", "d", [make_node("a", 0)])
                store.start_attempt("run1", "a", base_sha="s1")
                store.mark_launched(
                    "run1",
                    "a",
                    1,
                    pid=None,
                    extra={wd.SESSION_PATH_KEY: "/tmp/session.jsonl"},
                )
                if took_turn:
                    store.record_heartbeat(
                        store.get_attempt("run1", "a", 1),
                        turn_count=1,
                        observed_at=1.0,
                    )
                store.resume_run("run1")
                spent[name] = store.attempts_spent(
                    "run1", "a", st.RetryClass.ENVIRONMENTAL
                )
            self.assertEqual(spent, {"took-a-turn": 1, "no-evidence": 0})

    def test_a_pane_and_a_transcript_path_are_not_evidence_of_a_failure(self):
        """The a1/a6/a7 shape from `run-8a200af7f9044ce7a11a51b6908f37e3`, and
        the reason this predicate is not keyed on launch identity.

        The agent runner writes `launched_at` and the transcript path the
        instant herdr reports the pane — 30s to 2min before the prompt is even
        submitted — so an attempt the operator's restart killed in that window
        has full launch identity and has still done nothing. `pid` is None
        because the pane's foreground group is only meaningful after
        submission. There is no turn, no result, no sealed output: nothing to
        base a verdict on, and so no charge.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_launched(
                "run1",
                "a",
                1,
                pid=None,
                extra={wd.SESSION_PATH_KEY: "/tmp/session.jsonl"},
            )
            armed = store.get_attempt("run1", "a", 1)
            # The fixture is the point: identity present, evidence absent.
            self.assertIsNotNone(armed.launched_at)
            self.assertEqual(armed.extra[wd.SESSION_PATH_KEY], "/tmp/session.jsonl")
            self.assertEqual(armed.turn_count, 0)

            store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 0
            )
            attempt = store.get_attempt("run1", "a", 1)
            self.assertIsNone(attempt.retry_class)
            self.assertEqual(attempt.state, lc.CLOSED_ATTEMPT_STATE)

    def test_three_restarts_over_a_paned_attempt_cannot_exhaust_the_budget(self):
        """The incident's arithmetic, on the shape it actually had. Attempts
        a1, a6 and a7 each held a pane and each was killed by a restart; they
        drove `lane-wp6-tests` to 2 of 2 environmental spend. Keyed on
        evidence, the three cost nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            for attempt_no, base in enumerate(("s1", "s2", "s3"), start=1):
                store.start_attempt("run1", "a", base_sha=base)
                store.mark_launched(
                    "run1",
                    "a",
                    attempt_no,
                    pid=None,
                    extra={wd.SESSION_PATH_KEY: "/tmp/session.jsonl"},
                )
                store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 0
            )
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)

    def test_resume_charges_an_inherited_attempt_whose_result_was_superseded(self):
        """Only ACCEPTED spares the attempt. A SUPERSEDED row names an
        attempt that was no longer the live one when its result landed, so
        it is not this generation's declared work and must not exempt it."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.record_result(
                "run1",
                st.ResultRecord(
                    node_id="a",
                    attempt_no=1,
                    subject_sha="s1",
                    payload={"status": "success"},
                    adjudication=st.Adjudication.SUPERSEDED,
                ),
            )

            store.resume_run("run1")

            self.assertEqual(
                store.attempts_spent("run1", "a", st.RetryClass.ENVIRONMENTAL), 1
            )


# ── §7.8 cancellation ────────────────────────────────────────────────────────


class CancellationTests(unittest.TestCase):
    def test_cancel_run_writes_cancelled_for_every_non_terminal_node_in_one_transaction(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run(
                "run1",
                "d",
                [make_node("a", 0), make_node("b", 1, needs=("a",)), make_node("c", 0)],
            )
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged(
                "run1", "a"
            )  # a is MERGED — absolutely terminal, untouched

            cancelled = store.cancel_run("run1")
            self.assertEqual(set(cancelled), {"b", "c"})
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.MERGED)
            self.assertEqual(store.get_node("run1", "b").state, st.NodeState.CANCELLED)
            self.assertEqual(store.get_node("run1", "c").state, st.NodeState.CANCELLED)

    def test_cancel_run_never_blocks_and_declares_cancelled_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            report = store.declare_outcome("run1")
            self.assertEqual(report.outcome, st.RunOutcome.CANCELLED)

    def test_a_plain_run_cancel_stays_reopenable(self):
        """The default cause is what makes `run resume` legal (§7.8)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1")
            report = store.declare_outcome("run1")
            self.assertIs(report.cancel_cause, st.CancelCause.RUN_CANCEL)
            self.assertEqual(
                _node_cancel_cause(store, "run1", "a"), st.CancelCause.RUN_CANCEL.value
            )
            store.resume_run("run1")
            self.assertIs(store.get_node("run1", "a").state, st.NodeState.PENDING)

    def test_run_cancel_resume_restores_rejected_candidate_handoff_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            candidate_sha = "a" * 40
            store.start_attempt("run1", "build", base_sha="base")
            store.publish_candidate(
                "run1", "build", candidate_sha=candidate_sha, builder_generation=1
            )
            store.begin_review(
                "run1", "build::review", candidate_sha, reviewer_generation=1
            )
            store.mark_review_dispatched(
                "run1", "build::review", candidate_sha, reviewer_generation=1
            )
            store.reject_and_create_handoff(
                "run1",
                "build::review",
                candidate_sha,
                reviewer_generation=1,
                builder_generation=1,
                review_digest="d" * 64,
                receipt_path="/tmp/review.json",
                findings=(
                    {
                        "check_id": "diff.correctness",
                        "grade": "error",
                        "message": "candidate is incorrect",
                    },
                ),
            )
            self.assertTrue(
                store.set_lane_phase("run1", "build", st.LanePhase.REPAIR_HANDOFF)
            )
            store.cancel_run("run1")
            store.declare_outcome("run1")

            store.resume_run("run1")

            node = store.get_node("run1", "build")
            self.assertIs(node.state, st.NodeState.PENDING)
            self.assertIs(node.lane_phase, st.LanePhase.REPAIR_HANDOFF)
            handoff = store.repair_handoff("run1", "build", candidate_sha)
            self.assertIsNotNone(handoff)
            self.assertIs(handoff.state, st.RepairHandoffState.PENDING)

    def test_retry_budget_resume_recovers_declared_candidate_without_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            _review_lane(store)
            candidate_sha = "b" * 40
            store.start_attempt("run1", "build", base_sha="base")
            store.record_result(
                "run1",
                st.ResultRecord(
                    node_id="build",
                    attempt_no=1,
                    subject_sha=candidate_sha,
                    payload={"status": "success"},
                    adjudication=st.Adjudication.ACCEPTED,
                ),
            )
            store.publish_candidate(
                "run1", "build", candidate_sha=candidate_sha, builder_generation=1
            )
            store.begin_review(
                "run1", "build::review", candidate_sha, reviewer_generation=1
            )
            store.mark_review_dispatched(
                "run1", "build::review", candidate_sha, reviewer_generation=1
            )
            store.reject_and_create_handoff(
                "run1",
                "build::review",
                candidate_sha,
                reviewer_generation=1,
                builder_generation=1,
                review_digest="d" * 64,
                receipt_path="/tmp/review.json",
                findings=(
                    {
                        "check_id": "diff.correctness",
                        "grade": "error",
                        "message": "candidate is incorrect",
                    },
                ),
            )
            store.set_lane_phase("run1", "build", st.LanePhase.BLOCKED)
            store.mark_blocked(
                "run1",
                "build",
                st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
                retry_class=st.RetryClass.ENVIRONMENTAL,
            )
            store.declare_outcome("run1")

            store.resume_run("run1", late_envelope_attempts=(("build", 1),))

            node = store.get_node("run1", "build")
            self.assertIs(node.state, st.NodeState.PENDING)
            self.assertIs(node.lane_phase, st.LanePhase.REPAIRING)
            attempt = store.get_attempt("run1", "build", 1)
            self.assertIs(attempt.state, lc.CLOSED_ATTEMPT_STATE)
            self.assertIs(attempt.extra[lc.LATE_ENVELOPE_RECOVERY_KEY], True)

            claimed = store.claim_late_envelope_attempt("run1", "build", 1)

            self.assertIs(claimed.state, st.NodeState.RUNNING)
            self.assertEqual(claimed.attempt_no, 1)
            self.assertIs(
                store.get_attempt("run1", "build", 1).state,
                st.NodeState.RUNNING,
            )

    def test_review_grant_recovers_retained_candidate_without_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self.addCleanup(store.close)
            _review_lane(store)
            candidate_sha = "c" * 40
            store.start_attempt("run1", "build", base_sha="base")
            store.record_result(
                "run1",
                st.ResultRecord(
                    node_id="build",
                    attempt_no=1,
                    subject_sha=candidate_sha,
                    payload={"status": "success"},
                    adjudication=st.Adjudication.ACCEPTED,
                ),
            )
            store.publish_candidate(
                "run1", "build", candidate_sha=candidate_sha, builder_generation=1
            )
            store.begin_review(
                "run1", "build::review", candidate_sha, reviewer_generation=1
            )
            store.mark_review_dispatched(
                "run1", "build::review", candidate_sha, reviewer_generation=1
            )
            store.reject_and_create_handoff(
                "run1",
                "build::review",
                candidate_sha,
                reviewer_generation=1,
                builder_generation=1,
                review_digest="d" * 64,
                receipt_path="/tmp/review.json",
                findings=(
                    {
                        "check_id": "diff.correctness",
                        "grade": "error",
                        "message": "candidate is incorrect",
                    },
                ),
            )
            store.set_lane_phase("run1", "build", st.LanePhase.BLOCKED)
            store.mark_blocked(
                "run1",
                "build",
                st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                retry_class=st.RetryClass.SEMANTIC,
            )
            store.declare_outcome("run1")

            granted = store.retry("run1", "build", grant=1)

            self.assertIs(granted.state, st.NodeState.BLOCKED)
            self.assertEqual(granted.attempt_no, 1)
            self.assertIs(granted.lane_phase, st.LanePhase.BLOCKED)
            self.assertEqual(granted.granted_extra_attempts, 1)
            self.assertEqual(
                store.retry_budget_blocked_attempts("run1"), (("build", 1),)
            )
            self.assertEqual(len(store.attempts_for("run1", "build")), 1)
            self.assertIsNotNone(store.repair_handoff("run1", "build", candidate_sha))
            self.assertIsNotNone(
                store.candidate_review("run1", "build::review", candidate_sha)
            )

            # A plain `store.resume_run("run1")` used to be asserted here to
            # leave the node BLOCKED, because a review-budget block was
            # deliberately excluded from the resume refresh. That exclusion is
            # gone: a resume now refreshes the review ceiling like any other
            # budget, bounded per run by `RESUME_REVIEW_REFRESH_CEILING` (§3.6
            # A9). The assertion was removed rather than adapted because a
            # plain resume reopening the node is now the correct behaviour and
            # there is nothing left for it to distinguish here.
            #
            # What this test still owns is the rest: a grant alone does not
            # reopen the lane, and the late-envelope route reuses attempt 1 and
            # lands in REPAIRING rather than minting a new attempt. The ceiling
            # itself, and the fact that the recovery route below is not subject
            # to it, are proven in `test_resume_refreshes_review_budget.py`.
            store.resume_run("run1", late_envelope_attempts=(("build", 1),))

            resumed = store.get_node("run1", "build")
            self.assertIs(resumed.state, st.NodeState.PENDING)
            self.assertEqual(resumed.attempt_no, 1)
            self.assertIs(resumed.lane_phase, st.LanePhase.REPAIRING)
            self.assertEqual(len(store.attempts_for("run1", "build")), 1)

            claimed = store.claim_late_envelope_attempt("run1", "build", 1)
            self.assertIs(claimed.state, st.NodeState.RUNNING)
            self.assertEqual(claimed.attempt_no, 1)
            self.assertEqual(len(store.attempts_for("run1", "build")), 1)

    def test_retry_budget_resume_does_not_infer_recovery_from_result_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("build", 0)])
            store.start_attempt("run1", "build", base_sha="base")
            store.record_result(
                "run1",
                st.ResultRecord(
                    node_id="build",
                    attempt_no=1,
                    subject_sha="b" * 40,
                    payload={"status": "success"},
                    adjudication=st.Adjudication.ACCEPTED,
                ),
            )
            store.mark_blocked(
                "run1",
                "build",
                st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED,
                retry_class=st.RetryClass.ENVIRONMENTAL,
            )
            store.declare_outcome("run1")
            self.assertEqual(
                store.retry_budget_blocked_attempts("run1"), (("build", 1),)
            )

            store.resume_run("run1")

            node = store.get_node("run1", "build")
            self.assertIs(node.state, st.NodeState.PENDING)
            self.assertIsNone(node.lane_phase)
            attempt = store.get_attempt("run1", "build", 1)
            self.assertNotIn(lc.LATE_ENVELOPE_RECOVERY_KEY, attempt.extra)

    def test_a_discarding_cancel_is_absolutely_terminal(self):
        """`run cancel --discard` records DISCARDED at both levels, and that
        cause is what refuses the resume — not the verb's name, and not
        anything an operator wrote down (§1.2, §7.3)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1", cause=st.CancelCause.DISCARDED)
            report = store.declare_outcome(
                "run1", cancel_cause=st.CancelCause.DISCARDED
            )
            self.assertIs(report.outcome, st.RunOutcome.CANCELLED)
            self.assertIs(report.cancel_cause, st.CancelCause.DISCARDED)
            self.assertEqual(store.run_cancel_cause("run1"), st.CancelCause.DISCARDED)
            self.assertEqual(
                _node_cancel_cause(store, "run1", "a"), st.CancelCause.DISCARDED.value
            )
            self.assertNotIn(st.CancelCause.DISCARDED, st.REOPENABLE_CANCEL_CAUSES)
            with self.assertRaises(lc.ResumeRefused):
                store.resume_run("run1")

    def test_a_discarded_node_is_not_individually_reopenable(self):
        """Even reached node by node, the guard refuses it (§7.3)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.cancel_run("run1", cause=st.CancelCause.DISCARDED)
            with self.assertRaises(lc.IllegalTransition):
                store._reopen_run_cancelled_node("run1", "a")

    def test_adoptable_attempts_name_verified_unmerged_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0), make_node("b", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.start_attempt("run1", "b", base_sha="s1")
            found = store.adoptable_attempts("run1")
            self.assertEqual([row["node_id"] for row in found], ["a"])
            self.assertEqual(found[0]["why"], "verified")

    def test_adoptable_attempts_omit_review_rejected_accepted_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_agent_node("a")])
            store.ensure_derived_review_node("run1", "a", depth=1, downstream_needs=())
            store.start_attempt("run1", "a", base_sha="s1")
            store.record_result(
                "run1",
                st.ResultRecord(
                    node_id="a",
                    attempt_no=1,
                    subject_sha="s1",
                    payload={"status": "success"},
                    adjudication=st.Adjudication.ACCEPTED,
                ),
            )
            self.assertEqual(
                store.adoptable_attempts("run1")[0]["why"], "accepted-unmerged"
            )
            sha = "a" * 40
            store.publish_candidate(
                "run1", "a", candidate_sha=sha, builder_generation=1
            )
            store.begin_review("run1", "a::review", sha, reviewer_generation=1)
            store.mark_review_dispatched(
                "run1", "a::review", sha, reviewer_generation=1
            )
            store.reject_and_create_handoff(
                "run1",
                "a::review",
                sha,
                reviewer_generation=1,
                builder_generation=1,
                review_digest="d" * 64,
                receipt_path="/tmp/review.json",
                findings=(
                    {
                        "check_id": "diff.correctness",
                        "grade": "error",
                        "message": "candidate is incorrect",
                    },
                ),
            )
            self.assertEqual(store.adoptable_attempts("run1"), ())

    def test_abandon_cancels_a_single_node_absolutely_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            store.declare_outcome("run1")
            store.abandon("run1", "a")
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.CANCELLED)
            with self.assertRaises(lc.LifecycleError):
                store.retry("run1", "a")


# ── §11.3 operator escapes ───────────────────────────────────────────────────


class EscapeTests(unittest.TestCase):
    def test_retry_force_grants_exactly_one_extra_attempt_without_raising_a_ceiling(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)
            store.declare_outcome("run1")
            self.assertEqual(store.get_node("run1", "a").granted_extra_attempts, 0)
            store.retry("run1", "a", force=True)
            self.assertEqual(store.get_node("run1", "a").granted_extra_attempts, 1)

    def test_skip_verifies_ancestry_and_does_not_bypass_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            good_sha = _init_git_repo(repo)
            bad_sha = _orphan_sha(repo)

            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0), make_node("b", 0)])

            store.start_attempt("run1", "a", base_sha=good_sha)
            store.mark_blocked("run1", "a", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            store.skip("run1", "a", accept_sha=good_sha, repo_path=repo)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.MERGED)
            self.assertEqual(store.get_node("run1", "a").output_sha, good_sha)

            store.start_attempt("run1", "b", base_sha=good_sha)
            store.mark_blocked("run1", "b", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            with self.assertRaises(lc.LifecycleError):
                store.skip("run1", "b", accept_sha=bad_sha, repo_path=repo)
            self.assertEqual(store.get_node("run1", "b").state, st.NodeState.BLOCKED)

    def test_skip_names_an_abbreviated_sha_as_a_shape_defect(self):
        """#78. The refusal must describe the defect it actually found.

        `is_valid_output_commit` folds shape, existence and ancestry into one
        boolean, so an abbreviated SHA -- which fails on shape, before
        `cat-file` or `merge-base` ever run -- was reported as not descending
        from its base. In the incident that produced this, the commit
        descended from the base perfectly well and `git merge-base
        --is-ancestor` agreed; the only thing wrong with it was seven hex
        digits instead of forty. An operator reading that message goes looking
        for a history problem that does not exist.

        The requirement is deliberately unchanged: skip records a durable
        identity and an abbreviation is ambiguous by construction. This
        asserts the message, and that the node stays BLOCKED.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            head = _init_git_repo(repo)
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha=head)
            store.mark_blocked("run1", "a", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            with self.assertRaises(lc.SkipAncestryRefused) as caught:
                store.skip("run1", "a", accept_sha=head[:7], repo_path=repo)
            message = str(caught.exception)
            self.assertIn("not a full object digest", message)
            self.assertIn("rev-parse", message, "the refusal should carry the remedy")
            self.assertNotIn(
                "descending", message, "an abbreviated SHA is not an ancestry failure"
            )
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.BLOCKED)

    def test_skip_accepts_the_full_digest_of_that_same_commit(self):
        """The acquitting arm: the SHA above was fine, only its length was not."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            head = _init_git_repo(repo)
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha=head)
            store.mark_blocked("run1", "a", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            store.skip("run1", "a", accept_sha=head, repo_path=repo)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.MERGED)

    def test_skip_rejects_an_older_ancestor_of_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            parent = _init_git_repo(repo)
            (repo / "f.txt").write_text("later")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "-m", "later"], check=True
            )
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha=parent)
            store.mark_blocked("run1", "a", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            with self.assertRaises(lc.SkipAncestryRefused):
                store.skip("run1", "a", accept_sha=parent, repo_path=repo)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.BLOCKED)

    def test_skip_rejects_head_before_the_attempt_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            parent = _init_git_repo(repo)
            (repo / "f.txt").write_text("attempt base")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True
            )
            attempt_base = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(repo), "reset", "--hard", parent],
                check=True,
                capture_output=True,
            )
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha=attempt_base)
            store.mark_blocked("run1", "a", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            with self.assertRaises(lc.SkipAncestryRefused):
                store.skip("run1", "a", accept_sha=parent, repo_path=repo)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.BLOCKED)

    def test_skip_rejects_a_dirty_current_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            head = _init_git_repo(repo)
            (repo / "dirt.txt").write_text("uncommitted")
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha=head)
            store.mark_blocked("run1", "a", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            with self.assertRaises(lc.SkipAncestryRefused):
                store.skip("run1", "a", accept_sha=head, repo_path=repo)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.BLOCKED)

    def test_every_stored_block_reason_admits_its_declared_escapes(self):
        """§11.3's tested property, executed rather than asserted from the table.

        Every declared escape either leaves ``BLOCKED`` immediately or, for a
        retained review attempt, durably authorizes proof-backed recovery.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            good_sha = _init_git_repo(repo)

            for reason in st.BlockReason:
                for escape in st.Escape:
                    with tempfile.TemporaryDirectory() as run_tmp:
                        store = new_store(Path(run_tmp))
                        store.create_run("run1", "d", [make_node("a", 0)])
                        store.start_attempt("run1", "a", base_sha=good_sha)
                        store.mark_blocked("run1", "a", reason)
                        store.declare_outcome("run1")

                        if escape is st.Escape.RETRY:
                            store.retry("run1", "a")
                        elif escape is st.Escape.RETRY_FORCE:
                            store.retry("run1", "a", force=True)
                        elif escape is st.Escape.SKIP:
                            store.skip("run1", "a", accept_sha=good_sha, repo_path=repo)
                        elif escape is st.Escape.ABANDON:
                            store.abandon("run1", "a")
                        else:
                            self.fail(f"unhandled escape {escape!r}")

                        final = store.get_node("run1", "a").state
                        if (
                            reason is st.BlockReason.REVIEW_BUDGET_EXHAUSTED
                            and escape is st.Escape.RETRY_FORCE
                        ):
                            self.assertIs(final, st.NodeState.BLOCKED)
                            self.assertEqual(
                                store.retry_budget_blocked_attempts("run1"),
                                (("a", 1),),
                            )
                        else:
                            self.assertNotEqual(
                                final,
                                st.NodeState.BLOCKED,
                                f"{reason} -> {escape} did not leave BLOCKED"
                                f" (still {final})",
                            )


# ── concurrency ──────────────────────────────────────────────────────────────


class ConcurrencyTests(unittest.TestCase):
    def test_n_threads_transitioning_n_different_nodes_all_land(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            n = 12
            nodes = [make_node(f"n{i}", 0) for i in range(n)]
            store.create_run("run1", "d", nodes)

            def drive(i):
                node_id = f"n{i}"
                store.start_attempt("run1", node_id, base_sha="s1")
                store.mark_verified("run1", node_id, output_sha=f"sha{i}")
                store.mark_merged("run1", node_id)
                return node_id

            with ThreadPoolExecutor(max_workers=n) as pool:
                futures = [pool.submit(drive, i) for i in range(n)]
                results = [f.result() for f in as_completed(futures)]

            self.assertEqual(len(results), n)
            for i in range(n):
                self.assertEqual(
                    store.get_node("run1", f"n{i}").state, st.NodeState.MERGED
                )

    def test_two_threads_racing_one_node_produce_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])

            outcomes = []

            def race():
                try:
                    store.start_attempt("run1", "a", base_sha="s1")
                    return "won"
                except lc.LifecycleError:
                    return "refused"

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(race) for _ in range(8)]
                outcomes = [f.result() for f in as_completed(futures)]

            self.assertEqual(
                outcomes.count("won"), 1, f"expected exactly one winner, got {outcomes}"
            )
            self.assertEqual(outcomes.count("refused"), 7)
            # The loser was refused, not silently overwritten — attempt_no is 1.
            self.assertEqual(store.get_node("run1", "a").attempt_no, 1)


if __name__ == "__main__":
    unittest.main()
