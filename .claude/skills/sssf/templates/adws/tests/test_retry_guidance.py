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

    def __init__(self, passed: bool, findings=(), advisories=(),
                 subject_digest: str = "digest-1"):
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


class LedgerSemanticsTests(unittest.TestCase):
    """Accumulation across surfaces, and history within one."""

    def test_review_does_not_erase_verification_and_vice_versa(self):
        ledger = rp.GuidanceLedger()
        ledger = ledger.with_verification(rp.VerificationGuidance(
            reason="clause 4", offending_paths=("rogue.py",), failed_clause=4))
        ledger = ledger.with_review(rp.ReviewGuidance(
            subject_digest="d1",
            findings=(rp.ReviewFinding("diff.introduces_no_obvious_defect",
                                       "app.py", "naive datetime", True),)))
        self.assertEqual(len(ledger.verification), 1)
        self.assertEqual(len(ledger.review), 1)
        # And the other direction: a later verification failure keeps review.
        ledger = ledger.with_verification(rp.VerificationGuidance(
            reason="clause 4 again", offending_paths=("rogue2.py",),
            failed_clause=4))
        self.assertEqual(
            [item.offending_paths for item in ledger.verification],
            [("rogue.py",), ("rogue2.py",)])
        self.assertEqual(len(ledger.review), 1)

    def test_same_surface_history_is_appended_not_replaced(self):
        """The bug this replaced: the slot held one entry, so a second
        finding from the same surface erased the first and the next prompt
        named only the newer one. Both must survive."""
        ledger = rp.GuidanceLedger().with_review(rp.ReviewGuidance(
            subject_digest="d1",
            findings=(rp.ReviewFinding("c1", "o1", "old finding", True),)))
        ledger = ledger.with_review(rp.ReviewGuidance(
            subject_digest="d2",
            findings=(rp.ReviewFinding("c2", "o2", "new finding", True),)))
        messages = [f.message for g in ledger.review for f in g.findings]
        self.assertEqual(messages, ["old finding", "new finding"])

    def test_empty_ledger_renders_to_none(self):
        self.assertIsNone(rp.render_guidance(_Node(), rp.GuidanceLedger()))
        self.assertIsNone(rp.render_guidance(_Node(), None))


class RenderingTests(unittest.TestCase):
    def _full_ledger(self) -> rp.GuidanceLedger:
        return rp.GuidanceLedger().with_verification(
            rp.VerificationGuidance(
                reason="the measured delta failed the permission check",
                offending_paths=("rogue.py",), failed_clause=4),
        ).with_review(rp.ReviewGuidance(
            subject_digest="d1",
            findings=(rp.ReviewFinding(
                "diff.introduces_no_obvious_defect", "app.py",
                "WrittenOpinionsStage reads the wall clock", True),),
        ))

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
            for i in range(50))
        ledger = self._full_ledger().with_review(
            rp.ReviewGuidance(subject_digest="d2", findings=many))
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
    """The incident, reproduced and closed, through the real scheduler.

    clause 4 → review → clause 4 again, then an attempt that satisfies
    everything at once. Every retry prompt after the second failure must carry
    the standing constraints of BOTH surfaces, and the verification
    constraint must carry its full same-surface history: an offending path
    absent from the latest evaluation may be absent because the attempt
    stopped writing that file rather than because the constraint was
    satisfied, and the two are indistinguishable from the surface's output.
    """

    def test_constraints_accumulate_across_surfaces_until_the_node_converges(self):
        finding = _Cell("diff.introduces_no_obvious_defect", "a.py",
                        "WrittenOpinionsStage reads the wall clock")
        reviews = [_Review(False, findings=[finding]), _Review(True)]

        def review_attempt(attempt, node, record, base_sha, output_sha):
            return reviews.pop(0)

        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            files = {"a.py": "A\n"}
            if record.attempt_no == 1:
                files["rogue1.py"] = "X\n"     # clause 4, first shape
            elif record.attempt_no == 3:
                files["rogue3.py"] = "X\n"     # clause 4 again, new shape
            for rel, content in files.items():
                (attempt.path / rel).write_text(content)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        report = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=3),
            deps=self.deps(run_node=run_node,
                           review_attempt=review_attempt)).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

        prompts = self.prompts["a"]
        self.assertEqual(len(prompts), 4)
        self.assertIsNone(prompts[0])

        # After a1's clause-4 conviction: verification guidance only.
        self.assertIn("rogue1.py", prompts[1])

        # After a2's review rejection: BOTH constraints — this is the incident
        # fix. The old code overwrote the slot and rogue1.py vanished here.
        self.assertIn("rogue1.py", prompts[2])
        self.assertIn("WrittenOpinionsStage reads the wall clock", prompts[2])

        # After a3's second clause-4 conviction: the review constraint is
        # still standing, and the verification surface carries both of its
        # own entries rather than only the newer one.
        self.assertIn("rogue3.py", prompts[3])
        self.assertIn("WrittenOpinionsStage reads the wall clock", prompts[3])
        self.assertIn("rogue1.py", prompts[3])

    def test_a_pure_verification_history_still_mutates_the_prompt(self):
        """The pre-ledger behaviour §7.5 requires is unchanged: a clause-4
        failure alone still names the offending paths in the next prompt."""
        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            files = ({"a.py": "A\n", "rogue.py": "X\n"}
                     if record.attempt_no == 1 else {"a.py": "A\n"})
            for rel, content in files.items():
                (attempt.path / rel).write_text(content)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        self.schedule([self.agent("a")],
                      deps=self.deps(run_node=run_node)).run()
        self.assertIn("rogue.py", self.prompts["a"][1])
        self.assertIs(self.store.get_node("run1", "a").state,
                      st.NodeState.MERGED)

    def test_resume_reloads_guidance_into_the_next_prompt(self):
        """The durability half, driven through the store.

        A second scheduler object is constructed over the same store — the
        shape a resume actually takes — and the assertion is that its ledger
        came back from `attempts_for`, not from anything the first object
        held. The in-process ledger was the only copy, so before this the
        resumed builder was dispatched with no guidance at all.
        """
        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            files = ({"a.py": "A\n", "rogue.py": "X\n"}
                     if record.attempt_no < 3 else {"a.py": "A\n"})
            for rel, content in files.items():
                (attempt.path / rel).write_text(content)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        first = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node)).run()
        self.assertIs(first.outcome, st.RunOutcome.BLOCKED)
        rebuilt = rp.guidance_from_attempts(self.store.attempts_for("run1"))
        self.assertTrue(any(not ledger.empty for ledger in rebuilt.values()))
        self.store.retry("run1", "a", force=True)
        resumed = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node))
        resumed.project()
        self.assertEqual(set(resumed._guidance), set(rebuilt))
        resumed.run()
        self.assertIn("rogue.py", self.prompts["a"][-1])

    def test_the_capped_semantic_failure_persists_its_guidance(self):
        """Both failures reach the resumed prompt, including the capped one.

        The attempt that hits the semantic ceiling takes `mark_blocked`
        rather than `fail_attempt`, so its guidance rides a different write.
        A node blocked on the ceiling is also the one most likely to be
        resumed, which makes it the worst entry to lose. Two distinct
        offending paths go in; both come back out of the store.
        """
        def run_node(attempt, node, record, retry_prompt, on_launch,
                     cancel_requested):
            self.prompts.setdefault(node.node_id, []).append(retry_prompt)
            on_launch(None)
            files = {"a.py": "A\n"}
            if record.attempt_no == 1:
                files["rogue1.py"] = "X\n"
            elif record.attempt_no == 2:
                files["rogue2.py"] = "X\n"
            for rel, content in files.items():
                (attempt.path / rel).write_text(content)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        first = self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node)).run()
        self.assertIs(first.outcome, st.RunOutcome.BLOCKED)
        capped = next(item for item in self.store.attempts_for("run1")
                      if item.attempt_no == 2)
        self.assertIn("rogue2.py",
                      (capped.extra or {}).get(rp.GUIDANCE_KEY, {})
                      .get("offending_paths") or [])
        self.store.retry("run1", "a", force=True)
        self.schedule(
            [self.agent("a")],
            config=self.config(semantic_ceiling=2),
            deps=self.deps(run_node=run_node)).run()
        self.assertIn("rogue1.py", self.prompts["a"][-1])
        self.assertIn("rogue2.py", self.prompts["a"][-1])


class ReviewStalledClassificationTests(SchedulerFixture):
    """The reviewer-stall arm, executed through the scheduler.

    `rp.Classification(retry_class=..., reason=...)` was written at the
    scheduler's `except cr.ReviewStalled` arm before `Classification` had a
    `reason` field, so the first real reviewer stall would have raised
    TypeError inside the except handler instead of classifying ENVIRONMENTAL.
    No test drove that arm; this one does, and it also pins the §7.5 contract
    around it: a stall spends an infra retry, mutates no prompt, and its
    reason reaches the durable transition row.

    **What the stall spends changed with issue #90 and this test changed with
    it.** It used to assert two builder launches, because the stall failed the
    whole attempt and the node was re-derived from the integration head — which
    is the defect, asserted as the contract. The infra retry is now spent on
    re-dispatching the *reviewer* against the attempt's existing commit, so the
    builder launches once; the durable ENVIRONMENTAL row and its reason arrive
    only once the re-dispatch budget is spent, which is what the second case
    below drives. `tests/test_reviewer_redispatch.py` holds the rest.
    """

    def test_a_reviewer_stall_does_not_re_run_the_builder(self):
        from adw_modules import code_review as cr

        class _Stall(cr.ReviewStalled):
            def __init__(self):
                RuntimeError.__init__(self, "stall")

        self.written = {"a": {"a.py": "A\n"}}
        calls = {"n": 0}

        def review_attempt(attempt, node, record, base_sha, output_sha):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Stall()
            return _Review(True)

        report = self.schedule(
            [self.agent("a")],
            deps=self.deps(review_attempt=review_attempt)).run()

        node = self.store.get_node("run1", "a")
        self.assertIs(node.state, st.NodeState.MERGED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)

        # A stall says nothing about the code: no prompt mutation (§7.5) and,
        # since #90, no second builder either.
        self.assertEqual(self.prompts["a"], [None])
        self.assertEqual(calls["n"], 2)

    def test_an_exhausted_redispatch_budget_is_environmental_and_durable(self):
        from adw_modules import code_review as cr
        from adw_modules import lifecycle as lc

        class _Stall(cr.ReviewStalled):
            def __init__(self):
                RuntimeError.__init__(self, "stall")

        self.written = {"a": {"a.py": "A\n"}}

        def review_attempt(attempt, node, record, base_sha, output_sha):
            raise _Stall()

        self.schedule(
            [self.agent("a")],
            config=self.config(environmental_retries=1, semantic_ceiling=1),
            deps=self.deps(review_attempt=review_attempt)).run()

        # The classifier's reason survives into the durable transition row —
        # the evidence gap `_failure_detail` closes, now closed for stalls too.
        reader = lc.LifecycleReader.open(self.root / "lifecycle.db")
        try:
            rows = [t for t in reader.transitions("run1")
                    if t.get("reason") == "retry:ENVIRONMENTAL"]
        finally:
            reader.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0].get("detail", {}).get("reason"),
            "the code reviewer stalled without reporting")


if __name__ == "__main__":
    unittest.main()
