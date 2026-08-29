"""plan ship is gone. Resume is run resume <run-id>."""

from __future__ import annotations

import unittest

import maestro


class PlanShipRemovedResumeIsRunIdTest(unittest.TestCase):
    def test_plan_ship_is_not_a_verb(self) -> None:
        verbs = maestro.parser_verbs(maestro.build_parser())
        self.assertNotIn("plan ship", verbs)
        self.assertFalse(any(item.startswith("plan ") for item in verbs))
        with self.assertRaises(SystemExit):
            maestro.build_parser().parse_args(["plan", "ship"])

    def test_resume_takes_only_the_existing_run_id(self) -> None:
        args = maestro.build_parser().parse_args(["run", "resume", "run-existing"])
        self.assertEqual(args.run_id, "run-existing")
        self.assertFalse(hasattr(args, "plan"))
        with self.assertRaises(SystemExit):
            maestro.build_parser().parse_args(["run", "resume"])


if __name__ == "__main__":
    unittest.main()
