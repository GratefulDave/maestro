"""#18 / #22 — sibling clocks at config load.

After §19 M15, execution.turn_timeout_s is disarmed during review of an
ACCEPTED attempt. The shipping template (finalization 600, turn 300) is
therefore legal. The remaining live bound is the run-level backstop.
Observed worst-case review: 461s / 64 turns, which 600s holds with margin.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from test_retry_budget_configuration import _load


class ReviewClockSiblingsTest(unittest.TestCase):

    def test_shipping_finalization_may_exceed_the_builder_turn_clock(self):
        maestro._validate_review_clocks(
            {"finalization_timeout_s": 600, "turn_timeout_s": 120},
            {"backstop_t_s": 7200, "node_timeout_s": 1800})

    def test_the_shipping_600_vs_300_pair_is_legal_after_m15(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _load({"turn_timeout_s": 300, "backstop_t_s": 7200},
                           Path(tmp).resolve())
        self.assertEqual(layout["reviewer"]["finalization_timeout_s"], 60)
        self.assertEqual(layout["execution"]["turn_timeout_s"], 300)

    def test_a_review_window_that_meets_or_exceeds_the_backstop_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(maestro._MaestroConfigurationError) as caught:
                _load({"backstop_t_s": 50}, Path(tmp).resolve())
        self.assertIn("LIVENESS_BOUND_UNSATISFIED", str(caught.exception))
        self.assertIn("finalization", str(caught.exception))

    def test_a_reviewer_turn_clock_that_fills_the_window_is_refused(self):
        reviewer_turn_ge_window = {
            "turn_timeout_s": 30,
            "backstop_t_s": 600,
        }
        # The helper only overrides execution. Reach the validator directly.
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._validate_review_clocks(
                {"finalization_timeout_s": 60, "turn_timeout_s": 60},
                {"backstop_t_s": 600, "node_timeout_s": 120})
        self.assertIn("reviewer.turn_timeout_s", str(caught.exception))

    def test_a_node_plus_review_that_meets_the_backstop_is_refused(self):
        with self.assertRaises(maestro._MaestroConfigurationError) as caught:
            maestro._validate_review_clocks(
                {"finalization_timeout_s": 60, "turn_timeout_s": 20},
                {"backstop_t_s": 180, "node_timeout_s": 120})
        self.assertIn("LIVENESS_BOUND_UNSATISFIED", str(caught.exception))
        self.assertIn("node_timeout", str(caught.exception))
        self.assertIn("sequential", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
