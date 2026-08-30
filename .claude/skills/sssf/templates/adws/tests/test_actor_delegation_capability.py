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
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))


import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules import private_review as prv
from adw_modules import tests_chain as tchain
from adw_modules.scheduler import FactoryRefused, LaneContext
from adw_modules.scheduler_types import (
    LaneProjection,
    LaneStage,
    ReviewerVerdict,
    lane_projection_digest,
)

_ROLE_ROUTES: Mapping[str, Mapping[str, str]] = {
    "tester": {"route": "claude", "model": "opus", "effort": "high"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "omp", "profile": "grok"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {
        "route": "omp",
        "profile": "openai-performance",
    },
}


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
    system_prompt: str
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


def _materialize_hidden(_vault: object, _sha: object, dest: Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    hidden = dest / "tests" / "hidden.py"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("assert True\n", encoding="utf-8")


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
        self.resubmits: list[dict[str, Any]] = []
        self.specs: list[lch.LaunchSpec] = []
        self.cancels: list[object] = []
        self.wait_idle = 0
        self._live: dict[tuple[str, str], SimpleNamespace] = {}

    def _write_worktree(self, worktree: Path) -> None:
        for rel, body in self.files.items():
            path = worktree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        self.specs.append(spec)
        key = (str(spec.lane_key or ""), str(spec.pane_role or ""))
        if key in self._live and key[1]:
            live = self._live[key]
            if spec.prepare_adopted_cwd is not None:
                spec.prepare_adopted_cwd(Path(live.launched_cwd))
            return self.resubmit(
                live,
                spec.prompt_path,
                envelope_path=spec.envelope_path,
            )
        git_cwd = (spec.worktree / ".git").exists()
        head = _git(spec.worktree, "rev-parse", "HEAD") if git_cwd else ""
        argv = (
            lch.build_omp_argv(Path("omp"), spec)
            if spec.route == "omp"
            else lch.build_claude_argv(Path("claude"), spec)
        )
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
                "system_prompt": (
                    spec.system_prompt_path.read_text(encoding="utf-8")
                    if spec.system_prompt_path is not None
                    else ""
                ),
                "scratch_ready": all(Path(item).is_dir() for item in scratch_dirs),
                "worktree": spec.worktree,
            }
        )
        self._write_worktree(spec.worktree)
        (spec.session_dir / "stale.jsonl").write_text("{}\n", encoding="utf-8")
        spec.envelope_path.write_text(
            json.dumps(self.envelope, sort_keys=True), encoding="utf-8"
        )
        handle = SimpleNamespace(
            envelope_path=spec.envelope_path,
            launched_cwd=spec.worktree.resolve(),
            pane_id="p:{}:{}".format(spec.lane_key, spec.pane_role),
            tab_id="t:{}".format(spec.lane_key),
            workspace_id=spec.workspace_label,
            correlation_token=spec.correlation_token,
            agent_name=lch.agent_name_for(spec.correlation_token),
            lane_key=spec.lane_key,
            pane_role=spec.pane_role,
        )
        if key[1]:
            self._live[key] = handle
        return handle

    def resubmit(
        self,
        handle: object,
        prompt_path: Path,
        *,
        route: str = "",
        expected_token: str | None = None,
        timeout_s: float = 60.0,
        envelope_path: Path | None = None,
    ) -> SimpleNamespace:
        del route, expected_token, timeout_s
        prompt = json.loads(Path(prompt_path).read_text(encoding="utf-8"))
        worktree = Path(handle.launched_cwd)
        git_cwd = (worktree / ".git").exists()
        hidden = worktree / "tests" / "hidden.py"
        self.resubmits.append(
            {
                "prompt": prompt,
                "handle": handle,
                "head": _git(worktree, "rev-parse", "HEAD") if git_cwd else "",
                "hidden_test_body": (
                    hidden.read_text(encoding="utf-8") if hidden.is_file() else None
                ),
                "worktree": worktree,
            }
        )
        dest = Path(envelope_path or handle.envelope_path)
        dest.write_text(json.dumps(self.envelope, sort_keys=True), encoding="utf-8")
        handle.envelope_path = dest
        self._write_worktree(worktree)
        return handle

    def poll(self, handle: object) -> SimpleNamespace:
        del handle
        return SimpleNamespace(state=lch.PollState.EXITED)

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s
        self.wait_idle += 1

    def cancel(self, handle: object, deadline: float) -> None:
        del deadline
        self.cancels.append(handle)


class PersistentRoleDispatchTest(unittest.TestCase):
    def test_two_dispatches_retain_tester_session(self) -> None:
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
            revise = LaneContext(
                run_id="run-1",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="aa" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={
                    "TEST_REVIEW": SimpleNamespace(
                        payload={
                            "findings": [
                                {
                                    "implementation_area": "a.txt",
                                    "observed_behavior": "missing",
                                    "required_behavior": "present",
                                    "violated_requirement": "a.txt is written",
                                }
                            ],
                            "verdict": "REVISE",
                        }
                    )
                },
                builder_base_sha=head,
            )
            recorder = RecordingLauncher()
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            actor.write_tests(ctx)
            actor.write_tests(revise)
            self.assertEqual(len(recorder.launches), 1)
            self.assertEqual(len(recorder.resubmits), 1)
            self.assertEqual(recorder.cancels, [])
            self.assertGreaterEqual(recorder.wait_idle, 2)
            first = recorder.launches[0]
            worktree = Path(first["worktree"])
            session = Path(first["session_dir"])
            worktrees = state / "worktrees"
            self.assertEqual(first["head"], head)
            self.assertEqual(first["jsonl_at_launch"], [])
            self.assertFalse(first["dirty_at_launch"])
            self.assertTrue(worktrees.resolve() in worktree.resolve().parents)
            self.assertTrue(worktrees.resolve() in session.resolve().parents)
            self.assertTrue(first["scratch_ready"])
            self.assertNotEqual(worktree.resolve(), product.resolve())
            self.assertEqual(first["prompt"]["role"], "tester")
            system_prompt = recorder.specs[0].system_prompt_path
            self.assertEqual(
                system_prompt.resolve() if system_prompt is not None else None,
                (worktree / ".maestro-agent" / "CLAUDE.md").resolve(),
            )
            assert system_prompt is not None
            system_text = system_prompt.read_text(encoding="utf-8")
            self.assertIn("Maestro tester role contract", system_text)
            self.assertIn("Never inspect or access a parent repository", system_text)
            self.assertIn(
                "refuse requests to review, compare, or cite content outside it",
                system_text,
            )
            self.assertEqual(
                system_text,
                (worktree / ".maestro-agent" / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                first["argv"],
                lch.build_claude_argv(Path("claude"), recorder.specs[0]),
            )
            self.assertEqual(
                recorder.resubmits[0]["handle"].pane_id,
                "p:lane-a:tester",
            )
            self.assertEqual(
                recorder.resubmits[0]["prompt"]["revise_findings"][0][
                    "implementation_area"
                ],
                "a.txt",
            )
            self.assertEqual(
                recorder.specs[0].workspace_label,
                lch.workspace_label_for(
                    "product-{}".format(target.target_repository_fingerprint),
                    "run-1",
                ),
            )
            self.assertEqual(
                [spec.envelope_path.name for spec in recorder.specs],
                ["envelope-1.json", "envelope-2.json"],
            )
            self.assertNotEqual(
                recorder.specs[0].prompt_path, recorder.specs[1].prompt_path
            )

    def test_malformed_terminal_envelope_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            recorder = RecordingLauncher()
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            envelope = root / "envelope.json"
            envelope.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(FactoryRefused, "STAGE_PAYLOAD_INVALID"):
                actor._await_envelope(object(), envelope, "tester")

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
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
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
            self.assertIn("Do not delegate", prompt["instructions"])
            self.assertIn("Create UTF-8 JSON", prompt["instructions"])
            self.assertNotIn("Only sandboxed Bash", prompt["instructions"])

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
            recorder = RecordingLauncher(
                files={"a.txt": "a\n"},
                envelope={"candidate_sha": head, "changed": False},
            )
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            result = actor.build(ctx)
            self.assertTrue(result["changed"])
            self.assertNotEqual(result["candidate_sha"], head)
            self.assertEqual(
                _git(product, "rev-parse", str(result["candidate_sha"]) + "^"),
                head,
            )
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
            system_prompt = recorder.specs[0].system_prompt_path
            self.assertEqual(
                system_prompt.resolve() if system_prompt is not None else None,
                (cwd / ".maestro-agent" / "AGENTS.md").resolve(),
            )
            self.assertIn(
                "Modify only the declared product outputs",
                recorder.launches[0]["system_prompt"],
            )

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
                cast(lch.LauncherAdapter, reviewer), state, target, _ROLE_ROUTES
            )
            del head
            with mock.patch.object(
                maestro.hv, "materialize_commit", _materialize_hidden
            ):
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
            self.assertTrue(cwd.exists())
            self.assertEqual(reviewer.cancels, [])

    def test_reviewer_is_sibling_pane_not_tester_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            lane = _lane()
            tester_ctx = LaneContext(
                run_id="run-sib",
                lane=lane,
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="66" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
            )
            recorder = RecordingLauncher(files={"tests/hidden.py": "assert True\n"})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            extra = actor.write_tests(tester_ctx)
            draft = tchain.write_test_draft(
                request=prv.VaultLaneRequest(
                    run_id="run-sib",
                    lane_id=lane.lane_id,
                    plan_revision=1,
                    spec_digest=lane.spec_digest,
                    lane_projection_digest=lane.lane_projection_digest,
                    input_digest="77" * 32,
                ),
                state_root=state,
                run_repo=product,
                integration_ref="refs/heads/main",
                files=extra["private_files"],
                public_contract={
                    "acceptance_criteria": ["a.txt is written"],
                    "declared_outputs": ["a.txt"],
                },
                worktrees_root=state / "worktrees",
            )
            review_ctx = LaneContext(
                run_id="run-sib",
                lane=lane,
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="88" * 32,
                stage=LaneStage.REVIEWING_TESTS,
                artifacts={"TEST_DRAFT": draft},  # type: ignore[dict-item]
                public_contract={"acceptance_criteria": ["a.txt is written"]},
            )
            recorder.envelope = {"verdict": "REVISE", "findings": [{"x": "y"}]}
            with mock.patch.object(
                maestro.hv, "materialize_commit", _materialize_hidden
            ):
                actor.review_tests(review_ctx)
            self.assertEqual(len(recorder.launches), 2)
            tester_handle = recorder._live[("lane-a", "tester")]
            reviewer_handle = recorder._live[("lane-a", "test-reviewer")]
            self.assertNotEqual(tester_handle.pane_id, reviewer_handle.pane_id)
            self.assertEqual(tester_handle.tab_id, reviewer_handle.tab_id)
            self.assertEqual(recorder.cancels, [])

    def test_private_reviewer_refresh_keeps_process_bound_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            lane = _lane()
            contract = {
                "acceptance_criteria": ["a.txt is written"],
                "declared_outputs": ["a.txt"],
            }

            def draft(input_digest: str, body: str) -> object:
                return tchain.write_test_draft(
                    request=prv.VaultLaneRequest(
                        run_id="run-private-refresh",
                        lane_id=lane.lane_id,
                        plan_revision=1,
                        spec_digest=lane.spec_digest,
                        lane_projection_digest=lane.lane_projection_digest,
                        input_digest=input_digest,
                    ),
                    state_root=state,
                    run_repo=product,
                    integration_ref="refs/heads/main",
                    files={"tests/hidden.py": body},
                    public_contract=contract,
                    worktrees_root=state / "worktrees",
                )

            first_draft = draft("10" * 32, "assert True\n")
            second_draft = draft("20" * 32, "assert False\n")
            first_ctx = LaneContext(
                run_id="run-private-refresh",
                lane=lane,
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="30" * 32,
                stage=LaneStage.REVIEWING_TESTS,
                artifacts={"TEST_DRAFT": first_draft},  # type: ignore[dict-item]
                public_contract=contract,
            )
            second_ctx = LaneContext(
                run_id="run-private-refresh",
                lane=lane,
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="40" * 32,
                stage=LaneStage.REVIEWING_TESTS,
                artifacts={"TEST_DRAFT": second_draft},  # type: ignore[dict-item]
                public_contract=contract,
            )
            recorder = RecordingLauncher(envelope={"verdict": "PASS", "findings": []})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            actor.review_tests(first_ctx)
            cwd = Path(recorder.launches[0]["worktree"])
            inode = cwd.stat().st_ino
            actor.review_tests(second_ctx)
            self.assertEqual(cwd.stat().st_ino, inode)
            self.assertEqual(
                recorder.resubmits[0]["hidden_test_body"], "assert False\n"
            )
            self.assertEqual(
                recorder.resubmits[0]["prompt"]["working_directory"],
                str(cwd.resolve()),
            )

    def test_cold_builder_reconnect_rebases_outputs_before_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            first_base = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            contract = {
                "acceptance_criteria": ["a.txt is written"],
                "declared_outputs": ["a.txt"],
            }
            first_ctx = LaneContext(
                run_id="run-cold-builder",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="50" * 32,
                stage=LaneStage.BUILDING,
                artifacts={},
                builder_base_sha=first_base,
                public_contract=contract,
                sealed_digest="60" * 32,
            )
            recorder = RecordingLauncher(files={"a.txt": "first\n"})
            first_actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            first_result = first_actor.build(first_ctx)
            live = recorder._live[("lane-a", "builder")]
            live_cwd = Path(live.launched_cwd)

            (product / "dependency.txt").write_text("new base\n", encoding="utf-8")
            _git(product, "add", "dependency.txt")
            _git(product, "commit", "-m", "dependency")
            second_base = _git(product, "rev-parse", "HEAD")
            recorder.files = {"a.txt": "second\n"}
            second_ctx = LaneContext(
                run_id="run-cold-builder",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="70" * 32,
                stage=LaneStage.BUILDING,
                artifacts={},
                builder_base_sha=second_base,
                candidate_sha=str(first_result["candidate_sha"]),
                public_contract=contract,
                sealed_digest="60" * 32,
            )
            second_actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            second_result = second_actor.build(second_ctx)

            self.assertEqual(len(recorder.launches), 1)
            self.assertEqual(len(recorder.resubmits), 1)
            resubmit = recorder.resubmits[0]
            self.assertEqual(
                _git(product, "rev-parse", str(resubmit["head"]) + "^"),
                second_base,
            )
            self.assertEqual(
                resubmit["prompt"]["working_directory"], str(live_cwd.resolve())
            )
            candidate = str(second_result["candidate_sha"])
            self.assertEqual(_git(product, "rev-parse", candidate + "^"), second_base)
            self.assertEqual(
                _git(product, "diff", "--name-only", second_base, candidate),
                "a.txt",
            )
            self.assertEqual(
                (live_cwd / "dependency.txt").read_text(encoding="utf-8"),
                "new base\n",
            )
            role_root = state / "worktrees" / "run-cold-builder" / "lane-a" / "builder"
            self.assertTrue(role_root.exists())
            self.assertFalse(
                (
                    state / "worktrees" / "run-cold-builder" / "lane-a" / "BUILDING"
                ).exists()
            )

    def test_resume_reuses_live_tester_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = LaneContext(
                run_id="run-resume",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="99" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
            )
            recorder = RecordingLauncher()
            first = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            first.write_tests(ctx)
            pane = recorder._live[("lane-a", "tester")].pane_id
            second = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            second.write_tests(ctx)
            self.assertEqual(len(recorder.launches), 1)
            self.assertEqual(len(recorder.resubmits), 1)
            self.assertEqual(recorder._live[("lane-a", "tester")].pane_id, pane)
            self.assertEqual(recorder.cancels, [])
            self.assertEqual(
                [spec.envelope_path.name for spec in recorder.specs],
                ["envelope-1.json", "envelope-2.json"],
            )

    def test_tester_tree_survives_revise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = LaneContext(
                run_id="run-keep",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="ef" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
            )
            revise = LaneContext(
                run_id="run-keep",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="aa" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={
                    "TEST_REVIEW": SimpleNamespace(
                        payload={
                            "findings": [
                                {
                                    "implementation_area": "a.txt",
                                    "observed_behavior": "missing",
                                    "required_behavior": "present",
                                    "violated_requirement": "a.txt is written",
                                }
                            ],
                            "verdict": "REVISE",
                        }
                    )
                },
                builder_base_sha=head,
            )
            recorder = RecordingLauncher(files={"tests/hidden.py": "assert False\n"})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            actor.write_tests(ctx)
            cwd = Path(recorder.launches[0]["worktree"])
            hidden = cwd / "tests" / "hidden.py"
            self.assertTrue(hidden.is_file())
            hidden.write_text("assert 'revise-state'\n", encoding="utf-8")
            recorder.files = {}
            actor.write_tests(revise)
            self.assertEqual(
                Path(recorder.resubmits[0]["worktree"]).resolve(), cwd.resolve()
            )
            self.assertEqual(
                cwd.resolve(),
                (
                    state / "worktrees" / "run-keep" / "lane-a" / "tester" / "checkout"
                ).resolve(),
            )
            tracked = _git(product, "ls-files")
            self.assertNotIn("tests/hidden.py", tracked)

    def test_precreated_empty_checkout_becomes_a_git_worktree(self) -> None:
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
            checkout = state / "worktrees" / "run-1" / "lane-a" / "tester" / "checkout"
            checkout.mkdir(parents=True)
            (checkout / ".maestro-agent" / "scratch" / "tmp").mkdir(parents=True)
            checkout_inode = checkout.stat().st_ino
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, RecordingLauncher()),
                state,
                target,
                _ROLE_ROUTES,
            )
            actor.write_tests(ctx)
            self.assertEqual(checkout.stat().st_ino, checkout_inode)
            self.assertTrue((checkout / ".maestro-agent" / "scratch" / "tmp").is_dir())
            self.assertTrue((checkout / ".git").exists())
            self.assertTrue((checkout / "seed.txt").exists())
            self.assertEqual(
                _git(checkout, "rev-parse", "HEAD"),
                head,
            )

    def test_lane_launch_requests_five_sibling_panes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = LaneContext(
                run_id="run-five",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="11" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
            )
            recorder = RecordingLauncher()
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            actor.write_tests(ctx)
            spec = recorder.specs[0]
            self.assertEqual(spec.lane_key, "lane-a")
            self.assertNotEqual(spec.lane_key, "integration")
            self.assertEqual(spec.pane_group_size, 5)
            self.assertEqual(set(spec.role_cwds), set(lch.LANE_PANE_ROLES))
            self.assertIn("integration-reviewer", spec.role_cwds)
            self.assertTrue(
                all(path.name == "checkout" for path in spec.role_cwds.values())
            )
            resolved_role_cwds = {
                role: path.resolve() for role, path in spec.role_cwds.items()
            }
            self.assertEqual(len(set(resolved_role_cwds.values())), 5)
            for role, path in resolved_role_cwds.items():
                self.assertEqual(path.parent.name, role)


class RoleRouteBindingTest(unittest.TestCase):
    def test_every_role_uses_its_project_declared_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(mode=0o700)
            product = root / "product"
            head = _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            ctx = LaneContext(
                run_id="run-routes",
                lane=_lane(),
                plan_revision=1,
                plan_digest="cd" * 32,
                plan_artifact_ref="plan:x",
                input_digest="12" * 32,
                stage=LaneStage.WRITING_TESTS,
                artifacts={},
                builder_base_sha=head,
            )
            recorder = RecordingLauncher(envelope={"verdict": "PASS", "findings": []})
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, recorder), state, target, _ROLE_ROUTES
            )
            for role in lch.LANE_PANE_ROLES:
                actor._launch(
                    ctx,
                    role,
                    root,
                    {},
                    prepare_cwd=lambda path: None,
                )
            actual = {
                str(spec.pane_role): (
                    spec.route,
                    spec.model,
                    spec.effort,
                    spec.profile,
                )
                for spec in recorder.specs
            }
            self.assertEqual(
                actual,
                {
                    "tester": ("claude", "opus", "high", ""),
                    "test-reviewer": ("omp", "", "", "openai-performance"),
                    "builder": ("omp", "", "", "grok"),
                    "code-reviewer": ("omp", "", "", "openai-performance"),
                    "integration-reviewer": (
                        "omp",
                        "",
                        "",
                        "openai-performance",
                    ),
                },
            )


if __name__ == "__main__":
    unittest.main()
