"""run start binds deployment config, --repo, and Git common dir."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml

import maestro


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
                "runner_profile": "grok-maestro",
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


if __name__ == "__main__":
    unittest.main()
