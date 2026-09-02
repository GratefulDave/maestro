"""A merged lane stops paying rent on its review trees.

`measure_candidate` provisions `review-<lane>-<input_digest[:12]>` for every
code-review round and nothing removed it -- not on merge, not on run
completion, not on scheduler exit. FDAdb run f50638ab held 87 of them across
two build lanes, 5.8G, next to 5.7G of live checkouts. A long run grew without
bound and a finished one left everything behind.

The trees are derived: each is rebuilt from an immutable commit plus the sealed
blobs on demand, so dropping one costs a re-provision and never data. These
cases pin what may be dropped and, more importantly, what may not.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler as sch  # noqa: E402


def _tree(root: Path, name: str, *, size: int = 3) -> Path:
    path = root / name
    (path / "src").mkdir(parents=True)
    for index in range(size):
        (path / "src" / "file{0}.txt".format(index)).write_text("x" * 64)
    return path


class MergeReclaimsScratch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir(parents=True)
        self.said: list[tuple[str, str, str]] = []
        self.scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
        self.scheduler.runtime = SimpleNamespace(path=self.root)
        self.scheduler._say = lambda lane, message, detail="": self.said.append(
            (lane, message, detail)
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_merged_lane_drops_every_review_tree_it_made(self):
        kept = []
        for digest in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            kept.append(_tree(self.worktrees, "review-lane-a-" + digest))

        self.scheduler._reclaim_lane_scratch("lane-a")

        for path in kept:
            self.assertFalse(path.exists(), path.name)
        self.assertEqual(
            [entry[1] for entry in self.said], ["reclaimed review scratch"]
        )
        self.assertEqual(self.said[0][2], "3 tree(s)")

    def test_another_lanes_scratch_is_never_touched(self):
        mine = _tree(self.worktrees, "review-lane-a-aaaaaaaaaaaa")
        theirs = _tree(self.worktrees, "review-lane-b-bbbbbbbbbbbb")

        self.scheduler._reclaim_lane_scratch("lane-a")

        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    def test_a_lane_id_that_prefixes_another_does_not_take_it_with_it(self):
        # "lane-a" must not match "review-lane-alpha-...". The trailing dash in
        # the prefix is what separates them, and it is easy to drop.
        short = _tree(self.worktrees, "review-lane-a-aaaaaaaaaaaa")
        longer = _tree(self.worktrees, "review-lane-alpha-bbbbbbbbbbbb")

        self.scheduler._reclaim_lane_scratch("lane-a")

        self.assertFalse(short.exists())
        self.assertTrue(longer.exists())

    def test_live_checkouts_and_drafts_survive(self):
        run_dir = self.worktrees / "run-1" / "lane-a" / "builder" / "checkout"
        run_dir.mkdir(parents=True)
        (run_dir / "product.py").write_text("live\n")
        draft = _tree(self.worktrees, "draft-lane-a-aaaaaaaaaaaa")

        self.scheduler._reclaim_lane_scratch("lane-a")

        self.assertTrue(run_dir.exists())
        self.assertTrue((run_dir / "product.py").is_file())
        self.assertTrue(draft.exists())

    def test_a_symlink_wearing_the_prefix_is_not_followed(self):
        outside = self.root / "not-scratch"
        (outside / "keep").mkdir(parents=True)
        (outside / "keep" / "precious.txt").write_text("do not delete\n")
        link = self.worktrees / "review-lane-a-aaaaaaaaaaaa"
        link.symlink_to(outside, target_is_directory=True)

        self.scheduler._reclaim_lane_scratch("lane-a")

        self.assertTrue((outside / "keep" / "precious.txt").is_file())
        self.assertTrue(link.is_symlink())

    def test_nothing_to_reclaim_says_nothing(self):
        self.scheduler._reclaim_lane_scratch("lane-a")

        self.assertEqual(self.said, [])

    def test_a_missing_worktrees_root_is_not_an_error(self):
        scheduler = sch.FactoryScheduler.__new__(sch.FactoryScheduler)
        scheduler.runtime = SimpleNamespace(path=self.root / "absent")
        scheduler._say = lambda *args, **kwargs: None

        scheduler._reclaim_lane_scratch("lane-a")


if __name__ == "__main__":
    unittest.main()
