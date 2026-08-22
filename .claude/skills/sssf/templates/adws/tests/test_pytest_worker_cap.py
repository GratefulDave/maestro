"""Per-lane pytest worker cap — the value `scratch_env` actually writes.

Six concurrent lanes inheriting `pytest.ini`'s `-n auto` on an 18-core
box is 108 workers. A red final integration gate has no retry, so the
cap is a correctness bound, not a performance nicety. The formula is
`max(1, cores // concurrency)` and is asserted here by reading the
`PYTEST_ADDOPTS` value production will export, not a parallel copy of
the arithmetic.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import launcher, worktree


class PytestWorkerCapTest(unittest.TestCase):

    def test_six_lanes_on_eighteen_cores_share_three_workers_each(self):
        self.assertEqual(launcher.pytest_worker_cap(6, 18), 3)

    def test_more_lanes_than_cores_still_gets_one_worker(self):
        self.assertEqual(launcher.pytest_worker_cap(6, 4), 1)

    def test_a_single_lane_may_use_every_core(self):
        self.assertEqual(launcher.pytest_worker_cap(1, 8), 8)

    def test_concurrency_below_one_is_refused(self):
        with self.assertRaises(ValueError):
            launcher.pytest_worker_cap(0, 8)

    def test_unspecified_concurrency_is_cache_redirect_only(self):
        # ``-n`` requires xdist. A nested collection in a tree that does not
        # install it must still probe, so the unbound default carries no cap.
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            env = worktree.scratch_env(scratch)
            self.assertEqual(
                env["PYTEST_ADDOPTS"],
                "-o cache_dir={}".format(scratch / "pytest_cache"))

    def test_a_bound_lane_count_is_what_scratch_env_exports(self):
        self.addCleanup(worktree.bind_lane_concurrency, None)
        worktree.bind_lane_concurrency(6)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            env = worktree.scratch_env(scratch, cpu_count=18)
            self.assertEqual(
                env["PYTEST_ADDOPTS"],
                "-n 3 -o cache_dir={}".format(scratch / "pytest_cache"))
            self.assertEqual(launcher.pytest_worker_cap(6, 18), 3)


    def test_scratch_env_writes_the_computed_cap_into_pytest_addopts(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            env = worktree.scratch_env(scratch, concurrency=6, cpu_count=18)
            expected = "-n 3 -o cache_dir={}".format(scratch / "pytest_cache")
            self.assertEqual(env["PYTEST_ADDOPTS"], expected)
            self.assertEqual(
                launcher.pytest_worker_cap(6, 18), 3)
            flags = launcher.pane_env_flags(env)
            forwarded = dict(
                token.split("=", 1)
                for index, token in enumerate(flags)
                if index and flags[index - 1] == "--env")
            self.assertEqual(forwarded["PYTEST_ADDOPTS"], expected)


    def test_launch_env_still_creates_the_cache_dir_after_the_cap_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            env = worktree.launch_env(scratch, {}, concurrency=6, cpu_count=18)
            cache = scratch / "pytest_cache"
            self.assertTrue(cache.is_dir())
            self.assertIn("cache_dir={}".format(cache), env["PYTEST_ADDOPTS"])
            self.assertTrue(env["PYTEST_ADDOPTS"].startswith("-n 3 "))


if __name__ == "__main__":
    unittest.main()
