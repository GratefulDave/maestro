"""Declared runners are not a frozen CLI binding."""

from __future__ import annotations

import argparse
import unittest

import maestro


class DeclaredRunnersAreNotCliStateTest(unittest.TestCase):
    def test_start_and_resume_have_no_runners_flag(self) -> None:
        parser = maestro.build_parser()
        run = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["run"]
        run_sub = next(
            action
            for action in run._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for name in ("start", "resume", "amend", "status"):
            flags = []
            for action in run_sub.choices[name]._actions:
                flags.extend(action.option_strings)
            self.assertNotIn("--runners", flags)
            self.assertNotIn("--runner", flags)

    def test_start_does_not_bind_a_runners_attribute(self) -> None:
        args = maestro.build_parser().parse_args(
            [
                "run",
                "start",
                "plan.json",
                "--repo",
                "/abs/product",
                "--main-ref",
                "refs/heads/main",
            ]
        )
        self.assertFalse(hasattr(args, "runners"))


if __name__ == "__main__":
    unittest.main()
