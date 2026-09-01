"""One plan-name entry: inferred repository, ref, plan, and run identity.

Every case drives the real CLI entry (`maestro.main`) from a real temporary
Git repository and asserts durable ledger state. Nothing here inspects source
text, and no case sleeps: the concurrency case uses a `threading.Barrier`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import defaultdict
from io import StringIO
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from unittest import mock

import yaml

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot

FINDING = {
    "implementation_area": "tests",
    "observed_behavior": "no cases",
    "required_behavior": "behavior is asserted",
    "violated_requirement": "public acceptance",
}


def _role_routes() -> dict[str, dict[str, str]]:
    return {
        "tester": {"route": "claude", "model": "opus", "effort": "high"},
        "test-reviewer": {"route": "omp", "profile": "openai-performance"},
        "builder": {"route": "omp", "profile": "grok"},
        "code-reviewer": {"route": "omp", "profile": "openai-performance"},
        "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")


def plan_bytes(*, goal: str = "emit a.txt", output: str = "a.txt") -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": [output],
                "spec": {
                    "goal": goal,
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["{0} is written".format(output)],
            }
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def install_deployment(product: Path, state: Path) -> Path:
    """Stamp `adws/maestro.py` and its config into the target repository."""
    adws = product / "adws"
    adws.mkdir(parents=True, exist_ok=True)
    maestro_file = adws / "maestro.py"
    maestro_file.write_text("# deployment\n", encoding="utf-8")
    (adws / "maestro.config.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "maestro-config.v1",
                "runtime_state_root": str(state.resolve()),
                "role_routes": _role_routes(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return maestro_file


def ship_plan(repo: Path, name: str, body: bytes) -> Path:
    """Ship one plan the installed way: `.maestro/plans/<name>/maestro-plan.v1`.

    Committed, because publication refuses a target worktree carrying
    untracked files and a real shipped plan lives in the repository.
    """
    path = repo / ".maestro" / "plans" / name / "maestro-plan.v1"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    _git(repo, "add", "-f", str(path))
    _git(repo, "commit", "-m", "ship " + name)
    return path


def ship_path(repo: Path, relative: Path, body: bytes) -> Path:
    """Commit an arbitrary artifact into the target repository."""
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    _git(repo, "add", "-f", str(path))
    _git(repo, "commit", "-m", "add " + str(relative))
    return path


@contextlib.contextmanager
def working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ScriptedActor:
    """A whole factory turn with no agent: one REVISE per reviewer."""

    def __init__(self, repo: Path, worktrees: Path) -> None:
        self.repo = repo
        self.worktrees = worktrees
        self.test_rounds: dict[str, int] = defaultdict(int)
        self.code_rounds: dict[str, int] = defaultdict(int)
        self.stop_at: str = ""
        self.stopped: list[str] = []

    def _maybe_stop(self, where: str) -> None:
        if self.stop_at == where:
            self.stopped.append(where)
            raise KeyboardInterrupt

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        self._maybe_stop("write_tests")
        name = ctx.lane.lane_id.replace("-", "_")
        lines = ["# secret-selector", "from pathlib import Path", ""]
        for output in ctx.lane.declared_outputs:
            ident = Path(output).stem.replace("-", "_")
            lines.append("def test_{0}_{1}_exists():".format(name, ident))
            lines.append("    assert Path({0!r}).is_file()".format(output))
            lines.append("")
        return {
            "files": {"tests/test_{0}_private.py".format(name): "\n".join(lines)},
            "private_tokens": ("secret-selector",),
        }

    def review_tests(self, ctx: sch.LaneContext):
        self._maybe_stop("review_tests")
        n = self.test_rounds[ctx.lane.lane_id]
        self.test_rounds[ctx.lane.lane_id] += 1
        if n == 0:
            return st.ReviewerVerdict.REVISE, (FINDING,)
        return st.ReviewerVerdict.PASS, ()

    def build(self, ctx: sch.LaneContext) -> dict:
        self._maybe_stop("build")
        work = self.worktrees / ctx.lane.lane_id / ctx.input_digest[:12]
        if work.exists():
            _git(self.repo, "worktree", "remove", "--force", str(work))
        work.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repo, "worktree", "add", "--detach", str(work), ctx.builder_base_sha)
        for path in ctx.lane.declared_outputs:
            (work / path).write_text(
                "{0}:{1}\n".format(ctx.lane.lane_id, ctx.input_digest),
                encoding="utf-8",
            )
            _git(work, "add", path)
        _git(work, "commit", "-m", ctx.lane.lane_id)
        return {
            "candidate_sha": _git(work, "rev-parse", "HEAD"),
            "changed": True,
            "private_tokens": ("secret-selector",),
        }

    def review_code(self, ctx: sch.LaneContext):
        self._maybe_stop("review_code")
        n = self.code_rounds[ctx.lane.lane_id]
        self.code_rounds[ctx.lane.lane_id] += 1
        if n == 0:
            return st.ReviewerVerdict.REVISE, (FINDING,)
        return st.ReviewerVerdict.PASS, ()

    def review_integration(self, ctx, lanes, integration_sha):
        del ctx, lanes, integration_sha
        self._maybe_stop("review_integration")
        return st.ReviewerVerdict.PASS, (), ()

    def publish(self, ctx, *, fingerprint, expected_before, published_sha):
        del expected_before
        self._maybe_stop("publish")
        return {
            "receipt_object": published_sha,
            "receipt_ref": st.publication_ref(ctx.run_id, fingerprint),
        }

    def complete_run_spaces(self, run_id: str) -> None:
        del run_id


class SingleEntryBase(unittest.TestCase):
    """A stamped deployment, one shipped plan, and a recorded CLI entry."""

    plan_name = "one-lane"

    @classmethod
    def setUpClass(cls) -> None:
        cls._registry = tempfile.TemporaryDirectory()
        cls._registry_previous = os.environ.get("MAESTRO_REGISTRY")
        os.environ["MAESTRO_REGISTRY"] = str(
            Path(cls._registry.name) / "registry.json"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._registry_previous is None:
            os.environ.pop("MAESTRO_REGISTRY", None)
        else:
            os.environ["MAESTRO_REGISTRY"] = cls._registry_previous
        cls._registry.cleanup()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.repo)
        self.maestro_file = install_deployment(self.repo, self.state)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "install")
        self.plan_path = ship_plan(self.repo, self.plan_name, plan_bytes())
        self.starts: list[argparse.Namespace] = []
        self.resumes: list[argparse.Namespace] = []

    # -- invocation ---------------------------------------------------------

    def invoke(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        record: bool = True,
    ) -> tuple[int, Mapping[str, Any]]:
        buf = StringIO()
        patches = [
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch("sys.stdout", buf),
        ]
        if record:
            patches.append(
                mock.patch.object(maestro, "_run_start", side_effect=self._start)
            )
            patches.append(
                mock.patch.object(maestro, "_run_resume", side_effect=self._resume)
            )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(working_directory(cwd or self.repo))
            code = maestro.main(list(argv))
        text = buf.getvalue().strip()
        payload = json.loads(text.splitlines()[-1]) if text else {}
        return code, payload

    def _start(self, args: argparse.Namespace) -> int:
        self.starts.append(args)
        return 0

    def _resume(self, args: argparse.Namespace) -> int:
        self.resumes.append(args)
        return 0

    # -- ledger -------------------------------------------------------------

    @contextlib.contextmanager
    def ledger(self) -> Iterator[ArtifactStore]:
        runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        runtime.ensure_layout()
        store = ArtifactStore(runtime.ledger_path())
        try:
            yield store
        finally:
            store.close()
            runtime.close()

    def run_ids(self) -> tuple[str, ...]:
        with self.ledger() as store:
            rows = store.conn.execute(
                "SELECT run_id FROM runs ORDER BY created_at, run_id"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def run_row(self, run_id: str) -> Mapping[str, Any]:
        with self.ledger() as store:
            return sch.run_row(store, run_id)

    # -- real runs ----------------------------------------------------------

    def execute_run(
        self,
        actor: ScriptedActor,
        *,
        plan_path: Path | None = None,
        run_id: str = "",
    ) -> tuple[str, st.RunStatus]:
        """Drive a genuine run to whatever status the actor allows."""
        artifact = plan_path or self.plan_path
        runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        runtime.ensure_layout()
        store = ArtifactStore(runtime.ledger_path())
        try:
            compiled = plan_compiler.compile_plan(
                artifact.read_bytes(),
                plan_revision=1,
                plan_artifact_ref=str(artifact.resolve()),
            )
            target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
            identity = run_id or ("run" + os.urandom(8).hex())
            sch.create_factory_run(
                store=store,
                run_id=identity,
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
            scheduler = sch.FactoryScheduler(
                store, identity, actor, runtime, target, compiled=compiled
            )
            status = scheduler.run()
        finally:
            store.close()
            runtime.close()
        return identity, status

    def create_bare_run(self, run_id: str, *, plan_path: Path | None = None) -> str:
        artifact = plan_path or self.plan_path
        runtime = RuntimeStateRoot(self.state, overlap_paths=(self.repo,))
        runtime.ensure_layout()
        store = ArtifactStore(runtime.ledger_path())
        try:
            compiled = plan_compiler.compile_plan(
                artifact.read_bytes(),
                plan_revision=1,
                plan_artifact_ref=str(artifact.resolve()),
            )
            target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
            sch.create_factory_run(
                store=store,
                run_id=run_id,
                compiled=compiled,
                runtime=runtime,
                target=target,
            )
        finally:
            store.close()
            runtime.close()
        return run_id


class EntryInferenceTest(SingleEntryBase):
    """Repository, primary-worktree proof, main ref, and plan resolution."""

    def test_plan_only_invocation_infers_repository_and_symbolic_main_ref(self) -> None:
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.resumes, [])
        started = self.starts[0]
        self.assertEqual(Path(started.repo), self.repo)
        self.assertEqual(started.main_ref, "refs/heads/main")
        self.assertEqual(Path(started.plan), self.plan_path.resolve())
        self.assertTrue(started.run_id)
        self.assertEqual(self.run_ids(), (started.run_id,))

    def test_exact_name_resolves_the_installed_plan_artifact(self) -> None:
        other = ship_plan(self.repo, "beta", plan_bytes(output="b.txt"))
        code, _ = self.invoke(["--plan", "beta"])
        self.assertEqual(code, 0)
        self.assertEqual(Path(self.starts[0].plan), other.resolve())
        row = self.run_row(self.starts[0].run_id)
        expected = plan_compiler.compile_plan(
            other.read_bytes(), plan_revision=1, plan_artifact_ref=str(other.resolve())
        )
        self.assertEqual(row["plan_digest"], expected.plan_digest)

    def test_invocation_from_a_subdirectory_binds_the_same_repository(self) -> None:
        nested = self.repo / "src" / "deep"
        nested.mkdir(parents=True)
        code, _ = self.invoke(["--plan", self.plan_name], cwd=nested)
        self.assertEqual(code, 0)
        self.assertEqual(Path(self.starts[0].repo), self.repo)
        second, _ = self.invoke(["--plan", self.plan_name], cwd=nested)
        self.assertEqual(second, 0)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(len(self.resumes), 1)
        self.assertEqual(self.run_ids(), (self.starts[0].run_id,))

    def test_invocation_from_a_linked_worktree_refuses(self) -> None:
        linked = self.root / "lane-checkout"
        _git(self.repo, "worktree", "add", "-b", "lane", str(linked))
        code, payload = self.invoke(["--plan", self.plan_name], cwd=linked)
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertIn("primary worktree", payload["detail"])
        self.assertEqual(self.starts, [])
        self.assertEqual(self.resumes, [])
        self.assertEqual(self.run_ids(), ())

    def test_detached_head_refuses_instead_of_choosing_a_ref(self) -> None:
        head = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "--detach", head)
        code, payload = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "TARGET_DETACHED")
        self.assertEqual(self.starts, [])
        self.assertEqual(self.run_ids(), ())

    def test_invocation_outside_a_git_working_tree_refuses(self) -> None:
        outside = self.root / "not-a-repo"
        outside.mkdir()
        code, payload = self.invoke(["--plan", self.plan_name], cwd=outside)
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertEqual(self.run_ids(), ())

    def test_missing_plan_name_refuses_deterministically(self) -> None:
        first = self.invoke(["--plan", "absent"])
        second = self.invoke(["--plan", "absent"])
        self.assertEqual(first, second)
        self.assertEqual(first[0], 3)
        self.assertEqual(first[1]["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertEqual(self.starts, [])
        self.assertEqual(self.run_ids(), ())

    def test_malformed_plan_refuses_with_a_typed_outcome(self) -> None:
        ship_plan(self.repo, "broken", b"{not json")
        code, payload = self.invoke(["--plan", "broken"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "PLAN_COMPILE_REFUSED")
        self.assertEqual(self.starts, [])
        self.assertEqual(self.run_ids(), ())

    def test_path_escaping_plan_names_refuse(self) -> None:
        outside = self.root / "elsewhere" / "maestro-plan.v1"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(plan_bytes())
        for name in ("..", ".", "../elsewhere", "nested/one-lane", "", " one-lane"):
            with self.subTest(name=name):
                code, payload = self.invoke(["--plan", name])
                self.assertEqual(code, 3)
                self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertEqual(self.starts, [])
        self.assertEqual(self.run_ids(), ())

    def test_symlinked_plan_directory_leaving_the_plans_root_refuses(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "maestro-plan.v1").write_bytes(plan_bytes())
        link = self.repo / ".maestro" / "plans" / "escaped"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
        code, payload = self.invoke(["--plan", "escaped"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertIn("escapes", payload["detail"])
        self.assertEqual(self.run_ids(), ())

    def test_a_same_named_artifact_deeper_in_the_tree_is_never_searched(self) -> None:
        ship_path(
            self.repo,
            Path("sub") / ".maestro" / "plans" / "only-here" / "maestro-plan.v1",
            plan_bytes(output="c.txt"),
        )
        code, payload = self.invoke(["--plan", "only-here"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertEqual(self.run_ids(), ())


class RunSelectionTest(SingleEntryBase):
    """Start once, resume once, refuse ambiguity, never duplicate."""

    def test_no_matching_run_calls_the_start_path_once(self) -> None:
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.resumes, [])
        self.assertEqual(len(self.run_ids()), 1)

    def test_one_executing_run_resumes_with_its_persisted_run_id(self) -> None:
        self.invoke(["--plan", self.plan_name])
        created = self.run_ids()
        self.assertEqual(len(created), 1)
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(len(self.resumes), 1)
        self.assertEqual(self.resumes[0].run_id, created[0])
        self.assertEqual(self.run_ids(), created)

    def test_one_waiting_run_resumes_and_creates_nothing(self) -> None:
        actor = ScriptedActor(self.repo, self.state / "worktrees")
        actor.stop_at = "write_tests"
        run_id, status = self.execute_run(actor)
        self.assertEqual(status, st.RunStatus.WAITING)
        with self.ledger() as store:
            self.assertEqual(
                store.lane_stage(run_id, "lane-a"), st.LaneStage.WAITING_FOR_USER
            )
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual(self.starts, [])
        self.assertEqual([item.run_id for item in self.resumes], [run_id])
        self.assertEqual(self.run_ids(), (run_id,))

    def test_one_publishable_run_resumes_and_creates_nothing(self) -> None:
        actor = ScriptedActor(self.repo, self.state / "worktrees")
        # The actor's `publish` runs after the publication kernel, so the run
        # is stopped at the kernel itself: final review has passed and no
        # publication exists, which is exactly PUBLISHABLE.
        with mock.patch.object(
            sch.gitpub, "publish_or_reconcile_locked", side_effect=KeyboardInterrupt
        ):
            run_id, _ = self.execute_run(actor)
        with self.ledger() as store:
            status = store.derive_run_status(
                run_id, sch.durable_integration_tip(store, run_id)
            )
        self.assertEqual(status, st.RunStatus.PUBLISHABLE)
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual(self.starts, [])
        self.assertEqual([item.run_id for item in self.resumes], [run_id])
        self.assertEqual(self.run_ids(), (run_id,))

    def test_a_completed_run_is_not_resumed(self) -> None:
        actor = ScriptedActor(self.repo, self.state / "worktrees")
        done, status = self.execute_run(actor)
        self.assertEqual(status, st.RunStatus.COMPLETE)
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual(self.resumes, [])
        self.assertEqual(len(self.starts), 1)
        self.assertNotEqual(self.starts[0].run_id, done)
        self.assertEqual(set(self.run_ids()), {done, self.starts[0].run_id})

    def test_a_completed_run_beside_a_nonterminal_one_resumes_only_the_live_run(
        self,
    ) -> None:
        actor = ScriptedActor(self.repo, self.state / "worktrees")
        done, status = self.execute_run(actor)
        self.assertEqual(status, st.RunStatus.COMPLETE)
        self.invoke(["--plan", self.plan_name])
        live = self.starts[0].run_id
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual([item.run_id for item in self.resumes], [live])
        self.assertEqual(set(self.run_ids()), {done, live})

    def test_two_nonterminal_runs_refuse_naming_both_and_create_nothing(self) -> None:
        first = self.create_bare_run("run-alpha")
        second = self.create_bare_run("run-beta")
        code, payload = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "FACTORY_REFUSED")
        self.assertIn(first, payload["detail"])
        self.assertIn(second, payload["detail"])
        self.assertEqual(self.starts, [])
        self.assertEqual(self.resumes, [])
        self.assertEqual(set(self.run_ids()), {first, second})

    def test_same_plan_name_with_different_bytes_resumes_and_refuses_the_mismatch(
        self,
    ) -> None:
        self.invoke(["--plan", self.plan_name])
        created = self.starts[0].run_id
        self.plan_path.write_bytes(plan_bytes(goal="emit a.txt, revised"))
        code, payload = self.invoke(["--plan", self.plan_name], record=False)
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "FACTORY_REFUSED")
        self.assertIn("PLAN_ARTIFACT_MISMATCH", payload["detail"])
        self.assertEqual(self.run_ids(), (created,))

    def test_the_same_plan_against_a_different_main_ref_is_a_separate_run(self) -> None:
        self.invoke(["--plan", self.plan_name])
        first = self.starts[0].run_id
        _git(self.repo, "checkout", "-b", "release")
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual(self.resumes, [])
        self.assertEqual(len(self.starts), 2)
        second = self.starts[1].run_id
        self.assertNotEqual(first, second)
        self.assertEqual(self.starts[1].main_ref, "refs/heads/release")
        self.assertEqual(set(self.run_ids()), {first, second})

    def test_the_same_plan_against_a_different_repository_is_a_separate_run(
        self,
    ) -> None:
        self.invoke(["--plan", self.plan_name])
        first = self.starts[0].run_id
        other = self.root / "other-product"
        _init_repo(other)
        other_file = install_deployment(other, self.state)
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "install")
        ship_plan(other, self.plan_name, plan_bytes())
        self.maestro_file = other_file
        code, _ = self.invoke(["--plan", self.plan_name], cwd=other)
        self.assertEqual(code, 0)
        self.assertEqual(self.resumes, [])
        self.assertEqual(len(self.starts), 2)
        self.assertNotEqual(self.starts[1].run_id, first)
        self.assertEqual(len(self.run_ids()), 2)

    def test_the_ledger_records_no_process_ownership_that_could_block_a_resume(
        self,
    ) -> None:
        self.invoke(["--plan", self.plan_name])
        created = self.starts[0].run_id
        with self.ledger() as store:
            columns = {
                str(row[1])
                for row in store.conn.execute("PRAGMA table_info(runs)").fetchall()
            }
        for banned in ("owner", "pid", "host", "hostname", "lease", "lock", "process"):
            self.assertFalse(
                any(banned in column for column in columns),
                "runs carries process ownership: {0}".format(sorted(columns)),
            )
        code, _ = self.invoke(["--plan", self.plan_name])
        self.assertEqual(code, 0)
        self.assertEqual([item.run_id for item in self.resumes], [created])
        self.assertEqual(self.run_ids(), (created,))


class ConcurrentFirstInvocationTest(SingleEntryBase):
    def test_two_simultaneous_first_invocations_create_exactly_one_run(self) -> None:
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        outcomes: list[int] = []
        failures: list[BaseException] = []

        class Gated(sch.OrderedLocks):
            """Both invocations arrive at the run lock together, no sleeps."""

            def acquire(self, levels: int) -> None:
                barrier.wait(30)
                super().acquire(levels)

        def invoke() -> None:
            try:
                code = maestro.main(["--plan", self.plan_name])
                with lock:
                    outcomes.append(code)
            except BaseException as exc:  # pragma: no cover - reported below
                with lock:
                    failures.append(exc)

        # Patched once, in this thread: two threads patching the same module
        # attribute would restore each other's mocks out of order.
        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch.object(maestro, "OrderedLocks", Gated),
            mock.patch.object(maestro, "_run_start", side_effect=self._start),
            mock.patch.object(maestro, "_run_resume", side_effect=self._resume),
            mock.patch("sys.stdout", StringIO()),
            working_directory(self.repo),
        ):
            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(outcomes, [0, 0])
        self.assertEqual(len(self.run_ids()), 1)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(len(self.resumes), 1)
        self.assertEqual(self.resumes[0].run_id, self.starts[0].run_id)


class PlanFileMappingTest(SingleEntryBase):
    """A missing plan file is configuration. Nothing else is relabelled."""

    def test_missing_plan_path_on_run_start_is_a_typed_refusal(self) -> None:
        missing = self.repo / ".maestro" / "plans" / "gone" / "maestro-plan.v1"
        buf = StringIO()
        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch("sys.stdout", buf),
            working_directory(self.repo),
        ):
            code = maestro.main(
                [
                    "run",
                    "start",
                    str(missing),
                    "--repo",
                    str(self.repo),
                    "--main-ref",
                    "refs/heads/main",
                ]
            )
        payload = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_CONFIGURATION_REQUIRED")
        self.assertIn("maestro-plan.v1", payload["detail"])
        self.assertEqual(self.run_ids(), ())

    def test_a_missing_executable_inside_the_run_is_not_a_configuration_refusal(
        self,
    ) -> None:
        """A `FileNotFoundError` from the run is the run's failure, not the
        plan's: relabelling it `RUN_CONFIGURATION_REQUIRED` would send the
        operator to look at a plan file that is present and compiled."""

        class ExplodingScheduler:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def run(self) -> st.RunStatus:
                raise FileNotFoundError(2, "No such file or directory", "omp")

        buf = StringIO()
        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch.object(maestro, "_actor_for", return_value=mock.Mock()),
            mock.patch.object(maestro, "FactoryScheduler", ExplodingScheduler),
            mock.patch("sys.stdout", buf),
            working_directory(self.repo),
        ):
            with self.assertRaises(FileNotFoundError) as raised:
                maestro.main(["--plan", self.plan_name])
        self.assertEqual(raised.exception.filename, "omp")
        self.assertNotIn("RUN_CONFIGURATION_REQUIRED", buf.getvalue())
        # The run was created before the failure, so the next invocation
        # resumes it rather than starting a second one.
        self.assertEqual(len(self.run_ids()), 1)


class MovedMainRefTest(SingleEntryBase):
    def test_main_moving_between_the_two_binds_is_a_typed_refusal(self) -> None:
        """`_run_plan` binds the target and creates the run; `_run_start` then
        binds again. Both binds happen in one call stack, so the interleaving
        is pinned by counting binds rather than by a barrier -- there is no
        second thread in this window to synchronise with.
        """
        real_bind = maestro.gitpub.bind_target_worktree
        binds: list[str] = []

        def moving_bind(repo: str | Path, main_ref: str) -> gitpub.TargetBinding:
            target = real_bind(repo, main_ref)
            binds.append(str(target.target_initial_main_sha))
            if len(binds) == 1:
                # Main advances between `_run_plan`'s bind and `_run_start`'s.
                (self.repo / "moved.txt").write_text("moved\n", encoding="utf-8")
                _git(self.repo, "add", "-f", "moved.txt")
                _git(self.repo, "commit", "-m", "main moved")
            return target

        buf = StringIO()
        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=self.maestro_file
            ),
            mock.patch.object(
                maestro.gitpub, "bind_target_worktree", side_effect=moving_bind
            ),
            mock.patch("sys.stdout", buf),
            working_directory(self.repo),
        ):
            code = maestro.main(["--plan", self.plan_name])
        payload = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual(len(binds), 2)
        self.assertNotEqual(binds[0], binds[1], "main did not move")
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_ALREADY_EXISTS")
        self.assertNotIn("Traceback", buf.getvalue())
        # The run is durable and unduplicated, so the operator's next plain
        # invocation resumes it.
        created = self.run_ids()
        self.assertEqual(len(created), 1)
        second, resumed = self.invoke(["--plan", self.plan_name])
        self.assertEqual(second, 0)
        self.assertEqual([item.run_id for item in self.resumes], list(created))
        self.assertEqual(self.starts, [])
        self.assertEqual(self.run_ids(), created)


class FrozenSurfaceTest(SingleEntryBase):
    def test_single_entry_needs_no_repository_ref_or_run_flags(self) -> None:
        parser = maestro.build_parser()
        args = parser.parse_args(["--plan", self.plan_name])
        self.assertEqual(args.plan_name, self.plan_name)
        self.assertIsNone(args.command)
        options: list[str] = []
        for action in parser._actions:
            options.extend(action.option_strings)
        for banned in ("--repo", "--main-ref", "--run-id", "--run"):
            self.assertNotIn(banned, options)

    def test_the_frozen_verb_surface_is_unchanged(self) -> None:
        self.assertEqual(
            maestro.parser_verbs(maestro.build_parser()),
            ("run start", "run resume", "run amend", "run status"),
        )

    def test_a_plan_selector_and_a_verb_together_are_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            maestro.main(["--plan", self.plan_name, "run", "status", "run-1"])

    def test_no_selector_and_no_verb_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            maestro.main([])

    def test_a_retained_pane_is_named_in_the_launch_refusal(self) -> None:
        for retained in (True, False):
            with self.subTest(retained=retained):
                failure = maestro.LaunchFailed("NO_PANE:lane-a", pane_created=retained)
                with (
                    mock.patch.object(
                        maestro,
                        "_executing_maestro_file",
                        return_value=self.maestro_file,
                    ),
                    mock.patch.object(maestro, "_run_start", side_effect=failure),
                    mock.patch.object(maestro, "_run_resume", side_effect=failure),
                    mock.patch("sys.stdout", StringIO()) as buf,
                    working_directory(self.repo),
                ):
                    code = maestro.main(["--plan", self.plan_name])
                payload = json.loads(buf.getvalue().strip().splitlines()[-1])
                self.assertEqual(code, 3)
                self.assertEqual(payload["outcome"], "LAUNCH_FAILED")
                self.assertEqual(
                    payload["detail"].endswith(":pane_retained"), retained
                )


if __name__ == "__main__":
    unittest.main()
