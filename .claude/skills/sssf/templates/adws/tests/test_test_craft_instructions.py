"""The tester and the test reviewer carry the test-craft vocabulary.

A tautological or implementation-coupled case only becomes a located finding if
both roles have the words for it. These assert the assembled role contract the
role actually reads, not the constants it is built from.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

_ROLE_ROUTES = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}

ANTIPATTERN_NAMES = (
    "Implementation-coupled",
    "Tautological",
    "Horizontal slicing / shape-asserting",
)

TELLS = (
    "the test breaks on a refactor with no behaviour change",
    "assert add(2, 3) == 2 + 3",
    "assertions on structure (keys exist, type is list) with no "
    "behavioural expectation",
)


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
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")


class _SilentLauncher:
    """The role contract is written before anything is launched."""


class TestCraftInstructions(unittest.TestCase):
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

    def test_tester_instruction_names_the_three_antipatterns_and_tells(self):
        text = self._instruction("tester", st.LANE_KIND_TESTS)
        for name in ANTIPATTERN_NAMES:
            self.assertIn(name, text)
        for tell in TELLS:
            self.assertIn(tell, text)
        self.assertIn("red before green", text)
        self.assertIn("one seam, one test per cycle", text)
        self.assertIn("refactoring is not part of the loop", text)

    def test_hidden_validator_tester_carries_the_same_vocabulary(self):
        text = self._instruction("tester", "build")
        for name in ANTIPATTERN_NAMES:
            self.assertIn(name, text)
        for tell in TELLS:
            self.assertIn(tell, text)

    def test_test_reviewer_must_name_the_case_id(self):
        text = self._instruction("test-reviewer")
        self.assertEqual(text.count("with the case id"), 3)
        self.assertIn("implementation-coupled", text)
        self.assertIn("tautological", text)
        self.assertIn("shape rather than behaviour", text)
        self.assertIn(
            "A named case is a located finding that discharges nothing, so "
            "the verdict is REVISE.",
            text,
        )

    def test_the_builder_is_not_told_about_test_craft(self):
        text = self._instruction("builder")
        for name in ANTIPATTERN_NAMES:
            self.assertNotIn(name, text)


if __name__ == "__main__":
    unittest.main()
