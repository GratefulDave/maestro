"""§8.3: the measurement bracket's universe is git's working tree, not the disk.

The incident these tests are written from: a lane node ran its ecosystem's own
install step inside its attempt worktree, `.venv/` appeared with 16090 files,
and the after-inventory — a bare `os.walk` with "no excludes and no ignore
list" — counted every one of them as an undeclared write. §7.3 clause 4 failed
with 16090 offending paths and the attempt was discarded after 209 turns, over
content `.gitignore` had excluded from the repository all along and that §8.4's
private-index commit could never have carried onto the integration branch.

The fix narrows the check's INPUTS rather than adding a name to a permitted
set: `inventory()` enumerates through `git ls-files --cached --others
--exclude-standard`, so a git-ignored path is not a path this bracket has an
opinion about. Everything git would still carry — tracked content, untracked
content it would commit, and a path tracked in spite of an ignore rule — stays
fully in scope, which is what conjunct (2) needs to keep convicting.

Every test builds a real throwaway repository and runs real git, because this
is a question about what git does with an exclude file and a mock would only
assert our own answer back at us.

Run with:
    python -m pytest tests/test_permission_delta_ignored_paths.py -o addopts= -q
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import verification as vf  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} -> {result.returncode}: {result.stderr}")
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class IgnoredPathsTestCase(unittest.TestCase):
    """A repository whose `.gitignore` excludes `.venv/`, as a real one does."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "harness@example.invalid")
        _git(self.repo, "config", "user.name", "Harness")
        _git(self.repo, "config", "core.hooksPath", str(self.root / "no-such-hooks"))
        _write(self.repo / ".gitignore", ".venv/\n*.log\n")
        _write(self.repo / "src" / "app.py", "VALUE = 1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _attempt(self) -> wt.AttemptWorktree:
        return wt.create_attempt_worktree(
            repo=self.repo,
            run_id="run1",
            node_id="lane-x",
            attempt_no=1,
            integration_head=_git(self.repo, "rev-parse", "HEAD"),
            worktrees_root=self.root / "worktrees",
            scratch_root=self.root / "scratch",
        )

    @staticmethod
    def _install_dependency_tree(worktree: Path, files: int = 40) -> None:
        """What `uv sync` / `pip install` does to a worktree: a big ignored tree."""
        for index in range(files):
            _write(worktree / ".venv" / "lib" / f"pkg{index}" / "__init__.py",
                   f"# dependency {index}\n")
        _write(worktree / ".venv" / "pyvenv.cfg", "home = /usr\n")
        _write(worktree / "build.log", "installing...\n")


class InventoryUniverse(IgnoredPathsTestCase):
    """`inventory()` is the single choke point every §8.3 evaluation reads."""

    def test_ignored_paths_are_not_inventory_entries(self):
        attempt = self._attempt()
        self._install_dependency_tree(attempt.path)
        inv = wt.inventory(attempt.path)

        ignored = [rel for rel in inv if rel.startswith(".venv/") or rel.endswith(".log")]
        self.assertEqual(ignored, [], "git-ignored paths are outside the bracket's universe")
        self.assertIn("src/app.py", inv)
        self.assertIn(".gitignore", inv)

    def test_untracked_but_committable_content_stays_in_the_inventory(self):
        """The narrowing is git's exclude rules, not "untracked" — conjunct (2)
        still needs provisioned untracked content measured."""
        attempt = self._attempt()
        _write(attempt.path / "provisioned.txt", "written by provision\n")
        inv = wt.inventory(attempt.path)
        self.assertIn("provisioned.txt", inv)

    def test_a_path_tracked_in_spite_of_an_ignore_rule_stays_in_the_inventory(self):
        """Tracking settles membership, not the ignore rule: `git add -f` puts a
        path in the index, and the index is what git carries."""
        _write(self.repo / "vendored.log", "committed on purpose\n")
        _git(self.repo, "add", "-f", "vendored.log")
        _git(self.repo, "commit", "-qm", "track an ignored path deliberately")

        attempt = self._attempt()
        self.assertIn("vendored.log", wt.inventory(attempt.path))

    def test_a_deleted_tracked_file_leaves_the_inventory(self):
        """`--cached` lists it; the tree does not hold it, so it carries no tuple
        and `delta` reports it removed."""
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        (attempt.path / "src" / "app.py").unlink()
        measured = wt.delta(baseline, wt.inventory(attempt.path))
        self.assertEqual(measured.removed, ("src/app.py",))

    def test_a_symlink_is_still_measured_by_its_own_target_bytes(self):
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        (attempt.path / "link").symlink_to("src/app.py")
        after = wt.inventory(attempt.path)
        self.assertIn("link", after)
        self.assertEqual(after["link"][0], wt.MODE_SYMLINK)
        self.assertEqual(wt.delta(baseline, after).added, ("link",))


class Clause4PermissionCheck(IgnoredPathsTestCase):
    """§7.3 clause 4 over the narrowed universe — the incident, and its inverse."""

    def test_only_the_undeclared_tracked_write_is_reported(self):
        """A worktree holding a whole ignored dependency tree plus ONE genuinely
        undeclared path reports exactly that one path."""
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        self._install_dependency_tree(attempt.path)
        _write(attempt.path / "src" / "declared.py", "OK = True\n")
        _write(attempt.path / "src" / "sneaky.py", "not in outputs\n")

        measured = wt.delta(baseline, wt.inventory(attempt.path))
        permission = wt.permission_check(attempt, measured, ["src/declared.py"])

        self.assertFalse(permission.passes)
        verdict = vf.verify_agent_node(
            envelope_parsed=True,
            pre_gate=vf.GateVerdict(green=False, unparseable=False, counts=None, reason="red at base"),
            post_gate=vf.GateVerdict(green=True, unparseable=False, counts=None, reason="green"),
            permission=permission)
        self.assertEqual(verdict.failed_clause, 4)
        self.assertEqual(verdict.offending_paths, ("src/sneaky.py",))

    def test_a_worktree_whose_only_extra_content_is_ignored_passes(self):
        """The incident, inverted: the lane ran its install step and wrote its
        declared output, and nothing else. Clause 4 must pass."""
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        self._install_dependency_tree(attempt.path)
        _write(attempt.path / "src" / "declared.py", "OK = True\n")

        measured = wt.delta(baseline, wt.inventory(attempt.path))
        self.assertEqual(measured.touched, ("src/declared.py",))

        permission = wt.permission_check(attempt, measured, ["src/declared.py"])
        self.assertTrue(permission.passes, permission)
        verdict = vf.verify_agent_node(
            envelope_parsed=True,
            pre_gate=vf.GateVerdict(green=False, unparseable=False, counts=None, reason="red at base"),
            post_gate=vf.GateVerdict(green=True, unparseable=False, counts=None, reason="green"),
            permission=permission)
        self.assertTrue(verdict.verified)
        self.assertEqual(verdict.offending_paths, ())

    def test_an_install_step_alone_produces_an_empty_delta(self):
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        self._install_dependency_tree(attempt.path)
        measured = wt.delta(baseline, wt.inventory(attempt.path))
        self.assertTrue(measured.is_empty, measured.touched)

    def test_an_undeclared_non_ignored_path_still_trips_clause_4(self):
        """The guard is narrowed, not weakened."""
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        _write(attempt.path / "elsewhere" / "stray.py", "wrote outside its outputs\n")

        measured = wt.delta(baseline, wt.inventory(attempt.path))
        permission = wt.permission_check(attempt, measured, ["src/declared.py"])
        self.assertFalse(permission.passes)
        self.assertIn("elsewhere/stray.py", permission.conjunct1_violations)

    def test_conjunct_2_still_convicts_tampering_with_provisioned_content(self):
        """The baseline's untracked content is still content: rewriting a
        provisioned untracked file convicts however broad the declared glob."""
        attempt = self._attempt()
        _write(attempt.path / "provisioned.txt", "written by provision\n")
        wt.take_baseline(attempt)
        _write(attempt.path / "provisioned.txt", "rewritten by the agent\n")

        measured = wt.delta(attempt.baseline, wt.inventory(attempt.path))
        permission = wt.permission_check(attempt, measured, ["**"])
        self.assertFalse(permission.passes)
        self.assertEqual(permission.conjunct1_violations, ())
        self.assertTrue(any("provisioned.txt" in v
                            for v in permission.conjunct2_violations))


class CommittedOutputAndCleanliness(IgnoredPathsTestCase):
    """The second-order question: what the node's output SHA and the two
    cleanliness evaluations see once ignored paths are out of the universe."""

    def test_the_output_sha_carries_no_ignored_path(self):
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        self._install_dependency_tree(attempt.path)
        _write(attempt.path / "src" / "declared.py", "OK = True\n")

        after = wt.inventory(attempt.path)
        measured = wt.delta(baseline, after)
        sha = wt.commit_measured_delta(attempt, measured, after, "lane-x attempt 1")

        committed = _git(self.repo, "ls-tree", "-r", "--name-only", sha).splitlines()
        self.assertIn("src/declared.py", committed)
        self.assertFalse([rel for rel in committed if rel.startswith(".venv/")])
        self.assertNotIn("build.log", committed)

    def test_an_install_step_no_longer_convicts_the_cleanliness_evaluations(self):
        """§8.3's post-commit evaluation convicts on any divergence from the
        expected inventory. An ignored artifact written by the post-node gate
        used to be one; it is now outside what "expected" ranges over."""
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        _write(attempt.path / "src" / "declared.py", "OK = True\n")
        after = wt.inventory(attempt.path)
        measured = wt.delta(baseline, after)
        expected = wt.expected_inventory(baseline, measured, after)
        wt.commit_measured_delta(attempt, measured, after, "lane-x attempt 1")

        # The gate runs and leaves its ignored byproducts behind.
        self._install_dependency_tree(attempt.path)

        verdict = wt.compare_to_expected(attempt.path, expected, "convict")
        self.assertTrue(verdict.clean, verdict.divergences)

    def test_cleanliness_still_convicts_a_non_ignored_stray_write(self):
        attempt = self._attempt()
        baseline = wt.take_baseline(attempt)
        after = wt.inventory(attempt.path)
        expected = wt.expected_inventory(baseline, wt.delta(baseline, after), after)
        _write(attempt.path / "stray.txt", "nothing may write here\n")

        verdict = wt.compare_to_expected(attempt.path, expected, "convict")
        self.assertFalse(verdict.clean)
        self.assertEqual([d.path for d in verdict.divergences], ["stray.txt"])


if __name__ == "__main__":
    unittest.main()
