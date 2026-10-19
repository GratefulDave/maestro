"""An amendment that does not change a zero-delta build lane does not rebuild it.

A build lane whose accepted candidate is zero-delta
(`BUILDER_OUTPUT.changed=false`, `tree_delta=[]`) -- its code was already in
the integration head when it was rebuilt -- is reset MERGED -> READY_TO_MERGE
by every amendment that does not name it, as designed. The amendment then
re-merges the retained lanes, which moves the integration head's SHA.
`decide_merge_action` compared `base_head != integration_head`, so each time it
wrote BASE_INVALIDATION and the lane went back to its builder, which produced
the same zero delta again: one full builder and reviewer round per amendment,
while its declared outputs, and those of every lane it needs, stayed
byte-identical.

Staleness is now a content question: a zero-delta candidate whose inputs (its
declared outputs and those of its `needs` closure) hold the same blobs at the
new head revalidates there. A real change to one of them still invalidates. A
changed candidate never asks.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import plan_compiler  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules.lifecycle import ArtifactStore  # noqa: E402
from adw_modules.runtime_state import RuntimeStateRoot  # noqa: E402

from test_tests_lane_handoff import (  # noqa: E402
    HandoffActor,
    _git,
    _init_repo,
    _plan_bytes,
)

RUN = "run-zero-delta-replay"


def _three_lane_plan(*, build_goal: str, other_goal: str = "emit x.txt") -> bytes:
    """The typed pair from the handoff fixture plus an independent lane.

    lane-x shares nothing with lane-build, so an amendment that names it is an
    amendment that does not change lane-build.
    """
    document = json.loads(_plan_bytes(build_goal=build_goal))
    document["lanes"].append(
        {
            "id": "lane-x",
            "needs": [],
            "outputs": ["x.txt"],
            "spec": {
                "goal": other_goal,
                "integration": {"integration_branch": "refs/heads/main"},
            },
            "acceptance": ["x.txt is written"],
        }
    )
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ZeroDeltaActor(HandoffActor):
    """Writes a lane's output once; afterwards finds it in the base and changes nothing."""

    def build(self, ctx: sch.LaneContext) -> dict:
        self.build_lanes.append(ctx.lane.lane_id)
        self.building_entries.append((ctx.lane.lane_id, ctx.entry_kind))
        output = ctx.lane.declared_outputs[0]
        present = _git(self.repo, "ls-tree", "--name-only", ctx.builder_base_sha)
        if output in present.split("\n"):
            return {"candidate_sha": ctx.builder_base_sha, "changed": False}
        work = self.worktrees / ctx.lane.lane_id / ctx.input_digest[:12]
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        (work / output).write_text("ready\n", encoding="utf-8")
        _git(work, "add", output)
        _git(work, "commit", "-m", output)
        return {"candidate_sha": _git(work, "rev-parse", "HEAD"), "changed": True}

    def review_code(self, ctx: sch.LaneContext):
        self.review_code_lanes.append(ctx.lane.lane_id)
        return st.ReviewerVerdict.PASS, ()


def _kinds(store: ArtifactStore, lane_id: str) -> list[tuple[str, int]]:
    return [
        (row[0], row[1])
        for row in store.conn.execute(
            "SELECT artifact_kind, plan_revision FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? ORDER BY sequence",
            (RUN, lane_id),
        )
    ]


class ZeroDeltaReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "product"
        state = root / "state"
        state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        self.actor = ZeroDeltaActor(self.repo, self.runtime.path / "worktrees")

    def _compile(
        self, revision: int, build_goal: str, other_goal: str = "emit x.txt"
    ) -> st.CompiledPlan:
        return plan_compiler.compile_plan(
            _three_lane_plan(build_goal=build_goal, other_goal=other_goal),
            plan_revision=revision,
            plan_artifact_ref="plan:zero-delta-{0}".format(revision),
        )

    def _amend(self, compiled: st.CompiledPlan) -> None:
        sch.apply_factory_amendment(
            self.store, RUN, compiled, runtime=self.runtime, target=self.target
        )

    def _scheduler(self, compiled: st.CompiledPlan) -> sch.FactoryScheduler:
        return sch.FactoryScheduler(
            self.store, RUN, self.actor, self.runtime, self.target, compiled=compiled
        )

    def _stop_before_publication(self) -> None:
        original = sch.gitpub.publish_or_reconcile_locked

        def refuse(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("stop before publication")

        sch.gitpub.publish_or_reconcile_locked = refuse  # type: ignore[method-assign]
        self.addCleanup(
            setattr, sch.gitpub, "publish_or_reconcile_locked", original
        )

    def _zero_delta_lane_merged(self) -> st.CompiledPlan:
        """Revision 2 leaves lane-build MERGED on a zero-delta candidate.

        Revision 1 builds product.py; revision 2 changes the build lane's goal,
        so it is rebuilt on a base that already holds product.py and returns no
        delta.
        """
        self._stop_before_publication()
        first = self._compile(1, "implement product.py")
        sch.create_factory_run(
            store=self.store,
            run_id=RUN,
            compiled=first,
            runtime=self.runtime,
            target=self.target,
        )
        with self.assertRaises(RuntimeError):
            self._scheduler(first).run()
        second = self._compile(2, "implement product.py, revised")
        self._amend(second)
        with self.assertRaises(RuntimeError):
            self._scheduler(second).run()
        latest = sch._latest(
            self.store, RUN, "lane-build", st.ArtifactKind.BUILDER_OUTPUT
        )
        assert latest is not None
        self.assertIs(latest.payload["changed"], False)
        self.assertEqual(
            self.store.lane_stage(RUN, "lane-build"), st.LaneStage.MERGED
        )
        return second

    def test_unchanged_zero_delta_lane_revalidates_after_replay(self) -> None:
        second = self._zero_delta_lane_merged()
        builds = self.actor.build_lanes.count("lane-build")
        head_before = sch.durable_integration_tip(self.store, RUN)
        # Revision 3 names lane-x only; lane-build's projection is unchanged.
        third = self._compile(3, "implement product.py, revised", "emit x.txt again")
        old = {lane.lane_id: lane.lane_projection_digest for lane in second.lanes}
        new = {lane.lane_id: lane.lane_projection_digest for lane in third.lanes}
        self.assertEqual(old["lane-build"], new["lane-build"])
        self.assertNotEqual(old["lane-x"], new["lane-x"])
        self._amend(third)
        self.assertEqual(
            self.store.lane_stage(RUN, "lane-build"), st.LaneStage.READY_TO_MERGE
        )
        with self.assertRaises(RuntimeError):
            self._scheduler(third).run()

        # The tests lane re-merged, so the head SHA moved...
        self.assertNotEqual(sch.durable_integration_tip(self.store, RUN), head_before)
        # ...and lane-build merged again without a builder round.
        self.assertEqual(self.actor.build_lanes.count("lane-build"), builds)
        self.assertIn("lane-x", self.actor.build_lanes[-1:])
        self.assertEqual(
            self.store.lane_stage(RUN, "lane-build"), st.LaneStage.MERGED
        )
        at_three = [kind for kind, rev in _kinds(self.store, "lane-build") if rev == 3]
        self.assertEqual(at_three, [st.ArtifactKind.INTEGRATION_MERGE.value])
        merge = sch._latest(
            self.store, RUN, "lane-build", st.ArtifactKind.INTEGRATION_MERGE
        )
        assert merge is not None
        self.assertIs(merge.payload["revalidated"], True)
        # Anchored on the head it was revalidated against, not on its own
        # candidate: `durable_integration_tip` walks the chain and refuses
        # "revalidation not anchored" otherwise, and the ref agrees with it.
        self.assertEqual(merge.payload["before_sha"], merge.payload["after_sha"])
        self.assertNotEqual(merge.payload["candidate_sha"], merge.payload["after_sha"])
        self.assertEqual(
            sch.durable_integration_tip(self.store, RUN),
            _git(self.repo, "rev-parse", st.integration_ref(RUN)),
        )

    def test_a_ready_to_merge_lane_keeps_its_candidate_across_an_amendment(
        self,
    ) -> None:
        """An unchanged lane between its PASS and its merge keeps its candidate."""
        self.assertIs(
            st.amendment_reset_stage(
                st.LaneStage.READY_TO_MERGE,
                changed=False,
                wait_reason=None,
                lane_kind=st.LANE_KIND_BUILD,
            ),
            st.LaneStage.READY_TO_MERGE,
        )

    def _decide(self, base_head: str, head: str) -> gitpub.MergeDecision:
        lane = next(
            item
            for item in self.store.active_projection(RUN)
            if item.lane_id == "lane-build"
        )
        builder = sch._latest(
            self.store, RUN, "lane-build", st.ArtifactKind.BUILDER_OUTPUT
        )
        assert builder is not None
        return gitpub.decide_merge_action(
            changed=bool(builder.payload["changed"]),
            builder_base_sha=base_head,
            candidate_sha=base_head,
            integration_head=head,
            base_head=base_head,
            inputs_unchanged=sch._zero_delta_inputs_unchanged(
                self.store, self.target, RUN, lane, builder, base_head, head
            ),
        )

    def _commit_on(self, base: str, path: str, text: str) -> str:
        work = Path(self.tmp.name) / "edit-{0}".format(path.replace("/", "_"))
        _git(self.repo, "worktree", "add", "--detach", str(work), base)
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _git(work, "add", path)
        _git(work, "commit", "-m", path)
        return _git(work, "rev-parse", "HEAD")

    def test_a_base_whose_inputs_changed_still_invalidates(self) -> None:
        self._zero_delta_lane_merged()
        base = sch.durable_integration_tip(self.store, RUN)
        own = self._commit_on(base, "product.py", "rewritten ready\n")
        self.assertEqual(self._decide(base, own).action, "BASE_INVALIDATION")
        suite = self._commit_on(
            base, "tests/public_contract.py", "def test_x():\n    pass\n"
        )
        self.assertEqual(self._decide(base, suite).action, "BASE_INVALIDATION")

    def test_a_base_that_moved_elsewhere_revalidates(self) -> None:
        self._zero_delta_lane_merged()
        base = sch.durable_integration_tip(self.store, RUN)
        elsewhere = self._commit_on(base, "unrelated.txt", "x\n")
        self.assertEqual(self._decide(base, elsewhere).action, "REVALIDATE")

    def test_a_changed_candidate_is_unaffected(self) -> None:
        self._zero_delta_lane_merged()
        base = sch.durable_integration_tip(self.store, RUN)
        moved = self._commit_on(base, "product.py", "rewritten ready\n")
        candidate = self._commit_on(base, "unrelated.txt", "x\n")
        lane = next(
            item
            for item in self.store.active_projection(RUN)
            if item.lane_id == "lane-build"
        )
        changed = sch._latest(
            self.store, RUN, "lane-build", st.ArtifactKind.BUILDER_OUTPUT
        )
        assert changed is not None
        changed = sch.ArtifactRecord(
            **{**changed.__dict__, "payload": {**changed.payload, "changed": True}}
        )
        self.assertFalse(
            sch._zero_delta_inputs_unchanged(
                self.store, self.target, RUN, lane, changed, base, moved
            )
        )
        decision = gitpub.decide_merge_action(
            changed=True,
            builder_base_sha=base,
            candidate_sha=candidate,
            integration_head=moved,
            base_head=base,
        )
        self.assertEqual(decision.action, "MERGE")


if __name__ == "__main__":
    unittest.main()
