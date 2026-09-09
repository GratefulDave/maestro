"""A lane's Space is counted once, however many worktrees it has open.

`_adopt_existing_lane` (adw_modules/launcher.py) iterated `worktree list`
rows and appended one match per row. Herdr lists one Space on more than one
row when that Space has more than one linked worktree open: observed on
FDAdb run d246ae9592be478396ad5146a89f00ae, Space w1HH appeared for both
`.../ui-worktrees/<run>/<hash>/lane-faq-producer-tests` and
`.../worktrees/<run>/lane-faq-producer-tests/tester/checkout`. Two rows for
one Space doubled the count and refused `DUPLICATE_LANE_WORKSPACE` for a
lane that had exactly one Space open, tagged and correct.

These tests build the same shape by hand: a listing where one
`open_workspace_id` appears on two `WorktreeInfo` rows with different
`path` values, the way Herdr's real reply does.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch
from tests.test_herdr_workspace_topology import (
    RUN_HASH,
    TESTS_LANE,
    _RunFixture,
    _checkout,
)


class LaneSpaceCountedOnceTest(unittest.TestCase):
    def test_a_same_space_on_two_rows_is_adopted_not_duplicated(self) -> None:
        """One Space, two worktree rows (role checkout + ui-worktree):
        adopted with no refusal. On the parent commit this raised
        DUPLICATE_LANE_WORKSPACE because the loop counted the row, not the
        Space."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            parent_id = run.operator
            tokens = launcher._lane_identity_tokens(TESTS_LANE, parent_id)
            checkout = _checkout(run.root, "lane-wp6-tests")
            child_id = run.herdr.open_child(
                parent_id, checkout, TESTS_LANE, tokens=tokens
            )
            # The second row Herdr emits for the same Space: a ui-worktree
            # path, sharing `open_workspace_id` with the role checkout row
            # `open_child` already appended.
            ui_row = dict(run.herdr.worktrees[parent_id][-1])
            ui_row["path"] = str(
                run.root / "ui-worktrees" / RUN_HASH / "abcd1234" / TESTS_LANE
            )
            run.herdr.worktrees[parent_id].append(ui_row)
            rows_for_child = [
                item
                for item in run.herdr.worktrees[parent_id]
                if item.get("open_workspace_id") == child_id
            ]
            self.assertEqual(len(rows_for_child), 2)

            layout = launcher._adopt_existing_lane(
                parent_id, TESTS_LANE, TESTS_LANE, checkout, {}
            )
            self.assertIsNotNone(layout)
            assert layout is not None
            self.assertEqual(layout.child_workspace_id, child_id)
            self.assertEqual(layout.parent_workspace_id, parent_id)

    def test_b_two_distinct_matching_spaces_still_refuse_duplicate(self) -> None:
        """Two DIFFERENT Spaces both tagged for the same lane must still
        refuse -- the fix dedupes rows of one Space, not distinct Spaces."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            parent_id = run.operator
            tokens = launcher._lane_identity_tokens(TESTS_LANE, parent_id)
            for name in ("dup-a", "dup-b"):
                run.herdr.open_child(
                    parent_id, _checkout(run.root, name), TESTS_LANE, tokens=tokens
                )
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._adopt_existing_lane(
                    parent_id, TESTS_LANE, TESTS_LANE, run.root / "dup-a", {}
                )
            self.assertEqual(
                raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertIn("DUPLICATE_LANE_WORKSPACE", raised.exception.detail)

    def test_c_untagged_child_second_row_path_match_is_adopted_and_tagged(
        self,
    ) -> None:
        """An untagged child listed under two rows is adopted when EITHER
        row's path is the role checkout, and tagged on adoption -- the
        `_same_resolved_path` check must scan every row of the Space, not
        just whichever row the loop happened to keep."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            parent_id = run.operator
            checkout = _checkout(run.root, "lane-wp6-tests")
            other_path = (
                run.root / "ui-worktrees" / RUN_HASH / "abcd1234" / TESTS_LANE
            )
            # Untagged child, first row at a path that does NOT match the
            # role checkout.
            child_id = run.herdr.open_child(parent_id, other_path, TESTS_LANE)
            # Second row for the same Space: the role checkout path.
            role_row = dict(run.herdr.worktrees[parent_id][-1])
            role_row["path"] = str(checkout)
            run.herdr.worktrees[parent_id].append(role_row)

            layout = launcher._adopt_existing_lane(
                parent_id, TESTS_LANE, TESTS_LANE, checkout, {}
            )
            self.assertIsNotNone(layout)
            assert layout is not None
            self.assertEqual(layout.child_workspace_id, child_id)
            self.assertEqual(
                lch._herdr_tokens(run.herdr.workspaces[child_id]).get(
                    lch.METADATA_TOKEN_LANE
                ),
                TESTS_LANE,
            )


if __name__ == "__main__":
    unittest.main()
