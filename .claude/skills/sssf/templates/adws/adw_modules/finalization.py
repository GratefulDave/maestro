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

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Tuple, Type)

from pydantic import BaseModel, ConfigDict

from . import receipt_crypto as rc
from .finalization_window import (FinalizationSignal, ReviewerSession,
                                  WindowOutcome)

# ── vocabulary ──────────────────────────────────────────────────────────────


class ObjectKind(str, Enum):
    """What a reviewable object is. §6.2's model, from the reviewer's side."""

    PLAN = "plan"
    NODE = "node"
    GATE = "gate"
    EVIDENCE = "evidence"


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
#: settle: whether the graph accomplishes the stated intent, whether a
#: gate's selector is scoped to its own node's work, whether a hypothesis
#: is dischargeable, whether evidence supports the claim made from it.
#: Severity is a property of the question, fixed here in code.
DEFAULT_RUBRIC = Rubric(
    version="maestro-rubric.v1",
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
            check_id="gate.min_cases_is_meaningful",
            question="Is `min_cases` more than a formality for this gate?",
            applies_to=(ObjectKind.GATE,),
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
    seen: List[Type[BaseModel]] = []

    def walk(current: Type[BaseModel]) -> None:
        if current in seen:
            return
        seen.append(current)
        for name, field in current.model_fields.items():
            if name in FORBIDDEN_REPORT_FIELDS:
                found.append(name)
            for nested in _nested_models(field.annotation):
                walk(nested)

    walk(model)
    return found


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
    check_id: str
    object_id: str
    status: CellStatus
    severity: Severity
    message: str = ""
    canary: Optional[CanaryKind] = None


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


@dataclass(frozen=True)
class Receipt:
    plan_digest: str
    rubric_version: str
    verdict: Verdict
    cells: Tuple[DerivedCell, ...]
    reviewer: ReviewerIdentity
    created_at_epoch: float

    def to_bytes(self) -> bytes:
        """The receipt's stored bytes — what the signature covers.

        Canonical (sorted keys, no incidental whitespace) so the same
        receipt always serialises identically and its signature can be
        re-derived rather than only checked.
        """
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
            "cells": [
                {
                    "check_id": cell.check_id,
                    "object_id": cell.object_id,
                    "status": cell.status.value,
                    "severity": cell.severity.value,
                    "message": cell.message,
                    "canary": None if cell.canary is None else cell.canary.value,
                }
                for cell in self.cells
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "Receipt":
        payload = json.loads(data.decode("utf-8"))
        return cls(
            plan_digest=payload["plan_digest"],
            rubric_version=payload["rubric_version"],
            verdict=Verdict(payload["verdict"]),
            cells=tuple(
                DerivedCell(
                    check_id=cell["check_id"],
                    object_id=cell["object_id"],
                    status=CellStatus(cell["status"]),
                    severity=Severity(cell["severity"]),
                    message=cell["message"],
                    canary=(None if cell["canary"] is None
                            else CanaryKind(cell["canary"])))
                for cell in payload["cells"]),
            reviewer=ReviewerIdentity(
                route=payload["reviewer"]["route"],
                model=payload["reviewer"]["model"],
                session_id=payload["reviewer"]["session_id"]),
            created_at_epoch=payload["created_at_epoch"])


class ReceiptStoreLocationError(RuntimeError):
    """§6.6's startup invariant: receipts live outside both the repository
    and the SSSF data directory, and a store violating that refuses to
    run rather than warning."""


class ReceiptExists(RuntimeError):
    """Receipts are create-once. Rewriting one is how a verdict silently
    changes under a digest that has already been published."""


class SignatureMissing(RuntimeError):
    """A receipt with no signature beside it. A hard error, never treated
    as an absent receipt — otherwise deleting signatures forces re-review
    roulette (§6.6)."""


class SignatureInvalid(RuntimeError):
    """A receipt whose signature does not verify under any known key."""


class SigningKeyUnavailable(RuntimeError):
    """This store can verify but cannot sign — the ordinary state
    everywhere but the finalizer's own machine."""


class ReceiptStore:
    """The receipt store: create-once, signed, and located outside the
    repository and the data directory (§6.6).

    `verify_keys` is a list rather than a key: rotation appends a public
    key, and old receipts verify forever under the key that signed them.
    `signing_seed` is optional, which is what makes private-key loss
    survivable — verification needs only public material.
    """

    def __init__(self, root, *, repo_path, data_dir,
                 verify_keys: Sequence[bytes],
                 signing_seed: Optional[bytes] = None) -> None:
        self.root = Path(root).resolve()
        forbidden = (("repository", Path(repo_path).resolve()),
                     ("SSSF data directory", Path(data_dir).resolve()))
        for label, boundary in forbidden:
            if self.root == boundary or _is_inside(self.root, boundary):
                raise ReceiptStoreLocationError(
                    f"the receipt store is inside the {label} ({boundary}); "
                    "§6.6 places it outside both, because a store inside the "
                    "data directory grants every agent unconditional write "
                    "authority over receipts")
        self._verify_keys = tuple(verify_keys)
        self._signing_seed = signing_seed
        self.root.mkdir(parents=True, exist_ok=True)

    # ── layout ──────────────────────────────────────────────────────────

    def path_for(self, plan_digest: str) -> Path:
        return self.root / f"{plan_digest}.json"

    def signature_path_for(self, plan_digest: str) -> Path:
        return Path(str(self.path_for(plan_digest)) + ".sig")

    def has(self, plan_digest: str) -> bool:
        """Whether a receipt exists for this digest. Deliberately does not
        verify: an unverifiable receipt is still a receipt, and `load`
        raises rather than letting it read as absent."""
        return self.path_for(plan_digest).is_file()

    # ── writing and reading ─────────────────────────────────────────────

    def write(self, receipt: Receipt) -> Path:
        if self._signing_seed is None:
            raise SigningKeyUnavailable(
                "this store holds no signing key; the signing key is "
                "finalizer-held (§6.6)")
        path = self.path_for(receipt.plan_digest)
        if path.exists():
            raise ReceiptExists(
                f"a receipt already exists for {receipt.plan_digest}; receipts "
                "are create-once")
        data = receipt.to_bytes()
        signature = rc.sign(self._signing_seed, data)
        path.write_bytes(data)
        self.signature_path_for(receipt.plan_digest).write_text(
            signature.hex() + "\n", encoding="utf-8")
        return path

    def load(self, plan_digest: str) -> Receipt:
        """Verify before trusting (§6.6)."""
        path = self.path_for(plan_digest)
        if not path.is_file():
            raise FileNotFoundError(f"no receipt for {plan_digest}")
        signature_path = self.signature_path_for(plan_digest)
        if not signature_path.is_file():
            raise SignatureMissing(
                f"the receipt for {plan_digest} has no signature beside it")
        data = path.read_bytes()
        signature = bytes.fromhex(signature_path.read_text().strip())
        for key in self._verify_keys:
            if rc.verify(key, data, signature):
                return Receipt.from_bytes(data)
        raise SignatureInvalid(
            f"the receipt for {plan_digest} verifies under none of this "
            f"store's {len(self._verify_keys)} public key(s)")


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

    if store.has(plan_digest):
        receipt = store.load(plan_digest)
        return FinalizationOutcome(verdict=receipt.verdict, receipt=receipt,
                                   replayed=True)

    matrix = compute_matrix(rubric, plan_digest, objects)
    window = window_factory(matrix)
    outcome: WindowOutcome = window.run(sleep=sleep)
    if not outcome.completed:
        raise FinalizationStalled(outcome.session, outcome.signal,
                                  outcome.elapsed_s)

    report = ReviewerReport.model_validate(outcome.report)
    check_occupancy(occupancy_reader(outcome.session),
                    threshold=occupancy_threshold)
    verify_report(matrix, report)

    derived = derive_verdict(matrix, report, rubric)
    receipt = Receipt(
        plan_digest=plan_digest,
        rubric_version=rubric.version,
        verdict=derived.verdict,
        cells=derived.cells,
        reviewer=ReviewerIdentity(route=outcome.session.route,
                                  model=outcome.session.model,
                                  session_id=outcome.session.session_id),
        created_at_epoch=clock())
    store.write(receipt)
    return FinalizationOutcome(verdict=receipt.verdict, receipt=receipt,
                               replayed=False)
