"""`run attend` authors the amendment a NO_PROGRESS park needs.

FDAdb run `d246ae95` parked `NO_PROGRESS` on `lane-faq-producer` twice. Both
times a human read the gate table, the latest `CODE_REVIEW` findings and the
sealed suite, edited one seam contract, validated it, minted the receipt, and
ran `run amend`; both amendments converged in one round. These cases pin the
loop that does that without the human, and -- more importantly -- pin what it
still refuses to do:

- a deployment that never opted in gets `ATTEND_DISABLED`, not an agent;
- a wait that is not `NO_PROGRESS` parks exactly as it does today;
- a lane past its bound parks, and so does a run past its own;
- an operator agent that crashes parks the lane and records why;
- a revision that reaches a lane outside the parked lane's pair is refused
  whole, and so is one that changes no projection at all;
- the transition is still the `PLAN_AMENDMENT`. The agent's prose reaches the
  ledger only as an `AMENDMENT_RATIONALE` that nothing reads.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

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
        {"revision": revision, "lanes": [lane.lane_id for lane in lanes]},
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


TESTS_LANE = _lane("lane-faq-tests", kind=st.LANE_KIND_TESTS)
BUILD_LANE = _lane("lane-faq-build", needs=("lane-faq-tests",), kind=st.LANE_KIND_BUILD)
OTHER_LANE = _lane("lane-geo-build", kind=st.LANE_KIND_BUILD)
PLAN = _plan((TESTS_LANE, BUILD_LANE, OTHER_LANE), 1)


class _Record:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id


class _Store:
    """The ledger reads and the one ledger write `attend_run` performs."""

    def __init__(self, parked: Mapping[str, str]) -> None:
        self.lanes = PLAN.lanes
        self.parked = dict(parked)
        self.rationales: list[Mapping[str, Any]] = []

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


def _rationale(lane_id: str) -> dict[str, Any]:
    return {
        "lane": lane_id,
        "round": 4,
        "failing_cases_summary": "producer omits the disclaimer seam",
        "contract_gap": "seam contract never required the disclaimer field",
        "edit_path": "seams[2].contract",
        "edit_text": "the producer emits `disclaimer` on every FAQ record",
    }


class _Harness:
    """A run whose scheduler parks once, then completes after one amendment."""

    def __init__(self, tmp: Path, *, parked: Mapping[str, str],
                 policy: att.AttendPolicy, revision_lane: str = "lane-faq-build",
                 crash: BaseException | None = None,
                 stays_parked: bool = False) -> None:
        self.tmp = tmp
        self.store = _Store(parked)
        self.policy = policy
        self.revision_lane = revision_lane
        self.crash = crash
        self.stays_parked = stays_parked
        self.passes = 0
        self.dispatched: list[att.OperatorRequest] = []
        self.applied: list[st.CompiledPlan] = []

    def run_scheduler(self, plan: st.CompiledPlan) -> st.RunStatus:
        self.passes += 1
        if self.store.parked:
            return st.RunStatus.WAITING
        return st.RunStatus.COMPLETE

    def request_for(self, lane_id: str, plan: st.CompiledPlan) -> att.OperatorRequest:
        out = self.tmp / "operator" / "r{0}.ir.json".format(plan.plan_revision + 1)
        out.parent.mkdir(parents=True, exist_ok=True)
        return att.OperatorRequest(
            run_id="run-1",
            lane_id=lane_id,
            stage=st.LaneStage.WAITING_FOR_USER.value,
            round_number=4,
            plan_revision=plan.plan_revision,
            next_plan_revision=plan.plan_revision + 1,
            public_contract={"acceptance_criteria": ["emits FAQ records"]},
            reviews=({"kind": "CODE_REVIEW", "verdict": "REVISE"},),
            redacted_failures=("1 failed",),
            lane_gates="stage WAITING_FOR_USER",
            ir_path=str(self.tmp / "current.ir.json"),
            revision_out_path=str(out),
            sealed_files={"tests/faq.spec.ts": "expect(record.disclaimer)"},
            amendment_rules="rules",
            allowed_lane_ids=att.paired_lane_ids(plan.lanes, lane_id),
        )

    def dispatch(self, request: att.OperatorRequest) -> Mapping[str, Any]:
        self.dispatched.append(request)
        if self.crash is not None:
            raise self.crash
        Path(request.revision_out_path).write_text("{}", encoding="utf-8")
        return {
            "revision_path": request.revision_out_path,
            "rationale": _rationale(request.lane_id),
        }

    def project(self, revision_ir: Path, revision: int) -> st.CompiledPlan:
        del revision_ir
        lanes = tuple(
            _lane(
                lane.lane_id,
                needs=lane.needs,
                kind=lane.lane_kind,
                spec="revised" if lane.lane_id == self.revision_lane else "a",
            )
            for lane in PLAN.lanes
        )
        return _plan(lanes, revision)

    def apply(self, plan: st.CompiledPlan) -> _Record:
        self.applied.append(plan)
        if not self.stays_parked:
            self.store.parked = {}
        return _Record("amendment-{0}".format(len(self.applied)))

    def attend(self) -> att.AttendOutcome:
        return att.attend_run(
            store=self.store,
            run_id="run-1",
            policy=self.policy,
            compiled=PLAN,
            session_id="session-1",
            run_scheduler=self.run_scheduler,
            request_for=self.request_for,
            dispatch=self.dispatch,
            project=self.project,
            apply_amendment=self.apply,
        )


def _policy(**overrides: Any) -> att.AttendPolicy:
    fields: dict[str, Any] = {"max_amendments_per_lane": 2, "max_amendments_per_run": 10}
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
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
            policy=_policy(max_amendments_per_lane=0),
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.DISABLED)
        self.assertEqual(harness.passes, 0)
        self.assertEqual(harness.dispatched, [])

    def test_a_no_progress_park_is_amended_and_the_run_continues(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
            policy=_policy(),
        )
        outcome = harness.attend()
        self.assertIs(outcome.status, st.RunStatus.COMPLETE)
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_SETTLED)
        self.assertEqual(len(outcome.applied), 1)
        self.assertEqual(outcome.applied[0].lane_id, "lane-faq-build")
        self.assertEqual(outcome.applied[0].plan_revision, 2)
        # The scheduler ran again after the amendment: that continuation is
        # the whole point, and a loop that stopped at the amendment would
        # leave the run exactly where a bare `run resume` leaves it.
        self.assertEqual(harness.passes, 2)
        self.assertEqual(len(harness.applied), 1)

    def test_the_transition_carries_no_agent_prose(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
            policy=_policy(),
        )
        outcome = harness.attend()
        # What moved the lane is the PLAN_AMENDMENT the harness applied.
        self.assertEqual(len(harness.applied), 1)
        # The prose exists exactly once, in a record bound to that amendment.
        self.assertEqual(len(harness.store.rationales), 1)
        note = harness.store.rationales[0]
        self.assertEqual(note["amendment_artifact_id"], "amendment-1")
        self.assertEqual(
            note["payload"]["rationale"]["contract_gap"],
            "seam contract never required the disclaimer field",
        )
        self.assertEqual(
            outcome.applied[0].rationale_artifact_id, "rationale-1"
        )

    def test_a_pause_wait_parks_exactly_as_it_does_today(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.PAUSE.value},
            policy=_policy(),
        )
        outcome = harness.attend()
        self.assertIs(outcome.status, st.RunStatus.WAITING)
        self.assertEqual(outcome.stop_reason, att.STOP_WAIT_NOT_ATTENDABLE)
        self.assertEqual(harness.dispatched, [])

    def test_an_amendment_required_wait_parks_exactly_as_it_does_today(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.AMENDMENT_REQUIRED.value},
            policy=_policy(),
        )
        outcome = harness.attend()
        self.assertEqual(outcome.stop_reason, att.STOP_WAIT_NOT_ATTENDABLE)
        self.assertEqual(harness.dispatched, [])

    def test_a_lane_past_its_bound_parks(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
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
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
            policy=_policy(max_amendments_per_lane=5, max_amendments_per_run=1),
            stays_parked=True,
        )
        outcome = harness.attend()
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_CAP)
        self.assertEqual(len(outcome.applied), 1)

    def test_an_operator_crash_is_a_typed_refusal_naming_the_exception(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
            policy=_policy(),
            crash=RuntimeError("herdr pane died"),
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.OPERATOR_FAILED)
        self.assertIn("herdr pane died", caught.exception.detail)
        self.assertEqual(harness.applied, [])
        self.assertEqual(harness.store.rationales, [])

    def test_a_revision_reaching_an_unpaired_lane_is_refused_whole(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
            policy=_policy(),
            revision_lane="lane-geo-build",
        )
        with self.assertRaises(att.AttendRefused) as caught:
            harness.attend()
        self.assertEqual(caught.exception.code, att.TOO_WIDE)
        self.assertIn("lane-geo-build", caught.exception.detail)
        self.assertEqual(harness.applied, [])

    def test_a_revision_that_changes_nothing_is_refused(self) -> None:
        harness = _Harness(
            self.tmp,
            parked={"lane-faq-build": st.WaitReason.NO_PROGRESS.value},
            policy=_policy(),
            revision_lane="lane-none",
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
        payload = _rationale("lane-faq-build")
        payload["contract_gap"] = "   "
        with self.assertRaises(att.AttendRefused) as caught:
            att.require_rationale(payload)
        self.assertIn("contract_gap", caught.exception.detail)

    def test_every_rationale_key_is_required(self) -> None:
        payload = _rationale("lane-faq-build")
        del payload["edit_path"]
        with self.assertRaises(att.AttendRefused) as caught:
            att.require_rationale(payload)
        self.assertIn("edit_path", caught.exception.detail)


class PairedLanes(unittest.TestCase):
    def test_a_build_lane_admits_its_own_tests_predecessor(self) -> None:
        self.assertEqual(
            att.paired_lane_ids(PLAN.lanes, "lane-faq-build"),
            ("lane-faq-build", "lane-faq-tests"),
        )

    def test_a_tests_lane_admits_the_build_lane_that_consumes_it(self) -> None:
        self.assertEqual(
            att.paired_lane_ids(PLAN.lanes, "lane-faq-tests"),
            ("lane-faq-build", "lane-faq-tests"),
        )

    def test_an_unpaired_lane_admits_only_itself(self) -> None:
        self.assertEqual(
            att.paired_lane_ids(PLAN.lanes, "lane-geo-build"), ("lane-geo-build",)
        )

    def test_a_lane_added_or_removed_counts_as_changed(self) -> None:
        fewer = tuple(lane for lane in PLAN.lanes if lane.lane_id != "lane-geo-build")
        self.assertEqual(
            att.changed_lane_ids(PLAN.lanes, fewer), ("lane-geo-build",)
        )


class ParkedLanes(unittest.TestCase):
    def test_only_a_no_progress_wait_is_attendable(self) -> None:
        store = _Store(
            {
                "lane-faq-build": st.WaitReason.NO_PROGRESS.value,
                "lane-geo-build": st.WaitReason.PAUSE.value,
            }
        )
        self.assertEqual(att.parked_lane_ids(store, "run-1"), ("lane-faq-build",))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
