"""run start binds deployment config, --repo, and Git common dir."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

import maestro


def _role_routes() -> dict[str, dict[str, str]]:
    return {
        "tester": {"route": "claude", "model": "opus", "effort": "high"},
        "test-reviewer": {"route": "omp", "profile": "openai-performance"},
        "builder": {"route": "omp", "profile": "grok"},
        "code-reviewer": {"route": "omp", "profile": "openai-performance"},
        "integration-reviewer": {
            "route": "omp",
            "profile": "openai-performance",
        },
    }


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


def _install_deployment(product: Path, state: Path) -> Path:
    adws = product / "adws"
    adws.mkdir()
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


def _outcome(argv: list[str]) -> tuple[int, dict]:
    buf = StringIO()
    with mock.patch("sys.stdout", buf):
        code = maestro.main(argv)
    text = buf.getvalue().strip()
    return code, json.loads(text) if text else {}


class RunStartBindsRepositoryStateTest(unittest.TestCase):
    def test_start_requires_repo_and_main_ref(self) -> None:
        parser = maestro.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["run", "start", "plan.json", "--main-ref", "refs/heads/main"]
            )
        args = parser.parse_args(
            [
                "run",
                "start",
                "plan.json",
                "--repo",
                "/abs/product",
                "--main-ref",
                "refs/heads/main",
            ]
        )
        self.assertEqual(args.repo, "/abs/product")
        self.assertEqual(args.main_ref, "refs/heads/main")

    def test_config_is_loaded_from_executing_deployment_not_cwd_or_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy = root / "deploy"
            other = root / "other"
            cwd = root / "cwd"
            state = root / "state"
            state.mkdir(mode=0o700)
            _init_repo(deploy)
            _init_repo(other)
            cwd.mkdir()
            (cwd / "adws").mkdir()
            (cwd / "adws" / "maestro.config.yaml").write_text(
                "schema: maestro-config.v1\nruntime_state_root: /cwd/state\n",
                encoding="utf-8",
            )
            (other / "adws").mkdir()
            (other / "adws" / "maestro.config.yaml").write_text(
                "schema: maestro-config.v1\nruntime_state_root: /other/state\n",
                encoding="utf-8",
            )
            maestro_file = _install_deployment(deploy, state)
            loaded = maestro._load_deployment_config(maestro_file)
            self.assertEqual(loaded["repo"], deploy.resolve())
            self.assertEqual(loaded["runtime_state_root"], state.resolve())
            self.assertNotEqual(loaded["runtime_state_root"], Path("/cwd/state"))
            self.assertNotEqual(loaded["runtime_state_root"], Path("/other/state"))

    def test_relative_runtime_state_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config = repo / "adws" / "maestro.config.yaml"
            config.parent.mkdir()
            config.write_text(
                "schema: maestro-config.v1\nruntime_state_root: state\n",
                encoding="utf-8",
            )
            with self.assertRaises(maestro._MaestroConfigurationError):
                maestro._load_maestro_config(repo, config)

    def test_template_source_start_refuses(self) -> None:
        code, payload = _outcome(
            [
                "run",
                "start",
                "plan.json",
                "--repo",
                "/abs/product",
                "--main-ref",
                "refs/heads/main",
            ]
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_REPOSITORY_MISMATCH")

    def test_wrong_common_dir_start_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy = root / "deploy"
            target = root / "target"
            state = root / "state"
            state.mkdir(mode=0o700)
            _init_repo(deploy)
            _init_repo(target)
            maestro_file = _install_deployment(deploy, state)
            with mock.patch.object(
                maestro, "_executing_maestro_file", return_value=maestro_file
            ):
                code, payload = _outcome(
                    [
                        "run",
                        "start",
                        "plan.json",
                        "--repo",
                        str(target),
                        "--main-ref",
                        "refs/heads/main",
                    ]
                )
        self.assertEqual(code, 3)
        self.assertEqual(payload["outcome"], "RUN_REPOSITORY_MISMATCH")

    def test_start_has_no_legacy_state_or_reclaim_flags(self) -> None:
        parser = maestro.build_parser()
        args = parser.parse_args(
            [
                "run",
                "start",
                "plan.json",
                "--repo",
                "/abs/product",
                "--main-ref",
                "refs/heads/main",
            ]
        )
        self.assertFalse(hasattr(args, "data_dir"))
        self.assertFalse(hasattr(args, "state_root"))
        self.assertFalse(hasattr(args, "reclaim"))

    def test_start_prints_run_and_stage_before_scheduler_returns(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        stdout = StringIO()
        result: list[int] = []
        failure: list[BaseException] = []
        runtime = mock.Mock()
        runtime.path = Path("/runtime")
        runtime.ledger_path.return_value = Path("/runtime/lifecycle.sqlite3")
        store = mock.Mock()
        target = SimpleNamespace(
            target_repository_root="/product",
            target_main_ref="refs/heads/main",
        )
        compiled = SimpleNamespace(lanes=(SimpleNamespace(lane_id="lane-a"),))

        class BlockingScheduler:
            def __init__(self, *_args: object, **kwargs: object) -> None:
                self.stage_started = kwargs["stage_started"]
                self.stage_completed = kwargs["stage_completed"]

            def run(self) -> maestro.st.RunStatus:
                self.stage_started("lane-a", maestro.st.LaneStage.WRITING_TESTS)
                entered.set()
                release.wait(5)
                self.stage_completed(
                    "lane-a",
                    maestro.st.LaneStage.WRITING_TESTS,
                    maestro.st.LaneStage.REVIEWING_TESTS,
                )
                return maestro.st.RunStatus.WAITING

        args = argparse.Namespace(
            plan="plan.json",
            repo="/product",
            main_ref="refs/heads/main",
            run_id="run-live",
        )

        def invoke() -> None:
            try:
                result.append(maestro._run_start(args))
            except BaseException as exc:
                failure.append(exc)

        with (
            mock.patch("sys.stdout", stdout),
            mock.patch.object(
                maestro,
                "_executing_maestro_file",
                return_value=Path("/deploy/maestro.py"),
            ),
            mock.patch.object(maestro, "_load_deployment_config", return_value={}),
            mock.patch.object(maestro, "require_deployment"),
            mock.patch.object(maestro, "_open_runtime", return_value=runtime),
            mock.patch.object(maestro, "_compile_plan", return_value=compiled),
            mock.patch.object(
                maestro.gitpub, "bind_target_worktree", return_value=target
            ),
            mock.patch.object(maestro, "_open_store", return_value=store),
            mock.patch.object(maestro, "create_factory_run"),
            mock.patch.object(maestro, "register_installation"),
            mock.patch.object(maestro, "_actor_for", return_value=mock.Mock()),
            mock.patch.object(maestro, "FactoryScheduler", BlockingScheduler),
        ):
            thread = threading.Thread(target=invoke)
            thread.start()
            self.assertTrue(entered.wait(2))
            live = stdout.getvalue()
            self.assertIn("run-live", live)
            self.assertIn("lane-a", live)
            self.assertIn("WRITING_TESTS", live)
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failure, [])
        self.assertEqual(result, [0])
        self.assertIn('"outcome": "STARTED"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
