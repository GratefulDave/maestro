"""Reopening a quiescence block that was written over nothing.

`QUIESCENCE_UNPROVEN` is an adjudication about a writer: this attempt's owned
execution could not be shown absent, so nothing may proceed past it. Where a
writer could exist that is exactly right and the node stays blocked.

The runtime's quiescer, though, answers the same way about an attempt that
never created anything at all — it resolves a handle from a map only a
successful launch populates, and a missing key is `PROCESS_GROUP_UNTRACKED`
whatever the reason. `tests/test_predispatch_quiescence.py` stops that being
written going forward. This file is the other half: a ledger that already
carries such a block, and a resume that can tell — from durable evidence, not
from the block's own account of itself — that the attempt it names never
crossed dispatch, and can therefore reopen *that same generation* rather than
mint a replacement.

Same generation is the point. `start_attempt` allocates a new number and
charges a new try; `claim_undispatched_attempt` allocates nothing. An attempt
that never ran is not a retry, and recording one would spend a budget on a
launch that never happened — the budgets exist to bound the fix loop, and a
loop that never turned has nothing to bound.

The proof is a conjunction over two owners, and both must agree:

* the ledger's half (`undispatched_quiescence_attempts`) — never launched, no
  actor session, no result, no candidate, no repair handoff, no orphan, and
  the node's newest generation;
* the runtime's half (`_undispatched_attempt_residue`) — no submitted prompt,
  no envelope, no session directory, and a worktree still identical to the
  baseline its bracket recorded.

Every negative below removes exactly one of those and asserts the block holds.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro  # noqa: E402
from adw_modules import lifecycle as lc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402

from test_scheduler import SchedulerFixture, _git, _make_repo  # noqa: E402


def new_store(root: Path) -> lc.LifecycleStore:
    return lc.LifecycleStore(root / "lifecycle.db")


def code_node(node_id: str = "a") -> st.PlanNode:
    return st.PlanNode(
        node_id=node_id,
        kind=st.NodeKind.CODE,
        depth=0,
        command=("true",),
    )


class _Ledger:
    """One run, one node, one attempt blocked QUIESCENCE_UNPROVEN and never launched."""

    def __init__(self, root: Path, node_id: str = "a"):
        self.store = new_store(root)
        self.node_id = node_id
        self.store.create_run("run1", "d", [code_node(node_id)])
        self.attempt_no = self.store.start_attempt("run1", node_id, base_sha="s1")
        self.store.mark_blocked(
            "run1", node_id, st.BlockReason.QUIESCENCE_UNPROVEN
        )
        self.store.declare_outcome("run1")

    def eligible(self):
        return self.store.undispatched_quiescence_attempts("run1")


# ── the durable half ────────────────────────────────────────────────────────


class DurableEvidenceTests(unittest.TestCase):
    def test_a_never_launched_quiescence_block_is_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            self.assertEqual(ledger.eligible(), (("a", 1),))

    def test_a_launched_attempt_is_never_eligible(self):
        """The load-bearing negative. `mark_launched` is the one call that
        records a pid, a host, and a process start epoch, and an attempt that
        reached it may own a process this ledger cannot see."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.mark_launched("run1", "a", 1, 4321)
            self.assertEqual(ledger.eligible(), ())

    def test_a_bound_actor_session_is_never_eligible(self):
        """A pane bound to this generation is a writer whatever the attempt
        row says, and closing it later does not unbind the history."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.register_actor_session(
                "run1",
                "a",
                "builder",
                generation=1,
                pane_id="pane-1",
                session_path=str(Path(tmp) / "session.jsonl"),
                correlation_token="run1-a-1",
            )
            self.assertEqual(ledger.eligible(), ())

    def test_a_recorded_orphan_is_never_eligible(self):
        """An orphan row *is* a recorded abandoned pid. Reopening the
        generation that produced one would step over a process nobody killed."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.record_orphan(
                "run1", node_id="a", attempt_no=1, pid=999, reason="resume"
            )
            self.assertEqual(ledger.eligible(), ())

    def test_only_the_newest_generation_is_ever_eligible(self):
        """Reopening a superseded attempt would relaunch work a later
        generation already replaced.

        The second half constructs the shape `start_attempt`'s docstring
        describes and this predicate has to survive: operator recovery can
        leave the lifecycle pointer on a retained older attempt while later
        rows remain in the ledger as audit evidence. The pointer alone would
        then say "attempt 1, blocked, never launched" — true of the row, and
        no longer true of the node.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [code_node("a")])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked(
                "run1", "a", st.BlockReason.LAUNCHER_BUDGET_EXHAUSTED
            )
            store.resume_run("run1")
            second = store.start_attempt("run1", "a", base_sha="s2")
            self.assertEqual(second, 2)
            store.mark_blocked("run1", "a", st.BlockReason.QUIESCENCE_UNPROVEN)
            self.assertEqual(
                store.undispatched_quiescence_attempts("run1"), (("a", 2),)
            )

            store.conn.execute(
                "UPDATE node_lifecycle SET attempt_no=1"
                " WHERE run_id='run1' AND node_id='a'"
            )
            self.assertEqual(store.undispatched_quiescence_attempts("run1"), ())

    def test_a_different_block_reason_is_never_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = new_store(Path(tmp))
            store.create_run("run1", "d", [code_node("a")])
            store.start_attempt("run1", "a", base_sha="s1")
            store.mark_blocked("run1", "a", st.BlockReason.CREDENTIAL_REFUSED)
            self.assertEqual(store.undispatched_quiescence_attempts("run1"), ())

    def test_a_transcript_path_on_the_row_is_never_eligible(self):
        """Fail closed on a row this predicate does not understand: a session
        path is written by the same call as `launched_at`, so one without the
        other is a shape nothing here should be reasoning about."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.conn.execute(
                "UPDATE attempts SET extra_json='{\"session_path\": \"/x.jsonl\"}'"
                " WHERE run_id='run1' AND node_id='a' AND attempt_no=1"
            )
            self.assertEqual(ledger.eligible(), ())


class AuthorityIsNeverTakenOnTrustTests(unittest.TestCase):
    def test_resume_refuses_authority_the_ledger_cannot_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.mark_launched("run1", "a", 1, 4321)
            with self.assertRaises(lc.LifecycleError) as caught:
                ledger.store.resume_run("run1", undispatched_attempts=[("a", 1)])
            self.assertIn("never crossed dispatch", str(caught.exception))
            self.assertIs(
                ledger.store.get_node("run1", "a").state, st.NodeState.BLOCKED
            )

    def test_a_claim_without_the_marker_is_refused(self):
        """`claim_undispatched_attempt` mints no authority of its own: the
        one-shot marker is written at the resume boundary against evidence,
        and an attempt row without it is refused rather than reopened."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.resume_run("run1")
            with self.assertRaises(lc.LifecycleError) as caught:
                ledger.store.claim_undispatched_attempt("run1", "a", 1)
            self.assertIn("undispatched-resume authority", str(caught.exception))

    def test_the_marker_is_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.resume_run("run1", undispatched_attempts=[("a", 1)])
            self.assertEqual(ledger.store.claim_undispatched_attempt("run1", "a", 1), 1)
            self.assertIs(
                ledger.store.get_node("run1", "a").state, st.NodeState.RUNNING
            )
            with self.assertRaises(lc.LifecycleError):
                ledger.store.claim_undispatched_attempt("run1", "a", 1)

    def test_the_reopen_is_recorded_as_a_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = _Ledger(Path(tmp))
            ledger.store.resume_run("run1", undispatched_attempts=[("a", 1)])
            reasons = [
                row.get("reason") for row in ledger.store.audit_transitions("run1")
            ]
            self.assertIn("resume:undispatched-quiescence", reasons)
            node = ledger.store.get_node("run1", "a")
            self.assertIs(node.state, st.NodeState.PENDING)
            self.assertIsNone(node.block_reason)
            self.assertEqual(node.attempt_no, 1)


# ── the runtime's half, over the paths only it can see ──────────────────────


class RuntimeResidueTests(unittest.TestCase):
    def _args(self, root: Path, repo: Path) -> SimpleNamespace:
        return SimpleNamespace(
            run_id="run1",
            repo=str(repo),
            worktrees_root=str(root / "wt"),
            scratch_root=str(root / "scratch"),
        )

    def _scratch(self, root: Path) -> Path:
        scratch = Path(root / "scratch") / wt.worktree_dirname("run1", "a", 1)
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def test_no_worktree_and_an_empty_scratch_is_proven_clean(self):
        """§8.8's cleanup may already have taken the checkout. Nothing to
        measure, and the durable half already showed nothing was recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            self._scratch(root)
            store = new_store(root)
            self.assertEqual(
                maestro._undispatched_attempt_residue(
                    self._args(root, repo), store, "a", 1
                ),
                (),
            )

    def test_an_empty_session_directory_is_not_residue(self):
        """`worktree.launch_env` makes this skeleton before any dispatch, so
        its mere existence would refuse every genuinely undispatched attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            (self._scratch(root) / "session").mkdir()
            store = new_store(root)
            self.assertEqual(
                maestro._undispatched_attempt_residue(
                    self._args(root, repo), store, "a", 1
                ),
                (),
            )

    def test_a_submitted_prompt_or_envelope_or_transcript_is_residue(self):
        for name in (
            "agent-prompt.txt",
            "agent-envelope.json",
            "repair-prompt.md",
            "repair-acknowledgement.json",
        ):
            with self.subTest(artefact=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    repo = _make_repo(root)
                    (self._scratch(root) / name).write_text("{}")
                    store = new_store(root)
                    self.assertIn(
                        name,
                        maestro._undispatched_attempt_residue(
                            self._args(root, repo), store, "a", 1
                        ),
                    )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            session = self._scratch(root) / "session"
            session.mkdir()
            (session / "transcript.jsonl").write_text("{}\n")
            store = new_store(root)
            self.assertIn(
                "session/",
                maestro._undispatched_attempt_residue(
                    self._args(root, repo), store, "a", 1
                ),
            )

    def _worktree_case(self, root: Path, repo: Path, write: bool):
        store = new_store(root)
        store.create_run("run1", "d", [code_node("a")])
        base = _git(repo, "rev-parse", "HEAD")
        attempt_no = store.start_attempt("run1", "a", base_sha=base)
        attempt = wt.create_attempt_worktree(
            repo, "run1", "a", attempt_no, base, root / "wt", root / "scratch"
        )
        baseline = wt.take_baseline(attempt)
        store.record_baseline("run1", "a", attempt_no, baseline, attempt.ignored_at_base)
        store.mark_blocked("run1", "a", st.BlockReason.QUIESCENCE_UNPROVEN)
        if write:
            (attempt.path / "produced.py").write_text("output\n")
        return store, maestro._undispatched_attempt_residue(
            self._args(root, repo), store, "a", attempt_no
        )

    def test_a_worktree_still_at_its_baseline_is_proven_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            _store, residue = self._worktree_case(root, repo, write=False)
            self.assertEqual(residue, ())

    def test_any_worktree_output_is_residue(self):
        """Measured against the bracket's own recorded before-side, so a
        provisioned path the baseline already saw is not output and a produced
        one is — which is the only comparison that can tell them apart."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            _store, residue = self._worktree_case(root, repo, write=True)
            self.assertTrue(any("produced.py" in item for item in residue), residue)

    def test_a_worktree_with_no_recorded_baseline_is_unmeasurable(self):
        """Unmeasurable is not clean. Without a before-side, `git status`
        cannot tell a provisioned path from a produced one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _make_repo(root)
            store = new_store(root)
            store.create_run("run1", "d", [code_node("a")])
            base = _git(repo, "rev-parse", "HEAD")
            store.start_attempt("run1", "a", base_sha=base)
            wt.create_attempt_worktree(
                repo, "run1", "a", 1, base, root / "wt", root / "scratch"
            )
            store.mark_blocked("run1", "a", st.BlockReason.QUIESCENCE_UNPROVEN)
            residue = maestro._undispatched_attempt_residue(
                self._args(root, repo), store, "a", 1
            )
            self.assertIn("worktree present with no recorded baseline", residue)


# ── end to end, over the real scheduler ─────────────────────────────────────


class SameAttemptResumeTests(SchedulerFixture):
    def _blocked_undispatched(self, node_id="a", make_worktree=True):
        scheduler = self.schedule([self.agent(node_id)])
        scheduler.project()
        base = _git(self.integration, "rev-parse", "HEAD")
        attempt_no = self.store.start_attempt("run1", node_id, base)
        if make_worktree:
            attempt = wt.create_attempt_worktree(
                self.repo,
                "run1",
                node_id,
                attempt_no,
                base,
                self.root / "wt",
                self.root / "scratch",
            )
            baseline = wt.take_baseline(attempt)
            self.store.record_baseline(
                "run1", node_id, attempt_no, baseline, attempt.ignored_at_base
            )
        self.store.mark_blocked(
            "run1", node_id, st.BlockReason.QUIESCENCE_UNPROVEN
        )
        self.store.declare_outcome("run1")
        return scheduler, attempt_no

    def _attempt_rows(self, node_id="a"):
        return [a for a in self.store.attempts_for("run1") if a.node_id == node_id]

    def test_the_same_generation_is_redispatched_and_merges(self):
        """The incident's recovery, end to end. Attempt 1 runs — it never did
        — and no attempt 2 is ever created."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler, attempt_no = self._blocked_undispatched()
        self.store.resume_run("run1", undispatched_attempts=[("a", attempt_no)])

        report = scheduler.run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.MERGED)
        self.assertEqual(node.attempt_no, attempt_no)
        self.assertEqual(len(self._attempt_rows()), 1)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        details = [
            row.get("detail", {})
            for row in self.store.audit_transitions("run1")
            if row.get("node_id") == "a" and row.get("reason") == "attempt-start"
        ]
        self.assertIn({"repair": "undispatched-quiescence"}, details)

    def test_a_cleaned_up_worktree_is_recreated_for_the_same_generation(self):
        """§8.8 may have taken the checkout before the block was written. The
        generation is still the same one; only its directory is new."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler, attempt_no = self._blocked_undispatched(make_worktree=False)
        self.store.resume_run("run1", undispatched_attempts=[("a", attempt_no)])

        scheduler.run()

        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)
        self.assertEqual(len(self._attempt_rows()), 1)

    def test_the_reopened_attempt_spends_no_new_retry(self):
        """A generation that never ran is not a try. `claim_undispatched_attempt`
        allocates no number, so the budgets that bound the fix loop are
        untouched by a launch that never happened."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler, attempt_no = self._blocked_undispatched()
        self.store.resume_run("run1", undispatched_attempts=[("a", attempt_no)])
        scheduler.run()
        self.assertEqual(
            [a.attempt_no for a in self._attempt_rows()], [attempt_no]
        )
        self.assertEqual(
            [a.retry_class for a in self._attempt_rows()], [None]
        )

    def test_without_the_authority_the_block_holds(self):
        """A bare resume does not touch a quiescence block. This is the
        control: everything above depends on the authority being supplied,
        and nothing may reopen the node without it."""
        self.written = {"a": {"a.py": "A\n"}}
        scheduler, _ = self._blocked_undispatched()
        self.store.resume_run("run1")

        report = scheduler.run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.QUIESCENCE_UNPROVEN)
        self.assertEqual(len(self._attempt_rows()), 1)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_a_dispatched_quiescence_block_is_untouched_by_the_recovery(self):
        """The half a widened recovery gets wrong: this attempt launched, so
        its quiescence block is an adjudication about a writer and stays."""
        scheduler, attempt_no = self._blocked_undispatched()
        self.store.mark_launched("run1", "a", attempt_no, 4321)
        self.assertEqual(self.store.undispatched_quiescence_attempts("run1"), ())
        with self.assertRaises(lc.LifecycleError):
            self.store.resume_run(
                "run1", undispatched_attempts=[("a", attempt_no)]
            )
        self.assertIs(
            self.store.get_node("run1", "a").state, st.NodeState.BLOCKED
        )


if __name__ == "__main__":
    unittest.main()
