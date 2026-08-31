"""Tests-lane seal handoff and typed lifecycle routing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot

FINDING = {
    "implementation_area": "product",
    "observed_behavior": "output missing required behavior",
    "required_behavior": "behavior is asserted",
    "violated_requirement": "public acceptance",
}
SECRET = "secret-handoff-token"
V2_PLAN = Path(
    "/Users/davidandrews/PycharmProjects/.worktrees/fdadb/integration"
    "/.maestro/plans/fdadb-v2-wp6-geo-layer-r2.v2.json"
)
V3_PLAN = V2_PLAN.with_name("fdadb-v2-wp6-geo-layer-r2.v3.json")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")


def _plan_bytes(
    *,
    tests_goal: str = "author hidden tests",
    build_goal: str = "implement product.py",
) -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-tests",
                "lane_kind": "tests",
                "needs": [],
                "outputs": ["tests/public_contract.py"],
                "spec": {
                    "goal": tests_goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["declared test contract exists"],
            },
            {
                "id": "lane-build",
                "lane_kind": "build",
                "needs": ["lane-tests"],
                "outputs": ["product.py"],
                "spec": {
                    "goal": build_goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["product.py is written"],
            },
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")



def _unified_plan_bytes(*, goal: str = "emit a.txt") -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["a.txt is written"],
            }
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")



def _two_lane_plan_bytes(*, a_goal: str = "emit a.txt", b_goal: str = "emit b.txt") -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": a_goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["a.txt is written"],
            },
            {
                "id": "lane-b",
                "needs": ["lane-a"],
                "outputs": ["b.txt"],
                "spec": {
                    "goal": b_goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["b.txt is written"],
            },
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")

class HandoffActor:
    def __init__(self, repo: Path, worktrees: Path) -> None:
        self.repo = repo
        self.worktrees = worktrees
        self.write_tests_lanes: list[str] = []
        self.review_tests_lanes: list[str] = []
        self.build_lanes: list[str] = []
        self.review_code_lanes: list[str] = []
        self.building_entries: list[tuple[str, st.BuildingEntryKind]] = []
        self.code_rounds: dict[str, int] = defaultdict(int)
        self.builder_contracts: list[dict] = []

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        self.write_tests_lanes.append(ctx.lane.lane_id)
        if ctx.lane.lane_kind == st.LANE_KIND_TESTS:
            files = {
                path: (
                    "# {0}\nfrom pathlib import Path\n"
                    "def test_product_exists():\n"
                    "    assert Path('product.py').is_file()\n"
                ).format(SECRET)
                for path in ctx.lane.declared_outputs
            }
            return {"files": files, "private_tokens": (SECRET,)}
        return {
            "files": {
                "tests/test_{0}_private.py".format(ctx.lane.lane_id.replace("-", "_")): (
                    "# {0}\nfrom pathlib import Path\n"
                    "def test_output_exists():\n"
                    "    assert Path({1!r}).is_file()\n"
                ).format(SECRET, ctx.lane.declared_outputs[0])
            },
            "private_tokens": (SECRET,),
        }

    def review_tests(self, ctx: sch.LaneContext):
        self.review_tests_lanes.append(ctx.lane.lane_id)
        return st.ReviewerVerdict.PASS, ()

    def build(self, ctx: sch.LaneContext) -> dict:
        self.build_lanes.append(ctx.lane.lane_id)
        self.building_entries.append((ctx.lane.lane_id, ctx.entry_kind))
        self.builder_contracts.append(ctx.public_contract)
        work = self.worktrees / ctx.lane.lane_id / ctx.input_digest[:12]
        if work.exists():
            _git(self.repo, "worktree", "remove", "--force", str(work))
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        for path in ctx.lane.declared_outputs:
            dest = work / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                "{0}:{1}\n".format(ctx.lane.lane_id, ctx.input_digest),
                encoding="utf-8",
            )
            _git(work, "add", path)
        _git(work, "commit", "-m", ctx.lane.lane_id)
        return {
            "candidate_sha": _git(work, "rev-parse", "HEAD"),
            "changed": True,
        }

    def review_code(self, ctx: sch.LaneContext):
        self.review_code_lanes.append(ctx.lane.lane_id)
        n = self.code_rounds[ctx.lane.lane_id]
        self.code_rounds[ctx.lane.lane_id] += 1
        if n == 0:
            return st.ReviewerVerdict.REVISE, (FINDING,)
        return st.ReviewerVerdict.PASS, ()

    def review_integration(self, ctx, lanes, integration_sha):
        del ctx, lanes, integration_sha
        return st.ReviewerVerdict.PASS, (), ()

    def publish(self, ctx, *, fingerprint, expected_before, published_sha):
        del expected_before
        return {
            "receipt_object": published_sha,
            "receipt_ref": st.publication_ref(ctx.run_id, fingerprint),
        }

    def complete_run_spaces(self, run_id: str) -> None:
        del run_id


class NoopActor(HandoffActor):
    def __init__(self, repo: Path, worktrees: Path) -> None:
        super().__init__(repo, worktrees)
        self.first_sha = ""

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        self.write_tests_lanes.append(ctx.lane.lane_id)
        return {
            "files": {
                "tests/test_a_private.py": (
                    "from pathlib import Path\ndef test_a():\n"
                    "    assert Path('a.txt').is_file()\n"
                )
            }
        }

    def build(self, ctx: sch.LaneContext) -> dict:
        self.build_lanes.append(ctx.lane.lane_id)
        self.building_entries.append((ctx.lane.lane_id, ctx.entry_kind))
        if ctx.entry_kind is st.BuildingEntryKind.CODE_REVISE:
            return {"candidate_sha": self.first_sha, "changed": True}
        work = self.worktrees / ctx.lane.lane_id / ctx.input_digest[:12]
        if work.exists():
            _git(self.repo, "worktree", "remove", "--force", str(work))
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        (work / "a.txt").write_text("a\n", encoding="utf-8")
        _git(work, "add", "a.txt")
        _git(work, "commit", "-m", "a")
        self.first_sha = _git(work, "rev-parse", "HEAD")
        return {"candidate_sha": self.first_sha, "changed": True}

    def review_code(self, ctx: sch.LaneContext):
        return super().review_code(ctx)


class TestsLaneHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)

    def _start(self, compiled: st.CompiledPlan, actor, run_id: str) -> sch.FactoryScheduler:
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        return sch.FactoryScheduler(
            self.store,
            run_id,
            actor,
            self.runtime,
            target,
            compiled=compiled,
        )

    def test_next_stage_uses_existing_lane_kinds(self) -> None:
        self.assertEqual(
            st.next_stage_for(
                st.LaneStage.TESTS_SEALED,
                st.ArtifactKind.SEALED_TEST_BUNDLE,
                None,
            ),
            st.LaneStage.BUILDING,
        )
        self.assertEqual(
            st.next_stage_for(
                st.LaneStage.TESTS_SEALED,
                st.ArtifactKind.SEALED_TEST_BUNDLE,
                None,
                lane_kind="tests",
            ),
            st.LaneStage.MERGED,
        )
        self.assertEqual(
            st.next_stage_for(
                st.LaneStage.PLANNED,
                st.ArtifactKind.LANE_PLAN,
                None,
                lane_kind="build",
            ),
            st.LaneStage.BUILDING,
        )

    def test_tests_lane_seals_then_build_lane_implements(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:handoff"
        )
        actor = HandoffActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-handoff")
        status = scheduler.run()
        self.assertEqual(status, st.RunStatus.COMPLETE)
        self.assertEqual(actor.write_tests_lanes, ["lane-tests"])
        self.assertEqual(actor.review_tests_lanes, ["lane-tests"])
        self.assertEqual(actor.build_lanes, ["lane-build", "lane-build"])
        self.assertEqual(actor.review_code_lanes, ["lane-build", "lane-build"])
        self.assertEqual(
            actor.building_entries,
            [
                ("lane-build", st.BuildingEntryKind.INITIAL),
                ("lane-build", st.BuildingEntryKind.CODE_REVISE),
            ],
        )
        self.assertEqual(
            self.store.lane_stage("run-handoff", "lane-tests"), st.LaneStage.MERGED
        )
        self.assertEqual(
            self.store.lane_stage("run-handoff", "lane-build"), st.LaneStage.MERGED
        )
        self.assertTrue((self.repo / "product.py").is_file())
        self.assertFalse((self.repo / "tests/public_contract.py").exists())
        dumped = json.dumps(actor.builder_contracts)
        self.assertNotIn(SECRET, dumped)
        for contract in actor.builder_contracts:
            self.assertEqual(contract["declared_outputs"], ["product.py"])
            self.assertNotIn("tests/public_contract.py", json.dumps(contract))
        tests_kinds = {
            row[0]
            for row in self.store.conn.execute(
                "SELECT artifact_kind FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=?",
                ("run-handoff", "lane-tests"),
            )
        }
        self.assertIn(st.ArtifactKind.SEALED_TEST_BUNDLE.value, tests_kinds)
        self.assertIn(st.ArtifactKind.TEST_DRAFT.value, tests_kinds)
        self.assertIn(st.ArtifactKind.TEST_REVIEW.value, tests_kinds)
        self.assertNotIn(st.ArtifactKind.BUILDER_OUTPUT.value, tests_kinds)
        self.assertNotIn(st.ArtifactKind.CODE_REVIEW.value, tests_kinds)
        self.assertNotIn(st.ArtifactKind.INTEGRATION_MERGE.value, tests_kinds)
        build_kinds = {
            row[0]
            for row in self.store.conn.execute(
                "SELECT artifact_kind FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=?",
                ("run-handoff", "lane-build"),
            )
        }
        self.assertIn(st.ArtifactKind.BUILDER_OUTPUT.value, build_kinds)
        self.assertIn(st.ArtifactKind.CODE_REVIEW.value, build_kinds)
        self.assertIn(st.ArtifactKind.INTEGRATION_MERGE.value, build_kinds)
        self.assertNotIn(st.ArtifactKind.TEST_DRAFT.value, build_kinds)
        self.assertNotIn(st.ArtifactKind.TEST_REVIEW.value, build_kinds)
        reviews = list(
            self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=?",
                (
                    "run-handoff",
                    "lane-build",
                    st.ArtifactKind.CODE_REVIEW.value,
                ),
            )
        )
        self.assertGreaterEqual(len(reviews), 1)
        summary = json.loads(reviews[0][0])["public_result_summary"]
        self.assertGreaterEqual(summary["executed"], 1)

    def test_typed_build_review_uses_tests_lane_vitest_gate(self) -> None:
        document = {
            "schema_version": "maestro-plan.artifact-factory.v1",
            "lanes": [
                {
                    "id": "lane-tests",
                    "lane_kind": "tests",
                    "needs": [],
                    "outputs": ["suite.test.ts"],
                    "spec": {
                        "goal": "author hidden vitest suite",
                        "integration": {"integration_branch": "refs/heads/main"},
                        "gate": {
                            "runner": "vitest",
                            "argv": ["suite.test.ts"],
                            "cwd": ".",
                            "min_cases": 1,
                        },
                    },
                    "acceptance": ["declared test contract exists"],
                },
                {
                    "id": "lane-build",
                    "lane_kind": "build",
                    "needs": ["lane-tests"],
                    "outputs": ["product.py"],
                    "spec": {
                        "goal": "implement product.py",
                        "integration": {"integration_branch": "refs/heads/main"},
                        "gate": {
                            "runner": "pytest",
                            "argv": ["build-wrong.py"],
                            "cwd": ".",
                            "min_cases": 9,
                        },
                    },
                    "acceptance": ["product.py is written"],
                },
            ],
        }
        compiled = plan_compiler.compile_plan(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            plan_revision=1,
            plan_artifact_ref="plan:vitest-handoff",
        )
        bindir = self.root / "fake-bin"
        bindir.mkdir()
        binary = bindir / "vitest"
        binary.write_text(
            "#!{python}\n"
            "import sys\n"
            "from pathlib import Path\n"
            "stamp = Path(__file__).with_name('vitest.calls')\n"
            "prior = stamp.read_text() if stamp.exists() else ''\n"
            "stamp.write_text(prior + ' '.join(sys.argv[1:]) + '\\n')\n"
            "args = sys.argv[1:]\n"
            "if '--version' in args:\n"
            "    print('vitest/3.2.7')\n"
            "    raise SystemExit(0)\n"
            "if 'list' in args:\n"
            "    print('suite.test.ts > ok')\n"
            "    raise SystemExit(0)\n"
            "if 'run' in args:\n"
            "    print(' Test Files  1 passed (1)')\n"
            "    print('      Tests  1 passed (1)')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n".format(python=sys.executable),
            encoding="utf-8",
        )
        binary.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)
        os.environ["PATH"] = str(bindir) + os.pathsep + old_path
        actor = HandoffActor(self.repo, self.runtime.path / "worktrees")
        actor.lane_specs = {
            "lane-tests": document["lanes"][0]["spec"],
            "lane-build": document["lanes"][1]["spec"],
        }
        scheduler = self._start(compiled, actor, "run-vitest-handoff")
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        calls = binary.with_name("vitest.calls").read_text(encoding="utf-8")
        self.assertIn("run", calls)
        self.assertIn("suite.test.ts", calls)
        self.assertNotIn("pytest", calls)
        self.assertNotIn("build-wrong.py", calls)

    def test_typed_build_sealed_suite_gate_uses_tests_predecessor(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:gate-auth"
        )
        actor = HandoffActor(self.repo, self.runtime.path / "worktrees")
        actor.lane_specs = {
            "lane-tests": {
                "goal": "author hidden tests",
                "integration": {"integration_branch": "refs/heads/main"},
                "gate": {
                    "runner": "vitest",
                    "argv": ["suite.test.ts"],
                    "cwd": ".",
                    "min_cases": 1,
                },
            },
            "lane-build": {
                "goal": "implement product.py",
                "integration": {"integration_branch": "refs/heads/main"},
                "gate": {
                    "runner": "pytest",
                    "argv": ["build-wrong.py"],
                    "cwd": ".",
                    "min_cases": 9,
                },
            },
        }
        scheduler = self._start(compiled, actor, "run-gate-auth")
        build = next(lane for lane in compiled.lanes if lane.lane_id == "lane-build")
        gate = scheduler._sealed_suite_gate(build)
        self.assertIsNotNone(gate)
        self.assertEqual(gate.runner, "vitest")
        self.assertEqual(gate.argv, ("suite.test.ts",))
        self.assertEqual(gate.min_cases, 1)
        tests = next(lane for lane in compiled.lanes if lane.lane_id == "lane-tests")
        self.assertEqual(scheduler._sealed_suite_gate(tests).runner, "vitest")
        unified = plan_compiler.compile_plan(
            _unified_plan_bytes(), plan_revision=1, plan_artifact_ref="plan:gate-unified"
        )
        unified_actor = HandoffActor(self.repo, self.runtime.path / "worktrees")
        unified_actor.lane_specs = {
            "lane-a": {
                "goal": "emit a.txt",
                "integration": {"integration_branch": "refs/heads/main"},
                "gate": {
                    "runner": "pytest",
                    "argv": ["tests/test_a.py"],
                    "cwd": ".",
                    "min_cases": 2,
                },
            }
        }
        unified_scheduler = sch.FactoryScheduler(
            self.store,
            "run-gate-auth",
            unified_actor,
            self.runtime,
            gitpub.bind_target_worktree(self.repo, "refs/heads/main"),
            compiled=unified,
        )
        own = unified_scheduler._sealed_suite_gate(unified.lanes[0])
        self.assertEqual(own.runner, "pytest")
        self.assertEqual(own.argv, ("tests/test_a.py",))
        self.assertEqual(own.min_cases, 2)

    def test_untyped_lane_keeps_universal_lifecycle(self) -> None:
        compiled = plan_compiler.compile_plan(
            _unified_plan_bytes(), plan_revision=1, plan_artifact_ref="plan:unified"
        )
        actor = HandoffActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-unified")
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        self.assertEqual(actor.write_tests_lanes, ["lane-a"])
        self.assertEqual(actor.review_tests_lanes, ["lane-a"])
        self.assertEqual(actor.build_lanes, ["lane-a", "lane-a"])
        kinds = [
            row[0]
            for row in self.store.conn.execute(
                "SELECT artifact_kind FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? ORDER BY sequence",
                ("run-unified", "lane-a"),
            )
        ]
        self.assertIn(st.ArtifactKind.TEST_DRAFT.value, kinds)
        self.assertIn(st.ArtifactKind.SEALED_TEST_BUNDLE.value, kinds)
        self.assertIn(st.ArtifactKind.BUILDER_OUTPUT.value, kinds)
        self.assertIn(st.ArtifactKind.INTEGRATION_MERGE.value, kinds)
        self.assertLess(
            kinds.index(st.ArtifactKind.SEALED_TEST_BUNDLE.value),
            kinds.index(st.ArtifactKind.BUILDER_OUTPUT.value),
        )

    def test_code_revise_can_resubmit_byte_identical_builder_output(self) -> None:
        compiled = plan_compiler.compile_plan(
            _unified_plan_bytes(), plan_revision=1, plan_artifact_ref="plan:noop"
        )
        actor = NoopActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-noop")
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        self.assertEqual(self.store.lane_stage("run-noop", "lane-a"), st.LaneStage.MERGED)
        self.assertEqual(
            actor.building_entries,
            [
                ("lane-a", st.BuildingEntryKind.INITIAL),
                ("lane-a", st.BuildingEntryKind.CODE_REVISE),
            ],
        )
        builders = list(
            self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=? ORDER BY sequence",
                ("run-noop", "lane-a", st.ArtifactKind.BUILDER_OUTPUT.value),
            )
        )
        self.assertEqual(len(builders), 2)
        self.assertEqual(
            [json.loads(row[0])["candidate_sha"] for row in builders],
            [actor.first_sha, actor.first_sha],
        )
        reviews = list(
            self.store.conn.execute(
                "SELECT artifact_id FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=?",
                ("run-noop", "lane-a", st.ArtifactKind.CODE_REVIEW.value),
            )
        )
        self.assertEqual(len(reviews), 2)

    def test_amendment_changed_paused_resets_planned(self) -> None:
        self.assertEqual(
            st.amendment_reset_stage(
                st.LaneStage.WAITING_FOR_USER,
                changed=True,
                wait_reason=st.WaitReason.PAUSE,
            ),
            st.LaneStage.PLANNED,
        )
        self.assertEqual(
            st.amendment_reset_stage(
                st.LaneStage.WAITING_FOR_USER,
                changed=False,
                wait_reason=st.WaitReason.PAUSE,
            ),
            st.LaneStage.WAITING_FOR_USER,
        )
        self.assertEqual(
            st.amendment_reset_stage(
                st.LaneStage.MERGED,
                changed=False,
                wait_reason=None,
                lane_kind="tests",
            ),
            st.LaneStage.TESTS_SEALED,
        )
        self.assertEqual(
            st.amendment_reset_stage(
                st.LaneStage.MERGED,
                changed=True,
                wait_reason=None,
                lane_kind="tests",
            ),
            st.LaneStage.PLANNED,
        )
        self.assertEqual(
            st.amendment_reset_stage(
                st.LaneStage.WAITING_FOR_USER,
                changed=False,
                wait_reason=st.WaitReason.PAUSE,
                lane_kind="tests",
            ),
            st.LaneStage.WAITING_FOR_USER,
        )
        self.assertEqual(
            st.amendment_reset_stage(
                st.LaneStage.MERGED,
                changed=False,
                wait_reason=None,
            ),
            st.LaneStage.TESTS_SEALED,
        )
        self.assertEqual(
            st.amendment_reset_stage(
                st.LaneStage.BUILDING,
                changed=False,
                wait_reason=None,
                lane_kind="build",
            ),
            st.LaneStage.BUILDING,
        )


        compiled = plan_compiler.compile_plan(
            _two_lane_plan_bytes(), plan_revision=1, plan_artifact_ref="plan:pause"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id="run-pause",
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        row = sch.run_row(self.store, "run-pause")
        lane = next(item for item in compiled.lanes if item.lane_id == "lane-a")
        digest = st.planned_input_digest(
            run_id="run-pause",
            lane_id=lane.lane_id,
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            spec_digest=lane.spec_digest,
            projection_digest=lane.lane_projection_digest,
            plan_artifact_ref=sch.plan_artifact_ref_for(
                self.store, "run-pause", row["plan_revision"]
            ),
            needs=lane.needs,
            declared_outputs=lane.declared_outputs,
        )
        self.store.pause_lane(
            "run-pause", lane.lane_id, st.LaneStage.PLANNED, digest
        )
        sibling = plan_compiler.compile_plan(
            _two_lane_plan_bytes(b_goal="emit b.txt with more detail"),
            plan_revision=2,
            plan_artifact_ref="plan:pause-sibling",
        )
        sch.apply_factory_amendment(
            self.store, "run-pause", sibling, runtime=self.runtime, target=target
        )
        self.assertEqual(
            self.store.lane_stage("run-pause", "lane-a"),
            st.LaneStage.WAITING_FOR_USER,
        )
        changed = plan_compiler.compile_plan(
            _two_lane_plan_bytes(
                a_goal="emit a.txt with more detail",
                b_goal="emit b.txt with more detail",
            ),
            plan_revision=3,
            plan_artifact_ref="plan:pause-changed",
        )
        sch.apply_factory_amendment(
            self.store, "run-pause", changed, runtime=self.runtime, target=target
        )
        self.assertEqual(
            self.store.lane_stage("run-pause", "lane-a"), st.LaneStage.PLANNED
        )

    def test_typed_kind_reconstructs_without_dag_column(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:kind-col"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id="run-kind-col",
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        columns = {
            row[1]
            for row in self.store.conn.execute("PRAGMA table_info(dag_lanes)")
        }
        self.assertNotIn("lane_kind", columns)
        kinds = {
            lane.lane_id: lane.lane_kind
            for lane in self.store.active_projection("run-kind-col")
        }
        self.assertEqual(
            kinds, {"lane-tests": "tests", "lane-build": "build"}
        )
        self.assertEqual(
            self.store._lane_kind("run-kind-col", "lane-tests"), "tests"
        )

    def test_amended_tests_lane_does_not_reuse_stale_bundle(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:stale-bundle"
        )

        class StopOnBuild(HandoffActor):
            def build(self, ctx: sch.LaneContext):
                del ctx
                raise RuntimeError("stop-before-build")

        actor = StopOnBuild(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-stale-bundle")
        with self.assertRaisesRegex(RuntimeError, "stop-before-build"):
            scheduler.run()
        self.assertEqual(
            self.store.lane_stage("run-stale-bundle", "lane-tests"),
            st.LaneStage.MERGED,
        )
        stale = self.store.conn.execute(
            "SELECT artifact_id, plan_revision FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind=?",
            (
                "run-stale-bundle",
                "lane-tests",
                st.ArtifactKind.SEALED_TEST_BUNDLE.value,
            ),
        ).fetchone()
        self.assertIsNotNone(stale)
        self.assertEqual(stale[1], 1)
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        amended = plan_compiler.compile_plan(
            _plan_bytes(tests_goal="author revised hidden tests"),
            plan_revision=2,
            plan_artifact_ref="plan:stale-bundle-2",
        )
        sch.apply_factory_amendment(
            self.store,
            "run-stale-bundle",
            amended,
            runtime=self.runtime,
            target=target,
        )
        scheduler = sch.FactoryScheduler(
            self.store,
            "run-stale-bundle",
            actor,
            self.runtime,
            target,
            compiled=amended,
        )
        build = next(lane for lane in amended.lanes if lane.lane_id == "lane-build")
        with self.assertRaises(sch.FactoryRefused) as raised:
            scheduler._sealed_for(build)
        self.assertIn("missing dependency sealed tests", str(raised.exception))

    def test_unchanged_tests_reseal_before_build_after_build_amendment(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:reseal"
        )

        class StopOnBuild(HandoffActor):
            def build(self, ctx: sch.LaneContext):
                del ctx
                raise RuntimeError("stop-before-build")

        actor = StopOnBuild(self.repo, self.runtime.path / "worktrees")
        scheduler = self._start(compiled, actor, "run-reseal")
        with self.assertRaisesRegex(RuntimeError, "stop-before-build"):
            scheduler.run()
        self.assertEqual(
            self.store.lane_stage("run-reseal", "lane-tests"),
            st.LaneStage.MERGED,
        )
        stale = self.store.conn.execute(
            "SELECT artifact_id, plan_revision FROM lane_artifacts "
            "WHERE run_id=? AND lane_id=? AND artifact_kind=? "
            "ORDER BY sequence",
            (
                "run-reseal",
                "lane-tests",
                st.ArtifactKind.SEALED_TEST_BUNDLE.value,
            ),
        ).fetchone()
        self.assertIsNotNone(stale)
        self.assertEqual(stale[1], 1)
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        amended = plan_compiler.compile_plan(
            _plan_bytes(build_goal="implement product.py with extra detail"),
            plan_revision=2,
            plan_artifact_ref="plan:reseal-2",
        )
        tests_digest = next(
            lane.lane_projection_digest
            for lane in compiled.lanes
            if lane.lane_id == "lane-tests"
        )
        amended_tests_digest = next(
            lane.lane_projection_digest
            for lane in amended.lanes
            if lane.lane_id == "lane-tests"
        )
        self.assertEqual(tests_digest, amended_tests_digest)
        sch.apply_factory_amendment(
            self.store,
            "run-reseal",
            amended,
            runtime=self.runtime,
            target=target,
        )
        self.assertEqual(
            self.store.lane_stage("run-reseal", "lane-tests"),
            st.LaneStage.TESTS_SEALED,
        )
        self.assertEqual(
            self.store.lane_stage("run-reseal", "lane-build"),
            st.LaneStage.PLANNED,
        )
        scheduler = sch.FactoryScheduler(
            self.store,
            "run-reseal",
            actor,
            self.runtime,
            target,
            compiled=amended,
        )
        self.assertIsNone(scheduler._current_tests_sealed("lane-tests"))
        resume = HandoffActor(self.repo, self.runtime.path / "worktrees")
        scheduler = sch.FactoryScheduler(
            self.store,
            "run-reseal",
            resume,
            self.runtime,
            target,
            compiled=amended,
        )
        self.assertEqual(scheduler.run(), st.RunStatus.COMPLETE)
        self.assertEqual(resume.write_tests_lanes, [])
        self.assertEqual(
            self.store.lane_stage("run-reseal", "lane-tests"),
            st.LaneStage.MERGED,
        )
        seals = list(
            self.store.conn.execute(
                "SELECT plan_revision, sequence FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=? "
                "ORDER BY sequence",
                (
                    "run-reseal",
                    "lane-tests",
                    st.ArtifactKind.SEALED_TEST_BUNDLE.value,
                ),
            )
        )
        self.assertEqual([row[0] for row in seals], [1, 2])
        current = scheduler._current_tests_sealed("lane-tests")
        self.assertIsNotNone(current)
        self.assertEqual(current.plan_revision, 2)
        builders = list(
            self.store.conn.execute(
                "SELECT plan_revision, payload_json FROM lane_artifacts "
                "WHERE run_id=? AND lane_id=? AND artifact_kind=? "
                "ORDER BY sequence",
                (
                    "run-reseal",
                    "lane-build",
                    st.ArtifactKind.BUILDER_OUTPUT.value,
                ),
            )
        )
        self.assertGreaterEqual(len(builders), 1)
        self.assertEqual(builders[0][0], 2)
        self.assertIn(
            current.artifact_id,
            json.loads(builders[0][1])["input_artifact_ids"],
        )
        self.assertEqual(
            self.store.lane_stage("run-reseal", "lane-build"),
            st.LaneStage.MERGED,
        )

    def test_v3_plan_compiles_typed_kinds_and_preserves_semantics(self) -> None:
        self.assertTrue(V2_PLAN.is_file())
        self.assertTrue(V3_PLAN.is_file())
        compiled = plan_compiler.compile_plan(
            V3_PLAN.read_bytes(), plan_revision=1, plan_artifact_ref=str(V3_PLAN)
        )
        kinds = [(lane.lane_id, lane.lane_kind) for lane in compiled.lanes]
        self.assertEqual(kinds, [("lane-wp6-build", "build"), ("lane-wp6-tests", "tests")])
        v2 = json.loads(V2_PLAN.read_text(encoding="utf-8"))
        v3 = json.loads(V3_PLAN.read_text(encoding="utf-8"))
        self.assertEqual(v2["schema_version"], v3["schema_version"])
        v2_lanes = {lane["id"]: lane for lane in v2["lanes"]}
        v3_lanes = {lane["id"]: lane for lane in v3["lanes"]}
        self.assertEqual(set(v2_lanes), set(v3_lanes))
        self.assertEqual(v3_lanes["lane-wp6-tests"]["lane_kind"], "tests")
        self.assertEqual(v3_lanes["lane-wp6-build"]["lane_kind"], "build")
        for lane_id, raw in v2_lanes.items():
            typed = dict(v3_lanes[lane_id])
            typed.pop("lane_kind")
            self.assertEqual(raw, typed)


if __name__ == "__main__":
    unittest.main()
