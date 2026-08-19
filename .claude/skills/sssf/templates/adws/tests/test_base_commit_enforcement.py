"""#32 — single-repo run start compares integration tip to plan.base_commit."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import plan_model as pm
from adw_modules import worktree as wt


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


class BaseCommitEnforcementTest(unittest.TestCase):

    def _repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "Test")
        (repo / "f.txt").write_text("one\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "one")
        return repo

    def _plan(self, base: str) -> pm.Plan:
        return SimpleNamespace(
            base_commit=base,
            merge_policy=SimpleNamespace(integration_branch="main"),
        )

    def test_matching_tip_is_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            head = wt.integration_head(repo, "main")
            args = SimpleNamespace(repo=str(repo))
            self.assertIsNone(
                maestro._refuse_base_commit_divergence(args, self._plan(head)))

    def test_a_moved_main_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            base = wt.integration_head(repo, "main")
            (repo / "f.txt").write_text("two\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "two")
            args = SimpleNamespace(repo=str(repo))
            refused = maestro._refuse_base_commit_divergence(
                args, self._plan(base))
        self.assertIsNotNone(refused)
        self.assertEqual(refused, 3)


    def test_an_abbreviated_sha_of_the_same_commit_is_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            head = wt.integration_head(repo, "main")
            args = SimpleNamespace(repo=str(repo))
            self.assertIsNone(
                maestro._refuse_base_commit_divergence(
                    args, self._plan(head[:12])))

    def test_an_unresolvable_recorded_base_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            args = SimpleNamespace(repo=str(repo))
            refused = maestro._refuse_base_commit_divergence(
                args, self._plan("deadbeef" * 5))
        self.assertEqual(refused, 3)


if __name__ == "__main__":
    unittest.main()
