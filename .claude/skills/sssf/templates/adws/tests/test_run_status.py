"""run status derives from ArtifactStore. No list/cancel/pause."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import plan_compiler
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot


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


def _plan_bytes() -> bytes:
    document = {
        "schema_version": "maestro-plan.artifact-factory.v1",
        "lanes": [
            {
                "id": "lane-a",
                "needs": [],
                "outputs": ["a.txt"],
                "spec": {
                    "goal": "emit a.txt",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["a.txt is written"],
            },
            {
                "id": "lane-b",
                "needs": ["lane-a"],
                "outputs": ["b.txt"],
                "spec": {
                    "goal": "emit b.txt after a",
                    "integration": {"integration_branch": "refs/heads/main"},
                },
                "acceptance": ["b.txt is written"],
            },
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class RunStatusDerivedFromStoreTest(unittest.TestCase):
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

    def test_status_is_derived_from_lane_stages(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan_bytes(), plan_revision=1, plan_artifact_ref="plan:status"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        run_id = "run-status"
        sch.create_factory_run(
            store=self.store,
            run_id=run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        status = self.store.derive_run_status(run_id, target.integration_initial_sha)
        self.assertEqual(status, st.RunStatus.EXECUTING)
        self.assertEqual(self.store.lane_stage(run_id, "lane-a"), st.LaneStage.PLANNED)
        self.assertNotEqual(status.value, "RUNNING")

    def test_unknown_run_id_is_typed_cli_refusal(self) -> None:
        with self.assertRaises(SystemExit):
            maestro.build_parser().parse_args(["run", "status"])
        with self.assertRaises(SystemExit):
            maestro.build_parser().parse_args(["run", "list"])
        with self.assertRaises(SystemExit):
            maestro.build_parser().parse_args(["run", "cancel", "run-1"])

    def test_status_positional_run_id_is_the_only_selector(self) -> None:
        args = maestro.build_parser().parse_args(["run", "status", "run-abc"])
        self.assertEqual(args.run_id, "run-abc")
        self.assertFalse(hasattr(args, "plan"))


def _install_deployment(product: Path, state: Path) -> Path:
    adws = product / "adws"
    adws.mkdir(exist_ok=True)
    maestro_file = adws / "maestro.py"
    maestro_file.write_text("# deployment\n", encoding="utf-8")
    (adws / "maestro.config.yaml").write_text(
        "schema: maestro-config.v1\n"
        f"runtime_state_root: {state.resolve()}\n"
        "role_routes:\n"
        "  tester: {route: omp, profile: grok-maestro}\n"
        "  test-reviewer: {route: omp, profile: grok-maestro}\n"
        "  builder: {route: omp, profile: grok-maestro}\n"
        "  code-reviewer: {route: omp, profile: grok-maestro}\n"
        "  integration-reviewer: {route: omp, profile: grok-maestro}\n",
        encoding="utf-8",
    )
    return maestro_file


def _outcome(argv: list[str]) -> tuple[int, dict]:
    from io import StringIO
    from unittest import mock

    buf = StringIO()
    with mock.patch("sys.stdout", buf):
        code = maestro.main(argv)
    text = buf.getvalue().strip()
    return code, json.loads(text) if text else {}


class ResumeAmendStatusBindDeploymentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.product = self.root / "product"
        self.other = self.root / "other"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.product)
        _init_repo(self.other)
        self.runtime = RuntimeStateRoot(
            self.state.resolve(), overlap_paths=(self.product,)
        )
        self.runtime.ensure_layout()
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.plan = self.root / "plan.json"
        self.plan.write_bytes(_plan_bytes())
        compiled = plan_compiler.compile_plan(
            _plan_bytes(),
            plan_revision=1,
            plan_artifact_ref=str(self.plan.resolve()),
        )
        self.target = gitpub.bind_target_worktree(self.product, "refs/heads/main")
        self.run_id = "run-bind"
        sch.create_factory_run(
            store=self.store,
            run_id=self.run_id,
            compiled=compiled,
            runtime=self.runtime,
            target=self.target,
        )
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.addCleanup(self.tmp.cleanup)

    def test_template_source_resume_amend_status_refuse(self) -> None:
        for argv in (
            ["run", "resume", self.run_id],
            ["run", "amend", str(self.plan), "--run", self.run_id],
            ["run", "status", self.run_id],
        ):
            with self.subTest(argv=argv):
                code, payload = _outcome(list(argv))
                self.assertEqual(code, 3)
                self.assertEqual(payload["outcome"], "RUN_REPOSITORY_MISMATCH")

    def test_wrong_common_dir_resume_amend_status_refuse(self) -> None:
        from unittest import mock

        maestro_file = _install_deployment(self.other, self.state)
        with mock.patch.object(
            maestro, "_executing_maestro_file", return_value=maestro_file
        ):
            for argv in (
                ["run", "resume", self.run_id],
                ["run", "amend", str(self.plan), "--run", self.run_id],
                ["run", "status", self.run_id],
            ):
                with self.subTest(argv=argv):
                    code, payload = _outcome(list(argv))
                    self.assertEqual(code, 3)
                    self.assertEqual(payload["outcome"], "RUN_REPOSITORY_MISMATCH")

    def test_existing_run_binding_reads_plan_ref_from_active_revision(self) -> None:
        from unittest import mock

        maestro_file = _install_deployment(self.product, self.state)
        with (
            mock.patch.object(
                maestro, "_executing_maestro_file", return_value=maestro_file
            ),
            mock.patch.object(maestro, "register_installation") as register,
        ):
            _layout, runtime, store, row, _target, _compiled = maestro._bind_existing_run(
                self.run_id
            )
        try:
            self.assertNotIn("plan_artifact_ref", row)
            register.assert_called_once_with(
                database=self.runtime.ledger_path(),
                plans_dir=self.plan.resolve().parent,
                repository=str(self.product.resolve()),
                state=self.state.resolve(),
            )
        finally:
            store.close()
            runtime.close()

    def test_config_source_is_executing_deployment_not_target_worktree(self) -> None:
        publication = self.root / "publication"
        _init_repo(publication)
        (publication / "adws").mkdir()
        (publication / "adws" / "maestro.config.yaml").write_text(
            "schema: maestro-config.v1\nruntime_state_root: /publication/state\n",
            encoding="utf-8",
        )
        maestro_file = _install_deployment(self.other, self.state)
        loaded = maestro._load_deployment_config(maestro_file)
        self.assertEqual(loaded["repo"], self.other.resolve())
        self.assertEqual(loaded["runtime_state_root"], self.state.resolve())
        self.assertNotEqual(loaded["runtime_state_root"], Path("/publication/state"))


if __name__ == "__main__":
    unittest.main()
