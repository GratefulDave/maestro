"""One reviewer re-ask for a rejected submission, and replay from the record.

A rejected submission produces no finding and no transition effect: it is
recorded `REJECTED` with its code after the reviewer has had one more turn.
Accepted submissions survive that turn. Every later round rebuilds the finding
and the builder's feedback from the cumulative `probe_records`, with no
resubmission.
"""

import unittest

from adw_modules import probes
from adw_modules import scheduler_types as st
from tests.probe_lane_support import (
    REQUIREMENT,
    ProbeLaneCase,
    probe_path,
    probe_source,
    submission,
)

REVISE = st.ReviewerVerdict.REVISE
PASS = st.ReviewerVerdict.PASS
BAD = probes.ProbeSubmission(
    probe_path("bad"), probe_source("bad"), "nothing in the contract says this", "intent"
)


class FlowTest(ProbeLaneCase):
    def test_an_invalid_submission_is_asked_about_once_and_then_only_recorded(self):
        self.start(["8"], [{"submissions": [BAD]}, {"submissions": [BAD]}, {}])
        self.round()
        review = self.reviews()[0]
        self.assertEqual(self.actor.review_calls, 2)
        self.assertEqual(self.actor.review_ctxs[1].rejected_probes[0].code, probes.PROBE_UNCITED)
        self.assertEqual(
            review["probe_submissions"],
            [{"path": probe_path("bad"), "outcome": "REJECTED", "reason": probes.PROBE_UNCITED}],
        )
        self.assertEqual(len(review["findings"]), 1)
        self.assertNotIn("code", review["findings"][0])
        self.assertEqual(review["probe_records"], [])
        self.assertEqual(self.actor.accept_calls, [])
        self.assertIs(self.stage(), st.LaneStage.BUILDING)

    def test_accepted_submissions_survive_the_reask_and_the_second_ask_repairs(self):
        self.start(
            ["8"],
            [
                {"submissions": [submission("alpha"), BAD]},
                {"submissions": [submission("alpha"), submission("beta")]},
                {},
            ],
        )
        self.round()
        review = self.reviews()[0]
        self.assertEqual(sorted(r["path"] for r in review["probe_records"]), [probe_path("alpha"), probe_path("beta")])
        outcomes = {s["path"]: s["outcome"] for s in review["probe_submissions"]}
        self.assertEqual(
            outcomes,
            {probe_path("alpha"): "ADMITTED", probe_path("beta"): "ADMITTED", probe_path("bad"): "REJECTED"},
        )
        self.assertEqual(len(self.actor.accept_calls), 2, "alpha once, beta once: never re-decided")
        self.assertEqual(self.scheduler._probe_rounds_used(self.lane_id), 1)

    def test_later_rounds_rebuild_the_finding_from_the_record_alone(self):
        self.start(["8", "8", "8"], [{"probes": ["alpha"]}, {}, {"verdict": PASS}])
        self.round()
        self.round()
        second = self.reviews()[1]
        self.assertEqual(self.actor.review_calls, 2)
        self.assertEqual(len(self.actor.accept_calls), 1)
        self.assertEqual(second["probe_records"], self.reviews()[0]["probe_records"])
        self.assertIn("PROBE_FAILED", [f.get("code") for f in second["findings"]])
        self.assertTrue(
            any(line.startswith("[probe " + probe_path("alpha") + "]") for line in second["failure_output"]),
            second["failure_output"],
        )
        self.assertEqual(
            self.actor.builder_ctxs[1].probe_texts, {probe_path("alpha"): probe_source("alpha")}
        )
        self.round()
        third = self.reviews()[2]
        self.assertEqual(third["verdict"], "REVISE", "a PASS cannot stand over an open probe")


if __name__ == "__main__":
    unittest.main()
