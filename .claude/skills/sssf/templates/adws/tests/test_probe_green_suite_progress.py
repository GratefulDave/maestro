"""Green suite, a reviewer who finds a new defect each round, a builder who fixes each.

The accepted suite is eight cases and stays 8/8 for every round, so the passed
count never moves. Each round the reviewer names a different real defect and
authors one probe for it; the builder fixes the previous round's. Without
probes the lane parks NO_PROGRESS at round three on the flat count. With them
every round discharges one probe and admits another, and the lane merges.

The controls are the same lane with the feature off (`max_probe_rounds=0`) and
with prose-only REVISE rounds, both of which must still park at round three.
"""

import unittest

from adw_modules import scheduler_types as st
from tests.probe_lane_support import ProbeLaneCase

DEFECTS = ("alpha", "beta", "gamma", "delta")
BODIES = ["8", "8 alpha", "8 alpha beta", "8 alpha beta gamma", "8 alpha beta gamma delta"]
REVISE = st.ReviewerVerdict.REVISE
PASS = st.ReviewerVerdict.PASS


def scripted_reviewer():
    steps = [{"verdict": REVISE, "probes": [token]} for token in DEFECTS]
    return steps + [{"verdict": PASS}]


class RegressionTest(ProbeLaneCase):
    def test_lane_reaches_ready_to_merge_while_the_suite_never_moves(self):
        self.start(BODIES, scripted_reviewer())
        for _ in range(4):
            self.round()
            self.assertIs(self.stage(), st.LaneStage.BUILDING)
        self.round()
        self.assertIs(self.stage(), st.LaneStage.READY_TO_MERGE)
        reviews = self.reviews()
        self.assertEqual([r["public_result_summary"]["passed"] for r in reviews], [8] * 5)
        self.assertEqual([r["verdict"] for r in reviews], ["REVISE"] * 4 + ["PASS"])
        # Each review discharges the probe of the round before and admits one more.
        discharged = [
            sorted(pid for pid, state in r.get("probe_status", {}).items() if state == "DISCHARGED")
            for r in reviews
        ]
        self.assertEqual([len(d) for d in discharged], [0, 1, 2, 3, 4])
        for review in reviews[:4]:
            codes = [f.get("code") for f in review["findings"]]
            self.assertIn("PROBE_FAILED", codes)
        self.assertEqual(self.waits(), [])

    def test_control_without_the_feature_parks_at_round_three(self):
        self.start(BODIES, scripted_reviewer(), max_probe_rounds=0)
        for _ in range(3):
            self.round()
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual(self.waits()[0]["wait_reason"], st.WaitReason.NO_PROGRESS.value)
        self.assertNotIn("probe_records", self.reviews()[-1])

    def test_control_with_prose_only_revise_parks_at_round_three(self):
        self.start(BODIES, [{"verdict": REVISE}])
        for _ in range(3):
            self.round()
        self.assertIs(self.stage(), st.LaneStage.WAITING_FOR_USER)
        self.assertEqual(self.waits()[0]["wait_reason"], st.WaitReason.NO_PROGRESS.value)


if __name__ == "__main__":
    unittest.main()
