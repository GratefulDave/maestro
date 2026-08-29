"""CLI has no tool-policy flags. Dispatch uses a fresh worktree and session."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules.scheduler import LaneContext
from adw_modules.scheduler_types import (
    LaneProjection,
    LaneStage,
    lane_projection_digest,
)


class LaunchRecord(TypedDict):
    worktree: Path
    session_dir: Path
    head: str
    jsonl_at_launch: list[Path]
    dirty_at_launch: bool
    argv: tuple[str, ...]


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
    def __init__(self) -> None:
        self.launches: list[LaunchRecord] = []

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        head = _git(spec.worktree, "rev-parse", "HEAD")
        argv = lch.build_omp_argv(Path("omp"), replace(spec, profile="grok-maestro"))
        self.launches.append(
            {
                "worktree": spec.worktree,
                "session_dir": spec.session_dir,
                "head": head,
                "jsonl_at_launch": list(spec.session_dir.glob("*.jsonl")),
                "dirty_at_launch": (spec.worktree / "dirty.txt").exists(),
                "argv": argv,
            }
        )
        (spec.session_dir / "stale.jsonl").write_text("{}\n", encoding="utf-8")
        (spec.worktree / "dirty.txt").write_text("stale\n", encoding="utf-8")
        spec.envelope_path.write_text("{}", encoding="utf-8")
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
            worktrees = state / "worktrees"
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            spec_digest = "ab" * 32
            needs: tuple[str, ...] = ()
            outputs = ("a.txt",)
            lane = LaneProjection(
                lane_id="lane-a",
                needs=needs,
                spec_digest=spec_digest,
                declared_outputs=outputs,
                lane_projection_digest=lane_projection_digest(
                    spec_digest, needs, outputs
                ),
            )
            ctx = LaneContext(
                run_id="run-1",
                lane=lane,
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="ef" * 32,
                stage=LaneStage.BUILDING,
                artifacts={},
                builder_base_sha=head,
            )
            recorder = RecordingLauncher()
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), worktrees, target
            )
            actor.write_tests(ctx)
            actor.write_tests(ctx)
            self.assertEqual(len(recorder.launches), 2)
            first, second = recorder.launches
            self.assertNotEqual(first["worktree"], second["worktree"])
            self.assertNotEqual(first["session_dir"], second["session_dir"])
            for record in (first, second):
                worktree = Path(str(record["worktree"]))
                session = Path(str(record["session_dir"]))
                self.assertEqual(record["head"], head)
                self.assertEqual(record["jsonl_at_launch"], [])
                self.assertFalse(record["dirty_at_launch"])
                self.assertTrue(worktrees.resolve() in worktree.resolve().parents)
                self.assertTrue(worktrees.resolve() in session.resolve().parents)
                self.assertFalse(
                    str(product.resolve()) in str(worktree.resolve())
                    and worktree.resolve() == product.resolve()
                )
                self.assertNotEqual(worktree.resolve(), product.resolve())
            self.assertNotIn("-c", second["argv"])
            self.assertNotIn("-c", first["argv"])


if __name__ == "__main__":
    unittest.main()
