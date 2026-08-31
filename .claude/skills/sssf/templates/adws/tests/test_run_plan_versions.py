"""Plan revisions change only through apply_amendment."""

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


def _plan(*, goal: str = "emit a.txt") -> bytes:
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


class AmendmentIsApplyAmendmentTest(unittest.TestCase):
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

    def test_create_run_records_revision_one(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan(), plan_revision=1, plan_artifact_ref="plan:v1"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id="run-amend",
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        row = sch.run_row(self.store, "run-amend")
        self.assertEqual(row["plan_revision"], 1)

    def test_apply_amendment_is_the_only_revision_writer(self) -> None:
        compiled = plan_compiler.compile_plan(
            _plan(), plan_revision=1, plan_artifact_ref="plan:v1"
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id="run-amend",
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        amended = plan_compiler.compile_plan(
            _plan(goal="emit a.txt changed"),
            plan_revision=2,
            plan_artifact_ref="plan:v2",
        )
        sch.apply_factory_amendment(
            self.store,
            "run-amend",
            amended,
            runtime=self.runtime,
            target=target,
        )
        row = sch.run_row(self.store, "run-amend")
        self.assertEqual(row["plan_revision"], 2)
        self.assertEqual(row["plan_digest"], amended.plan_digest)

    def test_cli_amend_is_the_only_amendment_verb(self) -> None:
        verbs = maestro.parser_verbs(maestro.build_parser())
        self.assertIn("run amend", verbs)
        self.assertNotIn("plan ship", verbs)
        with self.assertRaises(SystemExit):
            maestro.build_parser().parse_args(["run", "amend"])


if __name__ == "__main__":
    unittest.main()
