"""Resume has no tuning/budget flags. Binding is config, not CLI ceilings."""

from __future__ import annotations

import argparse
import unittest

import maestro


class ResumeHasNoTuningFlagsTest(unittest.TestCase):
    def test_resume_accepts_only_run_id(self) -> None:
        parser = maestro.build_parser()
        run = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["run"]
        resume = next(
            action
            for action in run._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["resume"]
        option_strings = []
        for action in resume._actions:
            option_strings.extend(action.option_strings)
        self.assertEqual(option_strings, ["-h", "--help"])
        args = parser.parse_args(["run", "resume", "run-1"])
        self.assertEqual(args.run_id, "run-1")
        self.assertFalse(hasattr(args, "semantic_ceiling"))
        self.assertFalse(hasattr(args, "review_ceiling"))
        self.assertFalse(hasattr(args, "max_workers"))

    def test_start_has_no_budget_flags(self) -> None:
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
        self.assertFalse(hasattr(args, "semantic_ceiling"))
        self.assertFalse(hasattr(args, "review_ceiling"))


if __name__ == "__main__":
    unittest.main()
