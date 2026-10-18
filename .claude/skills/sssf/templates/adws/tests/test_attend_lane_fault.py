"""`LANE_FAULT` parks are attended, through the real scheduler and ledger.

`attend_run` drives a real `FactoryScheduler` over a real `ArtifactStore`;
the operator request is assembled by `maestro._attend_request` and the
amendment applied by `apply_factory_amendment`. Only the two agents and the
planctl projection are scripted. Pinned here:

- a `LANE_FAULT` park is amended and resumed, and the run completes;
- a `LANE_FAULT` declared a runtime defect files one issue and stays parked
  while its independent sibling merges, and the scheduler never resumes it;
- `run resume` attends on its own whenever the deployment enabled attend.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import maestro
from adw_modules import attend as att
from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot

from tests.test_factory_cutover import ScriptedActor, _init_repo

RUN_ID = "run-lane-fault"
LANE_FAULT = st.WaitReason.LANE_FAULT.value


def _plan_bytes(*, a_goal: str = "emit a.txt") -> bytes:
    """Two independent lanes: a fault in one must not stop the other."""
    lanes = []
    for lane_id, output, goal in (
        ("lane-a", "a.txt", a_goal),
        ("lane-c", "c.txt", "emit c.txt"),
    ):
        lanes.append(
            {
                "id": lane_id,
                "needs": [],
                "outputs": [output],
                "spec": {
                    "goal": goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["{0} is written".format(output)],
            }
        )
    document = {"schema_version": "maestro-plan.artifact-factory.v1", "lanes": lanes}
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _FaultOnRevisionOne(ScriptedActor):
    """`lane-a`'s builder launch refuses until the plan is amended."""

    builds_refused = 0

    def build(self, ctx: sch.LaneContext) -> dict:
        if ctx.lane.lane_id == "lane-a" and ctx.plan_revision == 1:
            type(self).builds_refused += 1
            raise sch.LaunchFailed("lane too large for one pane")
        return super().build(ctx)


class AttendedLaneFault(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.repo = self.root / "product"
        state = self.root / "state"
        state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:r1"
        )
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id=RUN_ID,
            compiled=self.compiled,
            runtime=self.runtime,
            target=self.target,
        )
        _FaultOnRevisionOne.builds_refused = 0
        self.dispatched: list[att.OperatorRequest] = []
        self.held: list[tuple[str, ...]] = []
        self.filed: list[tuple[str, str]] = []
        (self.root / "current.ir.json").write_text("{}", encoding="utf-8")

    # -- the loop's collaborators: real except the agents and planctl -------

    def run_scheduler(self, plan: st.CompiledPlan, held=()) -> st.RunStatus:
        self.held.append(tuple(held))
        scheduler = sch.FactoryScheduler(
            self.store,
            RUN_ID,
            _FaultOnRevisionOne(self.repo, self.runtime.path / "worktrees"),
            self.runtime,
            self.target,
            compiled=plan,
        )
        scheduler.resume_waiting(held)
        return scheduler.run()

    def request_for(self, parked, plan, review_round, reviewer_findings, previous):
        return maestro._attend_request(
            self.store,
            self.runtime,
            {},
            RUN_ID,
            parked,
            plan,
            self.root / "current.ir.json",
            self.root / "operator",
            review_round,
            reviewer_findings,
            previous,
            session_id="session-1",
        )

    def review_request_for(self, request, revision, rationales, review_round, digest):
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
            findings_out_path=str(self.root / "review-findings.json"),
            amendment_rules=request.amendment_rules,
            allowed_lane_ids=request.allowed_lane_ids,
            revision_sha256=digest,
        )

    def review(self, request: att.ReviewRequest) -> Mapping[str, Any]:
        Path(request.findings_out_path).write_text(
            json.dumps({"findings": []}), encoding="utf-8"
        )
        return {"verdict": "PASS", "findings": []}

    def project(self, revision_bytes: bytes, revision: int, findings: list):
        del revision_bytes, findings
        return plan_compiler.compile_plan(
            _plan_bytes(a_goal="emit a.txt from one small module"),
            plan_revision=revision,
            plan_artifact_ref="plan:r{0}".format(revision),
        )

    def apply(self, plan: st.CompiledPlan):
        return sch.apply_factory_amendment(
            self.store, RUN_ID, plan, runtime=self.runtime, target=self.target
        )

    def file_issue(self, title: str, body: str) -> Mapping[str, str]:
        self.filed.append((title, body))
        return {"status": att.ISSUE_CREATED, "url": "https://example.test/7"}

    def attend(self, dispatch) -> att.AttendOutcome:
        return att.attend_run(
            store=self.store,
            run_id=RUN_ID,
            policy=att.AttendPolicy(
                max_amendments_per_lane=2,
                reviewer_id="attend-reviewer:omp:b",
                reviewer_vendor="vendor-b",
            ),
            compiled=self.compiled,
            session_id="session-1",
            run_scheduler=self.run_scheduler,
            request_for=self.request_for,
            dispatch=dispatch,
            review_request_for=self.review_request_for,
            review=self.review,
            project=self.project,
            apply_amendment=self.apply,
            file_issue=self.file_issue,
        )

    # -- cases --------------------------------------------------------------

    def test_a_lane_fault_park_is_amended_and_resumed_to_completion(self) -> None:
        def dispatch(request: att.OperatorRequest) -> Mapping[str, Any]:
            self.dispatched.append(request)
            Path(request.revision_out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(request.revision_out_path).write_text("{}", encoding="utf-8")
            return {
                "revision_path": request.revision_out_path,
                "rationales": [
                    {
                        "lane": "lane-a",
                        "round": 1,
                        "failing_cases_summary": "builder launch refused",
                        "contract_gap": "the lane asked for more than one pane holds",
                        "edit_path": "lanes[0].spec.goal",
                        "edit_text": "emit a.txt from one small module",
                    }
                ],
            }

        outcome = self.attend(dispatch)
        self.assertIs(outcome.status, st.RunStatus.COMPLETE)
        self.assertEqual(outcome.stop_reason, att.STOP_RUN_SETTLED)
        self.assertEqual(len(self.dispatched), 1)
        # The author was handed the fault itself, read off the lane's USER_WAIT.
        evidence = self.dispatched[0].lanes[0]
        self.assertEqual(evidence.wait_reason, LANE_FAULT)
        self.assertEqual(
            evidence.fault,
            {"exception_type": "LaunchFailed", "message": "lane too large for one pane"},
        )
        self.assertEqual([item.lane_id for item in outcome.applied], ["lane-a"])
        self.assertEqual(self.store.lane_stage(RUN_ID, "lane-a"), st.LaneStage.MERGED)
        self.assertEqual(self.store.lane_stage(RUN_ID, "lane-c"), st.LaneStage.MERGED)
        self.assertEqual(outcome.parks, ())
        self.assertEqual(outcome.escalated, ())
        self.assertFalse(
            [title for title, _body in self.filed if title.startswith("Runtime defect")]
        )

    def test_a_runtime_defect_stays_parked_while_its_sibling_merges(self) -> None:
        def dispatch(request: att.OperatorRequest) -> Mapping[str, Any]:
            self.dispatched.append(request)
            return {
                "runtime_defects": [
                    {"lane": "lane-a", "detail": "LaunchFailed is a launcher bug"}
                ]
            }

        outcome = self.attend(dispatch)
        self.assertIs(outcome.status, st.RunStatus.WAITING)
        self.assertEqual(outcome.stop_reason, att.STOP_WAIT_NOT_ATTENDABLE)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(outcome.applied, ())
        self.assertEqual(self.store.lane_stage(RUN_ID, "lane-c"), st.LaneStage.MERGED)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, "lane-a"), st.LaneStage.WAITING_FOR_USER
        )
        self.assertEqual(outcome.parks, (("lane-a", LANE_FAULT),))
        self.assertEqual(len(self.filed), 1)
        self.assertEqual(
            self.filed[0][0], "Runtime defect on run {0}: lane-a LaunchFailed".format(RUN_ID)
        )
        self.assertIn("lane too large for one pane", self.filed[0][1])
        self.assertEqual(outcome.escalated[0].issue["url"], "https://example.test/7")
        # A later pass in the same session holds the lane: it is not retried.
        refused = _FaultOnRevisionOne.builds_refused
        self.assertIs(self.run_scheduler(self.compiled, held=("lane-a",)), st.RunStatus.WAITING)
        self.assertEqual(_FaultOnRevisionOne.builds_refused, refused)
        self.assertEqual(
            self.store.lane_stage(RUN_ID, "lane-a"), st.LaneStage.WAITING_FOR_USER
        )


class ResumeAttends(unittest.TestCase):
    """A3: `run resume` and `run start` need no separate verb."""

    class _Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def ledger_path(self) -> Path:
            return Path("/nonexistent/ledger")

    def _bound(self, attend: Mapping[str, Any]):
        layout = {"attend": dict(attend)}
        row = {"target_repository_root": "/nonexistent"}
        return layout, self._Closable(), self._Closable(), row, object(), object()

    def test_resume_attends_when_the_deployment_enabled_attend(self) -> None:
        bound = self._bound({"max_amendments_per_lane": 3})
        with mock.patch.object(
            maestro, "_bind_existing_run", return_value=bound
        ), mock.patch.object(
            maestro, "_run_attend", return_value=0
        ) as attended, mock.patch.object(
            maestro, "FactoryScheduler", side_effect=AssertionError("no bare scheduler")
        ):
            args = argparse.Namespace(run_id="run-1")
            self.assertEqual(maestro._run_resume(args), 0)
        attended.assert_called_once_with(args, verb="resume")
        # Closed before `run attend` binds the run again.
        self.assertTrue(bound[1].closed)
        self.assertTrue(bound[2].closed)

    def test_resume_without_attend_runs_the_bare_scheduler(self) -> None:
        bound = self._bound({})
        with mock.patch.object(
            maestro, "_bind_existing_run", return_value=bound
        ), mock.patch.object(
            maestro, "_run_attend", side_effect=AssertionError("attend is off")
        ), mock.patch.object(
            maestro, "maybe_autoload_dashboard", side_effect=RuntimeError("bare path")
        ):
            with self.assertRaises(RuntimeError):
                maestro._run_resume(argparse.Namespace(run_id="run-1"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
