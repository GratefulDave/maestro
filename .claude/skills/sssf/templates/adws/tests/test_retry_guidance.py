"""Retry guidance accumulates across acceptance surfaces — the oscillation fix.

Run 32b19abadf4a4d6b801ae0f4456976c7 BLOCKED after five attempts without ever
converging: attempts a1/a3/a5 failed §8.3 clause 4 (a write outside declared
outputs, convicted before the commit exists), a2/a4 were rejected by the
cross-vendor reviewer on real commits. Each retry prompt carried only the most
recent failure, so every attempt fixed exactly what the last prompt named and
silently regressed the constraint the prompt no longer mentioned. Each
individual fix was correct; the two constraints were never held simultaneously.

The fix is `retry_policy.GuidanceLedger`: every typed entry from each
acceptance surface, *including* the earlier entries from the same surface,
all of it rendered into every retry prompt and all of it durable on the
attempt rows. These tests prove four things:

* the ledger's accumulation semantics — a review rejection cannot erase what
  verification said, and vice versa; and within one surface a second finding
  appends rather than overwriting, because the slot held exactly one entry
  and a builder that had failed twice was told about one of the failures;
* the rendering bound — B13's overflow mode is refused by deterministic
  truncation that never silently drops a surface;
* **convergence through the real scheduler** — a node that fails clause 4,
  then review, then clause 4 again receives every standing constraint in each
  subsequent prompt, including the earlier same-surface one, and merges once
  an attempt satisfies all of them at once. A test that only checked the
  dataclasses would not prove that; the scheduler is where the overwrite bug
  lived;
* **durability** — the ledger was process-local and rebuilt from nothing, so
  a resumed run dispatched a builder with no idea why the earlier attempts
  were rejected. It is rebuilt from the store, and the tests drive that
  through the store rather than through the in-process object.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import retry_policy as rp  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_scheduler import SchedulerFixture  # noqa: E402


class _Cell:
    """The three facts a graded cell contributes to guidance."""

    def __init__(self, check_id: str, object_id: str, message: str):
        self.check_id = check_id
        self.object_id = object_id
        self.message = message


class _Review:
    """A reviewer verdict, duck-typed as the scheduler consumes it."""

    def __init__(
        self, passed: bool, findings=(), advisories=(), subject_digest: str = "digest-1"
    ):
        self.passed = passed
        self.findings = list(findings)
        self.advisories = list(advisories)
        self.subject_digest = subject_digest
        self.replayed = False

    def findings_text(self) -> str:
        lines = []
        for cell in self.findings:
            lines.append(f"  [{cell.object_id}] {cell.check_id}")
            lines.append(f"    {cell.message}")
        return "\n".join(lines)


class _Node:
    """Just enough of a PlanNode for rendering: declared outputs."""

    def __init__(self, outputs=("a.py",)):
        self.node_id = "a"
        self.outputs = tuple(outputs)


class WrongReasonRemediationTests(unittest.TestCase):
    """A verdict that cannot be acted on spends attempts without converging.

    Recorded 2026-08-27, run-d3bd665ce838456f989a15143f196710,
    `lane-routing-chemical-tests`: attempts a2 and a3 produced BYTE-IDENTICAL
    `TEST_STRENGTH_CONTROL_WRONG_REASON` refusals and a4 was dispatched with
    the same prompt again. The tester spent its turn grepping the harness for
    the gate implementation instead of writing cases, because nothing in the
    loop ever says what to do differently: guidance is assembled from ledger
    items by concatenation, and a reviewer never sees a candidate that died in
    verification.

    The remedy is deterministic text keyed on the typed code -- not a model's
    opinion -- so it decides no transition (§1.2).
    """

    REASON = (
        "TEST_STRENGTH_CONTROL_WRONG_REASON: every selected case failed, but "
        "none for the declared reason 'AssertionError|feeds_mart'; observed: "
        "ModuleNotFoundError: No module named 'pkg.mod'"
    )

    def test_an_import_crash_verdict_carries_the_way_out(self):
        lines = rp._remediation_lines(self.REASON)
        self.assertTrue(lines)
        joined = "\n".join(lines)
        # It must name the actual mechanism, not merely repeat the verdict.
        self.assertIn("raises before any assertion runs", joined)
        self.assertIn("except ModuleNotFoundError", joined)
        # And it must not undo the constraint that put the import in the body.
        self.assertIn("inside the body", joined)
        self.assertIn("uncollectable", joined)

    def test_the_remedy_reaches_the_rendered_guidance(self):
        """B15: text with no reader is not a fix. Assert it is rendered."""
        rendered = "\n".join(
            rp._verification_lines(
                _Node(("tests/test_x.py",)),
                rp.VerificationGuidance(reason=self.REASON, failed_clause=3),
            )
        )
        self.assertIn("How to reconcile this", rendered)
        self.assertIn(self.REASON, rendered)

    def test_a_wrong_reason_that_is_not_an_import_crash_gets_no_advice(self):
        """The advice is about import crashes; a plain wrong reason is not one."""
        self.assertEqual(
            rp._remediation_lines(
                "TEST_STRENGTH_CONTROL_WRONG_REASON: ... observed: "
                "AssertionError: some other message"
            ),
            [],
        )

    def test_unrelated_refusals_are_untouched(self):
        for reason in (
            "TEST_STRENGTH_CONTROL_NOT_RED: a case passed at the parent",
            "clause 4",
            "",
        ):
            self.assertEqual(rp._remediation_lines(reason), [])


class LedgerSemanticsTests(unittest.TestCase):
    """Accumulation across surfaces, and history within one."""

    def test_review_does_not_erase_verification_and_vice_versa(self):
        ledger = rp.GuidanceLedger()
        ledger = ledger.with_verification(
            rp.VerificationGuidance(
                reason="clause 4", offending_paths=("rogue.py",), failed_clause=4
            )
        )
        ledger = ledger.with_review(
            rp.ReviewGuidance(
                subject_digest="d1",
                findings=(
                    rp.ReviewFinding(
                        "diff.introduces_no_obvious_defect",
                        "app.py",
                        "naive datetime",
                        True,
                    ),
                ),
            )
        )
        self.assertEqual(len(ledger.verification), 1)
        self.assertEqual(len(ledger.review), 1)
        # And the other direction: a later verification failure keeps review.
        ledger = ledger.with_verification(
            rp.VerificationGuidance(
                reason="clause 4 again", offending_paths=("rogue2.py",), failed_clause=4
            )
        )
        self.assertEqual(
            [item.offending_paths for item in ledger.verification],
            [("rogue.py",), ("rogue2.py",)],
        )
        self.assertEqual(len(ledger.review), 1)

    def test_same_surface_history_is_appended_not_replaced(self):
        """The bug this replaced: the slot held one entry, so a second
        finding from the same surface erased the first and the next prompt
        named only the newer one. Both must survive."""
        ledger = rp.GuidanceLedger().with_review(
            rp.ReviewGuidance(
                subject_digest="d1",
                findings=(rp.ReviewFinding("c1", "o1", "old finding", True),),
            )
        )
        ledger = ledger.with_review(
            rp.ReviewGuidance(
                subject_digest="d2",
                findings=(rp.ReviewFinding("c2", "o2", "new finding", True),),
            )
        )
        messages = [f.message for g in ledger.review for f in g.findings]
        self.assertEqual(messages, ["old finding", "new finding"])

    def test_empty_ledger_renders_to_none(self):
        self.assertIsNone(rp.render_guidance(_Node(), rp.GuidanceLedger()))
        self.assertIsNone(rp.render_guidance(_Node(), None))


class RenderingTests(unittest.TestCase):
    def _full_ledger(self) -> rp.GuidanceLedger:
        return (
            rp.GuidanceLedger()
            .with_verification(
                rp.VerificationGuidance(
                    reason="the measured delta failed the permission check",
                    offending_paths=("rogue.py",),
                    failed_clause=4,
                ),
            )
            .with_review(
                rp.ReviewGuidance(
                    subject_digest="d1",
                    findings=(
                        rp.ReviewFinding(
                            "diff.introduces_no_obvious_defect",
                            "app.py",
                            "WrittenOpinionsStage reads the wall clock",
                            True,
                        ),
                    ),
                )
            )
        )

    def test_both_surfaces_render_into_one_prompt(self):
        rendered = rp.render_guidance(_Node(), self._full_ledger())
        self.assertIn("rogue.py", rendered)
        self.assertIn("WrittenOpinionsStage reads the wall clock", rendered)
        self.assertIn("Declared outputs: a.py", rendered)

    def test_truncation_is_marked_and_never_drops_a_surface(self):
        """B13: the budget is enforced by elision with an explicit marker, and
        every surface keeps at least its header — a silently absent surface is
        the oscillation bug reintroduced by the safety mechanism."""
        many = tuple(
            rp.ReviewFinding(f"check-{i}", f"obj-{i}", "m" * 400, True)
            for i in range(50)
        )
        ledger = self._full_ledger().with_review(
            rp.ReviewGuidance(subject_digest="d2", findings=many)
        )
        rendered = rp.render_guidance(_Node(), ledger, char_budget=800)
        self.assertLess(len(rendered), 2_000)
        self.assertIn("Verification", rendered)
        self.assertIn("Code review", rendered)
        self.assertIn("truncated", rendered)

    def test_within_budget_rendering_is_untruncated(self):
        rendered = rp.render_guidance(_Node(), self._full_ledger())
        self.assertNotIn("truncated", rendered)
        self.assertLessEqual(len(rendered), rp.GUIDANCE_CHAR_BUDGET)


class ConvergenceTests(SchedulerFixture):
    """Standing constraints converge inside one retained builder lifecycle."""

    def test_constraints_accumulate_across_surfaces_until_the_node_converges(self):
        finding = _Cell(
            "diff.introduces_no_obvious_defect",
            "a.py",
            "WrittenOpinionsStage reads the wall clock",
        )
        review_calls = {"n": 0}
        repair_calls = {"n": 0}
        worktrees = []

        def review_attempt(
            attempt, node, record, base_sha, output_sha, _resume_existing
        ):
            review_calls["n"] += 1
            return _Review(
                review_calls["n"] == 2,
                findings=() if review_calls["n"] == 2 else (finding,),
                subject_digest=output_sha,
            )

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            worktrees.append(attempt.path)
            on_launch(None)
            (attempt.path / "a.py").write_text("A0\n")
            (attempt.path / "rogue1.py").write_text("X\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def continue_node(
            attempt,
            node,
            record,
            repair_prompt,
            rejected_candidate_sha,
            builder_generation,
            cancel_requested,
        ):
            self.prompts.setdefault(node.node_id, []).append(repair_prompt)
            worktrees.append(attempt.path)
            repair_calls["n"] += 1
            if repair_calls["n"] == 1:
                (attempt.path / "rogue1.py").unlink()
                (attempt.path / "a.py").write_text("A1\n")
            elif repair_calls["n"] == 2:
                (attempt.path / "a.py").write_text("A2\n")
                (attempt.path / "rogue3.py").write_text("X\n")
            else:
                (attempt.path / "rogue3.py").unlink()
                (attempt.path / "a.py").write_text("A3\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_candidate_sha,
                builder_generation=builder_generation,
            )

        report = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=4),
            deps=self.deps(
                run_node=run_node,
                continue_node=continue_node,
                review_attempt=review_attempt,
            ),
        ).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(len(set(worktrees)), 1)
        self.assertEqual(len(self.store.attempts_for("run1", node_id="a")), 1)

        candidates = self.store.lane_candidates("run1", "a")
        reviews = self.store.candidate_reviews("run1", "a::review")
        handoff = self.store.repair_handoff("run1", "a", candidates[0].candidate_sha)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            [item.verdict for item in reviews],
            [st.ReviewVerdict.REJECTED, st.ReviewVerdict.PASS],
        )
        self.assertIsNotNone(handoff)
        self.assertIs(handoff.state, st.RepairHandoffState.ACKNOWLEDGED)
        self.assertEqual(node.output_sha, candidates[-1].candidate_sha)

        prompts = self.prompts["a"]
        self.assertEqual(len(prompts), 4)
        self.assertIsNone(prompts[0])
        self.assertIn("rogue1.py", prompts[1])
        self.assertIn("rogue1.py", prompts[2])
        self.assertIn("WrittenOpinionsStage reads the wall clock", prompts[2])
        self.assertIn("rogue3.py", prompts[3])
        self.assertIn("WrittenOpinionsStage reads the wall clock", prompts[3])
        self.assertIn("rogue1.py", prompts[3])

    def test_a_pure_verification_history_still_mutates_the_prompt(self):
        """The pre-ledger behaviour §7.5 requires is unchanged: a clause-4
        failure alone still names the offending paths in the next prompt."""

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            if retry_prompt is None:
                files = {"a.py": "A\n", "rogue.py": "X\n"}
            else:
                (attempt.path / "rogue.py").unlink()
                files = {"a.py": "A\n"}
            for rel, content in files.items():
                (attempt.path / rel).write_text(content)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def continue_node(
            attempt,
            node,
            record,
            repair_prompt,
            rejected_candidate_sha,
            builder_generation,
            cancel_requested,
        ):
            execution = run_node(
                attempt,
                node,
                record,
                repair_prompt,
                lambda _pid: None,
                cancel_requested,
            )
            return sch.RepairExecution(
                execution=execution,
                acknowledged_rejected_sha=rejected_candidate_sha,
                builder_generation=builder_generation,
            )

        self.schedule(
            [self.agent("a")],
            deps=self.deps(run_node=run_node, continue_node=continue_node),
        ).run()
        self.assertIn("rogue.py", self.prompts["a"][1])
        self.assertIs(self.store.get_node("run1", "a").state, st.NodeState.MERGED)

    def test_resume_reloads_guidance_into_the_next_prompt(self):
        """Resume rebuilds same-attempt failures from the lane retry ledger."""

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            (attempt.path / "a.py").write_text("A\n")
            if retry_prompt is None:
                (attempt.path / "rogue.py").write_text("X\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def keep_failing(
            attempt,
            node,
            record,
            repair_prompt,
            rejected_candidate_sha,
            builder_generation,
            cancel_requested,
        ):
            self.prompts.setdefault(node.node_id, []).append(repair_prompt)
            (attempt.path / "rogue.py").write_text("X\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_candidate_sha,
                builder_generation=builder_generation,
            )

        first = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node, continue_node=keep_failing),
        ).run()
        self.assertIs(first.outcome, st.RunOutcome.BLOCKED)
        spends = self.store.lane_retry_spends("run1", "a")
        rebuilt = rp.guidance_from_lane_history(spends, ())
        self.assertFalse(rebuilt.empty)

        self.store.retry("run1", "a", force=True)
        resumed = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node, continue_node=keep_failing),
        )
        resumed.project()
        self.assertTrue(any(not ledger.empty for ledger in resumed._guidance.values()))
        report = resumed.run()
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertIn("rogue.py", self.prompts["a"][-1])

    def test_the_capped_semantic_failure_persists_its_guidance(self):
        """The capped retained cycle is restored beside its predecessor."""

        def run_node(attempt, node, record, retry_prompt, on_launch, cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            (attempt.path / "a.py").write_text("A\n")
            if retry_prompt is None:
                (attempt.path / "rogue1.py").write_text("X\n")
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def introduce_second_failure(
            attempt,
            node,
            record,
            repair_prompt,
            rejected_candidate_sha,
            builder_generation,
            cancel_requested,
        ):
            self.prompts.setdefault(node.node_id, []).append(repair_prompt)
            (attempt.path / "rogue1.py").unlink()
            (attempt.path / "rogue2.py").write_text("X\n")
            return sch.RepairExecution(
                execution=sch.NodeExecution(envelope_parsed=True, exit_code=0),
                acknowledged_rejected_sha=rejected_candidate_sha,
                builder_generation=builder_generation,
            )

        first = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node, continue_node=introduce_second_failure),
        ).run()
        self.assertIs(first.outcome, st.RunOutcome.BLOCKED)
        spends = self.store.lane_retry_spends("run1", "a")
        self.assertEqual(len(spends), 2)
        self.assertIn("rogue1.py", spends[0].detail["offending_paths"])
        self.assertIn("rogue2.py", spends[1].detail["offending_paths"])

        self.store.retry("run1", "a", force=True)
        report = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node, continue_node=introduce_second_failure),
        ).run()
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertIn("rogue1.py", self.prompts["a"][-1])
        self.assertIn("rogue2.py", self.prompts["a"][-1])


class ReviewStalledClassificationTests(SchedulerFixture):
    """Reviewer stalls spend lane infrastructure budget, never builder work."""

    def test_a_reviewer_stall_does_not_re_run_the_builder(self):
        from adw_modules import code_review as cr

        class _Stall(cr.ReviewStalled):
            def __init__(self):
                RuntimeError.__init__(self, "stall")

        self.written = {"a": {"a.py": "A\n"}}
        calls = {"n": 0}

        def review_attempt(
            attempt, node, record, base_sha, output_sha, _resume_existing
        ):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Stall()
            return _Review(True)

        report = self.schedule(
            [self.agent("a")], deps=self.deps(review_attempt=review_attempt)
        ).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

        # A stall says nothing about the code: no prompt mutation (§7.5) and,
        # since #90, no second builder either.
        self.assertEqual(self.prompts["a"], [None])
        self.assertEqual(calls["n"], 2)

    def test_closed_reviewer_never_moves_generation_fence_backward(self):
        from adw_modules import code_review as cr

        class _Stall(cr.ReviewStalled):
            def __init__(self):
                RuntimeError.__init__(self, "stall")

        self.written = {"a": {"a.py": "A\n"}}

        calls = {"n": 0}

        def review_attempt(
            attempt, node, record, base_sha, output_sha, _resume_existing
        ):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Stall()
            return _Review(True)

        scheduler = self.schedule(
            [self.agent("a")], deps=self.deps(review_attempt=review_attempt)
        )
        scheduler.project()
        self.store.register_actor_session(
            "run1",
            "a",
            "reviewer",
            generation=5,
            pane_id="closed-reviewer-pane",
            session_path="/sessions/closed-reviewer",
            correlation_token="closed-reviewer",
        )
        self.assertTrue(
            self.store.close_actor_session("run1", "a", "reviewer", generation=5)
        )

        report = scheduler.run()

        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)
        self.assertEqual(calls["n"], 2)
        candidate = self.store.lane_candidates("run1", "a")[0]
        review = self.store.candidate_review(
            "run1", "a::review", candidate.candidate_sha
        )
        self.assertEqual(review.reviewer_generation, 5)
        self.assertIs(review.verdict, st.ReviewVerdict.PASS)

    def test_an_exhausted_redispatch_budget_is_environmental_and_durable(self):
        from adw_modules import code_review as cr

        class _Stall(cr.ReviewStalled):
            def __init__(self):
                RuntimeError.__init__(self, "stall")

        self.written = {"a": {"a.py": "A\n"}}
        calls = {"n": 0}

        def review_attempt(
            attempt, node, record, base_sha, output_sha, _resume_existing
        ):
            calls["n"] += 1
            raise _Stall()

        report = self.schedule(
            [self.agent("a")],
            config=self.config(environmental_retries=1, semantic_ceiling=1),
            deps=self.deps(review_attempt=review_attempt),
        ).run()

        node = self.store.get_node("run1", "a")
        spends = self.store.lane_retry_spends("run1", "a")
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)
        self.assertIs(node.state, st.NodeState.BLOCKED)
        self.assertIs(node.block_reason, st.BlockReason.ENVIRONMENTAL_BUDGET_EXHAUSTED)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(self.store.attempts_for("run1", node_id="a")), 1)
        self.assertEqual(len(self.store.lane_candidates("run1", "a")), 1)
        self.assertEqual(len(spends), 2)
        self.assertTrue(
            all(item.retry_class is st.LaneRetryClass.ENVIRONMENTAL for item in spends)
        )
        self.assertEqual(
            spends[-1].detail.get("reason"),
            "the code reviewer stalled without reporting",
        )

    def test_resume_refreshes_an_exhausted_reviewer_infrastructure_budget(self):
        from adw_modules import code_review as cr

        class _Stall(cr.ReviewStalled):
            def __init__(self):
                RuntimeError.__init__(self, "stall")

        self.written = {"a": {"a.py": "A\n"}}
        calls = {"n": 0}

        def review_attempt(
            attempt, node, record, base_sha, output_sha, _resume_existing
        ):
            calls["n"] += 1
            raise _Stall()

        config = self.config(environmental_retries=1, semantic_ceiling=1)
        deps = self.deps(
            review_attempt=review_attempt,
            recover_node=lambda _attempt, _record: sch.NodeExecution(
                envelope_parsed=True,
                envelope_payload={"success": True},
                exit_code=0,
            ),
        )
        first = self.schedule([self.agent("a")], config=config, deps=deps).run()
        self.assertIs(first.outcome, st.RunOutcome.BLOCKED)
        self.assertEqual(calls["n"], 2)

        self.store.resume_run("run1", late_envelope_attempts=(("a", 1),))
        second = self.schedule([self.agent("a")], config=config, deps=deps).run()

        self.assertIs(second.outcome, st.RunOutcome.BLOCKED)
        self.assertEqual(
            calls["n"],
            4,
            "resume charged the fresh reviewer generation for pre-boundary stalls",
        )
        self.assertEqual(len(self.store.current_lane_retry_spends("run1", "a")), 2)
        self.assertEqual(len(self.store.lane_retry_spends("run1", "a")), 4)


if __name__ == "__main__":
    unittest.main()
