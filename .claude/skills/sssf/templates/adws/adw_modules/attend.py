"""Attended amendments: an operator agent authors what a human would have.

A lane parks `NO_PROGRESS` when its reviewed rounds stop moving. Clearing that
park is not a retry -- a retry would run the same lane against the same
contract and stall again in the same place. What clears it is a plan
amendment, and on FDAdb run `d246ae95` a human wrote two of them by hand: read
the gate table and the latest `CODE_REVIEW` findings, read the sealed suite out
of the vault, edit one seam contract, validate, mint the receipt, project,
`run amend`. Both converged in one round.

`run attend` is that loop, with an agent holding the pen. What it does not
change is what a transition keys on. The operator agent's prose reaches the
ledger only as an `AMENDMENT_RATIONALE` record that nothing reads; the lane
moves because `apply_amendment` accepted a `PLAN_AMENDMENT` whose projection
digests changed, exactly as it does when a human runs `run amend`.

One privilege is deliberate and is stated in the operator agent's own prompt:
it reads the sealed suite. Builders and reviewers still do not, and nothing
here relaxes that -- the operator agent is a plan author, and the trade the
operator accepted is that an amendment it writes may state in a contract an
expectation the suite was asserting privately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple

from . import scheduler_types as st


class AttendRefused(RuntimeError):
    """A typed refusal from the attend loop. `code` is the operator's answer."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


#: `run attend` on a deployment that never opted in.
DISABLED = "ATTEND_DISABLED"
#: A revision reached lanes the parked lane is not paired with.
TOO_WIDE = "ATTEND_AMENDMENT_TOO_WIDE"
#: The operator agent returned no usable revision.
NO_REVISION = "ATTEND_OPERATOR_NO_REVISION"
#: The operator agent refused, crashed, or timed out.
OPERATOR_FAILED = "ATTEND_OPERATOR_FAILED"
#: The revision did not validate, project, or compile.
REVISION_REFUSED = "ATTEND_REVISION_REFUSED"
#: No `planctl` was resolvable, so no revision can be approved.
PLANCTL_UNRESOLVED = "ATTEND_PLANCTL_UNRESOLVED"
#: No reviewer key was resolvable, so no receipt can be minted.
KEY_UNRESOLVED = "ATTEND_REVIEWER_KEY_UNRESOLVED"
#: The Plan IR the live revision was projected from could not be located.
PLAN_IR_UNRESOLVED = "ATTEND_PLAN_IR_UNRESOLVED"

#: Why an attend session stopped. Recorded on the STOP `ATTEND_SESSION`.
STOP_RUN_SETTLED = "RUN_SETTLED"
STOP_WAIT_NOT_ATTENDABLE = "WAIT_NOT_ATTENDABLE"
STOP_LANE_CAP = "LANE_CAP_REACHED"
STOP_RUN_CAP = "RUN_CAP_REACHED"


@dataclass(frozen=True)
class AttendPolicy:
    """The deployment's opt-in. Absent `attend` means every field is off."""

    max_amendments_per_lane: int = 0
    max_amendments_per_run: int = 10
    route: Mapping[str, str] = field(default_factory=dict)
    planctl: Optional[Path] = None
    plan_ir: Optional[Path] = None
    validate_argv: Tuple[str, ...] = ()
    reviewer_id: str = "maestro-attend"
    reviewer_vendor: str = "maestro"

    @property
    def enabled(self) -> bool:
        return self.max_amendments_per_lane > 0


@dataclass(frozen=True)
class OperatorRequest:
    """Everything the operator agent is given, and nothing it is not.

    `sealed_files` is the privilege. It is present because the human this verb
    replaces read the same bytes, and because the contract gap that parks a
    lane is usually only visible by comparing what the suite asserts with what
    the contract says. Every other field is what a reviewer or a builder on
    this lane already had.
    """

    run_id: str
    lane_id: str
    stage: str
    round_number: int
    plan_revision: int
    next_plan_revision: int
    public_contract: Mapping[str, Any]
    reviews: Tuple[Mapping[str, Any], ...]
    redacted_failures: Tuple[str, ...]
    lane_gates: str
    ir_path: str
    revision_out_path: str
    sealed_files: Mapping[str, str]
    amendment_rules: str
    allowed_lane_ids: Tuple[str, ...]


class OperatorAgent(Protocol):
    def propose(self, request: OperatorRequest) -> Mapping[str, Any]:
        """Write the revision IR at `revision_out_path`; return the rationale.

        The envelope is `{"revision_path": <str>, "rationale": {...}}`. A
        refusal is an exception; a missing or unwritten path is
        `ATTEND_OPERATOR_NO_REVISION`.
        """


RATIONALE_KEYS = (
    "lane",
    "round",
    "failing_cases_summary",
    "contract_gap",
    "edit_path",
    "edit_text",
)


@dataclass(frozen=True)
class AppliedAmendment:
    lane_id: str
    plan_revision: int
    amendment_artifact_id: str
    rationale_artifact_id: str


@dataclass(frozen=True)
class AttendOutcome:
    status: st.RunStatus
    stop_reason: str
    applied: Tuple[AppliedAmendment, ...]
    compiled: st.CompiledPlan


def parked_lane_ids(store: Any, run_id: str) -> Tuple[str, ...]:
    """Lanes waiting on a `NO_PROGRESS` park, in projection order.

    Reads `lane_state.stage` and the lane's own latest `USER_WAIT`, which is
    the pair §5 makes authoritative. A lane waiting for any other reason --
    an explicit `PAUSE`, a final-review `AMENDMENT_REQUIRED` -- is not
    attendable and is deliberately left for the operator.
    """
    parked = []
    for lane in store.active_projection(run_id):
        if store.lane_stage(run_id, lane.lane_id) is not st.LaneStage.WAITING_FOR_USER:
            continue
        wait = store.latest_lane_artifact_payload(
            run_id, lane.lane_id, st.ArtifactKind.USER_WAIT
        )
        if wait is None:
            continue
        if wait.get("wait_reason") != st.WaitReason.NO_PROGRESS.value:
            continue
        parked.append(lane.lane_id)
    return tuple(parked)


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
    """Refuse a revision that reached past the parked lane and its pair.

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
    """The rationale the operator agent owes, or a typed refusal.

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


def attend_run(
    *,
    store: Any,
    run_id: str,
    policy: AttendPolicy,
    compiled: st.CompiledPlan,
    session_id: str,
    run_scheduler: Callable[[st.CompiledPlan], st.RunStatus],
    request_for: Callable[[str, st.CompiledPlan], OperatorRequest],
    dispatch: Callable[[OperatorRequest], Mapping[str, Any]],
    project: Callable[[Path, int], st.CompiledPlan],
    apply_amendment: Callable[[st.CompiledPlan], Any],
    say: Callable[[str, str], None] = lambda lane, message: None,
) -> AttendOutcome:
    """Run the factory, and amend a `NO_PROGRESS` park instead of stopping at it.

    One lane per pass. The plan changes under every amendment, so the parked
    set, the projection, and the gate table are all re-derived from the ledger
    after each one rather than carried across; that is also what keeps the loop
    honest when an amendment resets a lane the operator agent did not name.
    """
    if not policy.enabled:
        raise AttendRefused(DISABLED, "attend.max_amendments_per_lane is not set")
    applied: list[AppliedAmendment] = []
    per_lane: dict[str, int] = {}
    while True:
        status = run_scheduler(compiled)
        if status is not st.RunStatus.WAITING:
            return AttendOutcome(status, STOP_RUN_SETTLED, tuple(applied), compiled)
        parked = parked_lane_ids(store, run_id)
        if not parked:
            return AttendOutcome(
                status, STOP_WAIT_NOT_ATTENDABLE, tuple(applied), compiled
            )
        if len(applied) >= policy.max_amendments_per_run:
            return AttendOutcome(status, STOP_RUN_CAP, tuple(applied), compiled)
        lane_id = parked[0]
        if per_lane.get(lane_id, 0) >= policy.max_amendments_per_lane:
            return AttendOutcome(status, STOP_LANE_CAP, tuple(applied), compiled)
        say(lane_id, "attending a NO_PROGRESS park")
        request = request_for(lane_id, compiled)
        try:
            envelope = dispatch(request)
        except AttendRefused:
            raise
        except Exception as exc:
            raise AttendRefused(
                OPERATOR_FAILED, "{0}: {1}".format(type(exc).__name__, exc)
            ) from exc
        written = revision_path(envelope, Path(request.revision_out_path))
        rationale = require_rationale(envelope.get("rationale"))
        revised = project(written, request.next_plan_revision)
        require_amendment_scope(
            compiled.lanes,
            revised.lanes,
            paired_lane_ids(compiled.lanes, lane_id),
        )
        record = apply_amendment(revised)
        note = store.record_amendment_rationale(
            run_id,
            lane_id=lane_id,
            amendment_artifact_id=record.artifact_id,
            payload={
                "attend_session_id": session_id,
                "plan_revision": revised.plan_revision,
                "rationale": dict(rationale),
                "revision_ref": str(written),
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
        per_lane[lane_id] = per_lane.get(lane_id, 0) + 1
        compiled = revised
        say(lane_id, "amended to revision {0}".format(revised.plan_revision))
