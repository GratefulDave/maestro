"""`run attend` authors the amendment every attendable park needs.

On an earlier run a human cleared two `NO_PROGRESS` parks by hand: read the
gate table, the latest `CODE_REVIEW` findings and the suite, edited one seam
contract, validated it, minted the receipt, and ran `run amend`; both
amendments converged in one round. These cases pin the loop that does that
without the human, and -- more importantly -- pin what it still refuses to do:

- a deployment that never opted in gets `ATTEND_DISABLED`, not an agent;
- `NO_PROGRESS` and `AMENDMENT_REQUIRED` parks are answered together, by one
  author dispatch, one independent review, and one `PLAN_AMENDMENT`;
- `PAUSE` and `LANE_FAULT` park exactly as they do today;
- a reviewer REVISE goes back to the author with its located findings, and a
  revision still unresolved at the bound is never applied;
- a lane past its bound parks, and so does a run past its own;
- an author or reviewer that crashes is a typed refusal naming why;
- a revision that reaches a lane outside the batch's scope is refused whole,
  and so is one that changes no projection at all;
- the transition is still the `PLAN_AMENDMENT`. The agents' prose reaches the
  ledger only as `AMENDMENT_RATIONALE` and `ATTEND_REVIEW` records nothing
  reads.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

from adw_modules import attend as att
from adw_modules import scheduler_types as st


def _lane(lane_id: str, *, needs: tuple[str, ...] = (), kind: str | None = None,
          spec: str = "a") -> st.LaneProjection:
    spec_digest = st.digest_canonical({"lane": lane_id, "spec": spec})
    return st.LaneProjection(
        lane_id=lane_id,
        needs=tuple(sorted(needs)),
        spec_digest=spec_digest,
        declared_outputs=("{0}.txt".format(lane_id),),
        lane_projection_digest=st.lane_projection_digest(
            spec_digest, tuple(sorted(needs)), ("{0}.txt".format(lane_id),),
            lane_kind=kind,
        ),
        lane_kind=kind,
    )


def _plan(lanes: tuple[st.LaneProjection, ...], revision: int) -> st.CompiledPlan:
    body = json.dumps(
        {"revision": revision, "lanes": [{"id": lane.lane_id} for lane in lanes]},
        sort_keys=True,
    ).encode("utf-8")
    return st.CompiledPlan(
        plan_bytes=body,
        plan_artifact_ref="plan:r{0}".format(revision),
        plan_digest=st.digest_bytes(body),
        plan_revision=revision,
        lanes=lanes,
        integration_order=st.topological_integration_order(lanes),
    )


TESTS_LANE = _lane("lane-a-tests", kind=st.LANE_KIND_TESTS)
BUILD_LANE = _lane("lane-a-build", needs=("lane-a-tests",), kind=st.LANE_KIND_BUILD)
OTHER_LANE = _lane("lane-b-build", kind=st.LANE_KIND_BUILD)
PLAN = _plan((TESTS_LANE, BUILD_LANE, OTHER_LANE), 1)

NO_PROGRESS = st.WaitReason.NO_PROGRESS.value
AMENDMENT_REQUIRED = st.WaitReason.AMENDMENT_REQUIRED.value


class _Record:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        self.replayed = False


class _Store:
    """The ledger reads and the two ledger writes `attend_run` performs."""

    def __init__(self, parked: Mapping[str, str]) -> None:
        self.lanes = PLAN.lanes
        self.parked = dict(parked)
        self.rationales: list[Mapping[str, Any]] = []
        self.reviews: list[Mapping[str, Any]] = []
        self.amendments: list[Mapping[str, Any]] = []

    def active_projection(self, run_id: str) -> tuple[st.LaneProjection, ...]:
        del run_id
        return self.lanes

    def lane_stage(self, run_id: str, lane_id: str) -> st.LaneStage:
        del run_id
        if lane_id in self.parked:
            return st.LaneStage.WAITING_FOR_USER
        return st.LaneStage.MERGED

    def latest_lane_artifact_payload(
        self, run_id: str, lane_id: str, kind: st.ArtifactKind
    ) -> Mapping[str, Any] | None:
        del run_id
        if kind is not st.ArtifactKind.USER_WAIT:
            return None
        reason = self.parked.get(lane_id)
        return None if reason is None else {"wait_reason": reason}

    def record_amendment_rationale(
        self, run_id: str, *, lane_id: str, amendment_artifact_id: str,
        payload: Mapping[str, Any],
    ) -> _Record:
        self.rationales.append(
            {
                "run_id": run_id,
                "lane_id": lane_id,
                "amendment_artifact_id": amendment_artifact_id,
                "payload": dict(payload),
            }
        )
        return _Record("rationale-{0}".format(len(self.rationales)))

    def record_attend_review(
        self, run_id: str, *, session_id: str, plan_revision: int,
        review_round: int, payload: Mapping[str, Any],
    ) -> _Record:
        self.reviews.append(
            {"review_round": review_round, "plan_revision": plan_revision, **payload}
        )
        return _Record("review-{0}".format(len(self.reviews)))

    def run_artifacts_of_kind(
        self, run_id: str, kind: st.ArtifactKind
    ) -> tuple[Mapping[str, Any], ...]:
        if kind is st.ArtifactKind.PLAN_AMENDMENT:
            return tuple(self.amendments)
        return ()


def _rationale(lane_id: str) -> dict[str, Any]:
    return {
        "lane": lane_id,
        "round": 4,
        "failing_cases_summary": "producer omits the required field",
        "contract_gap": "seam contract never required the field",
        "edit_path": "seams[2].contract",
        "edit_text": "the producer emits `field` on every record",
    }


_FINDING = {
    "location": "/seams/2/contract",
    "defect": "the contract still allows an empty field",
    "required_change": "state that an empty field is refused",
}


class _Harness:
    """A run whose scheduler parks once, then completes after one amendment."""

    def __init__(self, tmp: Path, *, parked: Mapping[str, str],
                 policy: att.AttendPolicy,
                 revision_lanes: Sequence[str] = ("lane-a-build",),
                 crash: BaseException | None = None,
                 reviewer_crash: BaseException | None = None,
                 verdicts: Sequence[str] = ("PASS",),
                 stays_parked: bool = False,
                 file_issue: Any = None) -> None:
        self.tmp = tmp
        self.store = _Store(parked)
        self.policy = policy
        self.revision_lanes = tuple(revision_lanes)
        self.crash = crash
        self.reviewer_crash = reviewer_crash
        self.verdicts = list(verdicts)
        self.stays_parked = stays_parked
        self.file_issue = file_issue
        self.passes = 0
        self.dispatched: list[att.OperatorRequest] = []
        self.reviewed: list[att.ReviewRequest] = []
        self.projected: list[tuple[int, list]] = []
        self.applied: list[st.CompiledPlan] = []

    def run_scheduler(self, plan: st.CompiledPlan) -> st.RunStatus:
        self.passes += 1
        if self.store.parked:
            return st.RunStatus.WAITING
        return st.RunStatus.COMPLETE

    def request_for(self, parked, plan, review_round, reviewer_findings, previous):
        out = self.tmp / "operator" / "r{0}.ir.json".format(plan.plan_revision + 1)
        out.parent.mkdir(parents=True, exist_ok=True)
        return att.OperatorRequest(
            run_id="run-1",
            plan_revision=plan.plan_revision,
            next_plan_revision=plan.plan_revision + 1,
            lanes=tuple(
                att.LaneEvidence(
                    lane_id=lane_id,
                    wait_reason=reason,
                    stage=st.LaneStage.WAITING_FOR_USER.value,
                    round_number=4,
                    public_contract={"acceptance_criteria": ["emits records"]},
                    reviews=({"kind": "CODE_REVIEW", "verdict": "REVISE"},),
                    failure_output=("1 failed",),
                    lane_gates="stage WAITING_FOR_USER",
                    suite_files={"tests/a.spec.ts": "expect(record.field)"},
                )
                for lane_id, reason in parked
            ),
            ir_path=str(self.tmp / "current.ir.json"),
            revision_out_path=str(out),
            amendment_rules="rules",
            allowed_lane_ids=att.batch_scope(plan.lanes, parked),
            review_round=review_round,
            reviewer_findings=tuple(reviewer_findings),
            previous_revision_path=previous,
        )

    def dispatch(self, request: att.OperatorRequest) -> Mapping[str, Any]:
        self.dispatched.append(request)
        if self.crash is not None:
            raise self.crash
        Path(request.revision_out_path).write_text("{}", encoding="utf-8")
        return {
            "revision_path": request.revision_out_path,
            "rationales": [_rationale(lane.lane_id) for lane in request.lanes],
        }

    def review_request_for(self, request, revision, rationales, review_round):
        return att.ReviewRequest(
            run_id=request.run_id,
            plan_revision=request.plan_revision,
            next_plan_revision=request.next_plan_revision,
            review_round=review_round,
            lanes=request.lanes,
            current_ir_path=request.ir_path,
            revision_ir_path=str(revision),
            question_surface="{}",
            rationales=tuple(rationales),
            findings_out_path=str(
                self.tmp / "reviewer" / "round{0}.json".format(review_round)
            ),
            amendment_rules=request.amendment_rules,
            allowed_lane_ids=request.allowed_lane_ids,
        )

    def review(self, request: att.ReviewRequest) -> Mapping[str, Any]:
        self.reviewed.append(request)
        if self.reviewer_crash is not None:
            raise self.reviewer_crash
        verdict = self.verdicts.pop(0) if self.verdicts else "PASS"
        if verdict == "PASS":
            out = Path(request.findings_out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"findings": []}), encoding="utf-8")
            return {"verdict": "PASS", "findings": []}
        return {"verdict": "REVISE", "findings": [_FINDING]}

    def project(self, revision_ir: Path, revision: int, findings: list) -> st.CompiledPlan:
        del revision_ir
        self.projected.append((revision, findings))
        lanes = tuple(
            _lane(
                lane.lane_id,
                needs=lane.needs,
                kind=lane.lane_kind,
                spec="revised" if lane.lane_id in self.revision_lanes else "a",
            )
            for lane in PLAN.lanes
        )
        return _plan(lanes, revision)

    def apply(self, plan: st.CompiledPlan) -> _Record:
        self.applied.append(plan)
        artifact_id = "amendment-{0}".format(len(self.applied))
        self.store.amendments.append(
            {"artifact_id": artifact_id, "payload": {"resets": []}}
        )
        if not self.stays_parked:
            self.store.parked = {}
        return _Record(artifact_id)

    def attend(self, **extra: Any) -> att.AttendOutcome:
        return att.attend_run(
            store=self.store,
            run_id="run-1",
            policy=self.policy,
            compiled=PLAN,
            session_id="session-1",
            run_scheduler=self.run_scheduler,
            request_for=self.request_for,
            dispatch=self.dispatch,
            review_request_for=self.review_request_for,
            review=self.review,
            project=self.project,
            apply_amendment=self.apply,
            file_issue=self.file_issue,
            **extra,
        )


def _policy(**overrides: Any) -> att.AttendPolicy:
    fields: dict[str, Any] = {
        "max_amendments_per_lane": 2,
        "max_amendments_per_run": 10,
        "reviewer_id": "attend-reviewer:omp:profile-b",
        "reviewer_vendor": "vendor-b",
    }
    fields.update(overrides)
    return att.AttendPolicy(**fields)


class AttendLoop(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_a_deployment_that_never_opted_in_dispatches_nothing(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(max_amendments_per_lane=0),
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.DISABLED)
        self.assertEqual(harness.passes, 0)
        self.assertEqual(harness.dispatched, [])

    def test_a_no_progress_park_is_amended_and_the_run_continues(self) -> None:
        harness = _Harness(
            self.tmp, parked={"lane-a-build": NO_PROGRESS}, policy=_policy()
        )
        outcome = harness.attend()
        self.assertIs(outcome.status, st.RunStatus.COMPLETE)
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_SETTLED)
        self.assertEqual(len(outcome.applied), 1)
        self.assertEqual(outcome.applied[0].lane_id, "lane-a-build")
        self.assertEqual(outcome.applied[0].plan_revision, 2)
        # The scheduler ran again after the amendment: that continuation is
        # the whole point, and a loop that stopped at the amendment would
        # leave the run exactly where a bare `run resume` leaves it.
        self.assertEqual(harness.passes, 2)
        self.assertEqual(len(harness.applied), 1)

    def test_one_batch_answers_a_no_progress_and_an_amendment_required_park(self) -> None:
        """A1: one author, one reviewer, one PLAN_AMENDMENT, scheduler again."""
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS, "lane-b-build": AMENDMENT_REQUIRED},
            policy=_policy(),
            revision_lanes=("lane-a-build", "lane-b-build"),
        )
        outcome = harness.attend()
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_SETTLED)
        self.assertEqual(len(harness.dispatched), 1)
        self.assertEqual(len(harness.reviewed), 1)
        self.assertEqual(len(harness.applied), 1)
        self.assertEqual(harness.passes, 2)
        request = harness.dispatched[0]
        self.assertEqual(
            [(lane.lane_id, lane.wait_reason) for lane in request.lanes],
            [("lane-a-build", NO_PROGRESS), ("lane-b-build", AMENDMENT_REQUIRED)],
        )
        self.assertEqual(
            request.allowed_lane_ids, ("lane-a-build", "lane-a-tests", "lane-b-build")
        )
        # One amendment, one rationale per parked lane, each bound to it.
        self.assertEqual(
            {(note["lane_id"], note["amendment_artifact_id"]) for note in harness.store.rationales},
            {("lane-a-build", "amendment-1"), ("lane-b-build", "amendment-1")},
        )
        self.assertEqual(
            {item.amendment_artifact_id for item in outcome.applied}, {"amendment-1"}
        )

    def test_the_transition_carries_no_agent_prose(self) -> None:
        harness = _Harness(
            self.tmp, parked={"lane-a-build": NO_PROGRESS}, policy=_policy()
        )
        outcome = harness.attend()
        self.assertEqual(len(harness.applied), 1)
        self.assertEqual(len(harness.store.rationales), 1)
        note = harness.store.rationales[0]
        self.assertEqual(note["amendment_artifact_id"], "amendment-1")
        self.assertEqual(
            note["payload"]["rationale"]["contract_gap"],
            "seam contract never required the field",
        )
        self.assertEqual(outcome.applied[0].rationale_artifact_id, "rationale-1")

    def test_a_pause_or_lane_fault_wait_parks_exactly_as_it_does_today(self) -> None:
        """A2."""
        for reason in (st.WaitReason.PAUSE.value, st.WaitReason.LANE_FAULT.value):
            with self.subTest(reason=reason):
                harness = _Harness(
                    self.tmp, parked={"lane-a-build": reason}, policy=_policy()
                )
                outcome = harness.attend()
                self.assertIs(outcome.status, st.RunStatus.WAITING)
                self.assertEqual(outcome.stop_reason, att.STOP_WAIT_NOT_ATTENDABLE)
                self.assertEqual(harness.dispatched, [])
                self.assertEqual(harness.reviewed, [])

    def test_a_reviewer_revise_returns_its_findings_to_the_author(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(),
            verdicts=("REVISE", "PASS"),
        )
        outcome = harness.attend()
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_SETTLED)
        self.assertEqual(len(harness.dispatched), 2)
        second = harness.dispatched[1]
        self.assertEqual(second.review_round, 2)
        self.assertEqual(second.reviewer_findings, (_FINDING,))
        self.assertEqual(second.previous_revision_path, harness.dispatched[0].revision_out_path)
        self.assertEqual([row["verdict"] for row in harness.store.reviews], ["REVISE", "PASS"])
        self.assertEqual(len(harness.applied), 1)

    def test_an_unresolved_review_applies_nothing_at_the_bound(self) -> None:
        """A3."""
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(max_review_rounds=3),
            verdicts=("REVISE", "REVISE", "REVISE"),
        )
        outcome = harness.attend()
        self.assertIs(outcome.status, st.RunStatus.WAITING)
        self.assertEqual(outcome.stop_reason, att.STOP_REVIEW_UNRESOLVED)
        self.assertEqual(len(harness.dispatched), 3)
        self.assertEqual(len(harness.reviewed), 3)
        self.assertEqual(harness.projected, [])
        self.assertEqual(harness.applied, [])
        self.assertEqual(harness.store.rationales, [])
        self.assertEqual(len(harness.store.reviews), 3)

    def test_the_reviewer_identity_is_recorded_on_every_round(self) -> None:
        """A5: the reviewer agent's identity, never Maestro's."""
        harness = _Harness(
            self.tmp, parked={"lane-a-build": NO_PROGRESS}, policy=_policy()
        )
        harness.attend()
        (row,) = harness.store.reviews
        self.assertEqual(row["reviewer_id"], "attend-reviewer:omp:profile-b")
        self.assertEqual(row["reviewer_vendor"], "vendor-b")
        # And the PASS findings the reviewer wrote are what gets signed.
        self.assertEqual(harness.projected, [(2, [])])

    def test_a_lane_past_its_bound_parks(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(max_amendments_per_lane=1),
            stays_parked=True,
        )
        outcome = harness.attend()
        self.assertIs(outcome.status, st.RunStatus.WAITING)
        self.assertEqual(outcome.stop_reason, att.STOP_LANE_CAP)
        self.assertEqual(len(outcome.applied), 1)
        self.assertEqual(len(harness.dispatched), 1)

    def test_a_run_past_its_own_bound_parks_before_the_lane_bound(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(max_amendments_per_lane=5, max_amendments_per_run=1),
            stays_parked=True,
        )
        outcome = harness.attend()
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_CAP)
        self.assertEqual(len(outcome.applied), 1)

    def test_a_batch_counts_once_toward_the_run_bound(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS, "lane-b-build": NO_PROGRESS},
            policy=_policy(max_amendments_per_lane=5, max_amendments_per_run=1),
            revision_lanes=("lane-a-build", "lane-b-build"),
            stays_parked=True,
        )
        outcome = harness.attend()
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_CAP)
        self.assertEqual(len(harness.applied), 1)
        self.assertEqual(len(outcome.applied), 2)

    def test_an_operator_crash_is_a_typed_refusal_naming_the_exception(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(),
            crash=RuntimeError("herdr pane died"),
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.OPERATOR_FAILED)
        self.assertIn("herdr pane died", caught.exception.detail)
        self.assertEqual(harness.applied, [])
        self.assertEqual(harness.store.rationales, [])

    def test_a_reviewer_crash_is_a_typed_refusal_naming_the_exception(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(),
            reviewer_crash=RuntimeError("reviewer pane died"),
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.REVIEWER_FAILED)
        self.assertIn("reviewer pane died", caught.exception.detail)
        self.assertEqual(harness.applied, [])

    def test_a_revision_reaching_an_unscoped_lane_is_refused_whole(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(),
            revision_lanes=("lane-b-build",),
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.TOO_WIDE)
        self.assertIn("lane-b-build", caught.exception.detail)
        self.assertEqual(harness.applied, [])

    def test_a_revision_that_changes_nothing_is_refused(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-a-build": NO_PROGRESS},
            policy=_policy(),
            revision_lanes=("lane-none",),
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.TOO_WIDE)
        self.assertEqual(harness.applied, [])


class OperatorEnvelope(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_an_unwritten_revision_is_not_a_revision(self) -> None:
        expected = self.tmp / "r2.ir.json"
        with self.assertRaises(att.AttendRefused) as caught:
            att.revision_path({"revision_path": str(expected)}, expected)
        self.assertEqual(caught.exception.code, att.NO_REVISION)

    def test_a_revision_written_somewhere_else_is_refused(self) -> None:
        expected = self.tmp / "r2.ir.json"
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_text("{}", encoding="utf-8")
        with self.assertRaises(att.AttendRefused) as caught:
            att.revision_path({"revision_path": str(elsewhere)}, expected)
        self.assertEqual(caught.exception.code, att.NO_REVISION)

    def test_a_blank_contract_gap_is_not_a_rationale(self) -> None:
        payload = _rationale("lane-a-build")
        payload["contract_gap"] = "   "
        with self.assertRaises(att.AttendRefused) as caught:
            att.require_rationale(payload)
        self.assertIn("contract_gap", caught.exception.detail)

    def test_every_rationale_key_is_required(self) -> None:
        payload = _rationale("lane-a-build")
        del payload["edit_path"]
        with self.assertRaises(att.AttendRefused) as caught:
            att.require_rationale(payload)
        self.assertIn("edit_path", caught.exception.detail)

    def test_every_parked_lane_is_owed_exactly_one_rationale(self) -> None:
        lanes = ("lane-a-build", "lane-b-build")
        for payload, needle in (
            ([_rationale("lane-a-build")], "no rationale for parked lane lane-b-build"),
            (
                [_rationale("lane-a-build"), _rationale("lane-a-build")],
                "two rationales",
            ),
            (
                [_rationale("lane-a-build"), _rationale("lane-b-build"), _rationale("lane-c")],
                "not parked",
            ),
            ({"lane": "lane-a-build"}, "not a list"),
        ):
            with self.subTest(needle=needle):
                with self.assertRaises(att.AttendRefused) as caught:
                    att.require_rationales(payload, lanes)
                self.assertEqual(caught.exception.code, att.NO_REVISION)
                self.assertIn(needle, caught.exception.detail)


class ReviewerVerdict(unittest.TestCase):
    def test_a_located_revise_and_an_empty_pass_are_accepted(self) -> None:
        self.assertEqual(
            att.require_review_verdict({"verdict": "REVISE", "findings": [_FINDING]}),
            ("REVISE", (_FINDING,)),
        )
        self.assertEqual(
            att.require_review_verdict({"verdict": "PASS", "findings": []}),
            ("PASS", ()),
        )

    def test_every_untyped_or_unlocated_answer_is_refused(self) -> None:
        unlocated = dict(_FINDING, location=" ")
        for payload in (
            {"verdict": "MAYBE", "findings": []},
            {"verdict": "REVISE", "findings": []},
            {"verdict": "PASS", "findings": [_FINDING]},
            {"verdict": "REVISE", "findings": [unlocated]},
            {"verdict": "REVISE", "findings": "fix it"},
            "PASS",
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(att.AttendRefused) as caught:
                    att.require_review_verdict(payload)
                self.assertEqual(caught.exception.code, att.REVIEW_INVALID)

    def test_a_pass_without_written_findings_is_not_an_answer(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        missing = Path(holder.name) / "absent.json"
        with self.assertRaises(att.AttendRefused) as caught:
            att.require_review_findings(missing)
        self.assertEqual(caught.exception.code, att.REVIEW_INVALID)


class PairedLanes(unittest.TestCase):
    def test_a_build_lane_admits_its_own_tests_predecessor(self) -> None:
        self.assertEqual(
            att.paired_lane_ids(PLAN.lanes, "lane-a-build"),
            ("lane-a-build", "lane-a-tests"),
        )

    def test_a_tests_lane_admits_the_build_lane_that_consumes_it(self) -> None:
        self.assertEqual(
            att.paired_lane_ids(PLAN.lanes, "lane-a-tests"),
            ("lane-a-build", "lane-a-tests"),
        )

    def test_an_unpaired_lane_admits_only_itself(self) -> None:
        self.assertEqual(
            att.paired_lane_ids(PLAN.lanes, "lane-b-build"), ("lane-b-build",)
        )

    def test_a_batch_scope_is_the_union_of_its_lanes_scopes(self) -> None:
        self.assertEqual(
            att.batch_scope(
                PLAN.lanes,
                (("lane-a-build", NO_PROGRESS), ("lane-b-build", AMENDMENT_REQUIRED)),
            ),
            ("lane-a-build", "lane-a-tests", "lane-b-build"),
        )

    def test_a_lane_added_or_removed_counts_as_changed(self) -> None:
        fewer = tuple(lane for lane in PLAN.lanes if lane.lane_id != "lane-b-build")
        self.assertEqual(att.changed_lane_ids(PLAN.lanes, fewer), ("lane-b-build",))


class ParkedLanes(unittest.TestCase):
    def test_no_progress_and_amendment_required_are_attendable(self) -> None:
        store = _Store(
            {
                "lane-a-build": NO_PROGRESS,
                "lane-a-tests": st.WaitReason.LANE_FAULT.value,
                "lane-b-build": AMENDMENT_REQUIRED,
            }
        )
        self.assertEqual(
            att.parked_lane_ids(store, "run-1"),
            (("lane-a-build", NO_PROGRESS), ("lane-b-build", AMENDMENT_REQUIRED)),
        )

    def test_pause_and_lane_fault_are_not(self) -> None:
        store = _Store(
            {
                "lane-a-build": st.WaitReason.PAUSE.value,
                "lane-b-build": st.WaitReason.LANE_FAULT.value,
            }
        )
        self.assertEqual(att.parked_lane_ids(store, "run-1"), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
