"""What an operator can see, and what they can reclaim.

Two failures on `run-9d03105407f440079f3730f1fe4c67b3`, both of them the same
absence of a report:

1. Everything between claiming an attempt and opening its pane -- provision,
   pre-gate, baseline -- printed nothing. That window is minutes long, and an
   operator watching a silent terminal killed two healthy attempts inside it
   before a real hang was found there. Silence covered both cases equally.
2. Seven attempts left seven full `node_modules` checkouts on disk, 3.1GB of
   them, and nothing counted them or said they were there.

These are display and disk, never lifecycle: §1.2 keys transitions on typed
records, and nothing here is read back by any decision.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import maestro
from adw_modules import worktree as wt


def _repo(root: Path) -> None:
    def git(*argv):
        subprocess.run(("git", "-C", str(root)) + argv, check=True,
                       capture_output=True, text=True)
    subprocess.run(("git", "init", "-q", str(root)), check=True,
                   capture_output=True, text=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-qm", "base")


class StaleSelection(unittest.TestCase):
    """Which checkouts are named, asked of the ledger and not of a mtime."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "worktrees"
        self.root.mkdir(parents=True)
        self.run_id = "run-abc"
        for name in ("run-abc-lane-one-a1", "run-abc-lane-one-a2",
                     "run-abc-lane-two-a1", "run-other-lane-one-a1",
                     "not-a-worktree"):
            (self.root / name).mkdir()
            (self.root / name / "f").write_text("x" * 10, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_running_attempt_is_never_stale(self) -> None:
        stale = wt.stale_attempt_worktrees(
            self.root, self.run_id,
            {("lane-one", 1): "CANCELLED", ("lane-one", 2): "RUNNING",
             ("lane-two", 1): "MERGED"})
        self.assertEqual(
            sorted((item.node_id, item.attempt_no) for item in stale),
            [("lane-one", 1), ("lane-two", 1)],
            "the live attempt's own checkout must never be offered for removal",
        )

    def test_a_checkout_with_no_ledger_row_is_reported(self) -> None:
        stale = wt.stale_attempt_worktrees(self.root, self.run_id, {})
        states = {item.attempt_state for item in stale}
        self.assertEqual(states, {""})
        self.assertEqual(
            len(stale), 3,
            "a directory nobody's ledger claims is exactly the leftover this "
            "exists to find; dropping it would hide the worst case",
        )

    def test_another_run_and_a_foreign_directory_are_left_alone(self) -> None:
        named = {item.path.name
                 for item in wt.stale_attempt_worktrees(self.root, self.run_id, {})}
        self.assertNotIn("run-other-lane-one-a1", named)
        self.assertNotIn("not-a-worktree", named)

    def test_the_size_is_measured(self) -> None:
        stale = wt.stale_attempt_worktrees(self.root, self.run_id, {})
        self.assertTrue(all(item.bytes_on_disk == 10 for item in stale),
                        [item.bytes_on_disk for item in stale])
        self.assertTrue(
            all(item.bytes_on_disk == 0 for item in wt.stale_attempt_worktrees(
                self.root, self.run_id, {}, measure=False)),
            "measure=False must cost nothing; a listing that always walks the "
            "tree is one an operator stops running",
        )


class ReleaseKeepsTheBranch(unittest.TestCase):
    """Freeing the disk must not destroy the only copy of unmerged work."""

    def test_the_checkout_goes_and_the_branch_stays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _repo(repo)
            checkout = Path(tmp) / "wt" / "run-abc-lane-one-a1"
            checkout.parent.mkdir()
            subprocess.run(
                ("git", "-C", str(repo), "worktree", "add", "-b",
                 "attempt/lane-one/1", str(checkout)),
                check=True, capture_output=True, text=True)
            stale = wt.StaleWorktree(path=checkout, node_id="lane-one",
                                     attempt_no=1, attempt_state="CANCELLED",
                                     bytes_on_disk=0)

            released, detail = wt.release_stale_worktree(repo, stale)

            self.assertTrue(released, detail)
            self.assertFalse(checkout.exists())
            branches = subprocess.run(
                ("git", "-C", str(repo), "branch", "--list",
                 "attempt/lane-one/1"),
                capture_output=True, text=True, check=True).stdout
            self.assertIn(
                "attempt/lane-one/1", branches,
                "the branch may hold the only copy of unmerged work and costs "
                "a ref; only the checkout's disk is being reclaimed",
            )

    def test_a_refusal_is_surfaced_rather_than_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _repo(repo)
            checkout = Path(tmp) / "wt" / "run-abc-lane-one-a1"
            checkout.parent.mkdir()
            subprocess.run(
                ("git", "-C", str(repo), "worktree", "add", "-b",
                 "attempt/lane-one/1", str(checkout)),
                check=True, capture_output=True, text=True)
            (checkout / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
            stale = wt.StaleWorktree(path=checkout, node_id="lane-one",
                                     attempt_no=1, attempt_state="CANCELLED",
                                     bytes_on_disk=0)

            released, detail = wt.release_stale_worktree(repo, stale)

            self.assertFalse(released, "an unforced removal must not discard "
                                       "uncommitted content")
            self.assertTrue(detail, "git's refusal is the evidence and must "
                                    "reach the operator")
            self.assertTrue(checkout.exists())
            self.assertTrue(
                wt.release_stale_worktree(repo, stale, force=True)[0],
                "--force is the operator's escape and must work when typed",
            )


class PruneSparesThePostMortem(unittest.TestCase):
    """§8.8 keeps a blocked node's checkout on purpose."""

    def _item(self, state):
        return wt.StaleWorktree(path=Path("/nowhere"), node_id="lane-one",
                                attempt_no=1, attempt_state=state,
                                bytes_on_disk=0)

    def test_a_blocked_attempt_is_not_pruned_by_default(self) -> None:
        self.assertFalse(maestro._prunable(self._item("BLOCKED"), False))

    def test_every_other_state_is(self) -> None:
        for state in ("CANCELLED", "MERGED", "FAILED", ""):
            self.assertTrue(maestro._prunable(self._item(state), False), state)

    def test_the_operator_can_ask_for_it_by_name(self) -> None:
        self.assertTrue(maestro._prunable(self._item("BLOCKED"), True))


class ProgressIsDisplayOnly(unittest.TestCase):
    """The reporter is optional, unopinionated, and cannot fail an attempt."""

    def test_an_unknown_phase_prints_rather_than_vanishing(self) -> None:
        printed = []

        class _Console:
            def print(self, line):
                printed.append(line)

        report = maestro._attempt_progress_reporter(_Console())
        report("lane-one", 3, "provisioning", {"worktree": "/tmp/x"})
        report("lane-one", 3, "some-new-phase", {})

        self.assertIn("provisioning the worktree", printed[0])
        self.assertIn("a3", printed[0])
        self.assertIn(
            "some-new-phase", printed[1],
            "a phase the table does not name must still be seen; silently "
            "dropping it is how a field acquires zero readers (B15)",
        )

    def test_every_phase_the_scheduler_emits_has_a_reader(self) -> None:
        source = Path(__file__).resolve().parent.parent
        text = (source / "adw_modules" / "scheduler.py").read_text(
            encoding="utf-8")
        emitted = set()
        for line in text.splitlines():
            marker = "self._say("
            if marker not in line:
                continue
            tail = line.split(marker, 1)[1]
            parts = [item.strip() for item in tail.split(",")]
            for part in parts:
                if part.startswith('"') and part.endswith('"'):
                    emitted.add(part.strip('"'))
        self.assertTrue(emitted, "no phases found; the scan is wrong")
        self.assertEqual(
            emitted - set(maestro._ATTEMPT_PHASES), set(),
            "the scheduler emits a phase the reporter cannot name it by",
        )


if __name__ == "__main__":
    unittest.main()
