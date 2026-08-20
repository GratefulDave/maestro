"""The §8.3 bracket's evaluations, and the three seams settled around them.

Everything here is a *wiring* proof rather than a logic proof. The functions
below were all written, tested in isolation, and never called from a run — the
reader-without-writer shape inverted, which §16.3 item 42 and `test_no_dead_
seams` exist for. A unit test over an uncalled function is green forever while
production takes the other path, so each case here drives the real scheduler
and asserts an effect that only exists if the call happens.

What it covers, and which section owns each:

  §8.3  both cleanliness evaluations. Two are specified with two different
        consequences — post-commit **convicts**, pre-merge **reports** — and
        production ran neither: the first computed its verdict and discarded
        the return value, the second had no call site at all.
  §8.8  attempt-worktree cleanup after a proven merge. Nothing removed one.
  §7.5  the guidance ledger's scope, and the containment rule at the worker's
        top-level handler.

All four sit inside or immediately after the quiesce ladder §8.3 defines, which
is why they are settled together: the pre-merge evaluation is only meaningful
once the post-gate's group is proven absent, and the cleanup is only legal once
the merge proved ancestry.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import retry_policy as rp       # noqa: E402
from adw_modules import scheduler as sch         # noqa: E402
from adw_modules import scheduler_types as st    # noqa: E402
from adw_modules import worktree as wt           # noqa: E402

from test_scheduler import SchedulerFixture, green, red, _git  # noqa: E402


# ── §8.3, first evaluation: post-commit divergence convicts ─────────────────

class PostCommitConvictionTests(SchedulerFixture):
    """`check_post_commit`'s verdict was computed and thrown away.

    §8.3 is explicit that a divergence here convicts: nothing but the harness
    executes between the after-inventory and §8.4's index refresh, so a
    divergence is a stray write inside a window in which nothing is permitted
    to write. §8.4 classifies exactly that window's writes ENVIRONMENTAL — a
    fact about the machine, not a verdict about the work.
    """

    def _litter_after_the_commit(self, relpath="stray-residue.txt"):
        """Plant a write in the one window the evaluation exists to catch."""
        real = wt.commit_measured_delta

        def commit_then_litter(attempt, measured, after, message):
            output_sha = real(attempt, measured, after, message)
            (attempt.path / relpath).write_text("planted\n")
            return output_sha

        return mock.patch.object(wt, "commit_measured_delta",
                                 side_effect=commit_then_litter)

    def test_a_write_between_the_commit_and_the_refresh_convicts(self):
        self.written = {"a": {"a.py": "A\n"}}
        with self._litter_after_the_commit():
            self.schedule([self.agent("a")],
                          config=self.config(concurrency=1)).run()

        record = self.store.get_node("run1", "a")
        self.assertIs(record.state, st.NodeState.BLOCKED)
        self.assertIs(record.block_reason,
                      st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)

    def test_the_conviction_names_the_check_and_the_path(self):
        """A block is terminal, so its transition is the ledger's last chance
        to say what failed — the reason a discarded `CheckResult` costs more
        than the missing branch."""
        self.written = {"a": {"a.py": "A\n"}}
        with self._litter_after_the_commit():
            self.schedule([self.agent("a")],
                          config=self.config(concurrency=1)).run()

        blocked = [t for t in self.store.audit_transitions("run1")
                   if t.get("node_id") == "a"
                   and t.get("to_state") == st.NodeState.BLOCKED.value]
        self.assertTrue(blocked)
        self.assertIn("post-commit", str(blocked[-1]))
        self.assertIn("stray-residue.txt", str(blocked[-1]))

    def test_a_clean_attempt_is_untouched_by_the_new_branch(self):
        """The acquittal half. A node whose only writes are its declared
        outputs must still merge — baseline-present content the delta never
        touched is part of the expected inventory and is not dirt."""
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule([self.agent("a")]).run()
        self.assertEqual(self.states(), {"a": "MERGED"})
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)


# ── §8.3, second evaluation: pre-merge divergence is reported ───────────────

class PreMergeHygieneReportTests(SchedulerFixture):
    """`check_pre_merge` had no production caller, so §8.3's stated
    maintenance signal did not exist: an adapter could rewrite the tree after
    every post-gate of every run and nothing would say so."""

    def _gate_that_leaves_residue(self, relpath="post-gate-cache/report.xml"):
        def leaky(attempt, node, phase, cancel_requested):
            if phase == "post":
                target = attempt.path / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("adapter residue\n")
                return green()
            return red()
        return leaky

    def test_residue_left_by_the_post_gate_is_reported_with_its_paths(self):
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_gate=self._gate_that_leaves_residue())).run()

        self.assertIn("a", report.adapter_hygiene)
        self.assertTrue(
            any("post-gate-cache/report.xml" in entry
                for entry in report.adapter_hygiene["a"]),
            report.adapter_hygiene)

    def test_the_node_still_merges_because_the_commit_was_sealed_first(self):
        """The consequence is the whole point of there being two evaluations.
        The commit precedes the post-gate and §8.6 merges the output SHA, so
        residue is a maintenance signal about the adapter and never a verdict
        about the node — convicting here would block a node that did its work
        correctly for the harness's own hygiene."""
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_gate=self._gate_that_leaves_residue())).run()

        self.assertEqual(self.states(), {"a": "MERGED"})
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_a_clean_adapter_reports_nothing(self):
        """Empty is a claim, not an absence of one: a detector that has only
        ever returned 'dirty' proves as little as one that has only ever
        returned 'clean'."""
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule([self.agent("a")]).run()
        self.assertEqual(report.adapter_hygiene, {})


# ── issue #21: residue outside the attempt's own tree ──────────────────────

class UnprovisionedWorktreeReportTests(SchedulerFixture):
    """A worktree a reviewer registered against the repository and abandoned.

    Reviewer agents open detached worktrees under `/tmp` of their own accord
    — one transcript twelve times, with no matching removal — and no Maestro
    code creates or removes those paths. The pre-merge cleanliness comparison
    measures the attempt's own tree, so a tree registered beside it was
    invisible to it and the leak was silent.

    This is a wiring proof like the rest of this file: the detector could be
    correct in isolation forever while `check_pre_merge`'s new field had no
    production reader, which is exactly the shape §3.6 B15 makes a build
    failure. So it drives the real scheduler and reads the report.
    """

    def _gate_that_opens_a_reviewer_worktree(self, name="lexgenius-review-31ef146"):
        """The reviewer's own command, at a path Maestro never chose."""
        self.leaked = (self.root / name).resolve()

        def reviewing(attempt, node, phase, cancel_requested):
            if phase == "post":
                _git(attempt.path, "worktree", "add", "--detach",
                     str(self.root / name), "HEAD")
                return green()
            return red()
        return reviewing

    def test_a_worktree_a_reviewer_left_behind_is_reported_with_its_path(self):
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_gate=self._gate_that_opens_a_reviewer_worktree())).run()

        entries = report.adapter_hygiene.get("a", ())
        self.assertTrue(
            any(entry.startswith("unprovisioned-worktree")
                and str(self.leaked) in entry
                for entry in entries),
            report.adapter_hygiene)

    def test_the_node_still_merges_because_it_did_not_open_the_tree(self):
        """The consequence, chosen deliberately. The leak is real residue —
        a checkout and an administrative entry nothing will reclaim — but the
        node that produced the diff did not create it, and blocking the merge
        would strand correct, gated, committed work on someone else's
        housekeeping."""
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(run_gate=self._gate_that_opens_a_reviewer_worktree())).run()

        self.assertEqual(self.states(), {"a": "MERGED"})
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

    def test_the_runs_own_worktrees_are_never_reported(self):
        """The false-positive control at the wiring level. This fixture opens
        an integration checkout outside the worktrees root and an attempt
        worktree inside it, both on branches — reporting either would make the
        channel noise on every run that ever completed."""
        self.written = {"a": {"a.py": "A\n"}}
        report = self.schedule([self.agent("a")]).run()

        self.assertEqual(self.states(), {"a": "MERGED"})
        self.assertEqual(
            [entry for entries in report.adapter_hygiene.values()
             for entry in entries
             if entry.startswith("unprovisioned-worktree")],
            [])



# ── §8.8 cleanup: the attempt worktree goes, once ancestry is proven ────────

class MergedWorktreeCleanupTests(SchedulerFixture):
    """Nothing removed an attempt worktree. `run start`'s teardown releases
    the run's *integration* checkout and reclaims stranded integration
    checkouts, and both are explicit that attempt worktrees are not their
    business — so every attempt of every node left a checkout and a branch."""

    def _attempt_worktrees(self):
        listed = _git(self.repo, "worktree", "list", "--porcelain")
        return {Path(line[len("worktree "):].strip()).resolve()
                for line in str(listed).splitlines()
                if line.startswith("worktree ")}

    def test_a_merged_node_leaves_no_worktree_or_branch_behind(self):
        self.written = {"a": {"a.py": "A\n"}}
        self.schedule([self.agent("a")]).run()

        self.assertEqual(self.states(), {"a": "MERGED"})
        leftover = [p for p in self._attempt_worktrees()
                    if (self.root / "wt").resolve() in p.parents]
        self.assertEqual(leftover, [])
        self.assertNotIn("run1-a-a1", str(_git(self.repo, "branch", "--list")))

    def test_a_blocked_node_keeps_its_worktree_for_post_mortem(self):
        """§8.8's retention rule, and the mechanism is that cleanup is only
        reached from the merge path — not a second predicate that could
        disagree with the first."""
        self.gate_script = {("a", "pre"): [green()]}   # GATE_NOT_FALSIFIABLE
        self.schedule([self.agent("a")]).run()

        self.assertIs(self.store.get_node("run1", "a").state,
                      st.NodeState.BLOCKED)
        retained = [p for p in self._attempt_worktrees()
                    if (self.root / "wt").resolve() in p.parents]
        self.assertTrue(retained)

    def test_a_cleanup_refusal_is_reported_and_never_aborts_the_run(self):
        """§8.8 forbids forcing, so `remove_attempt_worktree` is allowed to
        refuse. This runs on the merge loop thread, where raising would
        abandon the remaining frontier — a cleanup failure must not be more
        destructive than the leak it is cleaning up."""
        self.written = {"a": {"a.py": "A\n"}, "b": {"b.py": "B\n"}}

        def refuse(attempt, ancestry_proven, integration_path=None):
            raise wt.WorktreeError("planted: refusing to remove " + str(attempt.path))

        with mock.patch.object(wt, "remove_attempt_worktree", side_effect=refuse):
            report = self.schedule([self.agent("a"), self.agent("b")]).run()

        self.assertEqual(self.states(), {"a": "MERGED", "b": "MERGED"})
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertTrue(
            any("was not removed" in entry
                for entries in report.adapter_hygiene.values()
                for entry in entries),
            report.adapter_hygiene)


# ── §7.5: the guidance ledger is evidence about a base ──────────────────────

class GuidanceScopeTests(SchedulerFixture):
    """Keyed by `node_id` alone, nothing cleared the ledger when the base
    advanced, so a node retrying against a new head was handed review and
    verification findings derived from a tree that no longer exists."""

    def _node_a_that_fails_once(self, advance_head: bool):
        """Attempt 1 produces nothing, so the post-node gate is red and the
        failure classifies SEMANTIC. Attempt 2 supplies the output.

        When `advance_head` is set, attempt 1 also commits an unrelated file
        onto the integration branch, exactly as an unrelated node's merge
        would — so attempt 2 starts from a different `base_sha` without
        needing a second node whose merge order would be timing-dependent.
        """
        seen = {"n": 0}

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            seen["n"] += 1
            if seen["n"] == 1:
                if advance_head:
                    (self.integration / "unrelated.txt").write_text("moved\n")
                    _git(self.integration, "add", "unrelated.txt")
                    _git(self.integration, "commit", "-q", "-m", "unrelated merge")
            else:
                (attempt.path / "a.py").write_text("A\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0,
                                     launched_pid=None)

        return run_node

    def _gate(self):
        calls = {"post": 0}

        def run_gate(attempt, node, phase, cancel_requested):
            if phase == "pre":
                return red()
            calls["post"] += 1
            return red() if calls["post"] == 1 else green()

        return run_gate

    def _bases(self):
        return [a.base_sha for a in self.store.attempts_for("run1", "a")]

    def test_guidance_from_a_stale_base_is_not_handed_to_the_retry(self):
        self.schedule(
            [self.agent("a")],
            config=self.config(concurrency=1, semantic_ceiling=3),
            deps=self.deps(run_node=self._node_a_that_fails_once(True),
                           run_gate=self._gate())).run()

        bases = self._bases()
        self.assertNotEqual(bases[0], bases[1],
                            "the fixture must actually move the base")
        self.assertEqual(len(self.prompts["a"]), 2)
        self.assertNotIn("Verification (§8.3)", self.prompts["a"][1] or "")

    def test_guidance_at_the_same_base_is_still_carried_forward(self):
        """The control, and the regression the re-keying could have caused.
        A retry against the *same* tree is retrying against the same evidence,
        and §7.5's whole point is that such a retry gets new instructions."""
        self.schedule(
            [self.agent("a")],
            config=self.config(concurrency=1, semantic_ceiling=3),
            deps=self.deps(run_node=self._node_a_that_fails_once(False),
                           run_gate=self._gate())).run()

        bases = self._bases()
        self.assertEqual(bases[0], bases[1])
        self.assertEqual(len(self.prompts["a"]), 2)
        self.assertIn("Verification (§8.3)", self.prompts["a"][1] or "")


# ── §7.5 containment at the worker's top-level handler ──────────────────────

class ContainmentTests(SchedulerFixture):
    """§7.5: "any exception that reaches [the top-level handler] without a
    classification defaults to ENVIRONMENTAL — fail-closed".

    The handler called bare `classify`, so the invariant held only as long as
    turning a raw failure into a `FailureSignal` happened not to raise. That
    is a property of today's field list rather than a rule. A build that
    raises is an engine bug, and an engine bug reaching a
    `ThreadPoolExecutor` future is exactly the shape the containment rule
    exists to stop: the node neither retried nor blocked, and stayed RUNNING
    with no transition while the run declared around it.
    """

    @staticmethod
    def _always_fails(attempt, node, record, retry_prompt, on_launch,
                      cancel_requested):
        on_launch(None)
        raise RuntimeError("the original failure")

    @staticmethod
    def _exploding_signal(*args, **kwargs):
        raise TypeError("planted engine bug while building the signal")

    def test_a_signal_that_cannot_be_built_still_classifies_environmental(self):
        real = rp.FailureSignal
        with mock.patch.object(sch.rp, "FailureSignal",
                               side_effect=self._exploding_signal):
            self.schedule([self.agent("a")],
                          config=self.config(concurrency=1),
                          deps=self.deps(run_node=self._always_fails)).run()

        self.assertIs(real, rp.FailureSignal)      # patch really was undone
        record = self.store.get_node("run1", "a")
        self.assertIs(record.state, st.NodeState.BLOCKED)
        self.assertIs(record.block_reason,
                      st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)

    def test_a_collapsing_worker_leaves_its_siblings_alone(self):
        """§7.5's second containment invariant, and §13.3's negative test: a
        worker failure writes only its own node's state."""
        self.written = {"b": {"b.py": "B\n"}}
        inner = self.run_node

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            if node.node_id == "a":
                return self._always_fails(attempt, node, record, retry_prompt,
                                          on_launch, cancel_requested)
            return inner(attempt, node, record, retry_prompt, on_launch,
                         cancel_requested)

        with mock.patch.object(sch.rp, "FailureSignal",
                               side_effect=self._exploding_signal):
            report = self.schedule([self.agent("a"), self.agent("b")],
                                   deps=self.deps(run_node=run_node)).run()

        self.assertEqual(self.store.get_node("run1", "b").state,
                         st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)


if __name__ == "__main__":
    unittest.main()
