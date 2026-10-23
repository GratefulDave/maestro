"""Attended amendments: an operator agent authors what a human would have.

A lane parks `NO_PROGRESS` when its reviewed rounds stop moving, and a lane
parks `AMENDMENT_REQUIRED` when the final integration review says REVISE and
names it. Clearing either park is not a retry -- a retry would run the same
lane against the same contract and park again in the same place. What clears
it is a plan amendment, and on an earlier run a human wrote two of them by
hand: read the gate table and the latest `CODE_REVIEW` findings, read the
accepted suite, edit one seam contract, validate, mint the receipt, project,
`run amend`. Both converged in one round.

`run attend` is that loop, with an agent holding the pen and a second agent
of a different declared vendor reviewing what it wrote. What it does not
change is what a transition keys on. The author's prose reaches the ledger
only as `AMENDMENT_RATIONALE` records that nothing reads, the reviewer's
verdict only as `ATTEND_REVIEW` records that nothing reads; the lanes move
because `apply_amendment` accepted a `PLAN_AMENDMENT` whose projection
digests changed, exactly as they do when a human runs `run amend`.

The scheduler is allowed to settle first. Every attendable park it left is
answered by one revision, reviewed, signed and applied as one amendment, and
then the scheduler runs again.

The operator agent reads the accepted suite, as the builder and every
reviewer do in their own checkouts; the operator agent is a plan author, and
what it writes is a contract clause stating an expectation the suite asserts,
never a test edit.

A `LANE_FAULT` park is attended too. The author reads the fault beside the
lane's evidence and either answers it with the same kind of revision, or
declares it a runtime defect: a fault in harness code no plan edit can fix.
A declared defect files one deduplicated issue and leaves that lane parked
for the rest of the session -- the scheduler never resumes it again -- while
every other lane keeps running and every other park is still attended. The
declaration moves no lane; it only withholds a resume.

Attend's own agents are contained the same way. An author or reviewer that
cannot be launched, crashes, or times out is retried once; a second failure
holds the batch's lanes as a runtime defect, with the same deduplicated issue,
instead of ending the run. A refusal about the revision is not contained.

A planctl step that failed only because EC2 had no spot capacity for its
mutation pass is not a defect either: nothing is wrong with the harness, the
capacity is just not there yet. It is retried after `CAPACITY_DELAY_SECONDS`,
up to `CAPACITY_RETRIES` times, with the reviewed revision kept; only once
that bound is spent is the batch contained as above.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from . import scheduler_types as st


class AttendRefused(RuntimeError):
    """A typed refusal from the attend loop. `code` is the operator's answer."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


#: `run attend` on a deployment that never opted in.
DISABLED = "ATTEND_DISABLED"
#: A revision reached lanes the parked lanes are not paired with.
TOO_WIDE = "ATTEND_AMENDMENT_TOO_WIDE"
#: The operator agent returned no usable revision.
NO_REVISION = "ATTEND_OPERATOR_NO_REVISION"
#: The operator agent refused, crashed, or timed out.
OPERATOR_FAILED = "ATTEND_OPERATOR_FAILED"
#: The reviewer agent refused, crashed, or timed out.
REVIEWER_FAILED = "ATTEND_REVIEWER_FAILED"
#: The reviewer agent's verdict or findings were not the typed shape.
REVIEW_INVALID = "ATTEND_REVIEW_INVALID"
#: No reviewer identity is configured, so nothing can sign a receipt.
REVIEWER_UNCONFIGURED = "ATTEND_REVIEWER_UNCONFIGURED"
#: The revision did not validate, project, or compile.
REVISION_REFUSED = "ATTEND_REVISION_REFUSED"
#: No `planctl` was resolvable, so no revision can be approved.
PLANCTL_UNRESOLVED = "ATTEND_PLANCTL_UNRESOLVED"
#: `planctl` itself failed -- it could not start, was killed, crashed, or its
#: remote mutation runner could not run -- so it said nothing about the
#: revision. Decided by planctl's exit status, never by its output.
PLANCTL_FAILED = "ATTEND_PLANCTL_FAILED"
#: The words planctl's EC2 launcher ends on when no spot type in any zone had
#: capacity. A `PLANCTL_FAILED` carrying them is waited out, not contained.
NO_SPOT_CAPACITY = "no spot capacity"
#: How many times one planctl step waits out a capacity failure.
CAPACITY_RETRIES = 3
#: How long attend waits before retrying a capacity failure.
CAPACITY_DELAY_SECONDS = 300.0
#: No reviewer key was resolvable, so no receipt can be minted.
KEY_UNRESOLVED = "ATTEND_REVIEWER_KEY_UNRESOLVED"
#: The Plan IR the live revision was projected from could not be located.
PLAN_IR_UNRESOLVED = "ATTEND_PLAN_IR_UNRESOLVED"
#: `attend.plan_ir` names bytes other than the IR the revision's own plan
#: was projected from (the receipt's `ir_sha256`). Config never substitutes.
PLAN_IR_MISMATCH = "ATTEND_PLAN_IR_MISMATCH"

#: The directory attend keeps its planctl roots in is one planctl refuses to
#: upload (it is, contains, or sits in the home directory or `~/.maestro`) or
#: holds key material. Nothing attend can move fixes a configured path.
PLANROOT_UNSAFE = "ATTEND_PLANROOT_UNSAFE"

#: The operator agent's runtime-defect declaration was not the typed shape.
DEFECT_INVALID = "ATTEND_RUNTIME_DEFECT_INVALID"

#: Why an attend session stopped. Recorded on the STOP `ATTEND_SESSION`.
STOP_RUN_SETTLED = "RUN_SETTLED"
STOP_WAIT_NOT_ATTENDABLE = "WAIT_NOT_ATTENDABLE"
STOP_LANE_CAP = "LANE_CAP_REACHED"
STOP_RUN_CAP = "RUN_CAP_REACHED"
STOP_REVIEW_UNRESOLVED = "REVIEW_UNRESOLVED"

#: The wait reasons the loop attends. `PAUSE` is a human decision and is
#: never attended. A `LANE_FAULT` is attended: the author either amends the
#: plan (an oversized or mis-specified lane) or declares a runtime defect.
ATTENDABLE_REASONS: Tuple[str, ...] = (
    st.WaitReason.NO_PROGRESS.value,
    st.WaitReason.AMENDMENT_REQUIRED.value,
    st.WaitReason.LANE_FAULT.value,
)

#: Issue filing outcomes, as `file_issue` reports them.
ISSUE_CREATED = "CREATED"
ISSUE_EXISTS = "EXISTS"
ISSUE_FAILED = "FAILED"


@dataclass(frozen=True)
class AttendPolicy:
    """The deployment's opt-in. Absent `attend` means every field is off.

    `reviewer_id` and `reviewer_vendor` are the reviewer agent's identity as
    the deployment declared and route admission bound it. They are what
    `planctl review` signs with; an empty one refuses rather than signing as
    somebody nobody declared.
    """

    max_amendments_per_lane: int = 0
    max_amendments_per_run: int = 10
    max_review_rounds: int = 3
    route: Mapping[str, str] = field(default_factory=dict)
    reviewer_route: Mapping[str, str] = field(default_factory=dict)
    planctl: Optional[Path] = None
    plan_ir: Optional[Path] = None
    planctl_root_dir: Optional[Path] = None
    validate_argv: Tuple[str, ...] = ()
    reviewer_id: str = ""
    reviewer_vendor: str = ""

    @property
    def enabled(self) -> bool:
        return self.max_amendments_per_lane > 0


@dataclass(frozen=True)
class LaneEvidence:
    """What one parked lane contributes to a batch, read off the ledger.

    `suite_files` is the accepted suite, the same bytes the builder and every
    reviewer have in their checkouts, because the contract gap that parks a
    lane is usually only visible by comparing what the suite asserts with what
    the contract says. `final_review` is the final integration review a
    `AMENDMENT_REQUIRED` park answers, empty for a `NO_PROGRESS` park.
    """

    lane_id: str
    wait_reason: str
    stage: str
    round_number: int
    public_contract: Mapping[str, Any]
    reviews: Tuple[Mapping[str, Any], ...]
    failure_output: Tuple[str, ...]
    lane_gates: str
    suite_files: Mapping[str, str]
    final_review: Mapping[str, Any] = field(default_factory=dict)
    #: The `USER_WAIT` fault (`exception_type`, `message`) of a `LANE_FAULT`
    #: park, empty for every other reason.
    fault: Mapping[str, Any] = field(default_factory=dict)
    #: The integration head the scheduler builds this lane on, and the
    #: declared outputs of every lane it builds on (`merged_source_lane_ids`)
    #: as that head holds them: the same bytes the builder's checkout has.
    #: Without them the author cannot name a merged callable the review
    #: demands, and the reviewer cannot check that it did.
    integration_head: str = ""
    merged_source: Mapping[str, str] = field(default_factory=dict)

    @property
    def merged_source_sha256(self) -> str:
        """Binds the head and every merged byte; a changed head changes it."""
        return merged_source_digest(self.integration_head, self.merged_source)


@dataclass(frozen=True)
class OperatorRequest:
    """Everything the operator agent is given for one batch, and nothing else.

    `reviewer_findings` is empty on the first round and carries the located
    findings of the reviewer's REVISE on every later one, with
    `previous_revision_path` naming the revision they were written against.
    """

    run_id: str
    plan_revision: int
    next_plan_revision: int
    lanes: Tuple[LaneEvidence, ...]
    ir_path: str
    revision_out_path: str
    amendment_rules: str
    allowed_lane_ids: Tuple[str, ...]
    review_round: int = 1
    reviewer_findings: Tuple[Mapping[str, Any], ...] = ()
    previous_revision_path: str = ""

    @property
    def lane_id(self) -> str:
        """The batch's anchor lane: where the operator's tree is bound."""
        return self.lanes[0].lane_id

    @property
    def stage(self) -> str:
        return self.lanes[0].stage


@dataclass(frozen=True)
class ReviewRequest:
    """Everything the reviewer agent is given for one review round.

    The reviewer reads the revision against the IR it revises, the planctl
    question surface of the revision, the author's rationale, and the same
    lane evidence the author read. It writes its answer to the independent
    review question at `findings_out_path`, and returns a typed verdict.
    """

    run_id: str
    plan_revision: int
    next_plan_revision: int
    review_round: int
    lanes: Tuple[LaneEvidence, ...]
    current_ir_path: str
    revision_ir_path: str
    question_surface: str
    rationales: Tuple[Mapping[str, Any], ...]
    findings_out_path: str
    amendment_rules: str
    allowed_lane_ids: Tuple[str, ...]
    #: sha256 of the revision bytes under review. The reviewer is handed
    #: exactly these bytes, and only these bytes are ever projected.
    revision_sha256: str = ""

    @property
    def lane_id(self) -> str:
        return self.lanes[0].lane_id

    @property
    def stage(self) -> str:
        return self.lanes[0].stage


RATIONALE_KEYS = (
    "lane",
    "round",
    "failing_cases_summary",
    "contract_gap",
    "edit_path",
    "edit_text",
)

#: Every key a reviewer's located finding carries, each a nonempty string.
#: `location` is a JSON pointer into the revision IR.
REVIEW_FINDING_KEYS = ("location", "defect", "required_change")


@dataclass(frozen=True)
class AppliedAmendment:
    lane_id: str
    plan_revision: int
    amendment_artifact_id: str
    rationale_artifact_id: str


@dataclass(frozen=True)
class RuntimeDefect:
    """A `LANE_FAULT` the author declared a harness defect, and its issue."""

    lane_id: str
    fault: Mapping[str, Any]
    detail: str
    issue: Mapping[str, str]


@dataclass(frozen=True)
class AttendOutcome:
    status: st.RunStatus
    stop_reason: str
    applied: Tuple[AppliedAmendment, ...]
    compiled: st.CompiledPlan
    #: `(lane_id, wait_reason)` for every lane still `WAITING_FOR_USER` when
    #: the loop stopped, attendable or not.
    parks: Tuple[Tuple[str, str], ...] = ()
    escalated: Tuple[RuntimeDefect, ...] = ()


@dataclass(frozen=True)
class MaterialChange:
    """The classifier's answer for one amendment. Computed, never read off prose."""

    material: bool
    hits: Tuple[str, ...]
    obligations_touched: Tuple[str, ...]


def waiting_lanes(store: Any, run_id: str) -> Tuple[Tuple[str, str], ...]:
    """`(lane_id, wait_reason)` for every `WAITING_FOR_USER` lane.

    Reads `lane_state.stage` and the lane's own latest `USER_WAIT`, which is
    the pair §5 makes authoritative.
    """
    waiting = []
    for lane in store.active_projection(run_id):
        if store.lane_stage(run_id, lane.lane_id) is not st.LaneStage.WAITING_FOR_USER:
            continue
        wait = store.latest_lane_artifact_payload(
            run_id, lane.lane_id, st.ArtifactKind.USER_WAIT
        )
        if wait is None:
            continue
        waiting.append((lane.lane_id, str(wait.get("wait_reason"))))
    return tuple(waiting)


def parked_lane_ids(
    store: Any, run_id: str, held: Sequence[str] = ()
) -> Tuple[Tuple[str, str], ...]:
    """`(lane_id, wait_reason)` for every attendable park, in projection order.

    A lane waiting on `PAUSE` is not attendable and is deliberately left for
    the operator, and so is a lane in `held`: one this session already
    escalated as a runtime defect.
    """
    return tuple(
        (lane_id, reason)
        for lane_id, reason in waiting_lanes(store, run_id)
        if reason in ATTENDABLE_REASONS and lane_id not in held
    )


def paired_lane_ids(
    lanes: Sequence[st.LaneProjection], lane_id: str
) -> Tuple[str, ...]:
    """The parked lane plus the tests/build lane it is paired with.

    A typed build lane has exactly one direct `tests` dependency, and that
    tests lane's canonical spec already embeds the build lane's claims. So an
    edit that reaches one of the pair re-digests the other whichever end it is
    made at, and admitting both is admitting one lane's worth of change -- not
    a wider blast radius. Anything beyond the pair is a different lane's work
    and is refused.
    """
    by_id = {lane.lane_id: lane for lane in lanes}
    target = by_id.get(lane_id)
    if target is None:
        return (lane_id,)
    allowed = {lane_id}
    for need in target.needs:
        peer = by_id.get(need)
        if peer is not None and peer.lane_kind == st.LANE_KIND_TESTS:
            allowed.add(need)
    if target.lane_kind == st.LANE_KIND_TESTS:
        for lane in lanes:
            if lane.lane_kind == st.LANE_KIND_BUILD and lane_id in lane.needs:
                allowed.add(lane.lane_id)
    return tuple(sorted(allowed))


def merged_source_lane_ids(
    lanes: Sequence[st.LaneProjection], lane_id: str
) -> Tuple[str, ...]:
    """Every lane the parked lane and its pair build on, transitively.

    Their declared outputs are on the integration head before either end of
    the pair starts, so they are what the builder's checkout already holds
    beside the suite. The pair itself is excluded: the tests end's outputs
    are the accepted suite, already in `suite_files`.
    """
    by_id = {lane.lane_id: lane for lane in lanes}
    pair = set(paired_lane_ids(lanes, lane_id))
    seen: set = set()
    pending = [need for member in pair if member in by_id for need in by_id[member].needs]
    while pending:
        need = pending.pop()
        if need in seen or need in pair or need not in by_id:
            continue
        seen.add(need)
        pending.extend(by_id[need].needs)
    return tuple(sorted(seen))


def merged_source_digest(head: str, files: Mapping[str, str]) -> str:
    """sha256 over the integration head and every merged file, path-ordered."""
    canonical = json.dumps(
        {"integration_head": head, "files": sorted(files.items())},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256_hex(canonical.encode("utf-8"))


def batch_scope(
    lanes: Sequence[st.LaneProjection], parked: Sequence[Tuple[str, str]]
) -> Tuple[str, ...]:
    """The union of every parked lane's scope in one batch.

    A `NO_PROGRESS` lane's scope is itself and its pair. An
    `AMENDMENT_REQUIRED` lane is parked only because the final review named
    it, so the named lanes are exactly the parked ones; each carries its pair
    for the same reason a `NO_PROGRESS` lane does. `apply_amendment` still
    refuses `AMENDMENT_DOES_NOT_ADDRESS_REVIEW` unless every named lane's
    spec digest moved.
    """
    allowed: set = set()
    for lane_id, _reason in parked:
        allowed.update(paired_lane_ids(lanes, lane_id))
    return tuple(sorted(allowed))


def changed_lane_ids(
    before: Sequence[st.LaneProjection], after: Sequence[st.LaneProjection]
) -> Tuple[str, ...]:
    """Lanes whose projection digest moved, plus lanes added or removed.

    Same definition §5 uses for a changed lane, read off the two projections
    rather than off the amendment: a lane that appears or disappears counts as
    changed, because either is a topology edit an attended amendment must never
    make.
    """
    old = {lane.lane_id: lane.lane_projection_digest for lane in before}
    new = {lane.lane_id: lane.lane_projection_digest for lane in after}
    moved = {
        lane_id
        for lane_id in set(old) | set(new)
        if old.get(lane_id) != new.get(lane_id)
    }
    return tuple(sorted(moved))


def require_amendment_scope(
    before: Sequence[st.LaneProjection],
    after: Sequence[st.LaneProjection],
    allowed: Sequence[str],
) -> Tuple[str, ...]:
    """Refuse a revision that reached past the parked lanes and their pairs.

    A revision that changes nothing is refused for the same reason
    `AMENDMENT_DOES_NOT_ADDRESS_REVIEW` exists: an amendment that moves no
    projection digest resets no lane, so the run would park again immediately
    on the same contract and burn the lane's whole budget doing it.
    """
    permitted = frozenset(allowed)
    changed = changed_lane_ids(before, after)
    if not changed:
        raise AttendRefused(TOO_WIDE, "revision changes no lane projection")
    extra = sorted(set(changed) - permitted)
    if extra:
        raise AttendRefused(
            TOO_WIDE,
            "revision also changes {0}; permitted {1}".format(
                ", ".join(extra), ", ".join(sorted(permitted))
            ),
        )
    return changed


def require_rationale(payload: Any) -> Mapping[str, Any]:
    """One lane's rationale the operator agent owes, or a typed refusal.

    Every key is required and every value must be a nonempty string. A
    rationale with a blank `contract_gap` is the shape a model returns when it
    could not name the gap, and recording it would leave the next reader a row
    that looks like evidence and carries none.
    """
    if not isinstance(payload, Mapping):
        raise AttendRefused(NO_REVISION, "rationale is not an object")
    missing = [key for key in RATIONALE_KEYS if key not in payload]
    if missing:
        raise AttendRefused(
            NO_REVISION, "rationale is missing " + ", ".join(missing)
        )
    blank = [
        key
        for key in RATIONALE_KEYS
        if key != "round"
        and not (isinstance(payload[key], str) and payload[key].strip())
    ]
    if blank:
        raise AttendRefused(NO_REVISION, "rationale is blank at " + ", ".join(blank))
    return {key: payload[key] for key in RATIONALE_KEYS}


def require_rationales(
    payload: Any, lane_ids: Sequence[str]
) -> Tuple[Mapping[str, Any], ...]:
    """Exactly one valid rationale per parked lane, in `lane_ids` order."""
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise AttendRefused(NO_REVISION, "rationales is not a list")
    by_lane: dict = {}
    for item in payload:
        rationale = require_rationale(item)
        lane = rationale["lane"].strip()
        if lane in by_lane:
            raise AttendRefused(NO_REVISION, "two rationales for " + lane)
        by_lane[lane] = rationale
    missing = [lane for lane in lane_ids if lane not in by_lane]
    if missing:
        raise AttendRefused(
            NO_REVISION, "no rationale for parked lane " + ", ".join(missing)
        )
    extra = sorted(set(by_lane) - set(lane_ids))
    if extra:
        raise AttendRefused(
            NO_REVISION, "rationale for a lane not parked: " + ", ".join(extra)
        )
    return tuple(by_lane[lane] for lane in lane_ids)


def require_runtime_defects(
    payload: Any, parked: Sequence[Tuple[str, str]]
) -> Tuple[Tuple[str, str], ...]:
    """`(lane_id, detail)` for each runtime defect the operator declared.

    Absent means none. Only a parked `LANE_FAULT` lane can be declared one:
    a `NO_PROGRESS` or `AMENDMENT_REQUIRED` park is a contract gap by
    definition, and letting the author call it a harness defect would be a
    way to stop answering it. An entry naming no lane in a one-lane batch
    names that lane: there is no other it could mean.
    """
    if payload is None:
        return ()
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise AttendRefused(DEFECT_INVALID, "runtime_defects is not a list")
    faulted = {
        lane_id
        for lane_id, reason in parked
        if reason == st.WaitReason.LANE_FAULT.value
    }
    declared: dict = {}
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise AttendRefused(
                DEFECT_INVALID, "runtime_defects[{0}] is not an object".format(index)
            )
        lane = item.get("lane")
        detail = item.get("detail")
        if len(parked) == 1 and (lane is None or (isinstance(lane, str) and not lane.strip())):
            lane = parked[0][0]
        if not (isinstance(lane, str) and lane.strip()):
            raise AttendRefused(
                DEFECT_INVALID, "runtime_defects[{0}] names no lane".format(index)
            )
        if not (isinstance(detail, str) and detail.strip()):
            raise AttendRefused(
                DEFECT_INVALID, "runtime_defects[{0}] has a blank detail".format(index)
            )
        lane = lane.strip()
        if lane not in faulted:
            raise AttendRefused(
                DEFECT_INVALID,
                "{0} is not a parked LANE_FAULT lane".format(lane),
            )
        if lane in declared:
            raise AttendRefused(DEFECT_INVALID, "two runtime defects for " + lane)
        declared[lane] = detail.strip()
    return tuple(
        (lane_id, declared[lane_id])
        for lane_id, _reason in parked
        if lane_id in declared
    )


def require_review_verdict(
    payload: Any,
) -> Tuple[str, Tuple[Mapping[str, str], ...]]:
    """The reviewer's typed verdict and located findings, or a typed refusal.

    PASS carries no finding. REVISE carries at least one, and every finding
    names its `location` in the revision IR, the `defect`, and the
    `required_change`: a REVISE the author cannot act on is the same
    unbounded loop B8 exists to forbid.
    """
    if not isinstance(payload, Mapping):
        raise AttendRefused(REVIEW_INVALID, "review envelope is not an object")
    verdict = payload.get("verdict")
    if verdict not in (st.ReviewerVerdict.PASS.value, st.ReviewerVerdict.REVISE.value):
        raise AttendRefused(REVIEW_INVALID, "verdict is {0!r}".format(verdict))
    raw = payload.get("findings")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise AttendRefused(REVIEW_INVALID, "findings is not a list")
    located = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise AttendRefused(
                REVIEW_INVALID, "findings[{0}] is not an object".format(index)
            )
        blank = [
            key
            for key in REVIEW_FINDING_KEYS
            if not (isinstance(item.get(key), str) and item[key].strip())
        ]
        if blank:
            raise AttendRefused(
                REVIEW_INVALID,
                "findings[{0}] is not located: blank {1}".format(
                    index, ", ".join(blank)
                ),
            )
        located.append({key: item[key] for key in REVIEW_FINDING_KEYS})
    if verdict == st.ReviewerVerdict.PASS.value and located:
        raise AttendRefused(REVIEW_INVALID, "PASS requires findings=[]")
    if verdict == st.ReviewerVerdict.REVISE.value and not located:
        raise AttendRefused(REVIEW_INVALID, "REVISE requires a located finding")
    return str(verdict), tuple(located)


def require_review_findings(path: Path) -> list:
    """The reviewer's answer to the independent review question, as a list.

    This is what `planctl review --findings` signs over. A missing or
    malformed file is the reviewer's failure to answer, never an empty answer.
    """
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AttendRefused(
            REVIEW_INVALID, "review findings {0}: {1}".format(path, exc)
        ) from exc
    findings = loaded.get("findings") if isinstance(loaded, Mapping) else None
    if not isinstance(findings, list):
        raise AttendRefused(
            REVIEW_INVALID, "review findings {0} carry no findings list".format(path)
        )
    return findings


#: The independent-review question the reviewer answers for the author's
#: revision. `planctl review --findings` refuses to sign while a
#: two_implementations finding's claim has no `decided_by` example whose input
#: is the finding's `divergent_input`, or while any interface finding stands
#: unresolved.
TWO_IMPLEMENTATIONS_QUESTION = (
    "For every claim a tests lane discharges in the revision, describe two "
    "implementations that satisfy every decided_by example and the "
    "observation_seam yet produce different observable results; for a claim whose "
    "witness store is external (it reads an upstream endpoint), include pairs that "
    "differ on the endpoint read, the response fields relied on, or the behavior "
    "when it is unavailable or returns a different release. Then, once for every "
    "tests lane, answer whether every obligation's cases can be written from the "
    "declared interface without inventing any name, field, type, string, status "
    "or error body, and whether the tests and build sides use one identical "
    "signature. Write {\"findings\": [...]} to findings_out_path, one "
    "finding per pair with exactly kind \"two_implementations\", finding_id, "
    "claim_id, implementation_a, implementation_b, divergent_input, outcome_a, "
    "outcome_b (each {\"expect\": <exact output>} or {\"refuses\": {\"error\": "
    "..., \"message\": ...}}, and different) and observable_difference, or one "
    "finding per interface defect with exactly kind \"interface\", finding_id, "
    "claim_id, lane_id (the build lane owning the declaration) and defect. "
    "Write {\"findings\": []} when there is no such pair or defect. A pair whose "
    "claim has no decided_by example at divergent_input, or an interface defect "
    "the revision leaves unrepaired, is a defect the author must fix: return "
    "REVISE with a located finding for it. Ask this once."
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reviewed_bytes(path: Path, expected_sha256: str) -> bytes:
    """The revision bytes the reviewer judged, or a typed refusal.

    The author's revision sits in the author's tree, which the author can
    still write to. A revision is projected only if its bytes are the ones
    the reviewer was handed; anything else is a revision nobody reviewed.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise AttendRefused(
            REVISION_REFUSED, "reviewed revision {0}: {1}".format(path, exc)
        ) from exc
    if not expected_sha256 or sha256_hex(data) != expected_sha256:
        raise AttendRefused(
            REVISION_REFUSED,
            "revision {0} is not the bytes that were reviewed (sha256 {1})".format(
                path, expected_sha256 or "unrecorded"
            ),
        )
    return data


def revision_path(envelope: Any, expected: Path) -> Path:
    """The IR the operator agent says it wrote, proved to be the one asked for."""
    if not isinstance(envelope, Mapping):
        raise AttendRefused(NO_REVISION, "envelope is not an object")
    raw = envelope.get("revision_path")
    if not isinstance(raw, str) or not raw.strip():
        raise AttendRefused(NO_REVISION, "envelope names no revision_path")
    written = Path(raw)
    if written.resolve() != Path(expected).resolve():
        raise AttendRefused(
            NO_REVISION,
            "revision_path {0} is not the requested {1}".format(written, expected),
        )
    if not written.is_file():
        raise AttendRefused(NO_REVISION, "revision file was not written")
    return written


@dataclass(frozen=True)
class OperatorAnswer:
    """What one operator envelope says, once every shape check has held.

    `held` are declared runtime defects on lanes the revision does not
    answer: their issues are filed and the lanes held. `reported` are
    declared defects on lanes the revision does answer: their issues are
    filed, and the revision still moves the lane. `ignored` is the refusal of
    a malformed `runtime_defects` that a complete revision outranked.
    """

    revision: Optional[Path]
    rationales: Tuple[Mapping[str, Any], ...]
    lane_ids: Tuple[str, ...]
    held: Tuple[Tuple[str, str], ...] = ()
    reported: Tuple[Tuple[str, str], ...] = ()
    ignored: str = ""


def read_operator_envelope(
    envelope: Any, request: OperatorRequest, *, strict: bool
) -> OperatorAnswer:
    """Every envelope-shape check on an operator's answer, in one place.

    The harness runs it `strict` before the operator's pane closes, and asks
    the same session once more with the exact refusal; the attend loop runs it
    again, not `strict`, on whatever came back. A revision answering every
    parked lane is never discarded over its `runtime_defects`: a malformed
    list is refused only when `strict`, and a well-formed one only files
    issues for lanes the revision already answers.
    """
    if not isinstance(envelope, Mapping):
        raise AttendRefused(NO_REVISION, "envelope is not an object")
    parked = tuple((lane.lane_id, lane.wait_reason) for lane in request.lanes)
    lane_ids = tuple(lane_id for lane_id, _reason in parked)
    expected = Path(request.revision_out_path)
    defect_error: Optional[AttendRefused] = None
    try:
        defects = require_runtime_defects(envelope.get("runtime_defects"), parked)
    except AttendRefused as exc:
        defects, defect_error = (), exc
    try:
        written: Optional[Path] = revision_path(envelope, expected)
        rationales = require_rationales(envelope.get("rationales"), lane_ids)
    except AttendRefused:
        written, rationales = None, ()
    if written is not None:
        if defect_error is not None and strict:
            raise defect_error
        return OperatorAnswer(
            written,
            rationales,
            lane_ids,
            reported=defects,
            ignored=defect_error.detail if defect_error is not None else "",
        )
    if defect_error is not None:
        raise defect_error
    declared = {lane_id for lane_id, _detail in defects}
    remaining = tuple(lane_id for lane_id in lane_ids if lane_id not in declared)
    if not remaining:
        return OperatorAnswer(None, (), (), held=defects)
    return OperatorAnswer(
        revision_path(envelope, expected),
        require_rationales(envelope.get("rationales"), remaining),
        remaining,
        held=defects,
    )


def check_review_answer(answer: Any, request: ReviewRequest) -> None:
    """Every shape check on an attend reviewer's answer, as the loop runs them.

    The verdict and located findings, and on PASS the findings file
    `planctl review` signs over. The harness runs it before the reviewer's
    pane closes and asks the same session once more with the exact refusal.
    """
    verdict, _located = require_review_verdict(answer)
    if verdict == st.ReviewerVerdict.PASS.value:
        require_review_findings(Path(request.findings_out_path))


def _plan_lanes(compiled: st.CompiledPlan) -> dict:
    document = json.loads(bytes(compiled.plan_bytes).decode("utf-8"))
    return {str(lane["id"]): lane for lane in document.get("lanes") or ()}


def _obligation_key(criterion: Mapping[str, Any]) -> str:
    """The claim a gating criterion discharges: its text up to ` (`.

    The ingress renders a claim as `<claim_id> (<polarity>): ...`, so an edit
    to the sentence keeps the key and a removed claim loses it. A criterion
    not in that shape is keyed by its whole text, which reads an edit as a
    removal -- the conservative direction for a narrowing check.
    """
    text = str(criterion.get("criterion") or "")
    head, sep, _rest = text.partition(" (")
    return head if sep and head else text


def _gating(lane: Mapping[str, Any]) -> dict:
    gating = {}
    for item in lane.get("acceptance") or ():
        if isinstance(item, Mapping) and item.get("gating") is True:
            gating[_obligation_key(item)] = item
    return gating


def _examples(criterion: Mapping[str, Any]) -> set:
    return {
        json.dumps(example, sort_keys=True, separators=(",", ":"))
        for example in criterion.get("decided_by") or ()
    }


def classify_material(
    before: st.CompiledPlan,
    after: st.CompiledPlan,
    resets: Sequence[Mapping[str, Any]] = (),
) -> MaterialChange:
    """Is an applied amendment material? Read off the two compiled plans.

    Material when any of: a lane's declared outputs, `needs` or `lane_kind`
    changed; a build lane's `spec.interface` changed; a gating obligation was
    removed or lost a `decided_by` example (a narrowing, which rule 3 calls a
    contract change); or the `PLAN_AMENDMENT`'s own `resets` moved a lane off
    `MERGED`. A change to spec text alone is not material.
    """
    old = _plan_lanes(before)
    new = _plan_lanes(after)
    hits: list = []
    touched: list = []
    for lane_id in sorted(set(old) & set(new)):
        prior, revised = old[lane_id], new[lane_id]
        for key, label in (
            ("outputs", "declared outputs"),
            ("needs", "needs"),
            ("lane_kind", "lane_kind"),
        ):
            if prior.get(key) != revised.get(key):
                hits.append("{0}: {1} changed".format(lane_id, label))
        kinds = {prior.get("lane_kind"), revised.get("lane_kind")}
        if st.LANE_KIND_BUILD in kinds:
            if (prior.get("spec") or {}).get("interface") != (
                revised.get("spec") or {}
            ).get("interface"):
                hits.append("{0}: spec.interface changed".format(lane_id))
        old_gating, new_gating = _gating(prior), _gating(revised)
        for key in sorted(set(old_gating) | set(new_gating)):
            if old_gating.get(key) != new_gating.get(key):
                touched.append("{0}: {1}".format(lane_id, key))
            if key not in new_gating:
                hits.append(
                    "{0}: gating obligation {1} removed".format(lane_id, key)
                )
            elif key in old_gating:
                lost = _examples(old_gating[key]) - _examples(new_gating[key])
                if lost:
                    hits.append(
                        "{0}: gating obligation {1} lost {2} decided_by "
                        "example(s)".format(lane_id, key, len(lost))
                    )
    for lane_id in sorted(set(old) ^ set(new)):
        hits.append("{0}: lane added or removed".format(lane_id))
    for reset in resets:
        if (
            reset.get("from_stage") == st.LaneStage.MERGED.value
            and reset.get("to_stage") != st.LaneStage.MERGED.value
        ):
            hits.append(
                "{0}: MERGED lane reset to {1}".format(
                    reset.get("lane_id"), reset.get("to_stage")
                )
            )
    return MaterialChange(bool(hits), tuple(hits), tuple(touched))


def issue_title(run_id: str, plan_revision: int) -> str:
    return "Attended amendment r{0} on run {1}: material change".format(
        plan_revision, run_id
    )


def defect_issue_title(run_id: str, lane_id: str, fault: Mapping[str, Any]) -> str:
    return "Runtime defect on run {0}: {1} {2}".format(
        run_id, lane_id, fault.get("exception_type") or "LANE_FAULT"
    )


def defect_issue_body(
    *,
    run_id: str,
    lane_id: str,
    fault: Mapping[str, Any],
    detail: str,
    declared: bool = True,
) -> str:
    if declared:
        opening = (
            "Run `{0}` parked `{1}` with `LANE_FAULT`, and the attend operator "
            "declared it a runtime defect rather than a plan gap."
        )
        heading = "## Operator's diagnosis"
    else:
        opening = "Run `{0}` could not attend `{1}`: attend itself failed twice."
        heading = "## Attend failure"
    return "\n".join(
        [
            opening.format(run_id, lane_id)
            + " The lane stays parked; every other lane keeps running.",
            "",
            "## Fault",
            "- exception_type: `{0}`".format(fault.get("exception_type", "")),
            "- message: {0}".format(fault.get("message", "")),
            "",
            heading,
            detail,
        ]
    ) + "\n"


def issue_body(
    *,
    run_id: str,
    plan_revision: int,
    material: MaterialChange,
    changed: Sequence[str],
    rationales: Sequence[Mapping[str, Any]],
    verdict: str,
    reviewer_id: str,
    reviewer_vendor: str,
    deviations_path: str,
) -> str:
    lines = [
        "Run `{0}` applied attended amendment r{1}, and the classifier over "
        "the two compiled plans found it material.".format(run_id, plan_revision),
        "",
        "## Classifier hits",
        *("- {0}".format(hit) for hit in material.hits),
        "",
        "## Changed lanes",
        *("- {0}".format(lane) for lane in changed),
        "",
        "## Rationale",
    ]
    for rationale in rationales:
        lines.append(
            "- {0}: {1} (edit at {2})".format(
                rationale.get("lane"),
                rationale.get("contract_gap"),
                rationale.get("edit_path"),
            )
        )
    lines += [
        "",
        "## Review",
        "- verdict {0} by {1} ({2})".format(verdict, reviewer_id, reviewer_vendor),
        "",
        "Deviations file: `{0}`".format(deviations_path),
    ]
    return "\n".join(lines) + "\n"


def _payload_of(store: Any, run_id: str, kind: st.ArtifactKind, artifact_id: str) -> Mapping[str, Any]:
    for row in store.run_artifacts_of_kind(run_id, kind):
        if row["artifact_id"] == artifact_id:
            return row["payload"]
    return {}


def render_deviations(store: Any, run_id: str) -> str:
    """`<run_id>.deviations.md`, rendered from ledger records and nothing else.

    One section per attended revision: the parked lanes and why they waited,
    the author's rationale, the changed lanes, the obligations touched, every
    review round and its verdict, whether the classifier found it material,
    and the issue it filed. A review session that never reached an amendment
    gets its own section, so an unresolved review is visible too. The
    rendering is a pure function of the rows, so re-rendering is
    byte-identical and the file is never authority.
    """
    reviews: dict = {}
    for row in store.run_artifacts_of_kind(run_id, st.ArtifactKind.ATTEND_REVIEW):
        payload = row["payload"]
        key = (str(payload.get("attend_session_id")), int(payload.get("plan_revision") or 0))
        reviews.setdefault(key, []).append(payload)
    revisions: dict = {}
    for row in store.run_artifacts_of_kind(run_id, st.ArtifactKind.AMENDMENT_RATIONALE):
        payload = row["payload"]
        key = (str(payload.get("attend_session_id")), int(payload.get("plan_revision") or 0))
        revisions.setdefault(key, []).append(payload)
    lines = ["# Attended amendments on run {0}".format(run_id), ""]
    lines.append(
        "Rendered from the ledger's `PLAN_AMENDMENT`, `AMENDMENT_RATIONALE` and "
        "`ATTEND_REVIEW` records. A record, never authority."
    )
    for key in sorted(set(reviews) | set(revisions), key=lambda item: (item[1], item[0])):
        session_id, plan_revision = key
        notes = revisions.get(key, [])
        lines += ["", "## Revision r{0} (session {1})".format(plan_revision, session_id), ""]
        if notes:
            amendment_id = str(notes[0].get("amendment_artifact_id"))
            amendment = _payload_of(store, run_id, st.ArtifactKind.PLAN_AMENDMENT, amendment_id)
            batch = notes[0].get("batch") or {}
            lines.append("- amendment: `{0}`".format(amendment_id))
            lines.append("- parked lanes:")
            for note in notes:
                lines.append(
                    "  - {0}: {1}".format(note.get("lane_id"), note.get("wait_reason"))
                )
            lines.append(
                "- changed lanes: {0}".format(
                    ", ".join(batch.get("changed_lanes") or ()) or "none"
                )
            )
            resets = [
                "{0} {1}->{2}".format(
                    item.get("lane_id"), item.get("from_stage"), item.get("to_stage")
                )
                for item in amendment.get("resets") or ()
                if item.get("from_stage") != item.get("to_stage")
            ]
            lines.append("- resets: {0}".format(", ".join(resets) or "none"))
            lines.append(
                "- obligations touched: {0}".format(
                    ", ".join(batch.get("obligations_touched") or ()) or "none"
                )
            )
            lines.append(
                "- material: {0}".format("yes" if batch.get("material") else "no")
            )
            for hit in batch.get("material_hits") or ():
                lines.append("  - {0}".format(hit))
            issue = batch.get("issue") or {}
            if issue:
                status = issue.get("status")
                if status == ISSUE_FAILED:
                    lines.append("- issue: FAILED {0}".format(issue.get("detail", "")))
                else:
                    lines.append("- issue: {0} {1}".format(status, issue.get("url", "")))
            else:
                lines.append("- issue: none")
            lines += ["", "### Rationale", ""]
            for note in notes:
                rationale = note.get("rationale") or {}
                lines.append("- {0}".format(note.get("lane_id")))
                for name in RATIONALE_KEYS[2:]:
                    lines.append("  - {0}: {1}".format(name, rationale.get(name, "")))
        else:
            lines.append("- amendment: none applied")
        lines += ["", "### Review rounds", ""]
        rounds = sorted(reviews.get(key, []), key=lambda item: int(item.get("review_round") or 0))
        if not rounds:
            lines.append("- none recorded")
        for review in rounds:
            lines.append(
                "- round {0}: {1} by {2} ({3})".format(
                    review.get("review_round"),
                    review.get("verdict"),
                    review.get("reviewer_id"),
                    review.get("reviewer_vendor"),
                )
            )
            for finding in review.get("findings") or ():
                lines.append(
                    "  - {0}: {1} -> {2}".format(
                        finding.get("location"),
                        finding.get("defect"),
                        finding.get("required_change"),
                    )
                )
    return "\n".join(lines) + "\n"


def _write_deviations(store: Any, run_id: str, path: Optional[Path]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_deviations(store, run_id), encoding="utf-8")


def attend_run(
    *,
    store: Any,
    run_id: str,
    policy: AttendPolicy,
    compiled: st.CompiledPlan,
    session_id: str,
    run_scheduler: Callable[[st.CompiledPlan, Tuple[str, ...]], st.RunStatus],
    request_for: Callable[..., OperatorRequest],
    dispatch: Callable[[OperatorRequest], Mapping[str, Any]],
    review_request_for: Callable[..., ReviewRequest],
    review: Callable[[ReviewRequest], Mapping[str, Any]],
    project: Callable[[bytes, int, list], st.CompiledPlan],
    apply_amendment: Callable[[st.CompiledPlan], Any],
    file_issue: Optional[Callable[[str, str], Mapping[str, str]]] = None,
    deviations_path: Optional[Path] = None,
    say: Callable[[str, str], None] = lambda lane, message: None,
    sleep: Callable[[float], None] = time.sleep,
) -> AttendOutcome:
    """Run the factory, and amend every attendable park instead of stopping.

    The scheduler runs until nothing else is runnable. Every attendable park
    it left -- `NO_PROGRESS`, `AMENDMENT_REQUIRED` and `LANE_FAULT` -- goes
    into one batch:
    one author dispatch, up to `max_review_rounds` independent reviews, one
    signed revision, one `apply_amendment`, and the scheduler runs again. The
    parked set, the projection and the evidence are all re-derived from the
    ledger after each batch rather than carried across.

    A `LANE_FAULT` lane the author declares a runtime defect leaves the
    batch: its issue is filed, and `run_scheduler(compiled, held)` is told
    never to resume it again this session. A batch that was only such lanes
    applies nothing and does not re-run the scheduler, which already ran
    every other lane as far as it could.

    `request_for(parked, compiled, review_round, reviewer_findings,
    previous_revision)` and `review_request_for(request, revision,
    rationales, review_round, revision_sha256)` assemble the two agents'
    inputs; `project(revision_bytes, plan_revision, findings)` signs,
    projects and compiles exactly the bytes the reviewer passed. `sleep` is
    how a planctl step waits out a spot capacity failure.
    """
    if not policy.enabled:
        raise AttendRefused(DISABLED, "attend.max_amendments_per_lane is not set")
    applied: list[AppliedAmendment] = []
    escalated: list[RuntimeDefect] = []
    per_lane: dict[str, int] = {}
    batches = 0
    status = st.RunStatus.WAITING
    rerun = True

    def held() -> Tuple[str, ...]:
        return tuple(item.lane_id for item in escalated)

    def done(reason: str) -> AttendOutcome:
        return AttendOutcome(
            status,
            reason,
            tuple(applied),
            compiled,
            waiting_lanes(store, run_id),
            tuple(escalated),
        )

    while True:
        if rerun:
            status = run_scheduler(compiled, held())
        rerun = True
        if status is not st.RunStatus.WAITING:
            return done(STOP_RUN_SETTLED)
        parked = parked_lane_ids(store, run_id, held())
        if not parked:
            return done(STOP_WAIT_NOT_ATTENDABLE)
        if batches >= policy.max_amendments_per_run:
            return done(STOP_RUN_CAP)
        if any(
            per_lane.get(lane_id, 0) >= policy.max_amendments_per_lane
            for lane_id, _reason in parked
        ):
            return done(STOP_LANE_CAP)
        for lane_id, reason in parked:
            say(lane_id, "attending a {0} park".format(reason))
        # Rendered whatever the batch's outcome: a review that never passed,
        # and a projection or apply refused after PASS, are deviations too.
        try:
            revised = _attend_batch(
                store=store,
                run_id=run_id,
                policy=policy,
                compiled=compiled,
                session_id=session_id,
                parked=parked,
                request_for=request_for,
                dispatch=dispatch,
                review_request_for=review_request_for,
                review=review,
                project=project,
                apply_amendment=apply_amendment,
                file_issue=file_issue,
                deviations_path=deviations_path,
                applied=applied,
                escalated=escalated,
                say=say,
                sleep=sleep,
            )
        finally:
            _write_deviations(store, run_id, deviations_path)
        if revised is None:
            return done(STOP_REVIEW_UNRESOLVED)
        if revised is compiled:
            rerun = False
            continue
        lane_ids = tuple(
            lane_id for lane_id, _reason in parked if lane_id not in held()
        )
        for lane_id in lane_ids:
            per_lane[lane_id] = per_lane.get(lane_id, 0) + 1
        batches += 1
        compiled = revised
        say(lane_ids[0], "amended to revision {0}".format(revised.plan_revision))


def _escalate(
    store: Any,
    run_id: str,
    defects: Sequence[Tuple[str, str]],
    file_issue: Optional[Callable[[str, str], Mapping[str, str]]],
    escalated: Optional[list],
    say: Callable[[str, str], None],
    fault: Optional[Mapping[str, Any]] = None,
    held: bool = True,
) -> None:
    """File one deduplicated issue per declared defect; hold each lane.

    `fault` is attend's own failure when attend, not the author, is the
    defect; otherwise each lane's `USER_WAIT` fault is reported. With
    `held=False` the issue is filed and the lane is not held: a revision
    answers it.
    """
    for lane_id, detail in defects:
        if fault is None:
            wait = store.latest_lane_artifact_payload(
                run_id, lane_id, st.ArtifactKind.USER_WAIT
            ) or {}
            lane_fault = dict(wait.get("fault") or {})
        else:
            lane_fault = dict(fault)
        issue: Mapping[str, str] = {}
        if file_issue is not None:
            try:
                issue = dict(
                    file_issue(
                        defect_issue_title(run_id, lane_id, lane_fault),
                        defect_issue_body(
                            run_id=run_id,
                            lane_id=lane_id,
                            fault=lane_fault,
                            detail=detail,
                            declared=fault is None,
                        ),
                    )
                )
            except Exception as exc:  # an unfiled issue still holds the lane
                issue = {
                    "status": ISSUE_FAILED,
                    "detail": "{0}: {1}".format(type(exc).__name__, exc),
                }
        if escalated is not None:
            escalated.append(RuntimeDefect(lane_id, lane_fault, detail, issue))
        say(
            lane_id,
            "runtime defect{0}: {1}".format(
                ", lane stays parked" if held else " reported, the revision answers the lane",
                issue.get("url") or issue.get("detail") or "no issue filed",
            ),
        )


def _agent_twice(
    step: Callable[[Any], Mapping[str, Any]],
    request: Any,
    code: str,
    say: Callable[[str, str], None],
) -> Tuple[Any, Optional[AttendRefused]]:
    """One attend agent, retried once. `(answer, None)` or `(None, failure)`.

    `code` is the agent's own failure: it could not be launched, crashed, or
    timed out. Any other refusal is about the revision, never retried here.
    """
    return _twice(lambda: step(request), request.lane_id, code, say, untyped=True)


def _twice(
    call: Callable[[], Any],
    lane_id: str,
    code: str,
    say: Callable[[str, str], None],
    *,
    untyped: bool,
    sleep: Optional[Callable[[float], None]] = None,
) -> Tuple[Any, Optional[AttendRefused]]:
    """Retry `code` failures; `(answer, None)` or `(None, failure)`.

    Only a refusal typed `code` is retried. `untyped` also counts any other
    exception as `code`: true for an agent, whose crash has no type of its
    own, false for a planctl step, which types its own failure. Given
    `sleep`, a refusal naming `NO_SPOT_CAPACITY` is instead waited out up to
    `CAPACITY_RETRIES` times and, once those are spent, is the failure.
    """
    failure: Optional[AttendRefused] = None
    waits = 0
    attempt = 0
    while True:
        try:
            return call(), None
        except AttendRefused as exc:
            if exc.code != code:
                raise
            failure = exc
        except Exception as exc:
            if not untyped:
                raise
            failure = AttendRefused(code, "{0}: {1}".format(type(exc).__name__, exc))
        if sleep is not None and NO_SPOT_CAPACITY in failure.detail:
            if waits >= CAPACITY_RETRIES:
                return None, failure
            waits += 1
            say(
                lane_id,
                "{0}, no spot capacity, retry {1}/{2} in {3:.0f}s: {4}".format(
                    code, waits, CAPACITY_RETRIES, CAPACITY_DELAY_SECONDS,
                    failure.detail,
                ),
            )
            sleep(CAPACITY_DELAY_SECONDS)
            continue
        attempt += 1
        if attempt == 2:
            return None, failure
        say(lane_id, "{0}, retrying once: {1}".format(code, failure.detail))


def _contain(
    store: Any,
    run_id: str,
    parked: Tuple[Tuple[str, str], ...],
    failure: AttendRefused,
    file_issue: Optional[Callable[[str, str], Mapping[str, str]]],
    escalated: Optional[list],
    say: Callable[[str, str], None],
    compiled: st.CompiledPlan,
) -> st.CompiledPlan:
    """Hold a batch's lanes as a runtime defect after attend exhausts retries.

    The same path a declared defect takes -- one deduplicated issue per lane,
    the lane held for the session -- so every other lane keeps running and
    only `PAUSE` stops the run. Nothing was revised, so `compiled` returns.
    """
    detail = "{0} retries exhausted: {1}".format(failure.code, failure.detail)
    defects = tuple((lane_id, detail) for lane_id, _reason in parked)
    fault = {"exception_type": failure.code, "message": failure.detail}
    _escalate(store, run_id, defects, file_issue, escalated, say, fault=fault)
    return compiled


def _refused_finding(
    refused: AttendRefused,
    lane_id: str,
    review_round: int,
    say: Callable[[str, str], None],
) -> Tuple[Mapping[str, str], ...]:
    """A planctl refusal of the revision, as the located finding its author fixes."""
    say(
        lane_id,
        "review round {0}: planctl refused the revision, returned to the "
        "author: {1}".format(review_round, (refused.detail.splitlines() or [""])[0]),
    )
    return (
        {
            "location": "revision IR (planctl)",
            "defect": refused.detail,
            "required_change": (
                "change the revision so planctl accepts it; the defect is "
                "planctl's own output, its error lines first"
            ),
        },
    )


def _attend_batch(
    *,
    store: Any,
    run_id: str,
    policy: AttendPolicy,
    compiled: st.CompiledPlan,
    session_id: str,
    parked: Tuple[Tuple[str, str], ...],
    request_for: Callable[..., OperatorRequest],
    dispatch: Callable[[OperatorRequest], Mapping[str, Any]],
    review_request_for: Callable[..., ReviewRequest],
    review: Callable[[ReviewRequest], Mapping[str, Any]],
    project: Callable[[bytes, int, list], st.CompiledPlan],
    apply_amendment: Callable[[st.CompiledPlan], Any],
    file_issue: Optional[Callable[[str, str], Mapping[str, str]]],
    deviations_path: Optional[Path],
    applied: list,
    say: Callable[[str, str], None],
    escalated: Optional[list] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[st.CompiledPlan]:
    """One batch: author, review rounds, sign, apply. None when unresolved.

    Each round's revision has its own path, and the reviewer judges the
    bytes whose sha256 the round records; only those bytes are projected.
    planctl's verdict on a revision (`REVISION_REFUSED`), before its review
    or in the projection after a PASS, is that round's finding for the
    author; only a projection refused in the last round still refuses.
    `compiled` itself is returned when every lane in the batch was declared
    a runtime defect, so nothing was revised.
    """
    lane_ids = tuple(lane_id for lane_id, _reason in parked)
    allowed = batch_scope(compiled.lanes, parked)
    reviewer_findings: Tuple[Mapping[str, Any], ...] = ()
    previous = ""
    passed: Optional[Tuple[OperatorRequest, Path, str, Tuple[Mapping[str, Any], ...], list, str]] = None
    for review_round in range(1, policy.max_review_rounds + 1):
        request = request_for(
            parked, compiled, review_round, reviewer_findings, previous
        )
        envelope, failed = _agent_twice(dispatch, request, OPERATOR_FAILED, say)
        if failed is not None:
            return _contain(store, run_id, parked, failed, file_issue, escalated, say, compiled)
        answer = read_operator_envelope(envelope, request, strict=False)
        if answer.ignored:
            say(
                request.lane_id,
                "runtime_defects ignored, the revision answers every lane: "
                + answer.ignored,
            )
        if answer.reported:
            _escalate(
                store, run_id, answer.reported, file_issue, None, say, held=False
            )
        if answer.held:
            _escalate(store, run_id, answer.held, file_issue, escalated, say)
            declared = {lane_id for lane_id, _detail in answer.held}
            parked = tuple(item for item in parked if item[0] not in declared)
            if not parked:
                return compiled
            allowed = batch_scope(compiled.lanes, parked)
        lane_ids = answer.lane_ids
        written, rationales = answer.revision, answer.rationales
        digest = sha256_hex(written.read_bytes())
        try:
            review_request, failed = _twice(
                lambda: review_request_for(
                    request, written, rationales, review_round, digest
                ),
                lane_ids[0], PLANCTL_FAILED, say, untyped=False, sleep=sleep,
            )
        except AttendRefused as exc:
            if exc.code != REVISION_REFUSED:
                raise
            # planctl's verdict about the revision: the author's to fix, and
            # no reviewer reads a revision planctl already refused.
            reviewer_findings = _refused_finding(exc, lane_ids[0], review_round, say)
            previous = str(written)
            continue
        if failed is not None:
            return _contain(store, run_id, parked, failed, file_issue, escalated, say, compiled)
        if review_request.revision_sha256 != digest:
            raise AttendRefused(
                REVISION_REFUSED, "review request does not bind the written revision"
            )
        answer, failed = _agent_twice(review, review_request, REVIEWER_FAILED, say)
        if failed is not None:
            return _contain(store, run_id, parked, failed, file_issue, escalated, say, compiled)
        verdict, located = require_review_verdict(answer)
        merged = {lane.lane_id: lane.merged_source_sha256 for lane in request.lanes}
        store.record_attend_review(
            run_id,
            session_id=session_id,
            plan_revision=request.next_plan_revision,
            review_round=review_round,
            payload={
                "verdict": verdict,
                "findings": [dict(item) for item in located],
                "reviewer_id": policy.reviewer_id,
                "reviewer_vendor": policy.reviewer_vendor,
                "revision_ref": str(written),
                "revision_sha256": digest,
                "lanes": [
                    {
                        "lane_id": lane_id,
                        "wait_reason": reason,
                        "merged_source_sha256": merged.get(lane_id, ""),
                    }
                    for lane_id, reason in parked
                ],
            },
        )
        say(lane_ids[0], "review round {0}: {1}".format(review_round, verdict))
        if verdict == st.ReviewerVerdict.PASS.value:
            findings = require_review_findings(Path(review_request.findings_out_path))
            data = reviewed_bytes(written, digest)
            try:
                revised, failed = _twice(
                    lambda: project(data, request.next_plan_revision, findings),
                    lane_ids[0], PLANCTL_FAILED, say, untyped=False, sleep=sleep,
                )
            except AttendRefused as exc:
                if exc.code != REVISION_REFUSED or review_round == policy.max_review_rounds:
                    raise
                reviewer_findings = _refused_finding(exc, lane_ids[0], review_round, say)
                previous = str(written)
                continue
            if failed is not None:
                return _contain(store, run_id, parked, failed, file_issue, escalated, say, compiled)
            passed = (request, written, digest, rationales, findings, verdict)
            break
        reviewer_findings = located
        previous = str(written)
    if passed is None:
        return None
    request, written, digest, rationales, findings, verdict = passed

    changed = require_amendment_scope(compiled.lanes, revised.lanes, allowed)
    record = apply_amendment(revised)
    amendment = _payload_of(
        store, run_id, st.ArtifactKind.PLAN_AMENDMENT, record.artifact_id
    )
    material = classify_material(compiled, revised, amendment.get("resets") or ())
    issue: Mapping[str, str] = {}
    if material.material and file_issue is not None:
        try:
            issue = dict(
                file_issue(
                    issue_title(run_id, revised.plan_revision),
                    issue_body(
                        run_id=run_id,
                        plan_revision=revised.plan_revision,
                        material=material,
                        changed=changed,
                        rationales=rationales,
                        verdict=verdict,
                        reviewer_id=policy.reviewer_id,
                        reviewer_vendor=policy.reviewer_vendor,
                        deviations_path=str(deviations_path or ""),
                    ),
                )
            )
        except Exception as exc:  # a failed issue never undoes the amendment
            issue = {
                "status": ISSUE_FAILED,
                "detail": "{0}: {1}".format(type(exc).__name__, exc),
            }
        if issue.get("status") == ISSUE_FAILED:
            say(lane_ids[0], "issue not filed: {0}".format(issue.get("detail", "")))
    batch = {
        "lanes": list(lane_ids),
        "changed_lanes": list(changed),
        "material": material.material,
        "material_hits": list(material.hits),
        "obligations_touched": list(material.obligations_touched),
        "issue": dict(issue),
        "revision_sha256": digest,
    }
    for (lane_id, reason), rationale in zip(parked, rationales):
        note = store.record_amendment_rationale(
            run_id,
            lane_id=lane_id,
            amendment_artifact_id=record.artifact_id,
            payload={
                "attend_session_id": session_id,
                "plan_revision": revised.plan_revision,
                "rationale": dict(rationale),
                "revision_ref": str(written),
                "wait_reason": reason,
                "batch": batch,
            },
        )
        applied.append(
            AppliedAmendment(
                lane_id=lane_id,
                plan_revision=revised.plan_revision,
                amendment_artifact_id=record.artifact_id,
                rationale_artifact_id=note.artifact_id,
            )
        )
    return revised
