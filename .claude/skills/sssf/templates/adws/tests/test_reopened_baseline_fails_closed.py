"""A reopened attempt has no baseline, and nothing may invent one for it.

`reopen_attempt_worktree` rebuilds an attempt handle after its process is
gone. It can rebuild `tracked_at_base` from the base commit — tracking is a
property of the commit — and it cannot rebuild the measurement baseline,
which §8.3 defines as the *provisioned* tree and which therefore holds
untracked paths no commit contains.

It used to return `inventory_at_commit` in that field anyway. Every
provisioned untracked path then read as a path the attempt had added, and one
covered by a declared output was committed as the attempt's measured delta and
signed for -- the false evidence chain §1.1 item 4 exists to prevent.

The field is `None` now, and the two consumers refuse it rather than reading
it as an empty baseline. That distinction is the point of this file: an empty
baseline reads *everything* as added, which is strictly worse than the
substitution it replaces. The last test measures that, so the claim is a
number rather than an assertion about intent.

Run with:
    /Users/davidandrews/PycharmProjects/lexgenius-pipeline/.venv/bin/python -m pytest tests/test_reopened_baseline_fails_closed.py -o addopts= -q
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import worktree as wt  # noqa: E402


RUN_ID = "run-6f0f4d0f0c2f4a2f8f2b6b2a1d4c9e77"
NODE_ID = "lane-reopen-baseline"
TRACKED = "src/pkg/committed.py"
PROVISIONED = "src/pkg/__init__.py"
DELIVERABLE = "src/pkg/rules.py"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} -> {result.returncode}: {result.stderr}")
    return result.stdout.strip()


class _Attempt:
    """A worktree carrying one tracked path, one provisioned untracked path,
    and one path the attempt itself wrote."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "harness@example.invalid")
        _git(self.repo, "config", "user.name", "Harness")
        _git(self.repo, "config", "core.hooksPath", str(self.root / "no-hooks"))
        tracked = self.repo / TRACKED
        tracked.parent.mkdir(parents=True)
        tracked.write_text("COMMITTED = True\n")
        _git(self.repo, "add", TRACKED)
        _git(self.repo, "commit", "-qm", "base")
        self.base = _git(self.repo, "rev-parse", "HEAD")

        self.worktrees = self.root / "worktrees"
        self.scratch = self.root / "scratch"
        self.attempt = wt.create_attempt_worktree(
            repo=self.repo, run_id=RUN_ID, node_id=NODE_ID, attempt_no=1,
            integration_head=self.base, worktrees_root=self.worktrees,
            scratch_root=self.scratch)
        # provision: untracked, not ignored, and never in any commit
        (self.attempt.path / PROVISIONED).write_text("# provisioned\n")
        self.baseline = wt.take_baseline(self.attempt)
        # the attempt's own work
        (self.attempt.path / DELIVERABLE).write_text("RULES = True\n")
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def reopened(self) -> wt.AttemptWorktree:
        return wt.reopen_attempt_worktree(
            self.repo, RUN_ID, NODE_ID, 1, self.base,
            self.worktrees, self.scratch)


class ReopenLeavesTheBaselineUnset(unittest.TestCase):

    def test_the_reopened_handle_has_no_baseline(self):
        with _Attempt() as a:
            self.assertIsNone(a.reopened().baseline)

    def test_tracked_at_base_still_comes_from_the_commit(self):
        """The half the commit *can* answer keeps its derivation, and the
        provisioned path is correctly absent from it."""
        with _Attempt() as a:
            reopened = a.reopened()
            self.assertEqual(set(reopened.tracked_at_base), {TRACKED})
            self.assertNotIn(PROVISIONED, reopened.tracked_at_base)
            self.assertIn(PROVISIONED, a.baseline)


class TheConsumersRefuseAnUnsetBaseline(unittest.TestCase):
    """Fail closed, not open. Neither may read `None` as an empty baseline."""

    def test_permission_check_refuses(self):
        with _Attempt() as a:
            reopened = a.reopened()
            after = wt.inventory(reopened.path)
            measured = wt.delta(a.baseline, after)
            with self.assertRaises(wt.WorktreeError):
                wt.permission_check(reopened, measured, (DELIVERABLE,))

    def test_commit_measured_delta_refuses(self):
        with _Attempt() as a:
            reopened = a.reopened()
            after = wt.inventory(reopened.path)
            measured = wt.delta(a.baseline, after)
            with self.assertRaises(wt.WorktreeError):
                wt.commit_measured_delta(
                    reopened, measured, after, "must not commit")
            self.assertEqual(
                _git(a.repo, "rev-parse",
                     f"refs/heads/{wt.branch_name(RUN_ID, NODE_ID, 1)}"),
                a.base)


class WhyAnEmptyBaselineWouldBeWorse(unittest.TestCase):

    def test_an_empty_baseline_claims_the_whole_tree_as_the_attempts_work(self):
        """The control for the refusals above. Measured against `{}`, the
        delta names the tracked file and the provisioned file as well as the
        deliverable; measured against the recorded baseline it names the
        deliverable alone."""
        with _Attempt() as a:
            after = wt.inventory(a.attempt.path)

            against_empty = wt.delta({}, after)
            against_recorded = wt.delta(a.baseline, after)

            self.assertEqual(set(against_recorded.added), {DELIVERABLE})
            self.assertEqual(
                set(against_empty.added), {TRACKED, PROVISIONED, DELIVERABLE})
            self.assertGreater(
                len(against_empty.added), len(against_recorded.added))


if __name__ == "__main__":
    unittest.main()
