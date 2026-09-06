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

import threading
import unittest

from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
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


if __name__ == "__main__":
    unittest.main()
