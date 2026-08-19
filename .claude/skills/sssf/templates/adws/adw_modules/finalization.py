"""Finalization (§6.5, §6.6, §11.1) — the rubric, the code-computed
matrix, the reviewer report, the verdict, and the signed receipt.

The shape of this module is dictated by one sentence in §6.5: *the
reviewer emits only per-cell `clear|finding` plus a message; it cannot
emit a verdict and it cannot emit a severity.* "Cannot" is load-bearing.
`ReportCell` and `ReviewerReport` carry no such fields and set
`extra="forbid"`, so a report containing either is rejected at parse
rather than parsed-and-ignored — the same construction `FailureSignal`
uses to make reading process output *unrepresentable* rather than merely
forbidden (§7.5). `find_forbidden_report_fields` is the executed detector
for that property, and its planted-violation fixture lives in the test,
because a detector never shown to convict is not a detector (§13.4).

Everything else follows from where authority sits:

* **Code computes the matrix.** Two reviewers receive identical coverage
  because neither chooses its own coverage.
* **Code derives the verdict and stamps severity from the rubric.** A
  confident PASS is not something a reviewer can say.
* **The canary is a pair.** One synthetic cell known-bad by construction
  and one known-good, both excluded from verdict derivation. An
  always-`clear` reviewer passes every plan; an always-`finding` reviewer
  permanently poisons the plan it was asked about, because FAIL is
  terminal for those bytes. Both are caught before any receipt exists.
* **The receipt persists the full per-cell matrix on PASS and on FAIL**,
  with the reviewer's (route, model, session id), signed with a detached
  Ed25519 signature, in a store that refuses to sit inside the repository
  or the SSSF data directory (§6.6).
* **Replay is keyed on the digest alone**, so changing a route does not
  re-review unchanged bytes.
* **A stall is not a receipt.** `FINALIZATION_STALLED` leaves no receipt
  for the digest, which is exactly what makes a rerun legal.
* **A FAIL has an operator escape, and only an operator has it.** §3.6 B10
  requires re-review of byte-identical input to be impossible *or
  explicitly recorded, with an operator escape*. `ReceiptStore.set_aside`
  is that escape: it retains the FAILed receipt and its signature under an
  archival name, writes a signed `SetAsideRecord` naming who invoked it,
  why, and exactly which receipt it superseded, and frees the live slot so
  the next `finalize` reviews afresh. It refuses a PASS and it refuses a
  digest with no receipt. Nothing calls it automatically and nothing reads
  its `reason`; what admits the fresh review is the same absence of a
  receipt that a stall leaves behind.

**Boundaries this module does not cross.** The plan model, its canonical
bytes, and its digest belong to §6.1–§6.4 and are another lane's: nothing
here parses a plan or computes a digest. `finalize` takes the digest and a
sequence of `ReviewObject`s as inputs, and takes the deterministic
obligations as an injected `validate` callable, so the ordering invariant
can be asserted without this module owning validation. The tracer's
reviewer-session row and stall record belong to the ledger and are reached
only through the window's injected recorders (`finalization_window`).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Mapping,
                    Optional, Sequence, Tuple, Type, Union)

from pydantic import BaseModel, ConfigDict

from . import receipt_crypto as rc
from .finalization_window import (FinalizationSignal, ReviewerSession,
                                  WindowOutcome)

# ── vocabulary ──────────────────────────────────────────────────────────────


class ObjectKind(str, Enum):
    """What a reviewable object is. §6.2's model, from the reviewer's side.

    The first five are plan-finalization's objects. `DIFF` and `CHANGED_FILE`
    are code review's (`code_review.py`), and they live here rather than in a
    parallel enum so that `compute_matrix`, `verify_report`, `derive_verdict`,
    `Receipt`, and `ReceiptStore` serve both reviews unchanged. A rubric only
    ever emits cells for the kinds its own checks declare, so the two review
    families never produce each other's cells.

    `GATE` and `INTEGRATION_GATE` are separate kinds because the two objects
    have opposite scope rules, not merely different ones. A node's gate is
    scoped to that node's own work and is red before it runs (§7.4); the
    plan's integration gate is the only gate in this design that runs the
    whole suite, at the final head where every merged node's work is present
    at once (§8.8), and its selector legitimately spans tests an earlier plan
    already merged alongside tests this run's lanes will write (§6.4's third
    executability arm, §19 M14). One kind serving both would ask the
    integration gate whether its selector is narrow, which is the question
    whose correct answer the architecture states as "no".
    """

    PLAN = "plan"
    NODE = "node"
    GATE = "gate"
    INTEGRATION_GATE = "integration_gate"
    EVIDENCE = "evidence"
    DIFF = "diff"
    CHANGED_FILE = "changed_file"


class CellStatus(str, Enum):
    """The only two answers a reviewer may give about a cell (§6.5)."""

    CLEAR = "clear"
    FINDING = "finding"


class Severity(str, Enum):
    """Stamped by code from the rubric. The reviewer cannot express it."""

    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"


class Verdict(str, Enum):
    """Derived by code. FAIL is terminal for those bytes (§6.5)."""

    PASS = "PASS"
    FAIL = "FAIL"


class CanaryKind(str, Enum):
    """§6.5's control pair, both excluded from verdict derivation."""

    KNOWN_BAD = "KNOWN_BAD"
    KNOWN_GOOD = "KNOWN_GOOD"


@dataclass(frozen=True)
class ReviewObject:
    """One object the matrix ranges over — supplied by the caller, because
    the plan model belongs to §6.1–§6.4 and not here."""

    object_id: str
    kind: ObjectKind


@dataclass(frozen=True)
class RubricCheck:
    """One semantic question, its applicable object kinds, and the
    severity **code** stamps when the answer is `finding`."""

    check_id: str
    question: str
    applies_to: Tuple[ObjectKind, ...]
    severity: Severity


class UnknownCheck(KeyError):
    """A check id the rubric does not define."""


@dataclass(frozen=True)
class Rubric:
    version: str
    checks: Tuple[RubricCheck, ...]

    def check(self, check_id: str) -> RubricCheck:
        for check in self.checks:
            if check.check_id == check_id:
                return check
        raise UnknownCheck(check_id)

    def applicable(self, obj: ReviewObject) -> Tuple[RubricCheck, ...]:
        return tuple(c for c in self.checks if obj.kind in c.applies_to)


#: The rubric's questions are §6.2's judgments that no git object can
#: settle: whether the graph accomplishes the stated intent, whether a node
#: can do its work from the reads it declares and within the writes it is
#: permitted, whether a node gate's selector is scoped to its own node's work
#: and whether the integration gate's covers the merged surface instead,
#: whether a hypothesis is dischargeable, whether evidence supports the claim
#: made from it. Severity is a property of the question, fixed here in code.
#:
#: The version names the applicability matrix as much as the questions. A
#: receipt persists its full per-cell matrix (§6.5) and that matrix is only
#: interpretable against the rubric that produced it, so moving a check
#: between object kinds is a new version even when no question's text
#: changes. `v2` moved the two node-scoped gate checks off the plan's
#: integration gate. `v3` added `node.writes_are_sufficient`, so a v2 receipt
#: records a matrix in which that question was never asked of any node —
#: which is exactly why the label has to move.
#:
#: A rubric version is not a receipt schema version, and §19 M21 is the price
#: of confusing the two: `Receipt.from_bytes` discriminates on the presence of
#: the key whose presence is in question, never on this label, so bumping the
#: rubric adds no key, requires no key, and leaves every signed v1 and v2
#: receipt parseable, verifiable and replayable at bytes nobody touched. The
#: new question is nonetheless mandatory from v3 onward rather than optional
#: forever (§3.6 B8): it is a matrix cell, `compute_matrix` emits it for every
#: node, and `verify_report` refuses a report that does not answer every cell,
#: so no v3 review can skip it.
DEFAULT_RUBRIC = Rubric(
    version="maestro-rubric.v3",
    checks=(
        RubricCheck(
            check_id="plan.intent_is_accomplished_by_the_graph",
            question=("Do the nodes, taken together, accomplish the stated "
                      "intent, with nothing load-bearing left unowned?"),
            applies_to=(ObjectKind.PLAN,),
            severity=Severity.BLOCKING),
        RubricCheck(
            check_id="plan.decomposition_is_honest",
            question=("Is the decomposition into nodes real work rather than "
                      "one node wearing several names?"),
            applies_to=(ObjectKind.PLAN,),
            severity=Severity.ADVISORY),
        RubricCheck(
            check_id="node.reads_are_sufficient",
            question=("Can this node's agent do the work from its declared "
                      "reads alone?"),
            applies_to=(ObjectKind.NODE,),
            severity=Severity.BLOCKING),
        # The write-scope counterpart of the check above, and the reason it
        # exists: `outputs` is not a wish list, it is the node's entire write
        # permission. Single-producer ownership gives a path to exactly one
        # node (§6.4 SINGLE_OUTPUT_OWNER) and the attempt's permission check
        # convicts any diff touching a path the node does not declare, so a
        # node whose instruction can only be discharged by editing someone
        # else's output is unsatisfiable from its first attempt — the reviewer
        # rejects every diff that does not wire production and the permission
        # check would reject every diff that does. That is a question about
        # the authored bytes, so it belongs at plan review, where the answer
        # costs one cell instead of a review budget.
        #
        # The question is deliberately about the *instruction*, not about the
        # shape of `outputs`: a node that creates a new module nothing at base
        # yet imports is the ordinary case, legitimate whenever its
        # instruction stops at the module it owns and a downstream node owns
        # the wiring. What convicts is an instruction stating a property that
        # only some other node's file could be changed to satisfy.
        RubricCheck(
            check_id="node.writes_are_sufficient",
            question=("Can this node's agent discharge its instruction by "
                      "changing only its declared `outputs`, or does the "
                      "instruction state a property that some file this node "
                      "is not permitted to write would have to change for it "
                      "to hold?"),
            applies_to=(ObjectKind.NODE,),
            severity=Severity.BLOCKING),
        RubricCheck(
            check_id="node.hypotheses_are_dischargeable",
            question=("Is every Hypothesis this node reads dischargeable by "
                      "this rubric rather than merely asserted?"),
            applies_to=(ObjectKind.NODE,),
            severity=Severity.BLOCKING),
        RubricCheck(
            check_id="gate.asserts_a_post_condition",
            question=("Does the gate assert the post-state of this node's own "
                      "work — red before, green after — rather than accepting "
                      "the null result?"),
            applies_to=(ObjectKind.GATE,),
            severity=Severity.BLOCKING),
        RubricCheck(
            check_id="gate.selector_is_scoped_to_this_node",
            question=("Does the selector name only what this node's outputs "
                      "supply?"),
            applies_to=(ObjectKind.GATE,),
            severity=Severity.BLOCKING),
        RubricCheck(
            check_id="gate.selector_covers_the_merged_surface",
            question=("Does the selector name the whole surface this plan "
                      "produces — every lane's declared test output — rather "
                      "than a subset of the lanes it must integrate?"),
            applies_to=(ObjectKind.INTEGRATION_GATE,),
            severity=Severity.BLOCKING),
        RubricCheck(
            check_id="gate.min_cases_is_meaningful",
            question="Is `min_cases` more than a formality for this gate?",
            applies_to=(ObjectKind.GATE, ObjectKind.INTEGRATION_GATE),
            severity=Severity.ADVISORY),
        RubricCheck(
            check_id="evidence.supports_the_claim_made_from_it",
            question=("Does this evidence actually support the claim the plan "
                      "makes from it?"),
            applies_to=(ObjectKind.EVIDENCE,),
            severity=Severity.BLOCKING),
    ))

#: The control pair's check and its two synthetic objects (§6.5).
CANARY_CHECK_ID = "canary.cell_was_read"
CANARY_KNOWN_BAD_OBJECT = "__canary_known_bad__"
CANARY_KNOWN_GOOD_OBJECT = "__canary_known_good__"


# ── the code-computed applicability matrix ──────────────────────────────────

@dataclass(frozen=True)
class MatrixCell:
    check_id: str
    object_id: str
    canary: Optional[CanaryKind] = None

    @property
    def is_canary(self) -> bool:
        return self.canary is not None

    @property
    def key(self) -> Tuple[str, str]:
        return (self.check_id, self.object_id)


@dataclass(frozen=True)
class ApplicabilityMatrix:
    plan_digest: str
    rubric_version: str
    cells: Tuple[MatrixCell, ...]

    @property
    def pair_count(self) -> int:
        """What the reviewer must echo — every cell it was asked to answer,
        canaries included, since it answers those too."""
        return len(self.cells)

    @property
    def graded_cells(self) -> Tuple[MatrixCell, ...]:
        return tuple(c for c in self.cells if not c.is_canary)

    @property
    def canary_cells(self) -> Tuple[MatrixCell, ...]:
        return tuple(c for c in self.cells if c.is_canary)


def compute_matrix(rubric: Rubric, plan_digest: str,
                   objects: Iterable[ReviewObject]) -> ApplicabilityMatrix:
    """The (check × object) pairs, computed in code so two reviewers
    receive identical coverage (§6.5).

    Deterministic in its ordering, so the same plan yields the same matrix
    on every machine — which is what makes the pair count a usable
    fabrication canary rather than a number that drifts.
    """
    graded = sorted(
        (MatrixCell(check_id=check.check_id, object_id=obj.object_id)
         for obj in objects
         for check in rubric.applicable(obj)),
        key=lambda cell: cell.key)
    canaries = (
        MatrixCell(check_id=CANARY_CHECK_ID,
                   object_id=CANARY_KNOWN_BAD_OBJECT,
                   canary=CanaryKind.KNOWN_BAD),
        MatrixCell(check_id=CANARY_CHECK_ID,
                   object_id=CANARY_KNOWN_GOOD_OBJECT,
                   canary=CanaryKind.KNOWN_GOOD),
    )
    return ApplicabilityMatrix(plan_digest=plan_digest,
                               rubric_version=rubric.version,
                               cells=tuple(graded) + canaries)


# ── the reviewer's report — verdict and severity are unrepresentable ────────

class ReportCell(BaseModel):
    """One answered cell. There is no `verdict` field and no `severity`
    field, and `extra="forbid"` means adding either is a parse error
    rather than an ignored key (§6.5)."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    object_id: str
    status: CellStatus
    message: str = ""


class ReviewerReport(BaseModel):
    """The whole report. `plan_digest` and `pair_count` are §6.5's
    fabrication canary: a report that cannot echo what it was given did
    not read what it was given."""

    model_config = ConfigDict(extra="forbid")

    plan_digest: str
    pair_count: int
    cells: List[ReportCell]


def report_is_complete(payload: object) -> bool:
    """Whether a polled report carries every cell it says it answers.

    A reviewer writes its report with an ordinary file write, so a poll that
    lands mid-write reads a file that parses and is not finished. Accepting
    it converts the reviewer's own incomplete draft into a `CELL_SET`
    rejection, which is terminal for the plan's bytes -- a read race
    condemning a plan the reviewer went on to clear.

    `pair_count` is the reviewer's echo of the matrix size (the fabrication
    canary above), so a payload carrying fewer cells than it claims is, by
    its own account, still being written. This is structural per S1.2: the
    only things read are the declared count and the number of cells present,
    never a message, a status, or any other prose the reviewer produced.

    A report that stays incomplete for the whole window stalls it, and a
    stall is a fact about the machine that permits a rerun (S6.5), which is
    the correct classification for a reviewer that stopped writing.
    """
    if not isinstance(payload, Mapping):
        return False
    declared = payload.get("pair_count")
    cells = payload.get("cells")
    if not isinstance(declared, int) or isinstance(declared, bool):
        return False
    if not isinstance(cells, list):
        return False
    return len(cells) == declared


#: Names that must never appear anywhere in the report schema.
FORBIDDEN_REPORT_FIELDS = ("verdict", "severity")


def find_forbidden_report_fields(model: Type[BaseModel]) -> List[str]:
    """The executed detector for §6.5's unrepresentability claim.

    Returns every forbidden field name reachable in `model`'s schema,
    including nested models. Returning `[]` for the real schema proves
    nothing on its own — the test plants a violating model and asserts
    this convicts it, per §13.4's rule.
    """
    found: List[str] = []
    for current in report_schema_closure(model):
        for name in current.model_fields:
            if name in FORBIDDEN_REPORT_FIELDS:
                found.append(name)
    return found


def report_schema_closure(model: Type[BaseModel]) -> List[Type[BaseModel]]:
    """`model` and every model reachable from its fields.

    Named and exported so a check over the report schema can *discover* its
    subjects instead of being handed them. A guard pointed at a hand-written
    list of classes stops covering the schema the moment the schema grows,
    and reports clean while doing it.
    """
    seen: List[Type[BaseModel]] = []

    def walk(current: Type[BaseModel]) -> None:
        if current in seen:
            return
        seen.append(current)
        for field in current.model_fields.values():
            for nested in _nested_models(field.annotation):
                walk(nested)

    walk(model)
    return seen


def _nested_models(annotation: Any) -> List[Type[BaseModel]]:
    models: List[Type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    for arg in getattr(annotation, "__args__", ()) or ():
        models.extend(_nested_models(arg))
    return models


# ── report verification, all of it before any receipt exists ────────────────

class RejectionReason(str, Enum):
    DIGEST_ECHO = "DIGEST_ECHO"
    PAIR_COUNT = "PAIR_COUNT"
    CELL_SET = "CELL_SET"
    CANARY_KNOWN_BAD_CLEARED = "CANARY_KNOWN_BAD_CLEARED"
    CANARY_KNOWN_GOOD_FLAGGED = "CANARY_KNOWN_GOOD_FLAGGED"
    OCCUPANCY = "OCCUPANCY"


class ReportRejected(RuntimeError):
    """The report is refused and no receipt is written (§6.5)."""

    def __init__(self, reason: RejectionReason, message: str) -> None:
        super().__init__(f"{reason.value}: {message}")
        self.reason = reason


def verify_report(matrix: ApplicabilityMatrix, report: ReviewerReport) -> None:
    """The fabrication canary and the control pair, in that order.

    Every rejection here happens *before* the receipt exists, which is
    what keeps a fabricated or unread report from ever minting an
    identity.
    """
    if report.plan_digest != matrix.plan_digest:
        raise ReportRejected(
            RejectionReason.DIGEST_ECHO,
            f"report echoes {report.plan_digest!r}, matrix is "
            f"{matrix.plan_digest!r}")
    if report.pair_count != matrix.pair_count:
        raise ReportRejected(
            RejectionReason.PAIR_COUNT,
            f"report echoes {report.pair_count} pairs, matrix has "
            f"{matrix.pair_count}")

    answered: Dict[Tuple[str, str], CellStatus] = {}
    for cell in report.cells:
        key = (cell.check_id, cell.object_id)
        if key in answered:
            raise ReportRejected(RejectionReason.CELL_SET,
                                 f"cell answered twice: {key}")
        answered[key] = cell.status

    expected = {cell.key for cell in matrix.cells}
    if set(answered) != expected:
        missing = sorted(expected - set(answered))
        invented = sorted(set(answered) - expected)
        raise ReportRejected(
            RejectionReason.CELL_SET,
            f"missing={missing} invented={invented}")

    for cell in matrix.canary_cells:
        status = answered[cell.key]
        if (cell.canary is CanaryKind.KNOWN_BAD
                and status is CellStatus.CLEAR):
            raise ReportRejected(
                RejectionReason.CANARY_KNOWN_BAD_CLEARED,
                "the reviewer cleared a cell that is known-bad by "
                "construction, so it is not reading the cells")
        if (cell.canary is CanaryKind.KNOWN_GOOD
                and status is CellStatus.FINDING):
            raise ReportRejected(
                RejectionReason.CANARY_KNOWN_GOOD_FLAGGED,
                "the reviewer flagged a cell that is known-good by "
                "construction, so it is not reading the cells")


def check_occupancy(occupancy: Optional[float], threshold: float = 0.8) -> None:
    """§6.5's occupancy gate. Convicts above threshold **and on a NULL
    row**, so it cannot rot back into unread telemetry: a missing
    context-window reading is a missing measurement, not a passing one.
    """
    if occupancy is None:
        raise ReportRejected(
            RejectionReason.OCCUPANCY,
            "no context-window occupancy was recorded for the reviewer "
            "session; a NULL row convicts")
    if occupancy > threshold:
        raise ReportRejected(
            RejectionReason.OCCUPANCY,
            f"reviewer context occupancy {occupancy} exceeds {threshold}")


# ── code derives the verdict, and stamps severity from the rubric ───────────

@dataclass(frozen=True)
class DerivedCell:
    """One adjudicated cell of a receipt's matrix.

    `grade` carries the reviewer's assessment of a *finding's consequence*
    where the review family that produced it grades findings (node code
    review, §19 M17). It is `None` for plan finalization, whose reviewer has
    no grade to give, and for any cell that is not a finding.

    It is optional because plan finalization's cells genuinely have no grade,
    not because it may be omitted where one exists. It is written into the
    receipt from the first version that grades anything, deliberately: §3.6
    B8's rule is that a field added after receipts exist is optional forever,
    and the grade is what decided the verdict, so a receipt without it records
    a verdict whose derivation cannot be re-checked from the receipt alone.
    """

    check_id: str
    object_id: str
    status: CellStatus
    severity: Severity
    message: str = ""
    canary: Optional[CanaryKind] = None
    grade: Optional[str] = None


@dataclass(frozen=True)
class DerivedVerdict:
    verdict: Verdict
    cells: Tuple[DerivedCell, ...]


def derive_verdict(matrix: ApplicabilityMatrix, report: ReviewerReport,
                   rubric: Rubric) -> DerivedVerdict:
    """FAIL if any graded cell carries a `finding` whose check is
    BLOCKING; PASS otherwise.

    Canary cells are carried into the receipt — coverage is auditable —
    but take no part in this decision. They must not: the known-bad cell
    is always answered `finding`, so counting it would fail every plan.
    """
    answered = {(c.check_id, c.object_id): c for c in report.cells}
    cells: List[DerivedCell] = []
    failed = False
    for cell in matrix.cells:
        answer = answered[cell.key]
        if cell.is_canary:
            severity = Severity.ADVISORY
        else:
            severity = rubric.check(cell.check_id).severity
            if (answer.status is CellStatus.FINDING
                    and severity is Severity.BLOCKING):
                failed = True
        cells.append(DerivedCell(
            check_id=cell.check_id, object_id=cell.object_id,
            status=answer.status, severity=severity, message=answer.message,
            canary=cell.canary))
    return DerivedVerdict(verdict=Verdict.FAIL if failed else Verdict.PASS,
                          cells=tuple(cells))


# ── the receipt and its store (§6.6) ────────────────────────────────────────

@dataclass(frozen=True)
class ReviewerIdentity:
    """§6.5: recorded, not asserted. Nothing here binds the reviewer to a
    different model family than the author's; the receipt makes
    independence an auditable fact after the run instead."""

    route: str
    model: str
    session_id: str


def _require_created_at_epoch(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReceiptInvalid("created_at_epoch must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ReceiptInvalid("created_at_epoch must be a finite number") from exc
    if not finite:
        raise ReceiptInvalid("created_at_epoch must be a finite number")


@dataclass(frozen=True)
class Receipt:
    """One signed verdict about one digest, and everything that derived it.

    `reject_at` is the grade threshold the review ran under, and it is the
    other half of what `DerivedCell.grade` records. A grade decides nothing on
    its own: a cell rejects when its check is BLOCKING **and** its grade
    reaches the threshold, so a receipt carrying grades without the threshold
    still records a verdict whose derivation cannot be re-checked from the
    receipt alone — which is the exact argument that put the grade there, one
    field short. It is `None` for plan finalization, whose verdict has no
    threshold to run under, and for any receipt written before grading
    existed. `rubric_version` names the rubric, not the receipt schema: the
    graded fields were added under `maestro-rubric.v2` without moving that
    version, so two incompatible v2 shapes exist. `from_bytes` discriminates
    on whether `reject_at` is present, not on the rubric label. A pre-grading
    v2 receipt — no `reject_at`, cells with no `grade` — replays with both
    honestly `None`. A receipt that carries the graded keys must carry both.
    The threshold is configuration (`execution.review_reject_grade`, §6.2), so
    it can differ between the review and any later reading of it. That is
    precisely why the receipt must carry the one the verdict was derived with
    rather than let a reader supply today's.
    """

    plan_digest: str
    rubric_version: str
    verdict: Verdict
    cells: Tuple[DerivedCell, ...]
    reviewer: ReviewerIdentity
    created_at_epoch: float
    reject_at: Optional[str] = None


    def to_bytes(self) -> bytes:
        """The receipt's stored bytes — what the signature covers.

        Canonical (sorted keys, no incidental whitespace) so the same
        receipt always serialises identically and its signature can be
        re-derived rather than only checked.
        """
        _require_created_at_epoch(self.created_at_epoch)
        payload = {
            "plan_digest": self.plan_digest,
            "rubric_version": self.rubric_version,
            "verdict": self.verdict.value,
            "created_at_epoch": self.created_at_epoch,
            "reviewer": {
                "route": self.reviewer.route,
                "model": self.reviewer.model,
                "session_id": self.reviewer.session_id,
            },
        }
        if self.rubric_version != "maestro-rubric.v1":
            payload["reject_at"] = self.reject_at
        payload["cells"] = [
                {
                    "check_id": cell.check_id,
                    "object_id": cell.object_id,
                    "status": cell.status.value,
                    "severity": cell.severity.value,
                    "message": cell.message,
                    "canary": None if cell.canary is None else cell.canary.value,
                    "grade": cell.grade,
                }
                for cell in self.cells
            ]
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "Receipt":
        if not isinstance(data, bytes):
            raise ReceiptInvalid("receipt bytes must be bytes")

        def object_without_duplicates(pairs):
            payload = {}
            for key, value in pairs:
                if key in payload:
                    raise ReceiptInvalid(
                        "receipt JSON contains a duplicate object field")
                payload[key] = value
            return payload

        try:
            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=object_without_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptInvalid("receipt bytes are not UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ReceiptInvalid("receipt fields do not match the frozen receipt schema")
        rubric_version = payload.get("rubric_version")
        if not isinstance(rubric_version, str):
            raise ReceiptInvalid("rubric_version must be a string")
        # `reject_at` present → graded schema. Absent → pre-grading (v1, or
        # v2 written before the fields existed). The rubric label is not
        # the discriminator: v2 names both shapes.
        has_reject_at = "reject_at" in payload
        expected = {
            "plan_digest", "rubric_version", "verdict", "created_at_epoch",
            "reviewer", "cells",
        }
        if has_reject_at:
            expected = expected | {"reject_at"}
        if set(payload) != expected:
            raise ReceiptInvalid("receipt fields do not match the frozen receipt schema")
        reviewer = payload["reviewer"]
        if (not isinstance(reviewer, dict) or
                set(reviewer) != {"route", "model", "session_id"} or
                not all(isinstance(value, str) for value in reviewer.values())):
            raise ReceiptInvalid("reviewer fields do not match the frozen receipt schema")
        cells = payload["cells"]
        if not isinstance(cells, list):
            raise ReceiptInvalid("receipt cells must be an array")
        try:
            _require_plan_digest(payload["plan_digest"])
            created_at_epoch = payload["created_at_epoch"]
            _require_created_at_epoch(created_at_epoch)
            verdict = Verdict(payload["verdict"])
            if has_reject_at:
                reject_at = payload["reject_at"]
                if reject_at is not None and not isinstance(reject_at, str):
                    raise ReceiptInvalid("receipt reject_at must be a string or null")
            else:
                reject_at = None
            cell_base = {
                "check_id", "object_id", "status", "severity",
                "message", "canary",
            }
            cell_graded = cell_base | {"grade"}
            derived_cells = []
            for cell in cells:
                if not isinstance(cell, dict):
                    raise ReceiptInvalid(
                        "receipt cell fields do not match the frozen receipt schema")
                keys = set(cell)
                if keys == cell_graded:
                    if not has_reject_at and rubric_version != "maestro-rubric.v1":
                        raise ReceiptInvalid(
                            "receipt fields do not match the frozen receipt schema")
                    grade = cell["grade"]
                elif keys == cell_base:
                    if has_reject_at:
                        raise ReceiptInvalid(
                            "receipt cell fields do not match the frozen receipt schema")
                    grade = None
                else:
                    raise ReceiptInvalid(
                        "receipt cell fields do not match the frozen receipt schema")
                if not all(isinstance(cell[field], str)
                           for field in ("check_id", "object_id", "message")):
                    raise ReceiptInvalid("receipt cell text fields must be strings")
                canary = cell["canary"]
                if canary is not None and not isinstance(canary, str):
                    raise ReceiptInvalid("receipt cell canary must be a string or null")
                if grade is not None and not isinstance(grade, str):
                    raise ReceiptInvalid("receipt cell grade must be a string or null")
                derived_cells.append(DerivedCell(
                    check_id=cell["check_id"],
                    object_id=cell["object_id"],
                    status=CellStatus(cell["status"]),
                    severity=Severity(cell["severity"]),
                    message=cell["message"],
                    canary=None if canary is None else CanaryKind(canary),
                    grade=grade))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReceiptInvalid("receipt values do not match the frozen receipt schema") from exc
        return cls(
            plan_digest=payload["plan_digest"],
            rubric_version=payload["rubric_version"],
            verdict=verdict,
            cells=tuple(derived_cells),
            reviewer=ReviewerIdentity(
                route=reviewer["route"],
                model=reviewer["model"],
                session_id=reviewer["session_id"]),
            created_at_epoch=created_at_epoch,
            reject_at=reject_at)


_PLAN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE_HEX = re.compile(r"^[0-9a-fA-F]{128}$")


class ReceiptStoreLocationError(RuntimeError):
    """§6.6's startup invariant: receipts live outside both the repository
    and the SSSF data directory, and a store violating that refuses to
    run rather than warning."""


class ReceiptExists(RuntimeError):
    """Receipts are create-once. Rewriting one is how a verdict silently
    changes under a digest that has already been published."""


class ReceiptInvalid(RuntimeError):
    """Receipt bytes do not satisfy the frozen receipt schema."""


class SignatureMissing(RuntimeError):
    """A receipt with no signature beside it. A hard error, never treated
    as an absent receipt — otherwise deleting signatures forces re-review
    roulette (§6.6)."""


class SignatureInvalid(RuntimeError):
    """A receipt whose signature does not verify under any known key."""


class SigningKeyUnavailable(RuntimeError):
    """This store can verify but cannot sign — the ordinary state
    everywhere but the finalizer's own machine."""


class SetAsideRefused(RuntimeError):
    """The operator escape refused to run.

    Three refusals, and each one is what keeps the escape from becoming a
    way to re-roll a reviewer: there is no receipt to set aside, the
    receipt is a PASS and there is nothing to escape from, or the operator
    named no invoker or gave no reason.
    """


def _require_plan_digest(plan_digest: str) -> None:
    if not isinstance(plan_digest, str) or not _PLAN_DIGEST.fullmatch(plan_digest):
        raise ReceiptInvalid("plan_digest must be a 64-character lowercase hex digest")


def _require_sha256_hex(value: object, field: str) -> None:
    if not isinstance(value, str) or not _PLAN_DIGEST.fullmatch(value):
        raise ReceiptInvalid(
            f"{field} must be a 64-character lowercase hex digest")


def _require_stated(value: object, field: str) -> str:
    """An escape nobody signed and nobody explained is not deliberate.

    Blank is refused rather than defaulted, because the whole value of the
    record is that a human wrote something into it.
    """
    if not isinstance(value, str) or not value.strip():
        raise SetAsideRefused(
            f"{field} must be a non-empty statement; setting a receipt aside "
            "is a deliberate, attributable operator act (§3.6 B10)")
    return value


def _require_sequence(sequence: object) -> int:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ReceiptInvalid("a set-aside sequence is a positive integer")
    if sequence > _MAX_SET_ASIDE_SEQUENCE:
        raise ReceiptInvalid(
            f"a set-aside sequence is at most {_MAX_SET_ASIDE_SEQUENCE}")
    return sequence


#: The archival filename encodes the sequence in a fixed width, so the
#: bound is a property of the layout rather than a policy about how often
#: an operator may escape. Deliberately not a quota: B10 asks for an
#: escape that is recorded, not one that is rationed.
_MAX_SET_ASIDE_SEQUENCE = 9999
_SET_ASIDE_SEQUENCE = re.compile(r"^\d{4}$")


@dataclass(frozen=True)
class SetAsideRecord:
    """§3.6 B10's operator escape, as a typed durable record.

    B10 requires that re-review of byte-identical input be impossible *or
    explicitly recorded, with an operator escape*. Maestro shipped the
    first half — receipts are create-once and replay is keyed on the
    digest alone (§6.5) — and had no second half, so a FAIL that Maestro's
    own defect produced condemned a correct plan at those bytes forever
    (§19 M16, §16.3 item 56).

    **`reason` is an audit field and nothing else.** §1.2 forbids any
    lifecycle transition caused by free text, so no decision anywhere
    reads it and none may be added that does. What admits a fresh review
    is the *absence* of a live receipt — exactly the condition
    `FINALIZATION_STALLED` already leaves behind (§6.5). This record
    explains an escape that happened; it never causes one.

    `superseded_receipt_sha256` binds the record to the exact bytes it set
    aside, so the archived receipt and the record that explains it cannot
    drift apart, and a record naming a receipt the store cannot produce is
    detectable rather than merely doubtful.
    """

    plan_digest: str
    sequence: int
    invoked_by: str
    reason: str
    superseded_verdict: Verdict
    superseded_rubric_version: str
    superseded_reviewer: ReviewerIdentity
    superseded_created_at_epoch: float
    superseded_receipt_sha256: str
    created_at_epoch: float

    def to_bytes(self) -> bytes:
        """The record's stored bytes — what its signature covers."""
        _require_created_at_epoch(self.created_at_epoch)
        _require_created_at_epoch(self.superseded_created_at_epoch)
        payload = {
            "plan_digest": self.plan_digest,
            "sequence": self.sequence,
            "invoked_by": self.invoked_by,
            "reason": self.reason,
            "superseded_verdict": self.superseded_verdict.value,
            "superseded_rubric_version": self.superseded_rubric_version,
            "superseded_reviewer": {
                "route": self.superseded_reviewer.route,
                "model": self.superseded_reviewer.model,
                "session_id": self.superseded_reviewer.session_id,
            },
            "superseded_created_at_epoch": self.superseded_created_at_epoch,
            "superseded_receipt_sha256": self.superseded_receipt_sha256,
            "created_at_epoch": self.created_at_epoch,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "SetAsideRecord":
        if not isinstance(data, bytes):
            raise ReceiptInvalid("set-aside record bytes must be bytes")

        def object_without_duplicates(pairs):
            payload = {}
            for key, value in pairs:
                if key in payload:
                    raise ReceiptInvalid(
                        "set-aside record JSON contains a duplicate object field")
                payload[key] = value
            return payload

        try:
            payload = json.loads(
                data.decode("utf-8"), object_pairs_hook=object_without_duplicates)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptInvalid(
                "set-aside record bytes are not UTF-8 JSON") from exc
        expected = {
            "plan_digest", "sequence", "invoked_by", "reason",
            "superseded_verdict", "superseded_rubric_version",
            "superseded_reviewer", "superseded_created_at_epoch",
            "superseded_receipt_sha256", "created_at_epoch",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ReceiptInvalid(
                "set-aside record fields do not match the frozen record schema")
        reviewer = payload["superseded_reviewer"]
        if (not isinstance(reviewer, dict) or
                set(reviewer) != {"route", "model", "session_id"} or
                not all(isinstance(value, str) for value in reviewer.values())):
            raise ReceiptInvalid(
                "set-aside reviewer fields do not match the frozen record schema")
        try:
            _require_plan_digest(payload["plan_digest"])
            _require_sequence(payload["sequence"])
            _require_sha256_hex(payload["superseded_receipt_sha256"],
                                "superseded_receipt_sha256")
            if not all(isinstance(payload[field], str) and payload[field].strip()
                       for field in ("invoked_by", "reason")):
                raise ReceiptInvalid(
                    "a set-aside record states its invoker and its reason")
            if not isinstance(payload["superseded_rubric_version"], str):
                raise ReceiptInvalid("superseded_rubric_version must be a string")
            _require_created_at_epoch(payload["created_at_epoch"])
            _require_created_at_epoch(payload["superseded_created_at_epoch"])
            verdict = Verdict(payload["superseded_verdict"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReceiptInvalid(
                "set-aside record values do not match the frozen record schema"
            ) from exc
        return cls(
            plan_digest=payload["plan_digest"],
            sequence=payload["sequence"],
            invoked_by=payload["invoked_by"],
            reason=payload["reason"],
            superseded_verdict=verdict,
            superseded_rubric_version=payload["superseded_rubric_version"],
            superseded_reviewer=ReviewerIdentity(
                route=reviewer["route"],
                model=reviewer["model"],
                session_id=reviewer["session_id"]),
            superseded_created_at_epoch=payload["superseded_created_at_epoch"],
            superseded_receipt_sha256=payload["superseded_receipt_sha256"],
            created_at_epoch=payload["created_at_epoch"])


class ReceiptStore:
    """Create-once detached receipts with verified read-only access.

    Writers publish a signed pending record before making receipt bytes
    visible.  A crash can therefore be recovered only when the pending
    signature verifies the exact persisted bytes; raw orphan JSON is never
    promoted to authority.
    """

    def __init__(self, root, *, repo_paths: Sequence[Union[str, Path]],
                 data_dir, verify_keys: Sequence[bytes],
                 signing_seed: Optional[bytes] = None,
                 create: bool = True) -> None:
        self.root = Path(root).resolve()
        forbidden = tuple(
            ("repository", Path(path).resolve()) for path in repo_paths
        ) + (("SSSF data directory", Path(data_dir).resolve()),)
        for label, boundary in forbidden:
            if self.root == boundary or _is_inside(self.root, boundary):
                raise ReceiptStoreLocationError(
                    f"the receipt store is inside the {label} ({boundary}); "
                    "§6.6 places it outside both, because a store inside the "
                    "data directory grants every agent unconditional write "
                    "authority over receipts")
        self._verify_keys = tuple(verify_keys)
        self._signing_seed = signing_seed
        if (signing_seed is not None and
                rc.seed_to_public_key(signing_seed) not in self._verify_keys):
            raise SigningKeyUnavailable(
                "the signing key's public key is absent from this store's "
                "verification keys")
        self._create = create
        self._replace = os.replace
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    # ── layout ──────────────────────────────────────────────────────────

    def _confined_path(self, filename: str) -> Path:
        candidate = self.root / filename
        if candidate.is_symlink() or (
                candidate.exists() and
                not _is_inside(candidate.resolve(), self.root)):
            raise ReceiptInvalid("receipt path resolves outside the receipt store")
        return candidate

    def path_for(self, plan_digest: str) -> Path:
        _require_plan_digest(plan_digest)
        return self._confined_path(f"{plan_digest}.json")

    def signature_path_for(self, plan_digest: str) -> Path:
        _require_plan_digest(plan_digest)
        return self._confined_path(f"{plan_digest}.json.sig")

    def _pending_path_for(self, plan_digest: str) -> Path:
        _require_plan_digest(plan_digest)
        return self._confined_path(f"{plan_digest}.pending")

    @contextlib.contextmanager
    def _locked(self, plan_digest: str) -> Iterator[None]:
        lock_path = self._confined_path(f"{plan_digest}.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def has(self, plan_digest: str) -> bool:
        """An orphan JSON remains visible as a hard verification error."""
        return self.path_for(plan_digest).is_file()

    # ── atomic publication and recovery ─────────────────────────────────

    def _stage(self, destination: Path, data: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".receipt-", dir=str(self.root))
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._replace(str(temporary_path), str(destination))
            self._fsync_root()
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _fsync_root(self) -> None:
        descriptor = os.open(str(self.root), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_signature(self, path: Path, plan_digest: str) -> bytes:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise SignatureInvalid(
                f"the receipt signature for {plan_digest} is not UTF-8") from exc
        encoded = text[:-1] if text.endswith("\n") else text
        if not _SIGNATURE_HEX.fullmatch(encoded):
            raise SignatureInvalid(
                f"the receipt signature for {plan_digest} is not hexadecimal")
        return bytes.fromhex(encoded)

    def _signature_verifies(self, data: bytes, signature: bytes) -> bool:
        return any(rc.verify(key, data, signature) for key in self._verify_keys)

    def _recover_locked(self, plan_digest: str) -> bool:
        recovered = False
        path = self.path_for(plan_digest)
        signature_path = self.signature_path_for(plan_digest)
        pending_path = self._pending_path_for(plan_digest)
        if pending_path.is_file():
            signature = self._read_signature(pending_path, plan_digest)
            if not path.is_file():
                pending_path.unlink()
                self._fsync_root()
            else:
                data = path.read_bytes()
                if not self._signature_verifies(data, signature):
                    raise SignatureInvalid(
                        f"the pending receipt for {plan_digest} verifies under "
                        f"none of this store's {len(self._verify_keys)} public "
                        f"key(s)")
                expected = signature.hex().encode("ascii") + b"\n"
                if signature_path.is_file():
                    existing = self._read_signature(signature_path, plan_digest)
                    if existing != signature:
                        raise ReceiptExists(
                            f"a conflicting receipt signature exists for "
                            f"{plan_digest}")
                else:
                    self._stage(signature_path, expected)
                pending_path.unlink()
                self._fsync_root()
                recovered = True
        if self._recover_set_aside_locked(plan_digest):
            recovered = True
        return recovered

    def recover(self, plan_digest: str) -> bool:
        """Complete only an authenticated interrupted publication."""
        _require_plan_digest(plan_digest)
        if not self._create:
            return False
        with self._locked(plan_digest):
            return self._recover_locked(plan_digest)

    # ── writing and reading ─────────────────────────────────────────────

    def write(self, receipt: Receipt) -> Path:
        if self._signing_seed is None:
            raise SigningKeyUnavailable(
                "this store holds no signing key; the signing key is "
                "finalizer-held (§6.6)")
        if not self._create:
            raise FileNotFoundError("receipt store is opened read-only")
        _require_plan_digest(receipt.plan_digest)
        _require_created_at_epoch(receipt.created_at_epoch)
        path = self.path_for(receipt.plan_digest)
        signature_path = self.signature_path_for(receipt.plan_digest)
        pending_path = self._pending_path_for(receipt.plan_digest)
        data = receipt.to_bytes()
        signature = rc.sign(self._signing_seed, data)
        encoded_signature = signature.hex().encode("ascii") + b"\n"
        with self._locked(receipt.plan_digest):
            self._recover_locked(receipt.plan_digest)
            if path.is_file():
                if not signature_path.is_file() and path.read_bytes() == data:
                    self._stage(signature_path, encoded_signature)
                    return path
                raise ReceiptExists(
                    f"a receipt already exists for {receipt.plan_digest}; receipts "
                    "are create-once")
            self._stage(pending_path, encoded_signature)
            self._stage(signature_path, encoded_signature)
            self._stage(path, data)
            pending_path.unlink()
            self._fsync_root()
        return path

    def _verified_receipt_at(self, path: Path, signature_path: Path,
                             plan_digest: str, label: str
                             ) -> Tuple[bytes, Receipt]:
        """Verify before parsing or trusting receipt bytes.

        Returns the *stored* bytes alongside the parsed receipt, because a
        signature covers exactly those bytes and anything binding itself to
        a receipt — `set_aside`'s `superseded_receipt_sha256` — has to bind
        to what was verified rather than to a re-serialisation of it.
        """
        if not path.is_file():
            raise FileNotFoundError(f"no {label} for {plan_digest}")
        if not signature_path.is_file():
            raise SignatureMissing(
                f"the {label} for {plan_digest} has no signature beside it")
        data = path.read_bytes()
        signature = self._read_signature(signature_path, plan_digest)
        if not self._signature_verifies(data, signature):
            raise SignatureInvalid(
                f"the {label} for {plan_digest} verifies under none of this "
                f"store's {len(self._verify_keys)} public key(s)")
        receipt = Receipt.from_bytes(data)
        if receipt.plan_digest != plan_digest:
            raise ReceiptInvalid(
                f"the {label} stored for {plan_digest} names {receipt.plan_digest}")
        return data, receipt

    def load(self, plan_digest: str) -> Receipt:
        """Verify before parsing or trusting receipt bytes."""
        _require_plan_digest(plan_digest)
        return self._verified_receipt_at(
            self.path_for(plan_digest), self.signature_path_for(plan_digest),
            plan_digest, "receipt")[1]

    # ── the operator escape (§3.6 B10) ──────────────────────────────────

    def _set_aside_paths(self, plan_digest: str,
                         sequence: int) -> Tuple[Path, Path, Path, Path]:
        _require_plan_digest(plan_digest)
        _require_sequence(sequence)
        stem = f"{plan_digest}.set-aside.{sequence:04d}"
        return (self._confined_path(f"{stem}.json"),
                self._confined_path(f"{stem}.json.sig"),
                self._confined_path(f"{stem}.record.json"),
                self._confined_path(f"{stem}.record.json.sig"))

    def _set_aside_sequences(self, plan_digest: str) -> List[int]:
        prefix = f"{plan_digest}.set-aside."
        sequences = set()
        for path in self.root.glob(f"{prefix}*"):
            rest = path.name[len(prefix):]
            seq_part = rest.split(".", 1)[0]
            if _SET_ASIDE_SEQUENCE.fullmatch(seq_part):
                sequences.add(int(seq_part))
        return sorted(sequences)

    def _discard_set_aside_files(self, plan_digest: str, sequence: int) -> bool:
        discarded = False
        for path in self._set_aside_paths(plan_digest, sequence):
            if path.is_file():
                path.unlink()
                discarded = True
        if discarded:
            self._fsync_root()
        return discarded

    def _recover_set_aside_locked(self, plan_digest: str) -> bool:
        """Resolve an interrupted escape so it either fully took effect
        or fully did not.

        The four archive/record files are the commit. A complete, signed,
        bound set whose sha256 still matches the live receipt means unlink
        has not happened yet — finish it. A partial set while the live
        receipt remains is an escape that did not happen — discard the
        staging so retry is legal. An unsigned record is incomplete
        staging, not corruption.
        """
        recovered = False
        live = self.path_for(plan_digest)
        live_sig = self.signature_path_for(plan_digest)
        live_data = live.read_bytes() if live.is_file() else None
        live_sha = (None if live_data is None
                    else hashlib.sha256(live_data).hexdigest())
        for sequence in self._set_aside_sequences(plan_digest):
            archive, archive_sig, record_path, record_sig = (
                self._set_aside_paths(plan_digest, sequence))
            files = (archive, archive_sig, record_path, record_sig)
            present = [path.is_file() for path in files]
            if not any(present):
                continue
            if not all(present):
                if live_sha is None:
                    continue
                if self._discard_set_aside_files(plan_digest, sequence):
                    recovered = True
                continue
            archive_data, _receipt = self._verified_receipt_at(
                archive, archive_sig, plan_digest,
                f"set-aside receipt {sequence:04d}")
            record = self._load_set_aside_record(plan_digest, sequence)
            archive_sha = hashlib.sha256(archive_data).hexdigest()
            if archive_sha != record.superseded_receipt_sha256:
                continue
            if live_sha == archive_sha:
                if live.is_file():
                    live.unlink()
                if live_sig.is_file():
                    live_sig.unlink()
                self._fsync_root()
                live_sha = None
                recovered = True
            elif live_sha is None and live_sig.is_file():
                live_sig.unlink()
                self._fsync_root()
                recovered = True
        return recovered

    def load_set_aside_receipt(self, plan_digest: str, sequence: int) -> Receipt:
        """The receipt a set-aside superseded, still signed, still verified.

        The escape retains it rather than deleting it: `rm` would destroy
        the store's whole audit value, which is that every verdict ever
        reached about a digest is still there to read.

        `SetAsideRecord.superseded_receipt_sha256` binds this archive to
        the record that claims it. Two valid archive/signature pairs for
        the same plan digest cannot be swapped between sequence numbers
        without detection (§6.6, §3.6 B15).
        """
        _require_plan_digest(plan_digest)
        path, signature_path, _, _ = self._set_aside_paths(plan_digest, sequence)
        data, receipt = self._verified_receipt_at(
            path, signature_path, plan_digest,
            f"set-aside receipt {sequence:04d}")
        record = self._load_set_aside_record(plan_digest, sequence)
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.superseded_receipt_sha256:
            raise ReceiptInvalid(
                f"the set-aside receipt {sequence:04d} for {plan_digest} "
                f"does not match the record that claims it")
        return receipt

    def _load_set_aside_record(self, plan_digest: str,
                               sequence: int) -> SetAsideRecord:
        _, _, path, signature_path = self._set_aside_paths(plan_digest, sequence)
        label = f"set-aside record {sequence:04d}"
        if not path.is_file():
            raise FileNotFoundError(f"no {label} for {plan_digest}")
        if not signature_path.is_file():
            raise SignatureMissing(
                f"the {label} for {plan_digest} has no signature beside it")
        data = path.read_bytes()
        signature = self._read_signature(signature_path, plan_digest)
        if not self._signature_verifies(data, signature):
            raise SignatureInvalid(
                f"the {label} for {plan_digest} verifies under none of this "
                f"store's {len(self._verify_keys)} public key(s)")
        record = SetAsideRecord.from_bytes(data)
        if record.plan_digest != plan_digest or record.sequence != sequence:
            raise ReceiptInvalid(
                f"the {label} stored for {plan_digest} names "
                f"{record.plan_digest} sequence {record.sequence}")
        return record

    def set_aside_records(self, plan_digest: str) -> Tuple[SetAsideRecord, ...]:
        """Every escape ever invoked against this digest, oldest first.

        Append-only and permanent. An operator who reaches for the escape
        repeatedly is leaving a growing, signed, attributable trail on the
        digest they are reaching for — which is the whole reason a plan's
        own FAIL does not become cheap to re-roll.

        A staged unsigned record is incomplete publication, not a
        finished escape, so it is skipped rather than raised as
        corruption. `_recover_locked` discards that staging when the live
        receipt is still present.
        """
        _require_plan_digest(plan_digest)
        records = []
        for sequence in self._set_aside_sequences(plan_digest):
            archive, archive_sig, record_path, record_sig = (
                self._set_aside_paths(plan_digest, sequence))
            if not all(path.is_file() for path in (
                    archive, archive_sig, record_path, record_sig)):
                continue
            records.append(self._load_set_aside_record(plan_digest, sequence))
        return tuple(records)



    def set_aside(self, plan_digest: str, *, invoked_by: str, reason: str,
                  clock: Callable[[], float] = time.time) -> SetAsideRecord:
        """§3.6 B10's operator escape: set a FAIL receipt aside.

        The FAILed receipt and its signature are **retained** under an
        archival name and the live slot is freed, so the next `finalize`
        reviews these bytes afresh by exactly the path a
        `FINALIZATION_STALLED` rerun takes — the absence of a receipt.
        Replay is untouched: every digest whose receipt has not been set
        aside still short-circuits on the digest alone, PASS or FAIL.

        What keeps a *plan's* FAIL from becoming cheap, so review does not
        turn into a slot machine (B10):

        * it needs the finalizer's signing key, the same authority that
          mints receipts — a read-only store cannot escape anything;
        * it refuses a PASS, so it can only reopen a FAIL and never
          overturn one;
        * it refuses a blank invoker or a blank reason;
        * every invocation is permanent, signed, and visible in the store
          forever, so re-rolling a reviewer accumulates a public record of
          exactly that; and
        * nothing in Maestro calls it. It is reachable only from the
          operator's verb.

        Deciding whether a given FAIL was the plan's or the machine's is a
        judgment, not a computation, which is why this is an operator act
        that records itself rather than a rule that fires.

        **Crash behaviour.** The four archive/record files are the commit.
        `_recover_locked` finishes an interrupted unlink when that commit
        is complete and bound to the live receipt, and discards a partial
        commit so the live FAIL remains and retry is legal. A staged
        unsigned record is incomplete, not corrupt.
        """
        if self._signing_seed is None:
            raise SigningKeyUnavailable(
                "this store holds no signing key; setting a receipt aside is "
                "the finalizer's act, under the same key that mints receipts "
                "(§6.6)")
        if not self._create:
            raise FileNotFoundError("receipt store is opened read-only")
        _require_plan_digest(plan_digest)
        invoked_by = _require_stated(invoked_by, "invoked_by")
        reason = _require_stated(reason, "reason")
        path = self.path_for(plan_digest)
        signature_path = self.signature_path_for(plan_digest)
        with self._locked(plan_digest):
            self._recover_locked(plan_digest)
            if not path.is_file():
                raise SetAsideRefused(
                    f"no receipt exists for {plan_digest}; there is nothing to "
                    "set aside")
            data, receipt = self._verified_receipt_at(
                path, signature_path, plan_digest, "receipt")
            if receipt.verdict is not Verdict.FAIL:
                raise SetAsideRefused(
                    f"the receipt for {plan_digest} is "
                    f"{receipt.verdict.value}; the escape sets aside a FAIL "
                    "and there is nothing to escape from a PASS")
            signature = self._read_signature(signature_path, plan_digest)
            sequence = _require_sequence(
                len(self.set_aside_records(plan_digest)) + 1)
            created_at_epoch = clock()
            _require_created_at_epoch(created_at_epoch)
            record = SetAsideRecord(
                plan_digest=plan_digest,
                sequence=sequence,
                invoked_by=invoked_by,
                reason=reason,
                superseded_verdict=receipt.verdict,
                superseded_rubric_version=receipt.rubric_version,
                superseded_reviewer=receipt.reviewer,
                superseded_created_at_epoch=receipt.created_at_epoch,
                superseded_receipt_sha256=hashlib.sha256(data).hexdigest(),
                created_at_epoch=created_at_epoch)
            record_bytes = record.to_bytes()
            record_signature = rc.sign(self._signing_seed, record_bytes)
            (archive, archive_signature, record_path,
             record_signature_path) = self._set_aside_paths(plan_digest, sequence)
            self._stage(archive, data)
            self._stage(archive_signature, signature.hex().encode("ascii") + b"\n")
            self._stage(record_path, record_bytes)
            self._stage(record_signature_path,
                        record_signature.hex().encode("ascii") + b"\n")
            path.unlink()
            signature_path.unlink()
            self._fsync_root()
        return record


def _is_inside(candidate: Path, boundary: Path) -> bool:
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True


# ── the verb (§11.1 `maestro plan finalize`) ────────────────────────────────

@dataclass(frozen=True)
class Blocker:
    """§6.4's typed blocker with a JSON pointer into the plan."""

    pointer: str
    code: str
    message: str


class AuthoringBlocked(RuntimeError):
    """`AUTHORING_BLOCKED` — typed blockers, no reviewer, nothing
    published (§6.4, §11.1)."""

    def __init__(self, blockers: Sequence[Blocker]) -> None:
        super().__init__(
            "AUTHORING_BLOCKED: " + "; ".join(
                f"{b.pointer} {b.code}" for b in blockers))
        self.blockers = tuple(blockers)


class FinalizationStalled(RuntimeError):
    """`FINALIZATION_STALLED` (§6.5) — printed with the reviewer's
    recorded (route, model, session id) and the signal that fired.

    Deliberately **not** a receipt: FAIL is terminal for the bytes, and a
    stall is a fact about the machine or the route, never a verdict about
    the plan. No receipt exists for the digest afterwards, so a rerun of
    `plan finalize` is legal and reviews afresh.
    """

    def __init__(self, session: ReviewerSession, signal: FinalizationSignal,
                 elapsed_s: float) -> None:
        super().__init__(
            f"FINALIZATION_STALLED: route={session.route} "
            f"model={session.model} session_id={session.session_id} "
            f"signal={signal.value} after {elapsed_s:.1f}s")
        self.route = session.route
        self.model = session.model
        self.session_id = session.session_id
        self.signal = signal
        self.elapsed_s = elapsed_s


@dataclass(frozen=True)
class FinalizationOutcome:
    verdict: Verdict
    receipt: Receipt
    replayed: bool


class WindowRunner(object):
    """Structural type for what `window_factory` returns: anything with
    `run(sleep)` yielding a `WindowOutcome`. `FinalizationWindow` is the
    one implementation."""


def finalize(
    *,
    plan_digest: str,
    objects: Sequence[ReviewObject],
    rubric: Rubric,
    store: ReceiptStore,
    validate: Callable[[], Sequence[Blocker]],
    window_factory: Callable[[ApplicabilityMatrix], Any],
    occupancy_reader: Callable[[ReviewerSession], Optional[float]],
    occupancy_threshold: float = 0.8,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> FinalizationOutcome:
    """`maestro plan finalize` (§11.1), in the order §6.5 fixes.

    1. **Every deterministic obligation first.** This ordering is the
       asserted, tested invariant: the failure being prevented is a
       reviewer passing thirty nodes and publication then dying on a pure
       function of the authored bytes.
    2. **Replay on the digest alone.** A receipt for this digest
       short-circuits with zero reviewer calls, whatever the route.
    3. Compute the matrix in code, launch exactly one reviewer inside the
       finalization window.
    4. A stalled window raises `FinalizationStalled` and writes no
       receipt.
    5. Verify the report against the matrix — echo, pair count, cell set,
       both canaries, occupancy — all before the receipt exists.
    6. Derive the verdict, stamp severity from the rubric, write the
       create-once signed receipt with the full per-cell matrix.
    """
    blockers = list(validate())
    if blockers:
        raise AuthoringBlocked(blockers)
    store.recover(plan_digest)


    if store.has(plan_digest):
        receipt = store.load(plan_digest)
        return FinalizationOutcome(verdict=receipt.verdict, receipt=receipt,
                                   replayed=True)

    matrix = compute_matrix(rubric, plan_digest, objects)
    window = window_factory(matrix)
    outcome: WindowOutcome = window.run(sleep=sleep)
    if not outcome.completed:
        if outcome.signal is None:
            raise RuntimeError(
                "FINALIZATION_PROTOCOL_ERROR: incomplete window without signal")
        raise FinalizationStalled(outcome.session, outcome.signal,
                                  outcome.elapsed_s)

    report = ReviewerReport.model_validate(outcome.report)
    check_occupancy(occupancy_reader(outcome.session),
                    threshold=occupancy_threshold)
    verify_report(matrix, report)

    derived = derive_verdict(matrix, report, rubric)
    created_at_epoch = clock()
    _require_created_at_epoch(created_at_epoch)
    receipt = Receipt(
        plan_digest=plan_digest,
        rubric_version=rubric.version,
        verdict=derived.verdict,
        cells=derived.cells,
        reviewer=ReviewerIdentity(route=outcome.session.route,
                                  model=outcome.session.model,
                                  session_id=outcome.session.session_id),
        created_at_epoch=created_at_epoch)
    try:
        store.write(receipt)
    except ReceiptExists:
        existing = store.load(plan_digest)
        return FinalizationOutcome(verdict=existing.verdict, receipt=existing,
                                   replayed=True)
    return FinalizationOutcome(verdict=receipt.verdict, receipt=receipt,
                               replayed=False)
