"""The standards rubric has a reader: the assembled code-reviewer instruction.

A rule that exists only as a constant discharges nothing (IMPROVEMENTS_PLAN B4).
These cases pin the rubric to the instruction file the code reviewer is
handed, and to no other role.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import maestro  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import review_standards as rvs  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

_ROLE_ROUTES = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    (path / "CONTRIBUTING.md").write_text("# house style\n", encoding="utf-8")
    _git(path, "add", "seed.txt", "CONTRIBUTING.md")
    _git(path, "commit", "-m", "seed")


class _SilentLauncher:
    """The role contract is written before anything is launched."""


class StandardsRubricReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.product = self.root / "product"
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        _init_repo(self.product)
        target = gitpub.bind_target_worktree(self.product, "refs/heads/main")
        self.actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, _SilentLauncher()),
            self.state,
            target,
            _ROLE_ROUTES,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _instruction(self, role: str, lane_kind: str | None = None) -> str:
        cwd = self.root / "cwd-{0}-{1}".format(role, lane_kind or "none")
        cwd.mkdir()
        path = self.actor._materialize_role_instructions(
            cwd, role, "omp", lane_kind
        )
        return path.read_text(encoding="utf-8")

    def test_code_reviewer_instruction_carries_every_smell_and_rule(self):
        text = self._instruction("code-reviewer", "build")
        for smell in rvs.STANDARDS_SMELLS:
            self.assertIn(smell[0], text)
        self.assertIn("Shallow module", text)
        for rule in rvs.STANDARDS_RULES:
            self.assertIn(rule, text)

    def test_code_reviewer_is_told_the_repo_standards_path_not_its_bytes(self):
        text = self._instruction("code-reviewer", "build")
        self.assertIn("CONTRIBUTING.md", text)
        self.assertNotIn("# house style", text)

    def test_other_roles_are_not_handed_the_standards_axis(self):
        for role, kind in (
            ("tester", "tests"),
            ("test-reviewer", "tests"),
            ("builder", "build"),
            ("integration-reviewer", None),
        ):
            with self.subTest(role=role):
                self.assertNotIn("Feature Envy", self._instruction(role, kind))

    def test_code_reviewer_schema_advertises_axis_and_severity(self):
        schema = self.actor._schema("code-reviewer")
        for key in st.FINDING_OPTIONAL_KEYS:
            self.assertIn(key, schema["findings"])
        self.assertNotIn(
            list(st.FINDING_OPTIONAL_KEYS)[0],
            self.actor._schema("test-reviewer")["findings"],
        )


if __name__ == "__main__":
    unittest.main()
