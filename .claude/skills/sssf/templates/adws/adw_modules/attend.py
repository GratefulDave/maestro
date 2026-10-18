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
"""

from __future__ import annotations

import json
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
#: No reviewer key was resolvable, so no receipt can be minted.
KEY_UNRESOLVED = "ATTEND_REVIEWER_KEY_UNRESOLVED"
#: The Plan IR the live revision was projected from could not be located.
PLAN_IR_UNRESOLVED = "ATTEND_PLAN_IR_UNRESOLVED"

#: Why an attend session stopped. Recorded on the STOP `ATTEND_SESSION`.
STOP_RUN_SETTLED = "RUN_SETTLED"
STOP_WAIT_NOT_ATTENDABLE = "WAIT_NOT_ATTENDABLE"
STOP_LANE_CAP = "LANE_CAP_REACHED"
STOP_RUN_CAP = "RUN_CAP_REACHED"
STOP_REVIEW_UNRESOLVED = "REVIEW_UNRESOLVED"

#: The wait reasons a plan amendment can answer. `PAUSE` is a human decision
#: and `LANE_FAULT` a runtime exception in harness code; neither is attended.
ATTENDABLE_REASONS: Tuple[str, ...] = (
    st.WaitReason.NO_PROGRESS.value,
    st.WaitReason.AMENDMENT_REQUIRED.value,
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

    @property
    def lane_id(self) -> str:
        return self.lanes[0].lane_id

    @property
    def stage(self) -> str:
        return self.lanes[0].stage


class OperatorAgent(Protocol):
    def propose(self, request: OperatorRequest) -> Mapping[str, Any]:
        """Write the revision IR at `revision_out_path`; return the rationales.

        The envelope is `{"revision_path": <str>, "rationales": [{...}]}`, one
        rationale per parked lane. A refusal is an exception; a missing or
        unwritten path is `ATTEND_OPERATOR_NO_REVISION`.
        """


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
class AttendOutcome:
    status: st.RunStatus
    stop_reason: str
    applied: Tuple[AppliedAmendment, ...]
    compiled: st.CompiledPlan


@dataclass(frozen=True)
class MaterialChange:
    """The classifier's answer for one amendment. Computed, never read off prose."""

    material: bool
    hits: Tuple[str, ...]
    obligations_touched: Tuple[str, ...]


def parked_lane_ids(store: Any, run_id: str) -> Tuple[Tuple[str, str], ...]:
    """`(lane_id, wait_reason)` for every attendable park, in projection order.

    Reads `lane_state.stage` and the lane's own latest `USER_WAIT`, which is
    the pair §5 makes authoritative. A lane waiting on `PAUSE` or `LANE_FAULT`
    is not attendable and is deliberately left for the operator.
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
        reason = wait.get("wait_reason")
        if reason not in ATTENDABLE_REASONS:
            continue
        parked.append((lane.lane_id, str(reason)))
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
            lines.append("- amendment: none (review did not pass)")
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
    run_scheduler: Callable[[st.CompiledPlan], st.RunStatus],
    request_for: Callable[..., OperatorRequest],
    dispatch: Callable[[OperatorRequest], Mapping[str, Any]],
    review_request_for: Callable[..., ReviewRequest],
    review: Callable[[ReviewRequest], Mapping[str, Any]],
    project: Callable[[Path, int, list], st.CompiledPlan],
    apply_amendment: Callable[[st.CompiledPlan], Any],
    file_issue: Optional[Callable[[str, str], Mapping[str, str]]] = None,
    deviations_path: Optional[Path] = None,
    say: Callable[[str, str], None] = lambda lane, message: None,
) -> AttendOutcome:
    """Run the factory, and amend every attendable park instead of stopping.

    The scheduler runs until nothing else is runnable. Every attendable park
    it left -- `NO_PROGRESS` and `AMENDMENT_REQUIRED` -- goes into one batch:
    one author dispatch, up to `max_review_rounds` independent reviews, one
    signed revision, one `apply_amendment`, and the scheduler runs again. The
    parked set, the projection and the evidence are all re-derived from the
    ledger after each batch rather than carried across.

    `request_for(parked, compiled, review_round, reviewer_findings,
    previous_revision)` and `review_request_for(request, revision,
    rationales, review_round)` assemble the two agents' inputs; `project`
    signs, projects and compiles a revision with the reviewer's findings.
    """
    if not policy.enabled:
        raise AttendRefused(DISABLED, "attend.max_amendments_per_lane is not set")
    applied: list[AppliedAmendment] = []
    per_lane: dict[str, int] = {}
    batches = 0
    while True:
        status = run_scheduler(compiled)
        if status is not st.RunStatus.WAITING:
            return AttendOutcome(status, STOP_RUN_SETTLED, tuple(applied), compiled)
        parked = parked_lane_ids(store, run_id)
        if not parked:
            return AttendOutcome(
                status, STOP_WAIT_NOT_ATTENDABLE, tuple(applied), compiled
            )
        if batches >= policy.max_amendments_per_run:
            return AttendOutcome(status, STOP_RUN_CAP, tuple(applied), compiled)
        if any(
            per_lane.get(lane_id, 0) >= policy.max_amendments_per_lane
            for lane_id, _reason in parked
        ):
            return AttendOutcome(status, STOP_LANE_CAP, tuple(applied), compiled)
        lane_ids = tuple(lane_id for lane_id, _reason in parked)
        for lane_id, reason in parked:
            say(lane_id, "attending a {0} park".format(reason))
        allowed = batch_scope(compiled.lanes, parked)
        reviewer_findings: Tuple[Mapping[str, Any], ...] = ()
        previous = ""
        passed: Optional[Tuple[OperatorRequest, Path, Tuple[Mapping[str, Any], ...], list, str]] = None
        for review_round in range(1, policy.max_review_rounds + 1):
            request = request_for(
                parked, compiled, review_round, reviewer_findings, previous
            )
            try:
                envelope = dispatch(request)
            except AttendRefused:
                raise
            except Exception as exc:
                raise AttendRefused(
                    OPERATOR_FAILED, "{0}: {1}".format(type(exc).__name__, exc)
                ) from exc
            written = revision_path(envelope, Path(request.revision_out_path))
            rationales = require_rationales(envelope.get("rationales"), lane_ids)
            review_request = review_request_for(
                request, written, rationales, review_round
            )
            try:
                answer = review(review_request)
            except AttendRefused:
                raise
            except Exception as exc:
                raise AttendRefused(
                    REVIEWER_FAILED, "{0}: {1}".format(type(exc).__name__, exc)
                ) from exc
            verdict, located = require_review_verdict(answer)
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
                    "lanes": [
                        {"lane_id": lane_id, "wait_reason": reason}
                        for lane_id, reason in parked
                    ],
                },
            )
            say(lane_ids[0], "review round {0}: {1}".format(review_round, verdict))
            if verdict == st.ReviewerVerdict.PASS.value:
                findings = require_review_findings(
                    Path(review_request.findings_out_path)
                )
                passed = (request, written, rationales, findings, verdict)
                break
            reviewer_findings = located
            previous = str(written)
        if passed is None:
            _write_deviations(store, run_id, deviations_path)
            return AttendOutcome(
                status, STOP_REVIEW_UNRESOLVED, tuple(applied), compiled
            )
        request, written, rationales, findings, verdict = passed
        revised = project(written, request.next_plan_revision, findings)
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
            per_lane[lane_id] = per_lane.get(lane_id, 0) + 1
        batches += 1
        compiled = revised
        _write_deviations(store, run_id, deviations_path)
        say(lane_ids[0], "amended to revision {0}".format(revised.plan_revision))
