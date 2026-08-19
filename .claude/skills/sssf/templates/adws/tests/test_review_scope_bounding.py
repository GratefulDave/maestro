"""§3.6 B9 at the place Maestro was breaking it: a demand no diff could satisfy.

`run-2a44d226e75a4be391a14f02b78a6d25`, node `lane-p4-enrichment-ordering`,
ended BLOCKED on `REVIEW_BUDGET_EXHAUSTED` after eight attempts. Its builder
prompt bounded it to two paths:

    src/lexgenius_pipeline/ingestion/judicial/cmo/enrichment_gate.py
    tests/unit/ingestion/test_cmo_enrichment_gate.py

The reviewer rejected all six reviews on `diff.implements_the_stated_instruction`
at grade `error`, with findings of the form *"changes no production caller to
use them"*, *"no existing production entry point is changed to import it"*,
*"the diff adds no import from an existing production module"*. The caller it
wanted was a **different, already-merged node's** declared output.
`plan_validate`'s single-producer rule forbade this node from declaring it, and
`worktree`'s permission check convicts any attempt that writes an undeclared
path. So the reviewer rejected every diff that did not touch it and the
permission check rejected every diff that did — a predicate with no satisfying
assignment, burning the whole review ceiling and landing on a block reason that
reads "the agent could not do the work" and is false. The work was fine: the
gate's case count rose 13 → 25 → 27 → 28 → 32 → 34, green every time, with a
distinct `review_subject_digest` per attempt.

The defect is not the reviewer's answer. It is that the rubric asked a question
the declared contract could not bound: B9 says the reviewer's input is a
declared contract of goal, `produces` and acceptance, and `ReviewHandoff.render`
does present the permitted paths — under "## Paths this node was permitted to
write" — while the question said only "does this diff do what the node was asked
to do". A reviewer that read the paths and ignored them for the question was
reading the rubric correctly.

What each group below settles:

  B9   every BLOCKING question is asked inside the declared write scope, and
       the reviewer is told so in the same prompt that lists the paths
  B8   `scope` is required on every finding from this schema's first version,
       so the axis cannot be optional-forever
  §1.2 the record that changes the outcome is typed — a check id, a severity
       stamped by code, a grade enum and a scope enum — never the reviewer's
       prose about being unable to comply
  A9   the ordinary rejection is untouched: a lazy diff inside its declared
       paths still refuses to merge, still spends the ceiling, still blocks
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import pydantic  # noqa: E402

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_graded_findings import (ScriptedReviewer, a_handoff,  # noqa: E402
                                  make_store, run_review)
from test_scheduler import SchedulerFixture  # noqa: E402


#: The finding verbatim from the incident, so the fixtures are the shape the
#: production reviewer actually produced.
UNREACHABLE_MESSAGE = (
    "enrichment_gate.py:1 — the module defines the ordering predicate but the "
    "diff changes no production caller to use it; no existing production entry "
    "point is changed to import it")

#: The consequence, honestly stated. Note that it is an ERROR by the rubric's
#: own anchor — "the node's stated work not actually done" — which is why
#: grading alone could never have carried this case.
UNREACHABLE_RATIONALE = (
    "the feature is unreachable at runtime, so the stated work is not "
    "delivered end to end")

#: The control: the same grade and the same check, inside the node's own paths.
LAZY_MESSAGE = (
    "enrichment_gate.py:41 — the predicate returns the constant the one "
    "assertion checks and never reads the ordering it was asked to enforce")

LAZY_RATIONALE = (
    "the behaviour the node was asked for is absent from the file the node "
    "owns and does write")


# ── B9: the questions are bounded, and the reviewer is told the bound ───────

class TheRubricBoundsItsDemandsTest(unittest.TestCase):
    """The two checks that carried the unbounded shape, and the five that did
    not.

    The audit is stated as a test rather than a comment because the shape
    recurs: a question is unbounded when a *truthful* answer to it can require
    an edit outside the node's declared paths. `no_unrelated_change_rides_along`
    and both `file.*` checks are answered by writing **less**, never more, so
    they cannot demand a forbidden path. `is_coherent_with_its_surroundings` is
    ADVISORY and cannot reject however it is answered.
    `introduces_no_obvious_defect` is already bounded by its own text to "the
    changed code". The remaining two asked about the whole of the node's stated
    work and about the whole of the gate, and both fired on the incident node.
    """

    BOUNDED = ("diff.implements_the_stated_instruction",
               "diff.gate_is_passed_on_the_merits")

    def test_the_two_unbounded_checks_now_name_the_declared_write_scope(self):
        for check_id in self.BOUNDED:
            question = cr.CODE_RUBRIC.check(check_id).question
            self.assertIn("permitted", question, check_id)
            self.assertIn("scope", question, check_id)

    def test_the_audit_found_exactly_two_and_the_rest_are_unchanged(self):
        """The other five are named here so that adding a check without asking
        the question forces this list to be revisited."""
        self.assertEqual(
            {"diff.no_unrelated_change_rides_along",
             "diff.introduces_no_obvious_defect",
             "diff.is_coherent_with_its_surroundings",
             "file.change_is_justified_by_the_instruction",
             "file.no_secret_or_credential_introduced"},
            {c.check_id for c in cr.CODE_RUBRIC.checks
             if c.check_id not in self.BOUNDED})

    def test_the_rubric_version_moved_so_a_v1_receipt_cannot_replay(self):
        """B10 replays a byte-identical subject, and `review_digest` binds the
        rubric version. Changing what a question asks without moving the
        version would replay an answer given to a different question."""
        self.assertEqual("maestro-code-rubric.v2", cr.CODE_RUBRIC.version)

    def test_the_prompt_states_the_bound_beside_the_paths_it_bounds(self):
        matrix = fin.compute_matrix(
            cr.CODE_RUBRIC, "a" * 64, cr.review_objects(("a.py",), "b" * 40))
        text = a_handoff("a" * 64, matrix,
                         declared_outputs=["enrichment_gate.py"]).render()

        paths_at = text.index("## Paths this node was permitted to write")
        bound_at = text.index("Every question below is asked inside those paths")
        self.assertLess(paths_at, bound_at)
        self.assertIn("enrichment_gate.py", text[paths_at:bound_at])
        self.assertIn("out_of_scope", text)
        self.assertIn("in_scope | out_of_scope", text)

    def test_the_prompt_forbids_softening_an_out_of_scope_finding(self):
        """The failure mode grading alone would have produced: a reviewer told
        to record the finding at a lower grade is being told to misreport the
        consequence, and A9's remedy is grading the bar, never lowering it."""
        matrix = fin.compute_matrix(
            cr.CODE_RUBRIC, "a" * 64, cr.review_objects(("a.py",), "b" * 40))
        text = a_handoff("a" * 64, matrix).render()

        self.assertIn("Grade and scope are independent", text)
        self.assertIn("do not soften it to a warning to get it through", text)


# ── B8: the axis is structurally required, from v1 ──────────────────────────

class TheScopeAxisIsNotOptionalTest(unittest.TestCase):
    """B8's rule is that a field added after reports exist is optional forever.
    An optional scope is one every reviewer omits, and an omitted scope reads
    as in-scope, which is the unbounded demand again."""

    def _cell(self, **kw: Any) -> Dict[str, Any]:
        base = {"check_id": "diff.implements_the_stated_instruction",
                "object_id": "diff:" + "b" * 40, "status": "finding",
                "message": UNREACHABLE_MESSAGE, "grade": "error",
                "grade_rationale": UNREACHABLE_RATIONALE,
                "scope": "out_of_scope"}
        base.update(kw)
        return base

    def test_the_real_article_parses(self):
        cell = cr.CodeReportCell.model_validate(self._cell())
        self.assertIs(cr.FindingScope.OUT_OF_SCOPE, cell.scope)

    def test_a_finding_without_a_scope_does_not_parse(self):
        payload = self._cell()
        payload.pop("scope")
        with self.assertRaises(pydantic.ValidationError) as caught:
            cr.CodeReportCell.model_validate(payload)
        self.assertIn("scope", str(caught.exception))

    def test_a_cleared_cell_carrying_a_scope_does_not_parse(self):
        with self.assertRaises(pydantic.ValidationError):
            cr.CodeReportCell.model_validate(
                {"check_id": "diff.implements_the_stated_instruction",
                 "object_id": "diff:" + "b" * 40, "status": "clear",
                 "scope": "in_scope"})

    def test_an_unknown_scope_does_not_parse(self):
        with self.assertRaises(pydantic.ValidationError):
            cr.CodeReportCell.model_validate(self._cell(scope="maybe"))

    def test_scope_is_still_not_a_verdict_or_a_severity(self):
        """§6.5's unrepresentability claim is untouched: the third axis says
        where a fix lands, and code alone turns that into an outcome."""
        fields = set(cr.CodeReportCell.model_fields)
        self.assertNotIn("verdict", fields)
        self.assertNotIn("severity", fields)


# ── §1.2: the typed record, and what it does to the derived verdict ─────────

class AForbiddenRemedyCannotRejectTest(unittest.TestCase):

    def _run(self, scope: str, grade: str = "error") -> cr.ReviewOutcome:
        with tempfile.TemporaryDirectory() as tmp:
            return run_review(Path(tmp), ScriptedReviewer({
                "diff.implements_the_stated_instruction": (
                    grade, UNREACHABLE_MESSAGE, UNREACHABLE_RATIONALE, scope)}))

    def test_an_out_of_scope_error_does_not_refuse_the_merge(self):
        outcome = self._run("out_of_scope")

        self.assertIs(fin.Verdict.PASS, outcome.verdict)
        self.assertEqual((), outcome.findings)
        self.assertEqual(1, len(outcome.unreachable))
        self.assertTrue(outcome.instruction_unreachable)

    def test_the_record_that_says_so_is_typed_and_not_prose(self):
        """§1.2 — the fields that changed the outcome are a check id, a
        severity stamped by code, a grade enum and a scope enum. The message is
        carried for a human and reads nothing."""
        cell = self._run("out_of_scope").unreachable[0]

        self.assertEqual("diff.implements_the_stated_instruction",
                         cell.check_id)
        self.assertIs(fin.Severity.BLOCKING, cell.severity)
        self.assertIs(cr.FindingGrade.ERROR, cell.grade)
        self.assertIs(cr.FindingScope.OUT_OF_SCOPE, cell.scope)

    def test_the_same_finding_in_scope_still_refuses_the_merge(self):
        """The control that keeps the check from being weakened into
        uselessness: one word of the report changes, nothing else."""
        outcome = self._run("in_scope")

        self.assertIs(fin.Verdict.FAIL, outcome.verdict)
        self.assertEqual(1, len(outcome.findings))
        self.assertEqual((), outcome.unreachable)
        self.assertFalse(outcome.instruction_unreachable)

    def test_a_sub_threshold_out_of_scope_finding_is_an_ordinary_advisory(self):
        """`unreachable` is the complement of `rejects` on the scope axis and
        on that axis only: a warning was never going to reject, so it stays
        where it was."""
        outcome = self._run("out_of_scope", grade="warning")

        self.assertIs(fin.Verdict.PASS, outcome.verdict)
        self.assertEqual((), outcome.unreachable)
        self.assertEqual(1, len(outcome.advisories))

    def test_an_advisory_check_cannot_reach_the_unreachable_partition(self):
        """Severity still binds first. An ADVISORY question cannot refuse a
        merge, so it also cannot claim the plan is broken."""
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.is_coherent_with_its_surroundings": (
                    "error", "it does not match the module's conventions",
                    "the merged tree reads inconsistently", "out_of_scope")}))

        self.assertIs(fin.Verdict.PASS, outcome.verdict)
        self.assertEqual((), outcome.unreachable)

    def test_an_unlocated_out_of_scope_claim_is_refused(self):
        """The plan-level record is held to B8's bar too: a claim that the
        plan is broken and locates nothing is the contentless verdict one
        level up."""
        graded = cr.GradedVerdict(
            verdict=fin.Verdict.PASS,
            cells=(cr.GradedCell(
                check_id="diff.implements_the_stated_instruction",
                object_id="diff:" + "b" * 40,
                status=fin.CellStatus.FINDING,
                severity=fin.Severity.BLOCKING,
                grade=cr.FindingGrade.ERROR, message="   ",
                rationale=UNREACHABLE_RATIONALE,
                scope=cr.FindingScope.OUT_OF_SCOPE),),
            reject_at=cr.FindingGrade.ERROR)

        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(graded)

    def test_a_receipt_replay_reads_no_scope_as_in_scope(self):
        """The receipt's frozen schema carries no scope, so a cell rebuilt from
        one is read exactly as it was before the axis existed. A replay must
        return the verdict that was signed, never a re-derivation of it."""
        cell = cr.GradedCell.from_receipt_cell(fin.DerivedCell(
            check_id="diff.implements_the_stated_instruction",
            object_id="diff:" + "b" * 40, status=fin.CellStatus.FINDING,
            severity=fin.Severity.BLOCKING, message=UNREACHABLE_MESSAGE,
            grade="error"))

        self.assertIsNone(cell.scope)
        self.assertFalse(cell.out_of_scope)
        self.assertTrue(cell.rejects(cr.FindingGrade.ERROR))
        self.assertFalse(cell.unreachable(cr.FindingGrade.ERROR))


# ── the lane, end to end: the trap, and the ordinary rejection ──────────────

class TheReviewLaneFixture(SchedulerFixture):
    """The scheduler end. The review runs for real — real matrix, real report
    verification, real graded derivation, real signed receipt — with only the
    reviewer's answers scripted, so the path under test is the production one.
    """

    def config(self, **kw):
        kw.setdefault("review_ceiling", 3)
        return super().config(**kw)

    def _review_stage(self, reviewer: ScriptedReviewer,
                      ledgers: Dict[str, Path]):
        store = make_store(self.root / "review-store")

        def review(attempt, node, record, base_sha, output_sha):
            digest = cr.review_digest(
                run_id="run1", node_id=node.node_id, base_sha=base_sha,
                output_sha=output_sha,
                rubric_version=cr.CODE_RUBRIC.version)
            ledger = self.root / "review" / digest / "findings.json"
            ledgers[node.node_id] = ledger
            objects = cr.review_objects((f"{node.node_id}.py",), output_sha)
            matrix = fin.compute_matrix(cr.CODE_RUBRIC, digest, objects)
            return cr.review_attempt(
                subject_digest=digest,
                handoff=a_handoff(digest, matrix, node_id=node.node_id,
                                  declared_outputs=[f"{node.node_id}.py"]),
                objects=objects, rubric=cr.CODE_RUBRIC, store=store,
                window_factory=reviewer.window_factory,
                occupancy_reader=lambda _s: 0.1,
                ledger_path=ledger)

        return review

    def run_lane(self, reviewer: ScriptedReviewer) -> Dict[str, Path]:
        ledgers: Dict[str, Path] = {}
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(
                          review_attempt=self._review_stage(reviewer, ledgers))
                      ).run()
        return ledgers


class TheTrapNoLongerConsumesTheCeilingTest(TheReviewLaneFixture):
    """`lane-p4-enrichment-ordering`, reproduced: the reviewer answers the
    instruction check ERROR on every attempt, and the only edit that would
    clear it writes a path this node may not write."""

    def test_the_lane_stops_on_the_first_review_instead_of_the_third(self):
        reviewer = ScriptedReviewer({
            "diff.implements_the_stated_instruction": (
                "error", UNREACHABLE_MESSAGE, UNREACHABLE_RATIONALE,
                "out_of_scope")})
        self.run_lane(reviewer)

        node = self.store.get_node("run1", "build")
        self.assertEqual(st.NodeState.MERGED.value, self.states()["build"])
        self.assertIsNone(node.block_reason)
        # The number that was 6 in production, against a ceiling of 3 here.
        self.assertEqual(1, len(reviewer.reports))

    def test_the_block_reason_it_used_to_land_on_is_gone(self):
        """`REVIEW_BUDGET_EXHAUSTED` reads "the agent could not do the work".
        For this node that was false — the gate went green on every one of the
        six attempts — so the assertion is that the reason never appears."""
        self.run_lane(ScriptedReviewer({
            "diff.implements_the_stated_instruction": (
                "error", UNREACHABLE_MESSAGE, UNREACHABLE_RATIONALE,
                "out_of_scope")}))

        self.assertNotEqual(
            st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
            self.store.get_node("run1", "build").block_reason)

    def test_the_plan_level_fact_survives_into_the_ledger(self):
        """Not discarded. The merged node's ledger carries the finding, its
        grade, its scope, and the flag an operator reads to learn that a
        blocking ERROR went unfixed because no path this node could write
        would have fixed it."""
        ledgers = self.run_lane(ScriptedReviewer({
            "diff.implements_the_stated_instruction": (
                "error", UNREACHABLE_MESSAGE, UNREACHABLE_RATIONALE,
                "out_of_scope")}))

        recorded = cr.read_finding_ledger(ledgers["build"])
        cells = [c for c in recorded
                 if c.check_id == "diff.implements_the_stated_instruction"]
        self.assertEqual(1, len(cells))
        self.assertIs(cr.FindingScope.OUT_OF_SCOPE, cells[0].scope)
        self.assertIs(cr.FindingGrade.ERROR, cells[0].grade)
        self.assertTrue(cells[0].unreachable(cr.FindingGrade.ERROR))
        self.assertFalse(cells[0].rejects(cr.FindingGrade.ERROR))


class TheOrdinaryRejectionIsUntouchedTest(TheReviewLaneFixture):
    """The control for the class above, and the reason the fix is a bound
    rather than a hole: a diff that stayed inside its declared paths and did
    something adjacent to its instruction is still refused, still spends the
    ceiling, and still blocks."""

    def test_a_lazy_diff_inside_its_declared_paths_still_blocks_the_lane(self):
        reviewer = ScriptedReviewer({
            "diff.implements_the_stated_instruction": (
                "error", LAZY_MESSAGE, LAZY_RATIONALE, "in_scope")})
        self.run_lane(reviewer)

        node = self.store.get_node("run1", "build")
        self.assertNotEqual(st.NodeState.MERGED.value, self.states()["build"])
        self.assertEqual(st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                         node.block_reason)
        self.assertEqual(3, len(reviewer.reports))

    def test_a_mixed_review_rejects_and_tells_the_builder_what_not_to_try(self):
        """One finding of each scope. The lane is refused on the in-scope one,
        and the retry guidance names the out-of-scope one under a heading that
        tells the builder not to attempt it — without which the next attempt is
        spent discovering that the permission check convicts it."""
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.implements_the_stated_instruction": (
                    "error", UNREACHABLE_MESSAGE, UNREACHABLE_RATIONALE,
                    "out_of_scope"),
                "diff.gate_is_passed_on_the_merits": (
                    "error", LAZY_MESSAGE, LAZY_RATIONALE, "in_scope")}))

        self.assertIs(fin.Verdict.FAIL, outcome.verdict)
        self.assertEqual(["diff.gate_is_passed_on_the_merits"],
                         [c.check_id for c in outcome.findings])
        self.assertEqual(["diff.implements_the_stated_instruction"],
                         [c.check_id for c in outcome.unreachable])

        text = outcome.findings_text()
        self.assertIn("Out of this node's scope", text)
        self.assertIn("Do NOT attempt them", text)
        self.assertIn(UNREACHABLE_MESSAGE, text)
        self.assertIn(LAZY_MESSAGE, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
