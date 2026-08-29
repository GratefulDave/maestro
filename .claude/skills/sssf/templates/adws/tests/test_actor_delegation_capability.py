"""CLI has no tool-policy flags. Actor prompts, cwd, privacy, candidate commits."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))


import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules import private_review as prv
from adw_modules import tests_chain as tchain
from adw_modules.scheduler import LaneContext
from adw_modules.scheduler_types import (
    LaneProjection,
    LaneStage,
    ReviewerVerdict,
    lane_projection_digest,
)


class LaunchRecord(TypedDict):
    argv: tuple[str, ...]
    dirty_at_launch: bool
    environment: dict[str, str]
    has_git_at_launch: bool
    head: str
    hidden_test_at_launch: bool
    jsonl_at_launch: list[Path]
    prompt: dict[str, Any]
    session_dir: Path
    scratch_ready: bool
    worktree: Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")
    return _git(path, "rev-parse", "HEAD")


def _lane() -> LaneProjection:
    spec_digest = "ab" * 32
    needs: tuple[str, ...] = ()
    outputs = ("a.txt",)
    return LaneProjection(
        lane_id="lane-a",
        needs=needs,
        spec_digest=spec_digest,
        declared_outputs=outputs,
        lane_projection_digest=lane_projection_digest(spec_digest, needs, outputs),
        public_acceptance=("a.txt is written",),
    )


class CliHasNoDelegationPolicyFlagsTest(unittest.TestCase):
    def test_frozen_verbs_have_no_tool_allowlist_flags(self) -> None:
        parser = maestro.build_parser()
        run = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["run"]
        run_sub = next(
            action
            for action in run._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        banned = (
            "--disallowedTools",
            "--allowedTools",
            "--restrict-actor-tools",
            "--tools",
        )
        for name in ("start", "resume", "amend", "status"):
            flags = []
            for action in run_sub.choices[name]._actions:
                flags.extend(action.option_strings)
            for flag in banned:
                self.assertNotIn(flag, flags)


class RecordingLauncher:
    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        envelope: Mapping[str, Any] | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.envelope = dict(envelope or {})
        self.launches: list[LaunchRecord] = []

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        git_cwd = (spec.worktree / ".git").exists()
        head = _git(spec.worktree, "rev-parse", "HEAD") if git_cwd else ""
        argv = lch.build_omp_argv(Path("omp"), spec)
        prompt = json.loads(spec.prompt_path.read_text(encoding="utf-8"))
        environment = dict(spec.environment)
        scratch_dirs = [
            environment["TMPDIR"],
            environment["PYTHONPYCACHEPREFIX"],
            environment["RUFF_CACHE_DIR"],
            environment["npm_config_cache"],
            environment["PYTEST_ADDOPTS"].split("cache_dir=", 1)[1].split()[0],
        ]
        self.launches.append(
            {
                "argv": argv,
                "dirty_at_launch": (spec.worktree / "dirty.txt").exists(),
                "environment": environment,
                "has_git_at_launch": git_cwd,
                "head": head,
                "hidden_test_at_launch": (
                    spec.worktree / "tests" / "hidden.py"
                ).is_file(),
                "jsonl_at_launch": list(spec.session_dir.glob("*.jsonl")),
                "prompt": prompt,
                "session_dir": spec.session_dir,
                "scratch_ready": all(Path(item).is_dir() for item in scratch_dirs),
                "worktree": spec.worktree,
            }
        )
        for rel, body in self.files.items():
            path = spec.worktree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        (spec.session_dir / "stale.jsonl").write_text("{}\n", encoding="utf-8")
        spec.envelope_path.write_text(
            json.dumps(self.envelope, sort_keys=True), encoding="utf-8"
        )
        return SimpleNamespace(envelope_path=spec.envelope_path)

    def poll(self, handle: object) -> SimpleNamespace:
        del handle
        return SimpleNamespace(state=lch.PollState.EXITED)

    def cancel(self, handle: object, deadline: float) -> None:
        del handle, deadline


class FreshAttemptDispatchTest(unittest.TestCase):
    def test_two_dispatches_same_input_get_distinct_clean_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = LaneContext(
                run_id="run-1",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="ef" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
            )
            recorder = RecordingLauncher()
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder),
                state,
                target,
                "grok-maestro",
            )
            actor.write_tests(ctx)
            actor.write_tests(ctx)
            self.assertEqual(len(recorder.launches), 2)
            first, second = recorder.launches
            self.assertNotEqual(first["worktree"], second["worktree"])
            self.assertNotEqual(first["session_dir"], second["session_dir"])
            worktrees = state / "worktrees"
            for record in (first, second):
                worktree = Path(record["worktree"])
                session = Path(record["session_dir"])
                self.assertEqual(record["head"], head)
                self.assertEqual(record["jsonl_at_launch"], [])
                self.assertFalse(record["dirty_at_launch"])
                self.assertTrue(worktrees.resolve() in worktree.resolve().parents)
                self.assertTrue(worktrees.resolve() in session.resolve().parents)
                self.assertTrue(record["scratch_ready"])
                for key in lch.SCRATCH_ENV_KEYS:
                    self.assertTrue(record["environment"][key])
                self.assertEqual(
                    record["environment"]["PYTEST_ADDOPTS"],
                    "-o cache_dir={}".format(
                        Path(record["session_dir"]).parent / "scratch" / "pytest_cache"
                    ),
                )
                self.assertNotEqual(worktree.resolve(), product.resolve())
                self.assertIn("envelope_path", record["prompt"])
                self.assertEqual(record["prompt"]["role"], "tester")
                self.assertIn(
                    str(record["prompt"]["envelope_path"]),
                    record["prompt"]["instructions"],
                )
            self.assertNotIn("-c", first["argv"])
            self.assertNotIn("-c", second["argv"])
            self.assertEqual(
                first["argv"][first["argv"].index("--profile") + 1],
                "grok-maestro",
            )

    def test_tester_prompt_names_envelope_and_keeps_private_files_off_product(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = LaneContext(
                run_id="run-priv",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="11" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
            )
            recorder = RecordingLauncher(files={"tests/hidden.py": "assert False\n"})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder),
                state,
                target,
                "grok-maestro",
            )
            extra = actor.write_tests(ctx)
            self.assertEqual(set(extra), {"private_files"})
            self.assertIn("tests/hidden.py", extra["private_files"])
            tracked = _git(product, "ls-files")
            self.assertNotIn("tests/hidden.py", tracked)
            prompt = recorder.launches[0]["prompt"]
            self.assertTrue(Path(prompt["envelope_path"]).is_absolute())
            self.assertIn("envelope_schema", prompt)
            self.assertEqual(prompt["role"], "tester")
            self.assertIn("Do not git commit", prompt["instructions"])

    def test_builder_prompt_omits_private_bytes_and_commits_declared_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = LaneContext(
                run_id="run-build",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="22" * 32,
                stage=LaneStage.BUILDING,
                artifacts={},
                builder_base_sha=head,
                public_contract={
                    "acceptance_criteria": ["a.txt is written"],
                    "declared_outputs": ["a.txt"],
                },
                sealed_digest="33" * 32,
            )
            recorder = RecordingLauncher(files={"a.txt": "a\n"})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder),
                state,
                target,
                "grok-maestro",
            )
            result = actor.build(ctx)
            self.assertTrue(result["changed"])
            self.assertNotEqual(result["candidate_sha"], head)
            prompt = recorder.launches[0]["prompt"]
            self.assertEqual(prompt["role"], "builder")
            self.assertNotIn("private_files", prompt)
            self.assertNotIn("vault_path", prompt)
            dumped = json.dumps(prompt, sort_keys=True)
            self.assertNotIn("tests/hidden.py", dumped)
            self.assertNotIn("vaults/", dumped)
            self.assertEqual(prompt["declared_outputs"], ["a.txt"])
            self.assertEqual(prompt["sealed_digest"], "33" * 32)
            cwd = Path(recorder.launches[0]["worktree"])
            self.assertTrue((state / "worktrees").resolve() in cwd.resolve().parents)

    def test_test_reviewer_runs_in_private_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            lane = _lane()
            digest = "44" * 32
            draft = tchain.write_test_draft(
                request=prv.VaultLaneRequest(
                    run_id="run-rev",
                    lane_id=lane.lane_id,
                    plan_revision=1,
                    spec_digest=lane.spec_digest,
                    lane_projection_digest=lane.lane_projection_digest,
                    input_digest=digest,
                ),
                state_root=state,
                run_repo=product,
                integration_ref="refs/heads/main",
                files={"tests/hidden.py": "assert True\n"},
                public_contract={
                    "acceptance_criteria": ["a.txt is written"],
                    "declared_outputs": ["a.txt"],
                },
                worktrees_root=state / "worktrees",
            )
            review_ctx = LaneContext(
                run_id="run-rev",
                lane=lane,
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="55" * 32,
                stage=LaneStage.REVIEWING_TESTS,
                artifacts={"TEST_DRAFT": draft},  # type: ignore[dict-item]
                public_contract={"acceptance_criteria": ["a.txt is written"]},
            )
            reviewer = RecordingLauncher(envelope={"verdict": "PASS", "findings": []})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, reviewer),
                state,
                target,
                "grok-maestro",
            )
            del head
            verdict, findings = actor.review_tests(review_ctx)
            self.assertEqual(verdict, ReviewerVerdict.PASS)
            self.assertEqual(list(findings), [])
            record = reviewer.launches[0]
            prompt = record["prompt"]
            self.assertEqual(prompt["role"], "test-reviewer")
            self.assertIn("PASS requires findings=[]", prompt["instructions"])
            self.assertIn(
                "REVISE requires at least one actionable finding",
                prompt["instructions"],
            )
            cwd = Path(record["worktree"])
            self.assertTrue(record["hidden_test_at_launch"])
            self.assertFalse(record["has_git_at_launch"])
            self.assertFalse(record["dirty_at_launch"])
            self.assertNotEqual(cwd.resolve(), product.resolve())
            self.assertFalse(cwd.exists())


if __name__ == "__main__":
    unittest.main()
