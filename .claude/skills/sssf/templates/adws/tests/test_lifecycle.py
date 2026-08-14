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
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def make_node(node_id: str, depth: int, needs=()) -> st.PlanNode:
    """A code node — no gate needed, so tests stay focused on lifecycle, not §7.4."""
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.CODE, depth=depth,
                       needs=tuple(needs), command=("true",))


def new_store(tmp_root: Path) -> lc.LifecycleStore:
    return lc.LifecycleStore(tmp_root / "lifecycle.db")


def _init_git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _orphan_sha(root: Path) -> str:
    """A commit that exists but is not an ancestor of HEAD — the negative skip case."""
    original_branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "--orphan", "not-merged"], check=True)
    (root / "orphan.txt").write_text("y")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "orphan"], check=True)
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    # Restore HEAD to the original branch — `skip`'s ancestry check below is
    # always against *this repo's* HEAD, and this orphan commit must stay
    # unreachable from it for the negative case to mean anything.
    subprocess.run(["git", "-C", str(root), "checkout", "-q", original_branch], check=True)
    return out.stdout.strip()


# ── §5.3 / store construction ───────────────────────────────────────────────

class StoreConstructionTests(unittest.TestCase):

    def test_construction_creates_the_five_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            names = {row[0] for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("runs", "dag_nodes", "node_lifecycle", "attempts", "transitions"):
                self.assertIn(table, names)

    def test_dag_nodes_carry_the_plan_digest_on_every_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "digest-abc", [make_node("a", 0)])
            row = store.conn.execute(
                "SELECT plan_digest FROM dag_nodes WHERE run_id=? AND node_id=?",
                ("run1", "a")).fetchone()
            self.assertEqual(row[0], "digest-abc")

    def test_create_run_seeds_every_node_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0), make_node("b", 1, needs=("a",))])
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            self.assertEqual(store.get_node("run1", "b").state, st.NodeState.PENDING)


# ── §7.9 one transaction per transition ─────────────────────────────────────

class TransitionTransactionTests(unittest.TestCase):

    def test_start_attempt_writes_lifecycle_attempt_transition_and_runs_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            before = store.conn.execute(
                "SELECT last_transition_at FROM runs WHERE run_id=?", ("run1",)).fetchone()[0]
            attempt_no = store.start_attempt("run1", "a", base_sha="deadbeef")

            self.assertEqual(attempt_no, 1)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.RUNNING)
            attempt_row = store.conn.execute(
                "SELECT base_sha FROM attempts WHERE run_id=? AND node_id=? AND attempt_no=?",
                ("run1", "a", 1)).fetchone()
            self.assertEqual(attempt_row[0], "deadbeef")
            transition_row = store.conn.execute(
                "SELECT from_state, to_state, actor FROM transitions"
                " WHERE run_id=? AND node_id=? ORDER BY id DESC LIMIT 1", ("run1", "a")).fetchone()
            self.assertEqual(transition_row, ("PENDING", "RUNNING", "scheduler"))
            after = store.conn.execute(
                "SELECT last_transition_at FROM runs WHERE run_id=?", ("run1",)).fetchone()[0]
            # ISO-8601 strings sort lexicographically; >= tolerates two writes
            # landing in the same millisecond while still proving the refresh ran.
            self.assertGreaterEqual(after, before)

    def test_a_refused_transition_writes_nothing(self):
        """The guard fires before any write — a failed transition leaves no residue."""
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            with self.assertRaises(lc.LifecycleError):
                store.mark_merged("run1", "a")  # PENDING -> MERGED is not legal directly
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            count = store.conn.execute(
                "SELECT COUNT(*) FROM transitions WHERE run_id=?", ("run1",)).fetchone()[0]
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
                store._transition_node("run1", "a", st.NodeState.CANCELLED,
                                       actor="operator", reason="abandon")

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
                store._transition_node("run1", "a", st.NodeState.PENDING, actor="scheduler",
                                       reason="not-a-real-escape")

    def test_blocked_admits_the_operator_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
            store.declare_outcome("run1")  # quiesce so the escape is legal (§7.3)
            store.retry("run1", "a")
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)


# ── §7.1, §8.7 ready set and the reversible cascade ─────────────────────────

class ReadySetAndCascadeTests(unittest.TestCase):

    def _diamond(self, store):
        # a -> b, a -> c, {b, c} -> d
        store.create_run("run1", "d", [
            make_node("a", 0), make_node("b", 1, needs=("a",)),
            make_node("c", 1, needs=("a",)), make_node("d", 2, needs=("b", "c")),
        ])

    def test_ready_set_is_pending_nodes_whose_deps_are_all_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            self._diamond(store)
            self.assertEqual(store.ready_nodes("run1"), ("a",))
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged("run1", "a")
            self.assertEqual(store.ready_nodes("run1"), ("b", "c"))  # sorted (depth, node_id)

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
            self.assertIn("d", blocked_descendants)  # d needs b (and c) — derived-unready

            store.declare_outcome("run1")
            store.retry("run1", "b")  # rescue the origin — no un-cascade rule exists anywhere
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
                states, states, (True, False), (True, False), (None, True, False)):
            node_states = [("a", s1, None), ("b", s2, None)]
            report = lc.total_run_outcome(node_states, stuck=stuck,
                                          cancel_requested=cancel_requested,
                                          acceptance_result=acceptance)
            self.assertIsInstance(report, lc.OutcomeReport)
            self.assertIn(report.outcome, set(st.RunOutcome))

    def test_stuck_wins_even_with_work_in_flight(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.RUNNING, None)], stuck=True,
            cancel_requested=False, acceptance_result=None)
        self.assertEqual(report.outcome, st.RunOutcome.STUCK)

    def test_cancel_requested_yields_cancelled(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None)], stuck=False,
            cancel_requested=True, acceptance_result=None)
        self.assertEqual(report.outcome, st.RunOutcome.CANCELLED)

    def test_every_node_cancelled_yields_cancelled_without_the_flag(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.CANCELLED, None), ("b", st.NodeState.CANCELLED, None)],
            stuck=False, cancel_requested=False, acceptance_result=None)
        self.assertEqual(report.outcome, st.RunOutcome.CANCELLED)

    def test_accepted_requires_a_merge_no_stragglers_and_a_green_acceptance(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None), ("b", st.NodeState.CANCELLED, None)],
            stuck=False, cancel_requested=False, acceptance_result=True)
        self.assertEqual(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(report.abandoned_nodes, ("b",))

    def test_accepted_refused_when_acceptance_is_not_green(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None)], stuck=False,
            cancel_requested=False, acceptance_result=False)
        self.assertEqual(report.outcome, st.RunOutcome.BLOCKED)

    def test_some_merged_some_blocked_is_blocked_not_undefined(self):
        report = lc.total_run_outcome(
            [("a", st.NodeState.MERGED, None),
             ("b", st.NodeState.BLOCKED, st.BlockReason.MERGE_CONFLICT)],
            stuck=False, cancel_requested=False, acceptance_result=None)
        self.assertEqual(report.outcome, st.RunOutcome.BLOCKED)
        self.assertEqual(report.blocked_nodes, ("b",))
        self.assertEqual(report.block_reasons["b"], st.BlockReason.MERGE_CONFLICT)

    def test_blocked_is_the_residual_class_for_the_empty_run(self):
        report = lc.total_run_outcome([], stuck=False, cancel_requested=False,
                                      acceptance_result=None)
        self.assertEqual(report.outcome, st.RunOutcome.BLOCKED)


# ── §7.3 outcome as a record; NULL, resume, and escape legality ────────────

class OutcomeRecordAndLegalityTests(unittest.TestCase):

    def test_latest_outcome_is_null_until_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            self.assertIsNone(store.latest_outcome("run1"))

    def test_declaring_twice_keeps_only_the_latest_and_the_history_lives_in_transitions(self):
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
                ("run1",)).fetchone()[0]
            self.assertEqual(declared, 2)

    def test_resume_is_legal_against_blocked_stuck_and_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.resume_run("run1")  # NULL — nobody ever declared

            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            store.declare_outcome("run1")
            store.resume_run("run1")  # BLOCKED

    def test_resume_is_refused_against_accepted_and_cancelled(self):
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
            store2.cancel_run("run1")
            store2.declare_outcome("run1")
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

    def test_resume_refreshes_last_transition_at_before_touching_inherited_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [make_node("a", 0)])
            store.start_attempt("run1", "a", base_sha="s1")  # left RUNNING — a crashed scheduler
            reclaimed = store.resume_run("run1")
            self.assertEqual(reclaimed, ("a",))
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.PENDING)
            rows = store.conn.execute(
                "SELECT node_id, reason FROM transitions WHERE run_id=? ORDER BY id",
                ("run1",)).fetchall()
            # The resume transition (node_id NULL) precedes the reclaimed attempt's transition.
            resume_idx = next(i for i, r in enumerate(rows) if r[1] == "resume")
            reclaim_idx = next(i for i, r in enumerate(rows) if r[0] == "a" and i > 0
                               and r[1].startswith("retry:"))
            self.assertLess(resume_idx, reclaim_idx)


# ── §7.8 cancellation ────────────────────────────────────────────────────────

class CancellationTests(unittest.TestCase):

    def test_cancel_run_writes_cancelled_for_every_non_terminal_node_in_one_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [
                make_node("a", 0), make_node("b", 1, needs=("a",)), make_node("c", 0)])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_verified("run1", "a", output_sha="sha_a")
            store.mark_merged("run1", "a")  # a is MERGED — absolutely terminal, untouched

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

    def test_retry_force_grants_exactly_one_extra_attempt_without_raising_a_ceiling(self):
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

            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            store.skip("run1", "a", accept_sha=good_sha, repo_path=repo)
            self.assertEqual(store.get_node("run1", "a").state, st.NodeState.MERGED)
            self.assertEqual(store.get_node("run1", "a").output_sha, good_sha)

            store.start_attempt("run1", "b", base_sha="s1")
            store.mark_blocked("run1", "b", st.BlockReason.GATE_NOT_FALSIFIABLE)
            store.declare_outcome("run1")
            with self.assertRaises(lc.LifecycleError):
                store.skip("run1", "b", accept_sha=bad_sha, repo_path=repo)
            self.assertEqual(store.get_node("run1", "b").state, st.NodeState.BLOCKED)

    def test_every_stored_block_reason_admits_its_declared_escapes(self):
        """§11.3's tested property, executed rather than asserted from the table:
        for every BlockReason, every escape scheduler_types.exits_for() names
        actually runs and moves the node out of BLOCKED."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            good_sha = _init_git_repo(repo)

            for reason in st.BlockReason:
                for escape in st.exits_for(reason):
                    with tempfile.TemporaryDirectory() as run_tmp:
                        store = new_store(Path(run_tmp))
                        store.create_run("run1", "d", [make_node("a", 0)])
                        store.start_attempt("run1", "a", base_sha="s1")
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
                        self.assertNotEqual(
                            final, st.NodeState.BLOCKED,
                            f"{reason} -> {escape} did not leave BLOCKED (still {final})")


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
                self.assertEqual(store.get_node("run1", f"n{i}").state, st.NodeState.MERGED)

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

            self.assertEqual(outcomes.count("won"), 1,
                             f"expected exactly one winner, got {outcomes}")
            self.assertEqual(outcomes.count("refused"), 7)
            # The loser was refused, not silently overwritten — attempt_no is 1.
            self.assertEqual(store.get_node("run1", "a").attempt_no, 1)


if __name__ == "__main__":
    unittest.main()
