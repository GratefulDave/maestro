"""Independent ready lanes advance concurrently; merges stay serialized.

`MAESTRO_architecture.md` §10: "Independent ready lanes may execute
author/review/build stages concurrently. Integration merges are serialized."
and "Concurrent first launches resolve exactly one parent and create exactly
one child per lane." Every case here observes real threads against the real
scheduler, store and launcher: a rendezvous that only two lanes executing at
the same time can satisfy, an overlap counter, a real SIGINT to the main
thread, and a fake Herdr whose creating verbs are slowed to widen the race.
"""

from __future__ import annotations

import json
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from tests import test_factory_cutover as cutover
from tests import test_herdr_workspace_topology as topo

RUN_ID = "run-independent-lanes"
LANES = ("lane-a", "lane-b")


def _independent_plan() -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": lane,
                "needs": [],
                "outputs": ["{}.txt".format(lane[-1])],
                "spec": {
                    "goal": "emit {}.txt".format(lane[-1]),
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["{}.txt is written".format(lane[-1])],
            }
            for lane in LANES
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class _Overlap:
    """How many callers are inside a section at once, and on which threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.threads: set[str] = set()

    def __enter__(self) -> "_Overlap":
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.current_thread().name)
        return self

    def __exit__(self, *exc: object) -> None:
        with self.lock:
            self.active -= 1


class _PassingActor(cutover.ScriptedActor):
    """Every review passes and the first candidate is already ready."""

    first_candidate_is_draft = False

    def __init__(self, repo: Path, worktrees: Path) -> None:
        super().__init__(repo, worktrees)
        self.authoring = _Overlap()
        self.building = _Overlap()
        self.rendezvous: threading.Barrier | None = None

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        with self.authoring:
            if self.rendezvous is not None:
                # Satisfiable only when both lanes are inside this stage at
                # the same instant. A sequential scheduler times out here.
                self.rendezvous.wait()
            return super().write_tests(ctx)

    def review_tests(self, ctx: sch.LaneContext):
        return st.ReviewerVerdict.PASS, ()

    def build(self, ctx: sch.LaneContext) -> dict:
        with self.building:
            return super().build(ctx)

    def review_code(self, ctx: sch.LaneContext):
        return st.ReviewerVerdict.PASS, ()


class _HoldingActor(_PassingActor):
    """Authoring parks every lane until the test releases it."""

    def __init__(self, repo: Path, worktrees: Path) -> None:
        super().__init__(repo, worktrees)
        self.arrived = threading.Barrier(len(LANES) + 1)
        self.release = threading.Event()
        self.workers: list[threading.Thread] = []

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        with self.authoring:
            self.workers.append(threading.current_thread())
            self.arrived.wait(30)
            if not self.release.wait(30):
                raise AssertionError("never released")
            return super().write_tests(ctx)


class _MergeWatch(sch.FactoryScheduler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.merging = _Overlap()

    def _ready_to_merge(self, lane_id: str) -> None:
        with self.merging:
            # Long enough that a second merge started meanwhile would overlap.
            time.sleep(0.05)
            super()._ready_to_merge(lane_id)


class _Run(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        cutover._init_repo(self.repo)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)
        compiled = plan_compiler.compile_plan(
            _independent_plan(), plan_revision=1, plan_artifact_ref="plan:independent"
        )
        self.target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id=RUN_ID,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )

    def scheduler(self, actor: cutover.ScriptedActor, **kwargs: object):
        cls = kwargs.pop("cls", sch.FactoryScheduler)
        return cls(self.store, RUN_ID, actor, self.runtime, self.target, **kwargs)

    def stages(self) -> dict[str, st.LaneStage]:
        return {lane: self.store.lane_stage(RUN_ID, lane) for lane in LANES}


class IndependentLanesAdvanceConcurrently(_Run):
    def test_both_lanes_author_at_the_same_time(self) -> None:
        actor = _PassingActor(self.repo, self.runtime.path / "worktrees")
        actor.rendezvous = threading.Barrier(len(LANES), timeout=10)
        status = self.scheduler(actor, concurrency=2).run()
        self.assertIs(status, st.RunStatus.COMPLETE)
        self.assertEqual(
            self.stages(), {lane: st.LaneStage.MERGED for lane in LANES}
        )
        self.assertEqual(actor.authoring.peak, 2)
        self.assertNotIn(threading.main_thread().name, actor.authoring.threads)

    def test_concurrency_one_keeps_every_stage_inline_and_sequential(self) -> None:
        actor = _PassingActor(self.repo, self.runtime.path / "worktrees")
        status = self.scheduler(actor, concurrency=1).run()
        self.assertIs(status, st.RunStatus.COMPLETE)
        self.assertEqual(actor.authoring.peak, 1)
        self.assertEqual(actor.building.peak, 1)
        self.assertEqual(actor.authoring.threads, {threading.main_thread().name})
        self.assertEqual(actor.building.threads, {threading.main_thread().name})

    def test_concurrency_below_one_is_refused(self) -> None:
        actor = _PassingActor(self.repo, self.runtime.path / "worktrees")
        with self.assertRaises(ValueError):
            self.scheduler(actor, concurrency=0)


class MergesStaySerialized(_Run):
    def test_one_merge_at_a_time_on_the_scheduler_thread(self) -> None:
        actor = _PassingActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self.scheduler(actor, concurrency=2, cls=_MergeWatch)
        self.assertIs(scheduler.run(), st.RunStatus.COMPLETE)
        self.assertEqual(
            self.stages(), {lane: st.LaneStage.MERGED for lane in LANES}
        )
        merges = [
            row[0]
            for row in self.store.conn.execute(
                "SELECT lane_id FROM lane_artifacts WHERE run_id=? AND artifact_kind=? "
                "ORDER BY sequence",
                (RUN_ID, st.ArtifactKind.INTEGRATION_MERGE.value),
            )
        ]
        self.assertEqual(sorted(merges), sorted(LANES))
        self.assertEqual(scheduler.merging.peak, 1)
        self.assertEqual(scheduler.merging.threads, {threading.main_thread().name})
        # Building takes the integration lock, so it serializes too, on the
        # workers rather than the scheduler thread.
        self.assertEqual(actor.building.peak, 1)
        self.assertNotIn(threading.main_thread().name, actor.building.threads)


class InterruptPausesEveryInFlightLane(_Run):
    def test_sigint_pauses_both_lanes_and_refuses_their_late_completion(self) -> None:
        actor = _HoldingActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self.scheduler(actor, concurrency=2)
        main_ident = threading.main_thread().ident
        assert main_ident is not None

        def interrupt_once_both_are_authoring() -> None:
            actor.arrived.wait(30)
            # The scheduler thread is parked on its futures by now.
            time.sleep(0.05)
            signal.pthread_kill(main_ident, signal.SIGINT)

        trigger = threading.Thread(target=interrupt_once_both_are_authoring)
        trigger.start()
        try:
            status = scheduler.run()
        finally:
            trigger.join(30)
        self.assertIs(status, st.RunStatus.WAITING)
        self.assertEqual(
            self.stages(), {lane: st.LaneStage.WAITING_FOR_USER for lane in LANES}
        )
        for lane in LANES:
            wait = sch._latest(self.store, RUN_ID, lane, st.ArtifactKind.USER_WAIT)
            assert wait is not None, lane
            self.assertEqual(
                wait.payload["resume_stage"], st.LaneStage.WRITING_TESTS.value
            )
        self.assertEqual(len(actor.workers), 2)
        self.assertNotIn(threading.main_thread(), actor.workers)
        # Both agents were still mid-turn when the operator interrupted. Let
        # them finish: their output arrives against a lane that is already
        # WAITING_FOR_USER and the ledger refuses it rather than advancing.
        actor.release.set()
        for worker in actor.workers:
            worker.join(30)
            self.assertFalse(worker.is_alive())
        self.assertEqual(
            self.stages(), {lane: st.LaneStage.WAITING_FOR_USER for lane in LANES}
        )
        self.assertEqual(scheduler._inflight, {})
        self.assertIsNone(scheduler._pool)


class ConcurrentFirstLaunchesCreateOneChildPerLane(unittest.TestCase):
    def test_four_lanes_racing_creation_get_one_parent_and_one_child_each(self) -> None:
        lanes = ["lane-wp{}-build".format(n) for n in range(1, 5)]
        with tempfile.TemporaryDirectory() as tmp:
            run = topo._RunFixture(tmp, operator=False)
            call_lock = threading.Lock()

            def slow_creating_verbs(*args: str, **kw: object) -> dict:
                # Widen the window between "no parent yet" / "no child yet"
                # and the object existing, so an unguarded launcher would
                # create twice (checked: it does, and refuses
                # RUN_WORKSPACE_UNBOUND on the second parent). The fake's
                # own bookkeeping stays serialized.
                if args[:2] in (topo.CREATE, topo.OPEN):
                    time.sleep(0.02)
                with call_lock:
                    return run.herdr(*args, **kw)

            launcher = run.launcher()
            launcher._herdr = slow_creating_verbs  # type: ignore[method-assign]
            specs = {lane: run.spec(lane, "builder") for lane in lanes}
            gate = threading.Barrier(len(lanes))
            outcomes: dict[str, object] = {}

            def worker(lane: str) -> None:
                gate.wait(30)
                try:
                    outcomes[lane] = topo._drive(launcher, specs[lane])
                except BaseException as exc:  # noqa: BLE001
                    outcomes[lane] = exc

            threads = [threading.Thread(target=worker, args=(lane,)) for lane in lanes]
            with topo._launch_patches():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
            handles: dict[str, lch.LaunchHandle] = {}
            for lane, outcome in outcomes.items():
                self.assertIsInstance(
                    outcome, lch.LaunchHandle, "{}: {!r}".format(lane, outcome)
                )
                assert isinstance(outcome, lch.LaunchHandle)
                handles[lane] = outcome
            self.assertEqual(len(topo._calls_after(run.herdr, 0, topo.CREATE)), 1)
            opens = topo._calls_after(run.herdr, 0, topo.OPEN)
            self.assertEqual(len(opens), len(lanes))
            self.assertEqual(
                sorted(str(topo._flag(call, "--label")) for call in opens), sorted(lanes)
            )
            parent_id = topo._assert_converged(
                self, run.herdr, run.launcher(),
                {lane: {"builder": specs[lane]} for lane in lanes},
            )
            for call in opens:
                self.assertEqual(topo._flag(call, "--workspace"), parent_id)
            self.assertEqual(
                {handles[lane].parent_workspace_id for lane in lanes}, {parent_id}
            )
            self.assertEqual(
                len({handles[lane].child_workspace_id for lane in lanes}), len(lanes)
            )


class ConcurrencyIsADeploymentKey(unittest.TestCase):
    def _load(self, extra: str) -> dict:
        template = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(
            encoding="utf-8"
        )
        lines = [
            line
            for line in template.splitlines()
            if not line.startswith(("runtime_state_root:", "concurrency:", "#"))
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = repo / "adws" / "maestro.config.yaml"
            config.parent.mkdir()
            config.write_text(
                "\n".join(lines)
                + "\nruntime_state_root: {}\n{}".format(repo / "state", extra),
                encoding="utf-8",
            )
            return maestro._load_maestro_config(repo, config)

    def test_absent_means_one(self) -> None:
        self.assertEqual(self._load("")["concurrency"], 1)

    def test_template_stamps_one(self) -> None:
        template = Path(maestro.__file__).with_name("maestro.config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("\nconcurrency: 1\n", template)

    def test_a_deployment_opts_in(self) -> None:
        self.assertEqual(self._load("concurrency: 3\n")["concurrency"], 3)

    def test_non_positive_and_non_integer_values_refuse(self) -> None:
        for bad in ("concurrency: 0\n", "concurrency: true\n", "concurrency: '2'\n", "concurrency: 1.5\n"):
            with self.subTest(bad=bad), self.assertRaises(maestro._MaestroConfigurationError):
                self._load(bad)


if __name__ == "__main__":
    unittest.main()
