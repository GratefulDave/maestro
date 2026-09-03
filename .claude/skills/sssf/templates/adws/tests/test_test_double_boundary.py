"""Both tester rules carry the test-double boundary.

A tester can only report an unreachable subject if it has the words for one.
`lane-wp7-gw-issue-build` did not: its acceptance posted an issued token to
`/v1/faers/dpa`, whose upstream in the test environment is a `SourceHandler`
stand-in owned by a different lane that routes a fixed path list and 404s
everything else. The route raised `SOURCE_ERROR` on every attempt. The lane
could not fix the stand-in, could not pass without it, and had no sanctioned
way to say so, so it spent three attempts and parked with no candidate.

These assert the assembled role contract the tester actually reads -- the
bytes written to `AGENTS.md` / `CLAUDE.md` -- not the constant it is built
from. A test over `maestro.TEST_DOUBLE_BOUNDARY` would still pass if the
append were deleted.
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


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *argv: subprocess.run(  # noqa: E731
        argv, cwd=path, check=True, capture_output=True
    )
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "factory@example.invalid")
    run("git", "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("git", "add", "seed.txt")
    run("git", "commit", "-q", "-m", "seed")


class _SilentLauncher:
    """Never called. `_materialize_role_instructions` writes files only."""


class TestDoubleBoundaryInRoleContract(unittest.TestCase):
    def _contract(self, role: str, route: str, lane_kind: str | None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product = root / "product"
            state = root / "state"
            state.mkdir(mode=0o700)
            _init_repo(product)
            target = gitpub.bind_target_worktree(product, "refs/heads/main")
            actor = maestro.HerdrStageActor(
                cast(lch.LauncherAdapter, _SilentLauncher()),
                state,
                target,
                _ROLE_ROUTES,
            )
            cwd = root / "role-cwd"
            cwd.mkdir()
            written = actor._materialize_role_instructions(
                cwd, role, route, lane_kind
            )
            return written.read_text(encoding="utf-8")

    def test_a_tests_lane_tester_is_told_where_a_double_belongs(self) -> None:
        contract = self._contract("tester", "omp", st.LANE_KIND_TESTS)
        self.assertIn("Where a test double belongs", contract)
        self.assertIn("Author files exactly at declared_outputs", contract)

    def test_a_hidden_validator_tester_is_told_the_same(self) -> None:
        """The seam does not depend on which kind of tester draws it."""
        contract = self._contract("tester", "omp", None)
        self.assertIn("Where a test double belongs", contract)
        self.assertIn("byte-identical hidden files", contract)

    def test_the_wp7_shape_is_named_not_merely_implied(self) -> None:
        """A whitelist dispatcher is the thing that cost the run."""
        contract = self._contract("tester", "omp", st.LANE_KIND_TESTS)
        self.assertIn("whitelist of paths", contract)
        self.assertIn("second implementation", contract)

    def test_an_unreachable_subject_is_a_complete_answer(self) -> None:
        """Without this the tester's only move is to assert through the fake."""
        contract = self._contract("tester", "omp", st.LANE_KIND_TESTS)
        self.assertIn("Say that in the envelope", contract)
        self.assertIn("asserting through it is not", contract)

    def test_the_boundary_does_not_leak_into_other_roles(self) -> None:
        """It is authoring guidance for the tester, not a review axis.

        A reviewer that read this would start voting REVISE on it, which is
        the unbounded-loop shape the tests lane has no defence against.
        """
        for role in ("test-reviewer", "builder", "code-reviewer"):
            with self.subTest(role=role):
                contract = self._contract(role, "omp", None)
                self.assertNotIn("Where a test double belongs", contract)


if __name__ == "__main__":
    unittest.main()
