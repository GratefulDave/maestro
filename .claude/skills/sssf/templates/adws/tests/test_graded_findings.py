"""§3.6 A9, at the one place Maestro was breaking it: node code review.

A9's rule is that progress is never gated on a zero-finding LLM sweep with
restart-on-any-finding, because such a loop has no bounded termination — "bound
the loop **or** accept graded findings". Maestro had the bound (`review_ceiling`)
and not the grading, so the bound only decided how many attempts preceded
BLOCKED.

The measurement that made this a defect rather than a worry, over all 27 review
reports written by the production deployment:

    25/27  diff.introduces_no_obvious_defect        BLOCKING
    13/27  diff.implements_the_stated_instruction   BLOCKING
     6/27  diff.gate_is_passed_on_the_merits        BLOCKING

Six of the seven checks are BLOCKING and any finding on one rejected the
attempt, so acceptance required an adversarial cross-vendor reviewer to find
zero defects in a real diff. `lane-p2-s3-inventory` in
`run-2a44d226e75a4be391a14f02b78a6d25` spent five attempts on five rejections,
each round fixing the previous finding and earning a new one — swallowed
HeadObject errors, truncated pagination, an unchecked zero-byte marker, a
bucket-blind SHA grouping, a LIST/HEAD race, a `VersionId` AWS never supplies.
Every finding was correct. That is the point: the response to them was wrong,
not the finding of them.

What each group below settles:

  A9   a sub-threshold finding is recorded and merges; an at-threshold one
       rejects; the threshold is configuration and changes the outcome
  B8   rejecting without a located, justified, at-or-above-threshold finding
       is unrepresentable — the report does not parse, and the derived
       invariant convicts a planted violation besides
  §6.5 the reviewer still cannot emit a verdict or a severity, and the
       known-bad control still convicts a reviewer that is not reading
  §6.2 the threshold is a property of the installation, not of the plan

The last group is the regression control: plan finalization shares
`ObjectKind`, `compute_matrix`, `verify_report` and `derive_verdict` with node
review, and its verdicts must be **unchanged** by every one of the above. If
grading leaks across that boundary this file fails.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import pydantic  # noqa: E402

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import finalization_window as fw  # noqa: E402
from adw_modules import receipt_crypto as rc  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

from test_report_schema_guard import find_permissive_report_models  # noqa: E402
from test_scheduler import SchedulerFixture  # noqa: E402


BASE_SHA = "1" * 40
OUTPUT_SHA = "2" * 40

#: One real finding from `lane-p2-s3-inventory`, so the fixtures are the shape
#: the production reviewer actually produced rather than a placeholder.
REAL_MESSAGE = ("inventory.py:118 — the HeadObject call is wrapped in a bare "
                "except that returns None, so a permissions failure is "
                "indistinguishable from a missing key")


def make_store(tmp: Path) -> fin.ReceiptStore:
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    data_dir = tmp / "sssf-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    seed = rc.generate_seed()
    return fin.ReceiptStore(tmp / "receipts", repo_paths=(repo,),
                            data_dir=data_dir,
                            verify_keys=[rc.seed_to_public_key(seed)],
                            signing_seed=seed)


def a_node(node_id: str = "build") -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.AGENT, depth=0,
                       outputs=(f"{node_id}.py",),
                       instruction=f"Implement {node_id} as the plan declares.",
                       gate_command=("pytest",),
                       gate_selector=f"tests/{node_id}")


def a_handoff(subject_digest: str, matrix: fin.ApplicabilityMatrix,
              **kw: Any) -> cr.ReviewHandoff:
    base: Dict[str, Any] = dict(
        subject_digest=subject_digest, run_id="run1", node_id="build",
        node_kind="agent", instruction="Add the inventory materializer.",
        declared_outputs=["inventory.py"], gate_command=["pytest"],
        gate_selector="tests/inventory", base_sha=BASE_SHA,
        output_sha=OUTPUT_SHA, diff="--- a\n+++ b\n",
        matrix=[{"check_id": c.check_id, "object_id": c.object_id}
                for c in matrix.cells],
        pair_count=matrix.pair_count, report_path="/tmp/report.json",
        rubric=[{"check_id": "c", "question": "is it right?"}])
    base.update(kw)
    return cr.ReviewHandoff(**base)


# ── a scripted reviewer, answering a real matrix ────────────────────────────

class ScriptedReviewer:
    """Answers every cell of the matrix it is handed, from a per-check script.

    Deliberately built from the *real* matrix rather than from a hand-written
    cell list: the cell set, the pair count and the two controls all come from
    `compute_matrix`, so `verify_report`'s fabrication canary and control pair
    run for real against these reports instead of being stepped around.
    """

    def __init__(self, findings: Dict[str, Tuple[str, str, str]],
                 *, clear_the_known_bad: bool = False,
                 mutate: Optional[Any] = None) -> None:
        #: check_id -> (grade, message, rationale)
        self.findings = findings
        self.clear_the_known_bad = clear_the_known_bad
        self.mutate = mutate
        self.reports: List[Dict[str, Any]] = []

    def report_for(self, matrix: fin.ApplicabilityMatrix) -> Dict[str, Any]:
        cells: List[Dict[str, Any]] = []
        for cell in matrix.cells:
            if cell.canary is fin.CanaryKind.KNOWN_BAD:
                cells.append(
                    {"check_id": cell.check_id, "object_id": cell.object_id,
                     "status": "clear"} if self.clear_the_known_bad else
                    {"check_id": cell.check_id, "object_id": cell.object_id,
                     "status": "finding", "message": "the control is bad",
                     "grade": "note",
                     "grade_rationale": "it is a control, by construction"})
                continue
            if cell.canary is fin.CanaryKind.KNOWN_GOOD:
                cells.append({"check_id": cell.check_id,
                              "object_id": cell.object_id, "status": "clear"})
                continue
            scripted = self.findings.get(cell.check_id)
            if scripted is None:
                cells.append({"check_id": cell.check_id,
                              "object_id": cell.object_id, "status": "clear"})
                continue
            grade, message, rationale = scripted
            cells.append({"check_id": cell.check_id,
                          "object_id": cell.object_id, "status": "finding",
                          "message": message, "grade": grade,
                          "grade_rationale": rationale})
        report: Dict[str, Any] = {"plan_digest": matrix.plan_digest,
                                  "pair_count": matrix.pair_count,
                                  "cells": cells}
        if self.mutate is not None:
            self.mutate(report)
        self.reports.append(report)
        return report

    def window_factory(self, matrix: fin.ApplicabilityMatrix) -> "FakeWindow":
        return FakeWindow(self.report_for(matrix))


class FakeWindow:
    def __init__(self, report: Dict[str, Any]) -> None:
        self.report = report

    def run(self, sleep: Any = None) -> fw.WindowOutcome:
        return fw.WindowOutcome(
            completed=True,
            session=fw.ReviewerSession(route="omp", model="reviewer-model",
                                       session_id="w1:p2"),
            elapsed_s=1.0, report=self.report)


def run_review(tmp: Path, reviewer: ScriptedReviewer, *,
               reject_at: cr.FindingGrade = cr.DEFAULT_REJECT_GRADE,
               ledger_path: Optional[Path] = None,
               objects: Optional[Sequence[fin.ReviewObject]] = None,
               store: Optional[fin.ReceiptStore] = None,
               digest: str = "a" * 64) -> cr.ReviewOutcome:
    """One whole node review, through the real driver."""
    store = store if store is not None else make_store(tmp)
    subjects = (objects if objects is not None
                else cr.review_objects(("inventory.py",), OUTPUT_SHA))
    matrix = fin.compute_matrix(cr.CODE_RUBRIC, digest, subjects)
    return cr.review_attempt(
        subject_digest=digest,
        handoff=a_handoff(digest, matrix),
        objects=subjects, rubric=cr.CODE_RUBRIC, store=store,
        window_factory=reviewer.window_factory,
        occupancy_reader=lambda _s: 0.1,
        reject_at=reject_at, ledger_path=ledger_path)


# ── A9: a sub-threshold finding is recorded, and merges ─────────────────────

class SubThresholdFindingsAreRecordedNotRejectedTest(unittest.TestCase):

    def test_a_warning_on_a_blocking_check_is_accepted(self):
        """The whole defect, inverted. `diff.introduces_no_obvious_defect` is
        BLOCKING and carried a finding in 25 of 27 real reviews; before
        grading, every one of those rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.introduces_no_obvious_defect": (
                    "warning", REAL_MESSAGE,
                    "the node's stated work is delivered; this is a "
                    "pre-existing robustness gap the instruction did not ask "
                    "about")}))

        self.assertIs(outcome.verdict, fin.Verdict.PASS)
        self.assertTrue(outcome.passed)
        self.assertEqual((), outcome.findings)

    def test_the_finding_is_recorded_rather_than_discarded(self):
        """Grading the bar, not lowering it: a merged node still carries what
        was found in it, or this change would hide the 25/27 instead of
        ranking them."""
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.introduces_no_obvious_defect": (
                    "warning", REAL_MESSAGE, "delivered work is unaffected"),
                "diff.is_coherent_with_its_surroundings": (
                    "note", "naming drifts from the module", "cosmetic")}))

        recorded = {c.check_id: c for c in outcome.advisories}
        self.assertEqual(
            {"diff.introduces_no_obvious_defect",
             "diff.is_coherent_with_its_surroundings"}, set(recorded))
        self.assertEqual(cr.FindingGrade.WARNING,
                         recorded["diff.introduces_no_obvious_defect"].grade)
        # And it reaches the builder, so a merge is not a silent one.
        self.assertIn(REAL_MESSAGE, outcome.findings_text())

    def test_the_advisories_are_on_disk_and_read_back(self):
        """Where an operator finds what a merged node merged with."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "review" / ("a" * 8) / "findings.json"
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.introduces_no_obvious_defect": (
                    "warning", REAL_MESSAGE, "delivered work is unaffected")}),
                ledger_path=ledger)
            self.assertTrue(outcome.passed)
            self.assertTrue(ledger.exists(),
                            "a merged node recorded no advisory ledger")

            payload = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(cr.FINDING_LEDGER_SCHEMA, payload["schema"])
            self.assertEqual("PASS", payload["verdict"])
            self.assertEqual("error", payload["reject_at"])
            self.assertEqual("build", payload["node_id"])
            self.assertEqual(1, len(payload["findings"]))
            entry = payload["findings"][0]
            self.assertEqual("warning", entry["grade"])
            self.assertFalse(entry["rejecting"])
            self.assertEqual(REAL_MESSAGE, entry["message"])

            read_back = cr.read_finding_ledger(ledger)

        self.assertEqual(1, len(read_back))
        self.assertEqual(cr.FindingGrade.WARNING, read_back[0].grade)
        self.assertEqual(REAL_MESSAGE, read_back[0].message)

    def test_a_replay_carries_the_recorded_grades(self):
        """B10's replay must not silently re-partition a stored review as if
        every blocking finding had rejected it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = make_store(root)
            ledger = root / "findings.json"
            reviewer = ScriptedReviewer({
                "diff.introduces_no_obvious_defect": (
                    "warning", REAL_MESSAGE, "delivered work is unaffected")})
            first = run_review(root, reviewer, ledger_path=ledger, store=store)

            def must_not_launch(_matrix):
                raise AssertionError("a byte-identical subject re-reviewed")

            matrix = fin.compute_matrix(
                cr.CODE_RUBRIC, "a" * 64,
                cr.review_objects(("inventory.py",), OUTPUT_SHA))
            second = cr.review_attempt(
                subject_digest="a" * 64,
                handoff=a_handoff("a" * 64, matrix),
                objects=cr.review_objects(("inventory.py",), OUTPUT_SHA),
                rubric=cr.CODE_RUBRIC, store=store,
                window_factory=must_not_launch,
                occupancy_reader=lambda _s: 0.1,
                ledger_path=ledger)

        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertTrue(second.passed)
        self.assertEqual((), second.findings)
        self.assertEqual([cr.FindingGrade.WARNING],
                         [c.grade for c in second.advisories])


class SubThresholdFindingsStillMergeTest(SchedulerFixture):
    """The scheduler end, because "accepted" is only interesting if the lane
    then merges. The review runs for real here — real matrix, real report
    verification, real receipt — with only the reviewer's answers scripted."""

    def config(self, **kw):
        kw.setdefault("review_ceiling", 3)
        return super().config(**kw)

    def _review_stage(self, reviewer: ScriptedReviewer, ledgers: Dict[str, Path],
                      reject_at: cr.FindingGrade = cr.DEFAULT_REJECT_GRADE):
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
                handoff=a_handoff(digest, matrix, node_id=node.node_id),
                objects=objects, rubric=cr.CODE_RUBRIC, store=store,
                window_factory=reviewer.window_factory,
                occupancy_reader=lambda _s: 0.1,
                reject_at=reject_at, ledger_path=ledger)

        return review

    def test_a_node_with_only_sub_threshold_findings_merges(self):
        ledgers: Dict[str, Path] = {}
        reviewer = ScriptedReviewer({
            "diff.introduces_no_obvious_defect": (
                "warning", REAL_MESSAGE, "delivered work is unaffected"),
            "file.change_is_justified_by_the_instruction": (
                "note", "build.py: the docstring is longer than it needs to be",
                "cosmetic")})
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(
                          review_attempt=self._review_stage(reviewer, ledgers))
                      ).run()

        self.assertEqual(st.NodeState.MERGED.value, self.states()["build"])
        recorded = cr.read_finding_ledger(ledgers["build"])
        self.assertEqual(
            {"diff.introduces_no_obvious_defect",
             "file.change_is_justified_by_the_instruction"},
            {c.check_id for c in recorded})
        self.assertTrue(all(not c.rejects(cr.FindingGrade.ERROR)
                            for c in recorded))

    def test_one_error_grade_finding_rejects_the_same_lane(self):
        """The control for the test above, with one grade changed and nothing
        else: an ERROR still refuses the merge, so grading did not delete the
        check's teeth."""
        ledgers: Dict[str, Path] = {}
        reviewer = ScriptedReviewer({
            "diff.introduces_no_obvious_defect": (
                "error", REAL_MESSAGE,
                "the node's stated work is to materialize the inventory and it "
                "silently drops objects it cannot head")})
        self.written["build"] = {"build.py": "ok\n"}
        self.schedule([self.agent("build")],
                      deps=self.deps(
                          review_attempt=self._review_stage(reviewer, ledgers))
                      ).run()

        self.assertNotEqual(st.NodeState.MERGED.value, self.states()["build"])
        self.assertEqual(st.BlockReason.REVIEW_BUDGET_EXHAUSTED,
                         self.store.get_node("run1", "build").block_reason)


# ── A9: at or above the threshold still rejects ─────────────────────────────

class AtThresholdFindingsRejectTest(unittest.TestCase):

    def test_one_error_grade_finding_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.gate_is_passed_on_the_merits": (
                    "error", "inventory.py:41 returns the literal the test "
                    "asserts", "the behaviour the gate exists to witness is "
                    "absent")}))

        self.assertIs(outcome.verdict, fin.Verdict.FAIL)
        self.assertFalse(outcome.passed)
        self.assertEqual(["diff.gate_is_passed_on_the_merits"],
                         [c.check_id for c in outcome.findings])
        self.assertIn("must be resolved", outcome.findings_text())

    def test_an_advisory_check_never_rejects_however_it_is_graded(self):
        """§6.5's severity is still the reviewer's ceiling: it is stamped from
        the rubric in code, so an ADVISORY question cannot be escalated into a
        rejection by grading its finding `error`."""
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.is_coherent_with_its_surroundings": (
                    "error", "the naming is inconsistent",
                    "the reviewer would like this to block")}))

        self.assertTrue(outcome.passed)
        self.assertEqual(["diff.is_coherent_with_its_surroundings"],
                         [c.check_id for c in outcome.advisories])


# ── §6.2: the threshold is configuration, and it moves the outcome ──────────

class ThresholdIsConfigurationTest(unittest.TestCase):

    #: The same report both times. Only the installation's threshold differs.
    SCRIPT = {"diff.introduces_no_obvious_defect": (
        "warning", REAL_MESSAGE, "delivered work is unaffected")}

    def test_the_same_report_passes_at_error_and_fails_at_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            lenient = run_review(Path(tmp) / "a", ScriptedReviewer(self.SCRIPT),
                                 reject_at=cr.FindingGrade.ERROR)
        with tempfile.TemporaryDirectory() as tmp:
            strict = run_review(Path(tmp) / "b", ScriptedReviewer(self.SCRIPT),
                                reject_at=cr.FindingGrade.WARNING)

        self.assertIs(fin.Verdict.PASS, lenient.verdict)
        self.assertIs(fin.Verdict.FAIL, strict.verdict)
        self.assertEqual(["diff.introduces_no_obvious_defect"],
                         [c.check_id for c in strict.findings])

    def test_the_threshold_is_read_from_the_installation_config(self):
        """End to end from `maestro.config.yaml`, because a threshold the
        loader does not carry is a threshold no deployment can set."""
        import maestro

        self.assertIs(cr.FindingGrade.WARNING,
                      maestro._config_reject_grade("warning"))
        self.assertIs(cr.FindingGrade.NOTE,
                      maestro._config_reject_grade("  NOTE  "))
        with self.assertRaises(maestro._MaestroConfigurationError):
            maestro._config_reject_grade("blocking")

    def test_an_absent_key_is_a_real_threshold_not_an_absent_one(self):
        self.assertIs(cr.FindingGrade.ERROR, cr.DEFAULT_REJECT_GRADE)

    def test_the_grades_are_ordered_and_nothing_compares_them_by_hand(self):
        self.assertTrue(cr.grade_at_or_above(cr.FindingGrade.ERROR,
                                             cr.FindingGrade.WARNING))
        self.assertFalse(cr.grade_at_or_above(cr.FindingGrade.NOTE,
                                              cr.FindingGrade.WARNING))
        self.assertTrue(cr.grade_at_or_above(cr.FindingGrade.WARNING,
                                             cr.FindingGrade.WARNING))
        self.assertFalse(cr.grade_at_or_above(None, cr.FindingGrade.NOTE))


# ── B8: rejecting without a located, justified finding is unrepresentable ───

class RejectionWithoutALocatedFindingIsImpossibleTest(unittest.TestCase):
    """The half of B8 that could not be retrofitted in Strav, so it is
    enforced here from the version that introduces the grade.

    Every case below asserts the malformed report is **refused**, not that it
    is accepted with an odd verdict — the failure mode B8 names is a rejection
    with nothing behind it, and a report that does not parse cannot produce
    one.
    """

    def _refused(self, cell: Dict[str, Any]) -> None:
        with self.assertRaises(pydantic.ValidationError):
            cr.CodeReportCell.model_validate(cell)

    def test_a_finding_without_a_grade_does_not_parse(self):
        self._refused({"check_id": "c", "object_id": "o", "status": "finding",
                       "message": REAL_MESSAGE})

    def test_a_finding_without_a_message_does_not_parse(self):
        self._refused({"check_id": "c", "object_id": "o", "status": "finding",
                       "grade": "error", "message": "   ",
                       "grade_rationale": "it is broken"})

    def test_a_finding_without_a_reason_for_its_grade_does_not_parse(self):
        self._refused({"check_id": "c", "object_id": "o", "status": "finding",
                       "grade": "error", "message": REAL_MESSAGE,
                       "grade_rationale": "  "})

    def test_a_cleared_cell_carrying_a_grade_does_not_parse(self):
        self._refused({"check_id": "c", "object_id": "o", "status": "clear",
                       "grade": "error"})

    def test_a_graded_finding_parses(self):
        """§13.4's other half: the detector must acquit the real article."""
        parsed = cr.CodeReportCell.model_validate(
            {"check_id": "c", "object_id": "o", "status": "finding",
             "grade": "warning", "message": REAL_MESSAGE,
             "grade_rationale": "delivered work is unaffected"})
        self.assertIs(cr.FindingGrade.WARNING, parsed.grade)

    def test_the_whole_review_is_refused_when_a_finding_is_ungraded(self):
        """Not merely the cell: the driver must refuse, so an ungraded finding
        can never reach a receipt or reject an attempt."""

        def strip_the_grade(report: Dict[str, Any]) -> None:
            for cell in report["cells"]:
                if cell["status"] == "finding" and cell["check_id"].startswith(
                        "diff."):
                    cell.pop("grade")
                    cell.pop("grade_rationale")

        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            with self.assertRaises(pydantic.ValidationError):
                run_review(Path(tmp), ScriptedReviewer(
                    {"diff.introduces_no_obvious_defect": (
                        "error", REAL_MESSAGE, "it is broken")},
                    mutate=strip_the_grade), store=store)
            self.assertFalse(
                store.has("a" * 64),
                "a report that does not parse minted a receipt anyway")

    def test_the_derived_invariant_convicts_a_planted_fail(self):
        """B15: the invariant is an executed object, not only a schema, so a
        rewrite of the schema cannot silently drop it."""
        planted = cr.GradedVerdict(
            verdict=fin.Verdict.FAIL, reject_at=cr.FindingGrade.ERROR,
            cells=(cr.GradedCell(
                check_id="diff.introduces_no_obvious_defect",
                object_id="diff:abc", status=fin.CellStatus.CLEAR,
                severity=fin.Severity.BLOCKING, grade=None),))
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_the_derived_invariant_convicts_a_sub_threshold_fail(self):
        """The shape grading introduces: a FAIL whose only finding is graded
        below the bar that decides rejection."""
        planted = cr.GradedVerdict(
            verdict=fin.Verdict.FAIL, reject_at=cr.FindingGrade.ERROR,
            cells=(cr.GradedCell(
                check_id="diff.introduces_no_obvious_defect",
                object_id="diff:abc", status=fin.CellStatus.FINDING,
                severity=fin.Severity.BLOCKING, grade=cr.FindingGrade.WARNING,
                message=REAL_MESSAGE, rationale="unaffected"),))
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_the_derived_invariant_convicts_a_pass_that_carries_a_rejection(self):
        """The inverted shape, which is the one that would silently merge."""
        planted = cr.GradedVerdict(
            verdict=fin.Verdict.PASS, reject_at=cr.FindingGrade.ERROR,
            cells=(cr.GradedCell(
                check_id="diff.introduces_no_obvious_defect",
                object_id="diff:abc", status=fin.CellStatus.FINDING,
                severity=fin.Severity.BLOCKING, grade=cr.FindingGrade.ERROR,
                message=REAL_MESSAGE, rationale="the work is not done"),))
        with self.assertRaises(cr.VerdictNotLocated):
            cr.require_located_findings(planted)

    def test_the_derived_invariant_acquits_the_real_article(self):
        cr.require_located_findings(cr.GradedVerdict(
            verdict=fin.Verdict.FAIL, reject_at=cr.FindingGrade.ERROR,
            cells=(cr.GradedCell(
                check_id="diff.introduces_no_obvious_defect",
                object_id="diff:abc", status=fin.CellStatus.FINDING,
                severity=fin.Severity.BLOCKING, grade=cr.FindingGrade.ERROR,
                message=REAL_MESSAGE, rationale="the work is not done"),)))
        cr.require_located_findings(cr.GradedVerdict(
            verdict=fin.Verdict.PASS, reject_at=cr.FindingGrade.ERROR,
            cells=(cr.GradedCell(
                check_id="diff.introduces_no_obvious_defect",
                object_id="diff:abc", status=fin.CellStatus.FINDING,
                severity=fin.Severity.BLOCKING, grade=cr.FindingGrade.WARNING,
                message=REAL_MESSAGE, rationale="unaffected"),)))


# ── §6.5: the reviewer still cannot say what it must not say ───────────────

class TheReviewerStillCannotDeclareAVerdictTest(unittest.TestCase):

    def test_the_graded_schema_declares_no_verdict_and_no_severity(self):
        self.assertEqual([], fin.find_forbidden_report_fields(
            cr.CodeReviewerReport))

    def test_the_graded_schema_accepts_no_unknown_key(self):
        self.assertEqual([], find_permissive_report_models(
            cr.CodeReviewerReport))

    def test_a_smuggled_severity_is_refused_unparsed(self):
        with self.assertRaises(pydantic.ValidationError):
            cr.CodeReportCell.model_validate(
                {"check_id": "c", "object_id": "o", "status": "clear",
                 "severity": "BLOCKING"})

    def test_severity_is_stamped_from_the_rubric_not_from_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({
                "diff.introduces_no_obvious_defect": (
                    "note", REAL_MESSAGE, "cosmetic")}))
            stamped = {c.check_id: c.severity for c in outcome.receipt.cells}

        self.assertIs(fin.Severity.BLOCKING,
                      stamped["diff.introduces_no_obvious_defect"])
        self.assertIs(fin.Severity.ADVISORY,
                      stamped["diff.is_coherent_with_its_surroundings"])


class TheReviewerIsToldHowToGradeTest(unittest.TestCase):
    """B9's rule applied to A9's field: a schema requirement the prompt never
    states is a report that never parses. The assertion is on the rendered
    bytes, because that is what reaches the reviewer."""

    def _rendered(self) -> str:
        matrix = fin.compute_matrix(
            cr.CODE_RUBRIC, "a" * 64,
            cr.review_objects(("inventory.py",), OUTPUT_SHA))
        return a_handoff("a" * 64, matrix).render()

    def test_the_prompt_names_every_grade_and_the_field_that_carries_it(self):
        text = self._rendered()
        for expected in ("grade", "grade_rationale", "error", "warning",
                         "note"):
            self.assertIn(expected, text)

    def test_the_prompt_still_forbids_a_verdict_and_a_severity(self):
        self.assertIn("Do not write a verdict, a severity, or a score",
                      self._rendered())

    def test_the_prompt_shows_a_cleared_cell_without_a_grade(self):
        """The shape that would otherwise refuse a whole honest report: a
        reviewer copying one skeleton onto every cell would put a grade on the
        cleared ones."""
        text = self._rendered()
        self.assertIn('"status": "clear"', text)
        self.assertIn("A `clear` cell carries no `grade`", text)


class TheCanaryStillConvictsTest(unittest.TestCase):
    """§6.5's control pair, unchanged by grading. A reviewer that clears the
    known-bad cell is not reading, and must be refused before any verdict
    exists — grading must not have introduced a path around that."""

    def test_a_cleared_known_bad_control_is_rejected_before_any_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(Path(tmp))
            with self.assertRaises(fin.ReportRejected) as caught:
                run_review(Path(tmp), ScriptedReviewer(
                    {}, clear_the_known_bad=True), store=store)
            self.assertIs(fin.RejectionReason.CANARY_KNOWN_BAD_CLEARED,
                          caught.exception.reason)
            self.assertFalse(store.has("a" * 64),
                             "a reviewer that cleared the control minted a "
                             "receipt")

    def test_the_known_bad_control_never_rejects_the_diff_itself(self):
        """It is answered `finding` by construction, so counting it would fail
        every diff ever reviewed."""
        with tempfile.TemporaryDirectory() as tmp:
            outcome = run_review(Path(tmp), ScriptedReviewer({}))

        self.assertTrue(outcome.passed)
        self.assertEqual((), outcome.findings)
        self.assertEqual((), outcome.advisories)


# ── the regression control: plan finalization is untouched ──────────────────

class PlanFinalizationVerdictsAreUnchangedTest(unittest.TestCase):
    """Node review and plan finalization share `ObjectKind`, `compute_matrix`,
    `verify_report` and `derive_verdict`. What isolates them is that grading
    lives entirely in `code_review`: a second report model, a second derivation
    function, a second located-findings invariant. `finalization` has no
    threshold to be handed and no grade to read.

    This class fails if any of that leaks.
    """

    PLAN_OBJECTS = (fin.ReviewObject(object_id="plan:1", kind=fin.ObjectKind.PLAN),
                    fin.ReviewObject(object_id="node:a", kind=fin.ObjectKind.NODE))

    def _matrix(self) -> fin.ApplicabilityMatrix:
        return fin.compute_matrix(fin.DEFAULT_RUBRIC, "c" * 64,
                                  self.PLAN_OBJECTS)

    def _report(self, matrix: fin.ApplicabilityMatrix,
                findings: Dict[str, str]) -> fin.ReviewerReport:
        cells = []
        for cell in matrix.cells:
            if cell.canary is fin.CanaryKind.KNOWN_BAD:
                cells.append({"check_id": cell.check_id,
                              "object_id": cell.object_id,
                              "status": "finding", "message": "control"})
            elif cell.check_id in findings:
                cells.append({"check_id": cell.check_id,
                              "object_id": cell.object_id,
                              "status": "finding",
                              "message": findings[cell.check_id]})
            else:
                cells.append({"check_id": cell.check_id,
                              "object_id": cell.object_id, "status": "clear"})
        return fin.ReviewerReport.model_validate(
            {"plan_digest": matrix.plan_digest,
             "pair_count": matrix.pair_count, "cells": cells})

    def test_any_blocking_finding_still_fails_a_plan(self):
        """Plan finalization has no threshold: one blocking finding is a FAIL,
        exactly as before. If grading had leaked into `derive_verdict` this
        would pass instead."""
        matrix = self._matrix()
        report = self._report(matrix, {"node.reads_are_sufficient":
                                       "the node cannot work from its reads"})
        derived = fin.derive_verdict(matrix, report, fin.DEFAULT_RUBRIC)
        self.assertIs(fin.Verdict.FAIL, derived.verdict)

    def test_an_advisory_finding_still_passes_a_plan(self):
        matrix = self._matrix()
        report = self._report(matrix, {"plan.decomposition_is_honest":
                                       "one node wearing two names"})
        derived = fin.derive_verdict(matrix, report, fin.DEFAULT_RUBRIC)
        self.assertIs(fin.Verdict.PASS, derived.verdict)

    def test_a_plan_reviewer_cannot_grade_and_is_refused_if_it_tries(self):
        """The isolation, from the other side: the plan report schema did not
        grow a grade, so a plan reviewer that emits one is rejected unparsed
        rather than silently graded."""
        with self.assertRaises(pydantic.ValidationError):
            fin.ReportCell.model_validate(
                {"check_id": "c", "object_id": "o", "status": "finding",
                 "message": "m", "grade": "warning"})

    def test_a_plan_finding_still_needs_no_grade_rationale(self):
        """The plan reviewer's contract is unchanged: `clear|finding` plus a
        message, as §6.5 states it."""
        parsed = fin.ReportCell.model_validate(
            {"check_id": "c", "object_id": "o", "status": "finding",
             "message": "m"})
        self.assertIs(fin.CellStatus.FINDING, parsed.status)

    def test_finalizations_derivation_takes_no_threshold(self):
        """Stated as an executed fact rather than as a comment: the function
        plan finalization uses has no parameter grading could ride in on."""
        import inspect
        self.assertEqual(
            ["matrix", "report", "rubric"],
            list(inspect.signature(fin.derive_verdict).parameters))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class GradeSurvivesTheReceipt(unittest.TestCase):
    """The grade that decided the verdict is inside the signed receipt.

    Without this the receipt records a conclusion whose derivation cannot be
    re-checked from the receipt alone: an auditor reading a FAIL could not
    tell whether it was an ERROR that rejected or a WARNING that should not
    have. §3.6 B8 is why it is here from the first version that grades
    anything rather than added later as an optional field.
    """

    def _cell(self, grade):
        return fin.DerivedCell(
            check_id="diff.introduces_no_obvious_defect",
            object_id="diff:abc", status=fin.CellStatus.FINDING,
            severity=fin.Severity.BLOCKING, message="a located defect",
            grade=grade)

    def test_a_graded_cell_round_trips_through_the_receipt_bytes(self):
        receipt = fin.Receipt(
            plan_digest="d" * 64, rubric_version="maestro-rubric.v2",
            verdict=fin.Verdict.FAIL, cells=(self._cell("error"),),
            reviewer=fin.ReviewerIdentity(route="omp", model="m", session_id="s"),
            created_at_epoch=1)
        restored = fin.Receipt.from_bytes(receipt.to_bytes())
        self.assertEqual(restored.cells[0].grade, "error")
        self.assertEqual(restored.verdict, fin.Verdict.FAIL)

    def test_an_ungraded_cell_round_trips_as_none(self):
        receipt = fin.Receipt(
            plan_digest="e" * 64, rubric_version="maestro-rubric.v2",
            verdict=fin.Verdict.PASS, cells=(self._cell(None),),
            reviewer=fin.ReviewerIdentity(route="omp", model="m", session_id="s"),
            created_at_epoch=1)
        self.assertIsNone(fin.Receipt.from_bytes(receipt.to_bytes()).cells[0].grade)

    def test_a_receipt_without_the_grade_field_is_refused(self):
        """The frozen schema requires the key, so an old-shaped cell fails
        closed rather than silently deriving `None` for a grade that existed."""
        receipt = fin.Receipt(
            plan_digest="f" * 64, rubric_version="maestro-rubric.v2",
            verdict=fin.Verdict.FAIL, cells=(self._cell("error"),),
            reviewer=fin.ReviewerIdentity(route="omp", model="m", session_id="s"),
            created_at_epoch=1)
        payload = json.loads(receipt.to_bytes())
        del payload["cells"][0]["grade"]
        with self.assertRaises(fin.ReceiptInvalid):
            fin.Receipt.from_bytes(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
