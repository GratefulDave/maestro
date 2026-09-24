"""A parked lane stops that lane, not the run.

A lane that parks is a fact about one lane. The scheduler used to read it as a
fact about the run: `run()` returned at the top of its loop the moment
`derive_run_status` said WAITING, and `_advance_ready` stopped refilling its
workers as soon as any lane was WAITING_FOR_USER. So one lane waiting on the
operator held every independent lane at whatever stage it had reached.

`ArtifactStore.ready_lane_ids` already skips a WAITING_FOR_USER lane and
already requires every lane in `needs` to be MERGED, so a parked lane and
everything downstream of it is unadvanceable without any help from the
scheduler. Removing the two run-wide reads therefore changes which lanes move,
never which lanes are allowed to move, and the run still ends on WAITING --
reached through the existing `if not progressed` fall-through, once nothing
can move, instead of before anything has been tried.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from tests import test_factory_cutover as cutover
from tests.test_lane_concurrency import LANES, RUN_ID, _PassingActor, _Run


class _CountsRefills(sch.FactoryScheduler):
    """Records how many batches `_advance_ready` was asked to run."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.batches = 0

    def _advance_ready(self, lane_ids: object) -> None:
        self.batches += 1
        super()._advance_ready(lane_ids)  # type: ignore[arg-type]


class ParkedLaneDoesNotStopTheRun(_Run):
    def _park(self, scheduler: sch.FactoryScheduler, lane_id: str) -> None:
        stage = self.store.lane_stage(RUN_ID, lane_id)
        digest, observed = scheduler._pause_input(lane_id, stage)
        self.store.pause_lane(
            RUN_ID,
            lane_id,
            stage,
            digest,
            observed=observed,
            reason=st.WaitReason.PAUSE,
        )

    def test_the_other_lane_finishes_and_only_then_the_run_reports_waiting(
        self,
    ) -> None:
        """lane-a parks before anything runs; lane-b must still merge.

        Under the old rule `run()` saw WAITING on its very first `status()`
        and returned with lane-b still PLANNED.
        """
        actor = _PassingActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self.scheduler(actor, concurrency=1)
        self._park(scheduler, "lane-a")
        status = scheduler.run()
        self.assertIs(status, st.RunStatus.WAITING)
        self.assertEqual(
            self.stages(),
            {"lane-a": st.LaneStage.WAITING_FOR_USER, "lane-b": st.LaneStage.MERGED},
        )

    def test_a_parked_lane_does_not_stop_the_workers_refilling(self) -> None:
        """The same at concurrency 2, pinning the refill specifically.

        `_advance_ready` drives lane-b every stage it can reach in one batch,
        so it is entered exactly once. Under the old rule the parked lane
        cleared the queue after each completed future, so the batch returned
        after a single stage and the outer loop had to re-enter it once per
        stage lane-b climbed.
        """
        actor = _PassingActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self.scheduler(actor, concurrency=2, cls=_CountsRefills)
        self._park(scheduler, "lane-a")
        self.assertIs(scheduler.run(), st.RunStatus.WAITING)
        self.assertEqual(
            self.stages(),
            {"lane-a": st.LaneStage.WAITING_FOR_USER, "lane-b": st.LaneStage.MERGED},
        )
        self.assertEqual(scheduler.batches, 1)
        # lane-b really did run on a worker, not on the scheduler thread.
        self.assertNotIn(threading.main_thread().name, actor.building.threads)

    def test_a_lane_needing_the_parked_lane_does_not_advance(self) -> None:
        """The termination argument: a parked lane blocks its dependents.

        `ready_lane_ids` is what makes the loop terminate without the removed
        early return, so assert it directly rather than trusting it.
        """
        actor = _PassingActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self.scheduler(actor, concurrency=1)
        self._park(scheduler, "lane-a")
        ready = self.store.ready_lane_ids(RUN_ID)
        self.assertNotIn("lane-a", ready)
        self.assertEqual(set(ready), {"lane-b"})
        # And once lane-b is finished too, nothing is advanceable at all.
        scheduler.run()
        self.assertEqual(self.store.ready_lane_ids(RUN_ID), ())
        self.assertEqual(len(LANES), 2)


FAULT_RUN_ID = "run-lane-fault"
FAULTED_LANE = "lane-faulted"
INDEPENDENT_LANE = "lane-independent"
DEPENDENT_LANE = "lane-dependent"


def _fault_plan() -> bytes:
    lanes = (
        (FAULTED_LANE, ()),
        (INDEPENDENT_LANE, ()),
        (DEPENDENT_LANE, (FAULTED_LANE,)),
    )
    return json.dumps(
        {
            "schema_version": "maestro-plan.artifact-factory.v1",
            "lanes": [
                {
                    "id": lane_id,
                    "needs": needs,
                    "outputs": [f"{lane_id}.txt"],
                    "spec": {
                        "goal": f"emit {lane_id}.txt",
                        "integration": {"integration_branch": "refs/heads/main"},
                    },
                    "acceptance": [f"{lane_id}.txt is written"],
                }
                for lane_id, needs in lanes
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _FaultingActor(_PassingActor):
    def write_tests(self, ctx: sch.LaneContext) -> dict:
        if ctx.lane.lane_id == FAULTED_LANE:
            raise RuntimeError("agent transport disconnected")
        return super().write_tests(ctx)


class _IntegrityFailingActor(_PassingActor):
    def write_tests(self, ctx: sch.LaneContext) -> dict:
        if ctx.lane.lane_id == FAULTED_LANE:
            raise sch.FactoryRefused("ledger invariant failed")
        return super().write_tests(ctx)


class LaneExecutionFaultsDoNotStopTheRun(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "product"
        state = root / "state"
        state.mkdir(mode=0o700)
        cutover._init_repo(self.repo)
        self.runtime = RuntimeStateRoot(state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)
        compiled = plan_compiler.compile_plan(
            _fault_plan(), plan_revision=1, plan_artifact_ref="plan:lane-fault"
        )
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id=FAULT_RUN_ID,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )

    def scheduler(self, actor: cutover.ScriptedActor) -> sch.FactoryScheduler:
        return sch.FactoryScheduler(
            self.store, FAULT_RUN_ID, actor, self.runtime, self.target
        )

    def test_lane_fault_waits_while_an_independent_lane_merges(self) -> None:
        status = self.scheduler(
            _FaultingActor(self.repo, self.runtime.path / "worktrees")
        ).run()

        self.assertIs(status, st.RunStatus.WAITING)
        self.assertEqual(
            {
                lane: self.store.lane_stage(FAULT_RUN_ID, lane)
                for lane in (FAULTED_LANE, INDEPENDENT_LANE, DEPENDENT_LANE)
            },
            {
                FAULTED_LANE: st.LaneStage.WAITING_FOR_USER,
                INDEPENDENT_LANE: st.LaneStage.MERGED,
                DEPENDENT_LANE: st.LaneStage.PLANNED,
            },
        )
        wait = sch._latest(
            self.store, FAULT_RUN_ID, FAULTED_LANE, st.ArtifactKind.USER_WAIT
        )
        assert wait is not None
        self.assertEqual(wait.payload["wait_reason"], st.WaitReason.LANE_FAULT.value)
        self.assertEqual(
            wait.payload["fault"],
            {
                "exception_type": "RuntimeError",
                "message": "agent transport disconnected",
            },
        )
        self.assertEqual(self.store.ready_lane_ids(FAULT_RUN_ID), ())

    def test_run_integrity_errors_still_propagate(self) -> None:
        scheduler = self.scheduler(
            _IntegrityFailingActor(self.repo, self.runtime.path / "worktrees")
        )

        with self.assertRaisesRegex(sch.FactoryRefused, "ledger invariant failed"):
            scheduler.run()

        self.assertEqual(
            self.store.lane_stage(FAULT_RUN_ID, FAULTED_LANE),
            st.LaneStage.WRITING_TESTS,
        )
        self.assertIsNone(
            sch._latest(
                self.store,
                FAULT_RUN_ID,
                FAULTED_LANE,
                st.ArtifactKind.USER_WAIT,
            )
        )

    def test_repository_binding_file_not_found_propagates(self) -> None:
        scheduler = self.scheduler(
            _PassingActor(self.repo, self.runtime.path / "worktrees")
        )
        scheduler._advance(FAULTED_LANE)

        with mock.patch.object(
            sch.gitpub,
            "revalidate_binding",
            side_effect=FileNotFoundError("repository binding disappeared"),
        ):
            with self.assertRaisesRegex(
                FileNotFoundError, "repository binding disappeared"
            ):
                scheduler._advance(FAULTED_LANE)

        self.assertEqual(
            self.store.lane_stage(FAULT_RUN_ID, FAULTED_LANE),
            st.LaneStage.WRITING_TESTS,
        )
        self.assertIsNone(
            sch._latest(
                self.store,
                FAULT_RUN_ID,
                FAULTED_LANE,
                st.ArtifactKind.USER_WAIT,
            )
        )


if __name__ == "__main__":
    unittest.main()
