"""Code review as a derived node kind: the diff a build lane produced, read
by a model that did not write it, before anything merges.

Until this module existed a run merged a lane's code on **test results alone**.
Gates prove a stated behaviour is present at a base; nothing read the diff, so
every failure a passing test cannot see — a scope-creeping change, a plausible
implementation of the wrong thing, a hardcoded return that satisfies the one
assertion written for it — merged green. B12 is the recorded shape of that
gap: the integrator/fixer path could declare `verified` on the strength of its
own acceptance re-run, and `quality_gate.review` was presence-checked and never
read by any runner.

**What this module reuses, and why it is not a second implementation.**
`finalization` already owns the whole review protocol: a rubric whose severity
lives in code, a matrix computed in code so two reviewers get identical
coverage, a report schema in which `verdict` and `severity` are
*unrepresentable*, canary cells that convict a reviewer who is not reading,
an occupancy gate that convicts on NULL, verdict derivation in code, and a
create-once Ed25519-signed receipt. Every one of those applies unchanged to a
diff. What is new here is only the *subject*: the objects are a diff and its
changed files instead of a plan and its nodes, and the digest binds a build
attempt instead of plan bytes. Writing a second review pipeline for code would
be the divergence this module exists to avoid.

**Why the receipt keys the transition.** §1.2 fails any run where a lifecycle
transition is caused by pane text, prompt text, a free-text envelope field, or
an agent's claim about its own work. A reviewer's prose is all four. So the
reviewer answers only `clear|finding` per cell, code derives the verdict from
the rubric's severities, and the merge is gated on a **signed receipt** whose
signature the store re-verifies on load. The transition is caused by the
receipt, not by anything the reviewer said.

The named invariants this module carries, each a testable object because B15's
lesson is that an invariant which is only a field is an invariant a rewrite
silently drops:

* `CodeReportCell`            — A9 + B8: a finding carries a reviewer-assigned
  grade, a message that locates it, and a reason for the grade, or the report
  does not parse. Severity is still stamped from the rubric by code and is
  still unrepresentable to the reviewer (§6.5); the grade is the second axis,
  and code alone decides what it means.
* `require_located_findings`  — B8: FAIL is unrepresentable without at least
  one located finding graded at or above the configured threshold.
* `ReviewHandoff`             — B9: the reviewer's input is a declared,
  validated contract, never an incidental assembly.
* `review_digest`             — B10: byte-identical bytes replay the stored
  verdict instead of being re-reviewed into a different answer.
* `require_distinct_vendor`   — B12: no actor reviews its own output.
* `preflight_handoff`         — B13: size-check the input against the
  reviewer's context window before dispatch, and fail closed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

from pydantic import BaseModel, ConfigDict, model_validator

from . import finalization as fin
from . import finalization_window as fw
from . import handoff_budget as hb
from . import scheduler_types as st


# ── the review subject: a diff, and the files it touches ────────────────────

#: A diff and each changed file are review objects in exactly the sense
#: `finalization.ObjectKind` already means. Extending that enum rather than
#: minting a parallel one is what lets `compute_matrix`, `verify_report`,
#: `derive_verdict`, `Receipt`, and `ReceiptStore` be reused verbatim.
DIFF = fin.ObjectKind.DIFF
CHANGED_FILE = fin.ObjectKind.CHANGED_FILE


#: The questions a diff is judged on. Severity is a property of the question
#: and is fixed here in code — the reviewer cannot express it, which is what
#: stops "I'll call this one advisory" from being a reviewer decision.
#:
#: Each BLOCKING check names a failure mode a green gate cannot see. That is
#: the selection rule: a question a passing test already answers does not
#: belong here, because it would spend a reviewer turn re-deriving something
#: mechanical, and §10.2's counting rule already owns it.
CODE_RUBRIC = fin.Rubric(
    version="maestro-code-rubric.v1",
    checks=(
        fin.RubricCheck(
            check_id="diff.implements_the_stated_instruction",
            question=("Does this diff do what the node was asked to do, rather "
                      "than something adjacent that happens to pass the gate?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.gate_is_passed_on_the_merits",
            question=("Does the code satisfy the gate because the behaviour is "
                      "present, rather than by hardcoding, special-casing the "
                      "test input, or weakening the assertion?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.no_unrelated_change_rides_along",
            question=("Is every hunk in service of this node's stated work, "
                      "with no unrelated refactor or drive-by edit?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.introduces_no_obvious_defect",
            question=("Is the changed code free of defects visible in the diff "
                      "— unhandled error paths, inverted conditions, resource "
                      "leaks, off-by-one, dead branches?"),
            applies_to=(DIFF,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="diff.is_coherent_with_its_surroundings",
            question=("Does the change fit the conventions and structure of the "
                      "code it lands in?"),
            applies_to=(DIFF,),
            severity=fin.Severity.ADVISORY),
        fin.RubricCheck(
            check_id="file.change_is_justified_by_the_instruction",
            question=("Is this file's change required by the node's stated work, "
                      "rather than incidental?"),
            applies_to=(CHANGED_FILE,),
            severity=fin.Severity.BLOCKING),
        fin.RubricCheck(
            check_id="file.no_secret_or_credential_introduced",
            question=("Does this file's change introduce no credential, token, "
                      "private key, or other secret?"),
            applies_to=(CHANGED_FILE,),
            severity=fin.Severity.BLOCKING),
    ))


# ── A9: graded findings, so a lane converges on a merge and not on a wall ───

class FindingGrade(str, Enum):
    """How bad *this instance* of a finding is, assigned by the reviewer.

    **This is not `Severity` and must never be read as one.** `Severity` is a
    property of the *question* and is stamped by code from the rubric, exactly
    as §6.5 requires — the reviewer still cannot express it, still cannot
    express a verdict, and code still derives the verdict. What a grade adds is
    the missing second axis: whether a true answer to a blocking question is a
    reason to refuse the merge, or a reason to record something and merge.

    Why the axis is needed at all is §3.6 A9's rule, and Maestro broke it. Six
    of `CODE_RUBRIC`'s seven checks are BLOCKING, so acceptance demanded a
    zero-finding sweep by an adversarial cross-vendor reviewer over a real
    diff. Measured over 27 real review reports,
    `diff.introduces_no_obvious_defect` carried a finding in 25 of them. The
    findings were correct — specific, located, genuine defects — which is
    exactly what A9 names: "any true finding rejects" is a predicate a
    competent reviewer fails forever. `review_ceiling` bounds the loop, and a
    bounded non-convergent loop only decides how many attempts precede BLOCKED:
    `lane-p2-s3-inventory` in `run-2a44d226e75a4be391a14f02b78a6d25` spent five
    attempts on five rejections, each finding a different real bug in the same
    file, each round fixing the last finding and earning a new one.

    A9's remedy is stated as two alternatives — "bound the loop **or accept
    graded findings**". The bound existed. This is the grading.

    The three grades are anchored on consequence rather than on effort, because
    a scale a reviewer reads as "how much work is this" collapses to one value:

    * ERROR   — the merged tree is wrong because of this: incorrect behaviour,
                data loss, a silently swallowed failure, a security hole, or
                the node's stated work not actually done.
    * WARNING — real and worth fixing, but the node's stated work is correctly
                delivered with it present.
    * NOTE    — an observation or a preference.
    """

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


#: Ascending. The one ordering, so nothing compares grades by hand.
GRADE_ORDER: Tuple[FindingGrade, ...] = (
    FindingGrade.NOTE, FindingGrade.WARNING, FindingGrade.ERROR)

#: The threshold when configuration says nothing: reject on ERROR, record on
#: WARNING and below. A real threshold, not "reject on anything", which is the
#: pre-grading behaviour this replaces.
DEFAULT_REJECT_GRADE = FindingGrade.ERROR


class UnknownGrade(ValueError):
    """A configured rejection grade that is not one of the three."""


def grade_rank(grade: FindingGrade) -> int:
    return GRADE_ORDER.index(grade)


def grade_at_or_above(grade: Optional[FindingGrade],
                      threshold: FindingGrade) -> bool:
    """Whether `grade` reaches the rejection threshold.

    An absent grade is **not** at or above anything. Absent means "no grade is
    recorded for this cell", which since `DerivedCell.grade` is reachable only
    from a receipt written before that field existed, or from a review family
    that grades nothing (plan finalization). It is unreachable from a report,
    because a report whose finding carries no grade does not parse.
    """
    if grade is None:
        return False
    return grade_rank(grade) >= grade_rank(threshold)


def parse_reject_grade(value: object) -> FindingGrade:
    """The configured threshold, read as configuration and never as plan
    content.

    §6.2's rule for retry budgets applies unchanged: how tolerant an
    installation is of a graded finding is a property of the installation, not
    of the work, so a plan cannot raise or lower its own bar.
    """
    try:
        return FindingGrade(str(value).strip().casefold())
    except ValueError as exc:
        raise UnknownGrade(
            "review rejection grade {0!r} is not one of {1}".format(
                value, ", ".join(g.value for g in GRADE_ORDER))) from exc


# ── the reviewer's report, extended by exactly one axis ─────────────────────

class CodeReportCell(fin.ReportCell):
    """A node-review cell: `finalization`'s cell, plus the grade and its reason.

    **What this does not add.** No `verdict` field and no `severity` field, so
    §6.5's unrepresentability claim is untouched and
    `find_forbidden_report_fields` still returns `[]` over this schema —
    asserted rather than assumed, in `tests/test_graded_findings.py`.
    `extra="forbid"` is inherited, so a smuggled `severity` is still a parse
    error rather than an ignored key.

    **B8, enforced in the schema rather than only downstream.** §3.6 B8's
    lesson is that a field added later is optional forever, so the invariant
    goes where it cannot be bypassed: a cell that says `finding` **must** carry
    a grade, a message that locates it, and a reason for the grade. A report
    violating that does not parse, so it never reaches verdict derivation,
    never reaches a receipt, and cannot reject an attempt. That is what makes
    "reject without a located at-or-above-threshold finding" structurally
    impossible rather than merely checked afterwards.

    The rule is uniform over every cell, controls included. A control answered
    `finding` states its grade like any other cell, which keeps the schema one
    rule instead of one rule and an exemption — and an exemption keyed on an
    object id would put matrix knowledge inside the cell model.
    """

    grade: Optional[FindingGrade] = None
    #: Why this grade and not the one above it. Read three times — by
    #: `require_located_findings` below, by `ReviewOutcome.findings_text` as
    #: retry guidance, and by the ledger an operator reads back — so it is a
    #: field with readers rather than B15's field with none.
    grade_rationale: str = ""

    @model_validator(mode="after")
    def _a_finding_is_graded_and_located(self) -> "CodeReportCell":
        if self.status is fin.CellStatus.FINDING:
            if self.grade is None:
                raise ValueError(
                    "a finding must carry a grade; an ungraded finding is the "
                    "non-convergent predicate this schema exists to end (A9)")
            if not self.message.strip():
                raise ValueError(
                    "a finding must carry a message naming what is wrong and "
                    "where; a finding that locates nothing is not one (B8)")
            if not self.grade_rationale.strip():
                raise ValueError(
                    "a finding must say why it carries the grade it does; a "
                    "grade nobody justified is a verdict by another name (B8)")
        elif self.grade is not None or self.grade_rationale.strip():
            raise ValueError(
                "a cleared cell carries no grade and no grade rationale")
        return self


class CodeReviewerReport(fin.ReviewerReport):
    """The whole node-review report. Narrows `cells` to the graded cell."""

    cells: List[CodeReportCell]


# ── code derives the verdict, now against the threshold ─────────────────────

@dataclass(frozen=True)
class GradedCell:
    """One answered cell, carrying both axes.

    `severity` is stamped from the rubric by code. `grade` is the reviewer's.
    It is `None` only where no grade was ever assigned: a cell that is not a
    finding, a review family that grades nothing, or a cell reconstructed from
    a receipt written before `DerivedCell.grade` existed (§6.6).
    """

    check_id: str
    object_id: str
    status: fin.CellStatus
    severity: fin.Severity
    grade: Optional[FindingGrade]
    message: str = ""
    rationale: str = ""
    canary: Optional[fin.CanaryKind] = None

    @property
    def is_finding(self) -> bool:
        return self.canary is None and self.status is fin.CellStatus.FINDING

    def rejects(self, reject_at: FindingGrade) -> bool:
        """Whether this cell alone is a reason to refuse the merge.

        Both axes, and both are load-bearing. `severity` keeps an ADVISORY
        check unable to reject however the reviewer grades it, which is the
        property `diff.is_coherent_with_its_surroundings` already had. `grade`
        is what lets a BLOCKING check answer truthfully without ending the lane.
        """
        return (self.is_finding
                and self.severity is fin.Severity.BLOCKING
                and grade_at_or_above(self.grade, reject_at))

    @classmethod
    def from_receipt_cell(cls, cell: fin.DerivedCell) -> "GradedCell":
        """A stored receipt's cell, carrying the grade the receipt recorded.

        The receipt's frozen schema (§6.6) carries `grade` from the first
        version that graded anything, so a receipt is a complete source for the
        grades its verdict was derived from and this reconstruction reads them
        rather than dropping them. It is the fallback for a receipt whose
        ledger is absent, and the grade it reports is the signed one.

        `grade=None` survives for the two cases where no grade was ever
        assigned — a non-finding cell, and plan finalization, whose reviewer
        has none to give — and for a receipt written before the field existed.
        An unparseable grade string is read as absent rather than raised: the
        verdict on this path is the receipt's signed one and is never
        re-derived here, so a grade this version cannot name may change how a
        finding is presented and must not be able to fail a replay.
        """
        grade: Optional[FindingGrade]
        try:
            grade = None if cell.grade is None else FindingGrade(cell.grade)
        except ValueError:
            grade = None
        return cls(check_id=cell.check_id, object_id=cell.object_id,
                   status=cell.status, severity=cell.severity, grade=grade,
                   message=cell.message, canary=cell.canary)


@dataclass(frozen=True)
class GradedVerdict:
    """The derivation's whole output: the verdict, the cells, the threshold.

    The threshold is carried rather than looked up a second time, so every
    consumer partitions against the number the verdict was derived with.
    """

    verdict: fin.Verdict
    cells: Tuple[GradedCell, ...]
    reject_at: FindingGrade

    @property
    def rejecting(self) -> Tuple[GradedCell, ...]:
        """The cells that caused a FAIL — the retry's content."""
        return tuple(c for c in self.cells if c.rejects(self.reject_at))

    @property
    def recorded(self) -> Tuple[GradedCell, ...]:
        """Every other finding: sub-threshold and advisory-severity alike.

        Not discarded, which is the difference between grading the bar and
        lowering it. These go to the ledger and into the retry prompt.
        """
        return tuple(c for c in self.cells
                     if c.is_finding and not c.rejects(self.reject_at))

    def derived(self) -> fin.DerivedVerdict:
        """The projection onto the receipt's cell shape.

        Total over every field the receipt has, the grade included: the grade
        is what decided the verdict, so a receipt carrying the verdict without
        it would record a conclusion whose derivation cannot be re-checked
        from the receipt alone. `DerivedCell.grade` exists for that reason and
        is written here from the first version that grades anything, because
        §3.6 B8's rule is that a field added after receipts exist is optional
        forever.

        A cell that is not a finding has no grade to carry, and `None` is the
        honest value rather than a default standing in for one.
        """
        return fin.DerivedVerdict(
            verdict=self.verdict,
            cells=tuple(fin.DerivedCell(
                check_id=c.check_id, object_id=c.object_id, status=c.status,
                severity=c.severity, message=c.message, canary=c.canary,
                grade=(c.grade.value if c.grade is not None else None))
                for c in self.cells))


def grade_verdict(matrix: fin.ApplicabilityMatrix,
                  report: CodeReviewerReport, rubric: fin.Rubric,
                  reject_at: FindingGrade) -> GradedVerdict:
    """FAIL iff some graded cell is a BLOCKING finding graded at or above
    `reject_at`. PASS otherwise.

    The same shape as `finalization.derive_verdict`, and deliberately a second
    function rather than a threshold parameter on it: plan finalization's
    verdicts are **unchanged** by this file, and the way to keep them unchanged
    is for its derivation to have no threshold to be handed. Canary cells take
    no part here for the reason they take none there — the known-bad control is
    answered `finding` by construction, so counting it would fail every diff.
    """
    answered = {(c.check_id, c.object_id): c for c in report.cells}
    cells: List[GradedCell] = []
    for cell in matrix.cells:
        answer = answered[cell.key]
        severity = (fin.Severity.ADVISORY if cell.is_canary
                    else rubric.check(cell.check_id).severity)
        cells.append(GradedCell(
            check_id=cell.check_id, object_id=cell.object_id,
            status=answer.status, severity=severity, grade=answer.grade,
            message=answer.message, rationale=answer.grade_rationale,
            canary=cell.canary))
    graded = tuple(cells)
    failed = any(c.rejects(reject_at) for c in graded)
    return GradedVerdict(
        verdict=fin.Verdict.FAIL if failed else fin.Verdict.PASS,
        cells=graded, reject_at=reject_at)


# ── the ledger: what a merged node merged with ──────────────────────────────

#: The file `write_finding_ledger` produces, beside the reviewer's own report
#: under the subject digest's directory, so a reader can tell at a glance which
#: schema they are looking at.
FINDING_LEDGER_SCHEMA = "maestro-code-review-findings.v1"

#: Its filename. One definition, so the writer and the operator's `cat` agree.
FINDING_LEDGER_FILENAME = "findings.json"


def write_finding_ledger(path: Path, *, run_id: str, node_id: str,
                         subject_digest: str, rubric_version: str,
                         graded: GradedVerdict) -> Path:
    """Persist **every** finding, rejecting and sub-threshold alike.

    Without this, grading would lower the bar rather than grade it: a node
    would merge carrying real findings and nothing would record that it did.
    With it, a merged node's advisories sit on disk beside the receipt that
    admitted the merge, under the same subject digest, and an operator reads
    them back with `read_finding_ledger`.

    Unsigned, deliberately. §1.2 requires the lifecycle transition to be caused
    by a typed, signed record: the transition is caused by the **receipt**,
    whose verdict is signed and whose full per-cell matrix is covered by that
    signature. This file is read by nothing that decides anything — it is
    evidence for a human and display material for a retry prompt.
    """
    payload = {
        "schema": FINDING_LEDGER_SCHEMA,
        "run_id": run_id,
        "node_id": node_id,
        "subject_digest": subject_digest,
        "rubric_version": rubric_version,
        "verdict": graded.verdict.value,
        "reject_at": graded.reject_at.value,
        "findings": [
            {
                "check_id": c.check_id,
                "object_id": c.object_id,
                "severity": c.severity.value,
                "grade": None if c.grade is None else c.grade.value,
                "rejecting": c.rejects(graded.reject_at),
                "message": c.message,
                "rationale": c.rationale,
            }
            for c in graded.cells if c.is_finding
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def read_finding_ledger(path: Path) -> Tuple[GradedCell, ...]:
    """The findings a ledger recorded, or `()` when there is no usable ledger.

    Read on the replay path, so a replayed outcome carries the grades the
    original review derived instead of falling back to severity alone. A
    missing or malformed ledger returns `()` and the caller falls back: this
    file is evidence, never authority, so it can never be why a review fails.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if (not isinstance(payload, dict)
            or payload.get("schema") != FINDING_LEDGER_SCHEMA):
        return ()
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return ()
    cells: List[GradedCell] = []
    for entry in findings:
        if not isinstance(entry, dict):
            return ()
        try:
            grade = entry["grade"]
            cells.append(GradedCell(
                check_id=str(entry["check_id"]),
                object_id=str(entry["object_id"]),
                status=fin.CellStatus.FINDING,
                severity=fin.Severity(entry["severity"]),
                grade=None if grade is None else FindingGrade(grade),
                message=str(entry["message"]),
                rationale=str(entry.get("rationale", "")),
                canary=None))
        except (KeyError, TypeError, ValueError):
            return ()
    return tuple(cells)


# ── B8: FAIL is unrepresentable without a located finding ───────────────────

class VerdictNotLocated(RuntimeError):
    """B8's invariant, violated: a verdict whose findings do not support it.

    Strav shipped `VerdictV3` as `schema`/`status`/`summary` with no findings
    at all, so the reducer's scope guard had nothing to scope and a FAIL was a
    bare word. The field was retrofitted later; the *invariant* could not be,
    because a field added after the fact is optional forever. So this is
    enforced from v1, before any receipt for this subject exists.
    """


def require_located_findings(graded: GradedVerdict) -> None:
    """FAIL iff at least one cell is a located finding at or above the
    threshold.

    Both directions are checked, and the second is the one that matters: a PASS
    carrying a rejecting finding would mean the derivation and the cells
    disagree, which is exactly the contentless verdict B8 convicts, only
    inverted. "Located" is enforced too — a finding with an empty message names
    no place and no reason, so it cannot be handed back to a builder as retry
    guidance and does not count as a finding at all — and so is "justified": a
    grade at or above the threshold decides the merge, and a decision nobody
    gave a reason for is the same contentless verdict one level down.

    This is the *second* place both hold. The first is `CodeReportCell`, where
    a report violating them does not parse. Keeping the invariant as an
    executed object here as well is B15's rule: an invariant that lives only in
    a schema is one a rewrite of the schema silently drops, and this function
    is what the negative tests plant violations against.
    """
    rejecting: List[GradedCell] = []
    for cell in graded.cells:
        if not cell.rejects(graded.reject_at):
            continue
        if not cell.message.strip():
            raise VerdictNotLocated(
                f"rejecting finding on {cell.check_id}/{cell.object_id} carries "
                "no message, so it locates nothing and cannot be acted on")
        if not cell.rationale.strip():
            raise VerdictNotLocated(
                f"rejecting finding on {cell.check_id}/{cell.object_id} grades "
                f"itself {cell.grade.value if cell.grade else '?'} with no "
                "reason, so the grade that refuses the merge is unjustified")
        rejecting.append(cell)

    if graded.verdict is fin.Verdict.FAIL and not rejecting:
        raise VerdictNotLocated(
            "FAIL without a located finding graded at or above {0} is "
            "unrepresentable (B8)".format(graded.reject_at.value))
    if graded.verdict is fin.Verdict.PASS and rejecting:
        raise VerdictNotLocated(
            "PASS while carrying {0} located finding(s) graded at or above "
            "{1} (B8)".format(len(rejecting), graded.reject_at.value))


def receipt_findings(derived: fin.DerivedVerdict, reject_at: FindingGrade,
                     ) -> Tuple[Tuple[GradedCell, ...], Tuple[GradedCell, ...]]:
    """A stored receipt's findings, partitioned by the grades it carries.

    The fallback for a replayed receipt whose ledger is absent, and the reason
    that fallback is no longer lossy. The ledger is written *after* the
    receipt, deliberately — an unsigned file claiming a verdict must never
    precede the signed record — so a crash in that window leaves a receipt with
    no ledger, and before `DerivedCell.grade` that review's grades were gone
    with it. They are not: the receipt carries the grade that decided each
    cell, under the same signature as the verdict, so the partition here is the
    one the original review derived rather than a severity-shaped
    approximation of it.

    A cell with no grade is partitioned on severity alone, which is the honest
    answer for the two cases that produce one — a receipt written before the
    field existed, and a review family that grades nothing — and is the old
    behaviour, kept exactly where it is still the truth.

    It never re-derives the verdict: the replayed verdict is the receipt's
    signed one, and this partition decides only what a retry prompt says.
    """
    rejecting: List[GradedCell] = []
    recorded: List[GradedCell] = []
    for cell in derived.cells:
        if cell.canary is not None or cell.status is not fin.CellStatus.FINDING:
            continue
        graded = GradedCell.from_receipt_cell(cell)
        if (graded.rejects(reject_at) if graded.grade is not None
                else graded.severity is fin.Severity.BLOCKING):
            rejecting.append(graded)
        else:
            recorded.append(graded)
    return tuple(rejecting), tuple(recorded)


# ── B12: no actor reviews its own output ────────────────────────────────────

class SelfJudgeRefused(RuntimeError):
    """B12: the reviewer and the builder are the same vendor."""


def require_distinct_vendor(builder_vendor: str, reviewer_vendor: str) -> None:
    """Refuse a review by the vendor that wrote the code.

    **This check is config-derived, not receipt-proven, and the difference is
    load-bearing.** `route_receipts` admits routes, and on the omp route only
    `profile` selects the model — `build_omp_argv` emits `--pm-profile` and
    `--session-dir` and passes neither model nor vendor. So a grok builder and
    an openai reviewer ride the *same* admitted `omp` route receipt, and no
    receipt anywhere can distinguish them. What is compared here is what the
    operator wrote in `maestro.config.yaml`, which an operator who edits both
    blocks to the same vendor can defeat.

    Closing it for real needs profile-scoped route receipts, so that the
    vendor a pane actually ran under is a signed fact rather than a
    configuration claim. That is a separate change. Until it lands this is a
    guard against the accident, not against the determined.
    """
    builder = (builder_vendor or "").strip().casefold()
    reviewer = (reviewer_vendor or "").strip().casefold()
    if not builder or not reviewer:
        raise SelfJudgeRefused(
            "both execution.vendor and reviewer.vendor must be configured; an "
            "unnamed vendor cannot be shown to differ from the builder's (B12)")
    if builder == reviewer:
        raise SelfJudgeRefused(
            f"reviewer vendor {reviewer_vendor!r} is the builder's; no actor may "
            "sign off on its own output (B12)")


# ── B13: size-check the handoff before dispatch, fail closed ────────────────

class HandoffTooLarge(RuntimeError):
    """B13: the reviewer's input would not fit its context window.

    An overflowing reviewer does not error — it compaction-loops and emits a
    plausible verdict about something else. Strav's round-two handoff was
    710,673 bytes against a 272K-context reviewer and produced a verdict
    describing a completely different workflow.
    """


#: B13's arithmetic and its route table live in `handoff_budget`, one module
#: below `launcher`, because the check that cannot be bypassed has to run
#: inside `launcher.HerdrLauncher.launch` and `launcher` cannot import this
#: module (`code_review` -> `scheduler_types` -> `worktree` -> `launcher`).
#: Re-exported here so the rule still reads as one rule from the reviewer's
#: side, with exactly one definition of it.
BYTES_PER_TOKEN = hb.BYTES_PER_TOKEN
HANDOFF_CONTEXT_FRACTION = hb.HANDOFF_CONTEXT_FRACTION
estimate_tokens = hb.estimate_tokens


def preflight_handoff(text: str, context_window_tokens: Optional[int],
                      fraction: float = HANDOFF_CONTEXT_FRACTION) -> int:
    """Refuse a handoff that will not fit. Returns the estimate when it does.

    Fails closed on an unknown window for the same reason
    `check_occupancy` convicts on NULL: an unmeasured window is not a
    passing one, and dispatching into it is how B13 happened.
    """
    if not context_window_tokens or context_window_tokens <= 0:
        raise HandoffTooLarge(
            "the reviewer's context window is unknown, so the handoff cannot be "
            "shown to fit; an unmeasured window is not a passing one (B13)")
    estimate = estimate_tokens(text)
    budget = hb.handoff_budget(context_window_tokens, fraction)
    if estimate > budget:
        raise HandoffTooLarge(
            f"handoff is ~{estimate} tokens against a {context_window_tokens}-token "
            f"window at fraction {fraction} (budget {budget}); an overflowing "
            "reviewer hallucinates rather than erroring (B13)")
    return estimate


#: Which routes publish a measurable window is a fact about the route, owned by
#: `handoff_budget` and re-exported here.
#:
#: The *lookup* deliberately lives at the CLI rather than in either module:
#: reading a route's catalog means importing `agent_pi`, and both are on the
#: far side of the `base-execution-import` boundary that `enforcement.py`
#: convicts. So the arithmetic and the rule are here, and the caller owns the
#: window.
ROUTES_PUBLISHING_A_WINDOW = hb.ROUTES_PUBLISHING_A_WINDOW
route_publishes_a_window = hb.route_publishes_a_window


# ── B9: the reviewer's input is a declared, validated contract ──────────────

class HandoffIncomplete(RuntimeError):
    """B9: a required part of the reviewer's contract is missing or empty."""


class ReviewHandoff(BaseModel):
    """Everything the reviewer is entitled to, declared rather than assembled.

    Strav's `review-golden_vendor` handoff was **409 bytes** — identifiers and
    a null. No goal, no `produces`, no acceptance contract. A reviewer given
    that cannot produce a meaningful verdict, and its PASS is worthless. So
    every field here is required and validated non-empty before a pane opens:
    a reviewer that would be starved is a launch that does not happen, which is
    a diagnosable refusal instead of a confident meaningless answer.

    `extra="forbid"` for the same reason the report schema forbids it — the
    contract is the whole contract, and a field smuggled in beside it is one
    nothing validates.
    """

    model_config = ConfigDict(extra="forbid")

    subject_digest: str
    run_id: str
    node_id: str
    node_kind: str
    #: The goal. B9's first missing field.
    instruction: str
    #: What the node was permitted to write. Without it "unrelated change"
    #: is not a question the reviewer can answer.
    declared_outputs: List[str]
    #: The acceptance contract. B9's third missing field.
    gate_command: List[str]
    gate_selector: str
    #: §10.2's counting threshold, which is half of what the gate demands.
    #: A gate over one new module passes on one test and on seven; the plan
    #: that asked for seven said so here and nowhere else, so a reviewer
    #: shown the command alone cannot tell a satisfied acceptance contract
    #: from a third of one.
    gate_min_cases: int = 1
    base_sha: str
    output_sha: str
    diff: str
    #: What the code inside this node may do about each act the plan forbids.
    #:
    #: Every other field of this contract answers *where* work may happen —
    #: the instruction, the declared outputs, the gate, its selector. None
    #: answered what the code inside them may do, and the attempt-3 prompt
    #: from run-0120c32064d144c2aa55c344087e0b0a shows the cost: its whole
    #: brief was "make the gate pass over selector …, changing only the
    #: declared outputs", and against that brief an object materializer that
    #: constructs a client and copies bytes is compliant. The words the plan
    #: actually used about that node — pure derivation, no object mutation,
    #: injected clients — appeared nowhere in it.
    #:
    #: Empty for a code node, and for an agent node from a plan authored
    #: before the field existed, so it is not in `require_complete` below. It
    #: is not defaulted around either: an empty list renders no block rather
    #: than an empty heading, so a reviewer is never shown a contract section
    #: with nothing under it.
    effects: List[Dict[str, str]] = []
    #: The cells to answer, and where to write the answers.
    matrix: List[Dict[str, Any]]
    pair_count: int
    report_path: str
    rubric: List[Dict[str, str]]

    def require_complete(self) -> "ReviewHandoff":
        """Every field carries content. Presence is not the check — the 409-byte
        handoff had every field it declared, and they were null and empty."""
        for name in ("subject_digest", "run_id", "node_id", "node_kind",
                     "instruction", "base_sha", "output_sha", "diff",
                     "report_path"):
            if not str(getattr(self, name) or "").strip():
                raise HandoffIncomplete(
                    f"the reviewer's contract has no {name}; a starved reviewer's "
                    "verdict is worthless (B9)")
        if not self.matrix:
            raise HandoffIncomplete(
                "the reviewer's contract names no cells to answer (B9)")
        if self.pair_count != len(self.matrix):
            raise HandoffIncomplete(
                f"pair_count {self.pair_count} does not match {len(self.matrix)} "
                "matrix cells; the fabrication canary must be internally consistent")
        if not self.rubric:
            raise HandoffIncomplete(
                "the reviewer's contract states no questions; a cell id without "
                "its question is not something a reviewer can answer (B9)")
        # A code node has no gate, so an empty gate contract is legal for it and
        # only for it — demanding one of every kind would refuse the composition
        # §6.7 recommends.
        if self.node_kind == st.NodeKind.AGENT.value and not self.gate_command:
            raise HandoffIncomplete(
                "an agent node's acceptance contract is its gate; the reviewer "
                "cannot judge 'passed on the merits' without it (B9)")
        return self

    def render(self) -> str:
        """The prompt text. Deterministic, so the same subject renders the same
        bytes and the size preflight measures what is actually sent."""
        lines = [
            "You are reviewing one node's diff before it merges. You did not "
            "write this code.",
            "",
            f"Run: {self.run_id}",
            f"Node: {self.node_id} (kind: {self.node_kind})",
            f"Base commit: {self.base_sha}",
            f"Proposed commit: {self.output_sha}",
            "",
            "## What this node was asked to do",
            self.instruction,
            "",
            "## Paths this node was permitted to write",
        ]
        lines.extend("  " + path for path in (self.declared_outputs or ["(none declared)"]))
        lines.append("")
        lines.append("## The acceptance contract it had to satisfy")
        if self.gate_command:
            lines.append("  " + " ".join(self.gate_command))
            lines.append(f"  selector: {self.gate_selector or '(none)'}")
            lines.append(
                f"  and it must collect at least {self.gate_min_cases} "
                "passing case(s)")
        else:
            lines.append("  (code node: acceptance is the command's exit code)")
        if self.effects:
            lines.extend([
                "",
                "## What this node may do",
                "This plan forbids the acts below. Each line is what this node "
                "is authorised to do about one of them, and the sentence under "
                "it is the prohibition in the source document's own words.",
                "",
                "  performed = executes it for real | planned = emits a record "
                "describing it and executes nothing",
                "  fake_only = executes it only against an injected fake | "
                "none = does not perform, plan, or fake it",
            ])
            for entry in self.effects:
                lines.append("")
                lines.append("  {0}: {1}".format(
                    entry["effect"], entry["disposition"]))
                lines.append("      forbidden act: " + entry["meaning"])
        lines.extend([
            "",
            "## The diff, base..proposed",
            "```diff",
            self.diff,
            "```",
            "",
            "## The questions",
        ])
        for entry in self.rubric:
            lines.append(f"  {entry['check_id']}: {entry['question']}")
        lines.extend([
            "",
            "## What to write",
            "",
            f"Answer every one of these {self.pair_count} cells — exactly these, "
            "no more and no fewer:",
        ])
        for cell in self.matrix:
            lines.append(f"  {cell['check_id']} | {cell['object_id']}")
        lines.extend([
            "",
            "Two of those cells are controls whose object ids begin and end with "
            "a double underscore. Answer them the way the cell itself reads: the "
            "known-bad control is a finding, the known-good control is clear.",
            "",
            "Write this file and then stop:",
            "  " + self.report_path,
            "",
            "```json",
            json.dumps({
                "plan_digest": self.subject_digest,
                "pair_count": self.pair_count,
                "cells": [
                    {"check_id": "<id>", "object_id": "<id>",
                     "status": "clear"},
                    {"check_id": "<id>", "object_id": "<id>",
                     "status": "finding",
                     "message": "<what is wrong, and where>",
                     "grade": "error|warning|note",
                     "grade_rationale": "<why that grade>"},
                ],
            }, indent=2),
            "```",
            "",
            "Do not write a verdict, a severity, or a score — they are not "
            "fields, and a report carrying one is rejected unparsed. Whether "
            "this diff merges is decided by code from what you write here.",
            "",
            "Report every problem you find. Grade it honestly; do not soften a "
            "finding to let it through and do not withhold one.",
            "",
            "A `clear` cell carries no `grade` and no `grade_rationale`. A "
            "report that puts either on a cleared cell is rejected unparsed.",
            "",
            "Every `finding` carries three things, and a finding missing any of "
            "them makes the whole report unparseable:",
            "",
            "  message          what is wrong and exactly where",
            "  grade            error | warning | note",
            "  grade_rationale  one sentence: the consequence that puts it at "
            "that grade",
            "",
            "Grade on consequence, not on how much work the fix is:",
            "",
            "  error   — the merged tree is wrong because of this: incorrect "
            "behaviour, data loss, a silently swallowed failure, a security "
            "hole, or this node's stated work not actually done.",
            "  warning — real and worth fixing, but this node's stated work is "
            "correctly delivered with it present.",
            "  note    — an observation or a preference.",
            "",
            "Judge against what this node was asked to do, above. A defect in "
            "code this diff did not write, or a hardening this node's "
            "instruction did not ask for, is at most a warning however real it "
            "is.",
            "",
            "A control cell answered `finding` states its grade like any other.",
        ])
        return "\n".join(lines)


# ── B10: the subject digest, and what it deliberately excludes ──────────────

def review_digest(*, run_id: str, node_id: str, base_sha: str,
                  output_sha: str, rubric_version: str) -> str:
    """The identity of "this code, reviewed against this base, by this rubric".

    **`attempt_no` is deliberately not a component.** Including it would mint a
    fresh identity for a rebuilt-but-byte-identical output, which is exactly
    B10: FAIL then PASS on a byte-identical commit, the second review reaching
    the opposite conclusion and nothing recording that the intervening fix was
    empty. Excluding it means an empty resubmission hits the stored receipt and
    **replays the recorded verdict** rather than rolling the dice again — the
    guard is structural, not a comparison someone has to remember to run.

    `base_sha` *is* a component, because the same tree over a different base is
    genuinely different evidence: the surrounding code moved, so "coherent with
    its surroundings" and "no unrelated change" have different answers.

    `rubric_version` is a component so that changing the questions invalidates
    the cached answers rather than silently keeping them.
    """
    payload = json.dumps({
        "kind": "maestro-code-review.v1",
        "run_id": run_id,
        "node_id": node_id,
        "base_sha": base_sha,
        "output_sha": output_sha,
        "rubric_version": rubric_version,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ── the diff, and the objects it yields ─────────────────────────────────────

class DiffUnavailable(RuntimeError):
    """The subject of the review could not be read from git."""


#: A diff larger than this is truncated before the size preflight sees it, with
#: the truncation stated in the text. Refusing outright would block a
#: legitimately large node; sending it whole is B13.
MAX_DIFF_BYTES = 400_000


def read_diff(repo: Path, base_sha: str, output_sha: str,
              runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
              ) -> Tuple[str, Tuple[str, ...]]:
    """The unified diff `base..output`, and the paths it touches.

    Read from git rather than from the agent's envelope for the reason §1.2
    states: a `changed_files` list the agent wrote is the agent's claim about
    its own work, and a review scoped by it would review only what the author
    chose to disclose.
    """
    def git(*args: str) -> str:
        result = runner(["git", "-C", str(repo), *args],
                        capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise DiffUnavailable(
                "git {0} -> {1}: {2}".format(" ".join(args), result.returncode,
                                             (result.stderr or "").strip()[-300:]))
        return result.stdout

    names = tuple(sorted(
        line.strip() for line in
        git("diff", "--name-only", f"{base_sha}..{output_sha}").splitlines()
        if line.strip()))
    text = git("diff", "--unified=3", f"{base_sha}..{output_sha}")
    if len(text.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES:
        text = (text.encode("utf-8", errors="replace")[:MAX_DIFF_BYTES]
                .decode("utf-8", errors="ignore")
                + "\n\n[diff truncated at {0} bytes]\n".format(MAX_DIFF_BYTES))
    return text, names


def review_objects(changed_files: Sequence[str], output_sha: str,
                   ) -> Tuple[fin.ReviewObject, ...]:
    """The diff itself, plus one object per changed file.

    Per-file objects are what make a finding *located* in B8's sense: a finding
    on `file.no_secret_or_credential_introduced` carries the path in its
    `object_id`, so the retry prompt can name the file rather than handing the
    builder "there is a secret somewhere".
    """
    objects = [fin.ReviewObject(object_id=f"diff:{output_sha}", kind=DIFF)]
    objects.extend(
        fin.ReviewObject(object_id=f"file:{path}", kind=CHANGED_FILE)
        for path in changed_files)
    return tuple(objects)


# ── the outcome the scheduler acts on ───────────────────────────────────────

@dataclass(frozen=True)
class ReviewOutcome:
    """One review, as one reportable fact."""

    subject_digest: str
    verdict: fin.Verdict
    receipt: fin.Receipt
    replayed: bool
    #: The findings that refused the merge — at or above the threshold.
    findings: Tuple[GradedCell, ...] = ()
    #: Everything else that was found. Recorded, never a reason to reject, and
    #: still handed to the builder.
    advisories: Tuple[GradedCell, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict is fin.Verdict.PASS

    def findings_text(self) -> str:
        """The findings as retry guidance, rejecting first.

        This is the whole justification for recycling the attempt rather than
        blocking it: a retry that repeats the same request is not new
        instructions. The builder is told which cell failed, on which object,
        why, and — since grading — what grade the reviewer put on it and on
        what ground, which is the difference between "fix this or the lane
        dies" and "we recorded this and merged".
        """
        lines: List[str] = []
        if self.findings:
            lines.append("Findings that refused the merge — each must be "
                         "resolved:")
            lines.extend(self._render(self.findings))
        if self.advisories:
            lines.append("Recorded findings — they did not refuse the merge; "
                         "address them if you agree:")
            lines.extend(self._render(self.advisories))
        return "\n".join(lines)

    @staticmethod
    def _render(cells: Sequence[GradedCell]) -> List[str]:
        lines: List[str] = []
        for cell in cells:
            grade = "" if cell.grade is None else f" ({cell.grade.value})"
            lines.append(f"  [{cell.object_id}] {cell.check_id}{grade}")
            lines.append(f"    {cell.message.strip()}")
            if cell.rationale.strip():
                lines.append(f"    why this grade: {cell.rationale.strip()}")
        return lines


class ReviewStalled(RuntimeError):
    """The reviewer never produced a report inside its window.

    Not a FAIL, and deliberately not a receipt: a stall is a fact about the
    machine or the route, never a verdict about the code. It carries no
    review-rejection marker either, so it spends no review budget — a wedged
    herdr must not consume a node's three attempts.
    """

    def __init__(self, session: fw.ReviewerSession,
                 signal: fw.FinalizationSignal, elapsed_s: float) -> None:
        super().__init__(
            f"REVIEW_STALLED: route={session.route} model={session.model} "
            f"session_id={session.session_id} signal={signal.value} "
            f"after {elapsed_s:.1f}s")
        self.route = session.route
        self.model = session.model
        self.session_id = session.session_id
        self.signal = signal
        self.elapsed_s = elapsed_s


# ── the driver ──────────────────────────────────────────────────────────────

def review_attempt(
    *,
    subject_digest: str,
    handoff: ReviewHandoff,
    objects: Sequence[fin.ReviewObject],
    rubric: fin.Rubric,
    store: fin.ReceiptStore,
    window_factory: Callable[[fin.ApplicabilityMatrix], Any],
    occupancy_reader: Callable[[fw.ReviewerSession], Optional[float]],
    occupancy_threshold: float = 0.8,
    reject_at: FindingGrade = DEFAULT_REJECT_GRADE,
    ledger_path: Optional[Path] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> ReviewOutcome:
    """One code review, in the order `finalization.finalize` fixes.

    The order is the same and for the same reasons: replay before any reviewer
    call, every rejection before any receipt exists, the verdict derived in
    code, the receipt create-once and signed. What differs is the subject and
    three added obligations — B9's contract completeness, checked before a pane
    opens; A9's grading, which decides what a true finding costs; and B8's
    located-findings invariant, checked before the receipt is written.

    `reject_at` is the installation's threshold and reaches here from
    `maestro.config.yaml`, never from the plan. `ledger_path`, when given, is
    where every finding is recorded — including the sub-threshold ones a merged
    node merged with.
    """
    handoff.require_complete()
    store.recover(subject_digest)

    # B10. A byte-identical resubmission replays the recorded verdict, and a
    # replayed FAIL still recycles the attempt — it just does so without
    # spending a reviewer turn to re-derive an answer already on disk.
    if store.has(subject_digest):
        receipt = store.load(subject_digest)
        findings, advisories = _replayed_findings(receipt, ledger_path,
                                                  reject_at)
        return ReviewOutcome(
            subject_digest=subject_digest, verdict=receipt.verdict,
            receipt=receipt, replayed=True,
            findings=findings, advisories=advisories)

    matrix = fin.compute_matrix(rubric, subject_digest, objects)
    window = window_factory(matrix)
    outcome: fw.WindowOutcome = window.run(sleep=sleep)
    if not outcome.completed:
        if outcome.signal is None:
            raise RuntimeError(
                "REVIEW_PROTOCOL_ERROR: incomplete window without signal")
        raise ReviewStalled(outcome.session, outcome.signal, outcome.elapsed_s)

    # A report whose finding carries no grade, no message, or no reason for its
    # grade is refused here, unparsed, before the canary and the occupancy gate
    # have anything to run over. That is B8's structural half.
    report = CodeReviewerReport.model_validate(outcome.report)
    fin.check_occupancy(occupancy_reader(outcome.session),
                        threshold=occupancy_threshold)
    fin.verify_report(matrix, report)

    graded = grade_verdict(matrix, report, rubric, reject_at)
    require_located_findings(graded)
    derived = graded.derived()

    created_at_epoch = clock()
    receipt = fin.Receipt(
        plan_digest=subject_digest,
        rubric_version=rubric.version,
        verdict=derived.verdict,
        cells=derived.cells,
        reviewer=fin.ReviewerIdentity(route=outcome.session.route,
                                      model=outcome.session.model,
                                      session_id=outcome.session.session_id),
        created_at_epoch=created_at_epoch,
        # The threshold beside the grades, because neither decides anything
        # alone. Written from the first version that grades, for the same B8
        # reason the grade itself is: a receipt that recorded the grades and
        # not the bar they were measured against would still be a verdict
        # nobody could re-derive from the signed record.
        reject_at=graded.reject_at.value)
    replayed = False
    findings, advisories = graded.rejecting, graded.recorded
    try:
        store.write(receipt)
    except fin.ReceiptExists:
        # Another writer got there first, so its receipt and its ledger are the
        # record and ours are discarded — the same create-once rule the store
        # enforces, applied to the evidence beside it.
        receipt = store.load(subject_digest)
        findings, advisories = _replayed_findings(receipt, ledger_path,
                                                  reject_at)
        replayed = True
    else:
        # After the receipt, never before it: the receipt is the record the
        # merge turns on and must not be preceded by an unsigned file claiming
        # a verdict it does not yet have. A crash in this window leaves a
        # receipt with no ledger; since `DerivedCell.grade` that costs the
        # rationales and the recorded threshold, not the grades, because
        # `_replayed_findings` reads those from the signed receipt.
        if ledger_path is not None:
            write_finding_ledger(
                ledger_path, run_id=handoff.run_id, node_id=handoff.node_id,
                subject_digest=subject_digest, rubric_version=rubric.version,
                graded=graded)
    return ReviewOutcome(
        subject_digest=subject_digest, verdict=receipt.verdict, receipt=receipt,
        replayed=replayed, findings=findings, advisories=advisories)


def _replayed_findings(receipt: fin.Receipt, ledger_path: Optional[Path],
                       reject_at: FindingGrade,
                       ) -> Tuple[Tuple[GradedCell, ...], Tuple[GradedCell, ...]]:
    """A stored receipt's findings, partitioned by the grades that decided it.

    The receipt is the authority for the verdict — signed, already loaded by
    the caller, and since `DerivedCell.grade` a complete record of the grades
    that verdict was derived from. So both branches below report the grades the
    original review actually derived, and neither invents one.

    The ledger is preferred where it exists because it carries what the receipt
    does not: each grade's `rationale`, and the threshold the review ran under.
    Absent or malformed it costs those two fields and nothing else, which is
    what makes the receipt-then-ledger write order (§1.2) free rather than paid
    for in lost grades. It is consulted for how to *present* findings and for
    nothing else — it is unsigned, so it can never be why a review fails.
    """
    threshold = _recorded_threshold(receipt, reject_at)
    derived = fin.DerivedVerdict(verdict=receipt.verdict, cells=receipt.cells)
    if ledger_path is None:
        return receipt_findings(derived, threshold)
    recorded = read_finding_ledger(ledger_path)
    if not recorded:
        return receipt_findings(derived, threshold)
    return (tuple(c for c in recorded if c.rejects(threshold)),
            tuple(c for c in recorded if not c.rejects(threshold)))


def _recorded_threshold(receipt: fin.Receipt,
                        configured: FindingGrade) -> FindingGrade:
    """The threshold the stored verdict was derived under, not today's.

    `reject_at` is configuration (§6.2) and an installation may raise or lower
    it between a review and any later reading of it. Partitioning a replayed
    receipt against the *current* value would report a finding as rejecting
    that the signed verdict recorded as merged, or the reverse — a presentation
    that contradicts the record it is presenting.

    A receipt written before the field existed falls back to the configured
    value, which is the only number available and is the old behaviour. So does
    a threshold this version cannot name, for `from_receipt_cell`'s reason: the
    verdict here is the receipt's signed one and is never re-derived, so an
    unrecognised threshold must not be able to fail a replay.
    """
    if receipt.reject_at is None:
        return configured
    try:
        return FindingGrade(receipt.reject_at)
    except ValueError:
        return configured


def build_handoff(
    *,
    subject_digest: str,
    run_id: str,
    node: st.PlanNode,
    base_sha: str,
    output_sha: str,
    diff: str,
    matrix: fin.ApplicabilityMatrix,
    rubric: fin.Rubric,
    report_path: Path,
) -> ReviewHandoff:
    """Assemble and validate the reviewer's contract (B9)."""
    asked = {cell.check_id for cell in matrix.cells}
    return ReviewHandoff(
        subject_digest=subject_digest,
        run_id=run_id,
        node_id=node.node_id,
        node_kind=node.kind.value,
        instruction=_node_goal(node),
        declared_outputs=list(node.outputs),
        gate_command=list(node.gate_command),
        gate_selector=node.gate_selector or "",
        gate_min_cases=node.gate_min_cases,
        # Read as an attribute, never through a defaulted lookup: that
        # operator is what let `min_cases` and `instruction` read as their
        # defaults for every node in every run, and a field reached that way
        # cannot fail loudly when the projection stops carrying it.
        effects=[{"effect": effect.effect, "disposition": effect.disposition,
                  "meaning": effect.meaning}
                 for effect in node.effects],
        base_sha=base_sha,
        output_sha=output_sha,
        diff=diff,
        matrix=[{"check_id": cell.check_id, "object_id": cell.object_id}
                for cell in matrix.cells],
        pair_count=matrix.pair_count,
        report_path=str(report_path.resolve()),
        rubric=[{"check_id": check.check_id, "question": check.question}
                for check in rubric.checks if check.check_id in asked]
        + [{"check_id": fin.CANARY_CHECK_ID,
            "question": ("Control cell. Answer it as the object id states: the "
                         "known-bad object is a finding, the known-good object "
                         "is clear.")}],
    ).require_complete()


class InstructionNotCarried(HandoffIncomplete):
    """An agent node reached the reviewer with no instruction on it.

    Kept as its own class because the two ways a goal can be missing are not
    the same failure and must not be handled the same way. A code node has no
    instruction to carry — its goal *is* its command (§6.2), and deriving one
    from that command is reading the contract, not guessing at it. An agent
    node's instruction is `min_length=1` in the plan model, so a blank one
    cannot be a plan that omitted a goal; it can only be a projection that
    dropped one, and that is exactly the state the system shipped in.

    So this is a refusal rather than a fallback. A fallback here would rebuild
    the defect it is meant to catch: the derivable goal is "make your own gate
    pass", which is not independent of the thing the reviewer is judging, and
    it is indistinguishable in the prompt from a real instruction that happens
    to be terse. Refusing fails closed at the one place that can still tell
    the difference — before a reviewer pane opens, with the node named.
    """


def _node_goal(node: st.PlanNode) -> str:
    """The goal the plan declared for this node, by node kind (§1.1 item 4).

    An agent node's goal is carried; a code node's goal is its command, which
    is what a code node declares in place of an instruction.
    """
    if node.kind is st.NodeKind.CODE:
        return ("Run the command {0!r} and change only the declared outputs."
                .format(" ".join(node.command)))
    if not node.instruction.strip():
        raise InstructionNotCarried(
            "{0}: the reviewer's contract has no instruction. An agent node's "
            "instruction is required and non-empty in the plan, so this is a "
            "projection that dropped it, not a plan that omitted it. Deriving "
            "one from the node's own gate is what made every agent node in "
            "every run reviewable only against 'make this command pass' "
            "(B9).".format(node.node_id))
    return node.instruction
