"""An amendment never runs agents or a suite for merged work it did not affect.

A merged lane is unaffected when its projection (spec digest, needs, declared
outputs, lane kind) is unchanged and nothing in its `needs` closure changed or
had its inputs invalidated. The integration ref is append-only -- an amendment
never rewinds it -- so an unaffected lane's merge is still in the head's
ancestry and it stays MERGED wherever it sits in integration order.

Before, only the lanes ahead of the first changed lane in integration order
stayed MERGED. An unaffected lane after it was reset and replayed; an untyped
one went TESTS_SEALED -> BUILDING, i.e. a builder, a code reviewer and a suite
run for work nothing touched.
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

from test_tests_lane_handoff import HandoffActor, _git, _init_repo, _plan_bytes  # noqa: E402

RUN = "run-keep-unaffected"


def _plan(build_goal: str) -> bytes:
    """lane-tests -> lane-build, plus lane-x (independent) and lane-y (needs lane-build).

    Integration order is lane-tests, lane-build, lane-x, lane-y: lane-x sits
    after lane-build without depending on it.
    """
    document = json.loads(_plan_bytes(build_goal=build_goal))
    for lane_id, needs, output in (
        ("lane-x", [], "x.txt"),
        ("lane-y", ["lane-build"], "y.txt"),
    ):
        document["lanes"].append(
            {
                "id": lane_id,
                "needs": needs,
                "outputs": [output],
                "spec": {
                    "goal": "emit {0}".format(output),
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["{0} is written".format(output)],
            }
        )
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _legacy_prefix(needs, roots):
    """The pre-fix policy: only the integration-order prefix stays MERGED."""
    spec = "0" * 64
    lanes = [
        st.LaneProjection(
            lane_id=lane_id,
            needs=tuple(sorted(deps)),
            spec_digest=spec,
            declared_outputs=(),
            lane_projection_digest=st.lane_projection_digest(
                spec, tuple(sorted(deps)), ()
            ),
        )
        for lane_id, deps in needs.items()
    ]
    prefix = []
    for lane_id in st.topological_integration_order(lanes):
        if lane_id in set(roots):
            break
        prefix.append(lane_id)
    return frozenset(prefix)


class AmendmentKeepsUnaffectedMergedTests(unittest.TestCase):
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
        self.actor = HandoffActor(self.repo, self.runtime.path / "worktrees")
        self.suite_runs: list[str] = []
        measure = sch.cr.measure_candidate

        def counting(**kwargs):
            self.suite_runs.append(kwargs["request"].lane_id)
            return measure(**kwargs)

        sch.cr.measure_candidate = counting  # type: ignore[assignment]
        self.addCleanup(setattr, sch.cr, "measure_candidate", measure)
        publish = sch.gitpub.publish_or_reconcile_locked

        def refuse(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("stop before publication")

        sch.gitpub.publish_or_reconcile_locked = refuse  # type: ignore[assignment]
        self.addCleanup(setattr, sch.gitpub, "publish_or_reconcile_locked", publish)

    def _compile(self, revision: int, build_goal: str) -> st.CompiledPlan:
        return plan_compiler.compile_plan(
            _plan(build_goal),
            plan_revision=revision,
            plan_artifact_ref="plan:keep-unaffected-{0}".format(revision),
        )

    def _run(self, compiled: st.CompiledPlan) -> None:
        with self.assertRaises(RuntimeError):
            sch.FactoryScheduler(
                self.store, RUN, self.actor, self.runtime, self.target, compiled=compiled
            ).run()

    def _all_merged_at_rev1(self) -> None:
        first = self._compile(1, "implement product.py")
        self.assertEqual(
            st.topological_integration_order(first.lanes),
            ("lane-tests", "lane-build", "lane-x", "lane-y"),
        )
        sch.create_factory_run(
            store=self.store,
            run_id=RUN,
            compiled=first,
            runtime=self.runtime,
            target=self.target,
        )
        self._run(first)
        for lane in first.lanes:
            self.assertEqual(
                self.store.lane_stage(RUN, lane.lane_id), st.LaneStage.MERGED
            )

    def _dispatches(self, lane_id: str) -> int:
        return sum(
            calls.count(lane_id)
            for calls in (
                self.actor.write_tests_lanes,
                self.actor.review_tests_lanes,
                self.actor.build_lanes,
                self.actor.review_code_lanes,
            )
        )

    def _artifact_ids(self, lane_id: str) -> list[str]:
        return [
            row[0]
            for row in self.store.conn.execute(
                "SELECT artifact_id FROM lane_artifacts WHERE run_id=? AND lane_id=? "
                "ORDER BY sequence",
                (RUN, lane_id),
            )
        ]

    def _assert_middle_amendment_replays_only_affected(self, second) -> None:
        x_dispatches = self._dispatches("lane-x")
        y_dispatches = self._dispatches("lane-y")
        x_suites = self.suite_runs.count("lane-x")
        x_artifacts = self._artifact_ids("lane-x")
        self._run(second)
        # lane-x: no agent, no suite, no new artifact, still MERGED.
        self.assertEqual(self._dispatches("lane-x"), x_dispatches)
        self.assertEqual(self.suite_runs.count("lane-x"), x_suites)
        self.assertEqual(self._artifact_ids("lane-x"), x_artifacts)
        self.assertEqual(self.store.lane_stage(RUN, "lane-x"), st.LaneStage.MERGED)
        # lane-y needs the changed lane: the scheduler took it through again.
        self.assertGreater(self._dispatches("lane-y"), y_dispatches)
        self.assertEqual(self.store.lane_stage(RUN, "lane-y"), st.LaneStage.MERGED)
        # The merge chain is unbroken and the ref agrees with it.
        self.assertEqual(
            _git(self.repo, "rev-parse", st.integration_ref(RUN)),
            sch.durable_integration_tip(self.store, RUN),
        )
        for path in ("x.txt", "y.txt", "product.py"):
            self.assertIn(
                path,
                _git(
                    self.repo, "ls-tree", "--name-only", st.integration_ref(RUN)
                ).split("\n"),
            )

    def test_amending_a_middle_lane_keeps_a_later_unaffected_lane_merged(self) -> None:
        self._all_merged_at_rev1()
        second = self._compile(2, "implement product.py, revised")
        sch.apply_factory_amendment(
            self.store, RUN, second, runtime=self.runtime, target=self.target
        )
        # Classified inside `run amend`, before any scheduler runs.
        stages = {
            lane.lane_id: self.store.lane_stage(RUN, lane.lane_id)
            for lane in second.lanes
        }
        self.assertEqual(stages["lane-tests"], st.LaneStage.MERGED)
        self.assertEqual(stages["lane-x"], st.LaneStage.MERGED)
        self.assertEqual(stages["lane-build"], st.LaneStage.PLANNED)
        self.assertEqual(stages["lane-y"], st.LaneStage.TESTS_SEALED)
        self._assert_middle_amendment_replays_only_affected(second)

    def test_resume_restores_a_lane_a_legacy_amendment_reset(self) -> None:
        """An amendment applied under the prefix-only policy is repaired on resume."""
        self._all_merged_at_rev1()
        second = self._compile(2, "implement product.py, revised")
        current = st.amendment_unaffected_lanes
        st.amendment_unaffected_lanes = _legacy_prefix  # type: ignore[assignment]
        try:
            sch.apply_factory_amendment(
                self.store, RUN, second, runtime=self.runtime, target=self.target
            )
        finally:
            st.amendment_unaffected_lanes = current  # type: ignore[assignment]
        self.assertEqual(
            self.store.lane_stage(RUN, "lane-x"), st.LaneStage.TESTS_SEALED
        )
        self._assert_middle_amendment_replays_only_affected(second)

    def test_resume_leaves_a_lane_that_already_moved_to_the_scheduler(self) -> None:
        self._all_merged_at_rev1()
        second = self._compile(2, "implement product.py, revised")
        current = st.amendment_unaffected_lanes
        st.amendment_unaffected_lanes = _legacy_prefix  # type: ignore[assignment]
        try:
            sch.apply_factory_amendment(
                self.store, RUN, second, runtime=self.runtime, target=self.target
            )
        finally:
            st.amendment_unaffected_lanes = current  # type: ignore[assignment]
        # The legacy replay already moved lane-x on: the scheduler owns it.
        self.store.conn.execute(
            "UPDATE lane_state SET stage=? WHERE run_id=? AND lane_id=?",
            (st.LaneStage.BUILDING.value, RUN, "lane-x"),
        )
        self.store.conn.commit()
        self.assertEqual(self.store.retain_unaffected_merges(RUN), ())
        self.assertEqual(self.store.lane_stage(RUN, "lane-x"), st.LaneStage.BUILDING)


class UnaffectedClassificationTests(unittest.TestCase):
    NEEDS = {
        "t": (),
        "b": ("t",),
        "x": (),
        "y": ("b",),
        "z": ("y", "x"),
    }

    def test_needs_closure_decides(self) -> None:
        self.assertEqual(
            st.amendment_unaffected_lanes(self.NEEDS, {"b"}), frozenset({"t", "x"})
        )
        self.assertEqual(
            st.amendment_unaffected_lanes(self.NEEDS, {"x"}),
            frozenset({"t", "b", "y"}),
        )
        self.assertEqual(
            st.amendment_unaffected_lanes(self.NEEDS, ()), frozenset(self.NEEDS)
        )

    def test_roots_are_new_moved_or_invalidated(self) -> None:
        roots = st.amendment_affected_roots(
            {"t": "1", "b": "2", "x": "3"},
            {"t": "1", "b": "2*", "x": "3", "n": "4"},
            ({"lane_id": "t", "plan_revision": 1, "input_digest": "d"},),
        )
        self.assertEqual(roots, frozenset({"b", "n", "t"}))


if __name__ == "__main__":
    unittest.main()
