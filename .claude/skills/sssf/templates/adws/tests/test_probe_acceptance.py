"""A failing probe reaches the builder only through the acceptor's receipt.

The acceptor answers whether a probe follows from the requirement it cites. A
wrong expectation, an executed fixture error, a malformed answer and a missing
capability each leave the reviewer's ordinary verdict alone and admit nothing.
A probe leaves the lane only through a challenge the acceptor upholds; the code
reviewer alone cannot retire one.
"""

import unittest

from adw_modules import probes
from adw_modules import scheduler_types as st
from tests.probe_lane_support import (
    REQUIREMENT,
    ProbeLaneCase,
    all_pass,
    all_revise,
    probe_path,
    probe_source,
)

REVISE = st.ReviewerVerdict.REVISE
ALPHA = probes.probe_id(
    probe_path("alpha"), probes.source_digest(probe_source("alpha")), REQUIREMENT
)


def by_mode(accept, challenge):
    return lambda items, mode: (accept if mode == "accept" else challenge)(items, mode)


def codes(review):
    return [finding.get("code") for finding in review["findings"]]


class AcceptanceTest(ProbeLaneCase):
    def test_a_wrong_expectation_is_rejected_and_never_reaches_the_builder(self):
        self.start(["8", "8"], [{"probes": ["alpha"]}, {}], acceptance=all_revise)
        self.round()
        review = self.reviews()[0]
        self.assertEqual(review["probe_records"], [])
        self.assertEqual(
            review["probe_submissions"],
            [
                {
                    "path": probe_path("alpha"),
                    "outcome": "REJECTED",
                    "probe_id": ALPHA,
                    "reason": probes.PROBE_NOT_ACCEPTED,
                }
            ],
        )
        self.assertEqual([r["verdict"] for r in review["probe_receipts"]], ["REVISE"])
        self.assertNotIn("PROBE_FAILED", codes(review))
        self.assertEqual(review["verdict"], "REVISE")
        self.assertEqual(self.actor.review_calls, 2, "the reviewer is asked once more")
        self.assertEqual(self.actor.review_ctxs[1].rejected_probes[0].code, probes.PROBE_NOT_ACCEPTED)
        self.round()
        self.assertIsNone(self.actor.builder_ctxs[1].probe_texts)
        self.assertEqual(self.actor.builder_ctxs[1].protected_test_paths, ("tests/test_counts.py",))

    def test_an_executed_fixture_error_is_rejected_before_the_acceptor_is_asked(self):
        broken = probes.ProbeSubmission(
            probe_path("boom"),
            "import pytest\n\n\n@pytest.fixture\ndef boom():\n    raise RuntimeError('fixture')\n\n\n"
            "def test_boom(boom):\n    pass\n",
            REQUIREMENT,
            "intent",
        )
        self.start(["8"], [{"submissions": [broken]}, {}])
        self.round()
        review = self.reviews()[0]
        self.assertEqual(self.actor.accept_calls, [])
        entry = review["probe_submissions"][0]
        self.assertEqual((entry["outcome"], entry["reason"]), ("REJECTED", probes.PROBE_INSTRUMENTATION_ERROR))
        self.assertEqual(review["probe_records"], [])

    def test_a_malformed_answer_admits_nothing_and_keeps_the_verdict(self):
        malformed = lambda items, mode: {"verdict": "PASS", "probe_verdicts": []}
        self.start(["8"], [{"probes": ["alpha"]}], acceptance=malformed)
        self.round()
        review = self.reviews()[0]
        self.assertEqual(len(self.actor.accept_calls), 1)
        entry = review["probe_submissions"][0]
        self.assertEqual((entry["outcome"], entry["reason"]), ("NOT_ADMITTED", probes.PROBE_ACCEPTANCE_MALFORMED))
        self.assertEqual(review["probe_records"], [])
        self.assertEqual(review["probe_receipts"], [])
        self.assertEqual(self.actor.review_calls, 1, "infrastructure is not the reviewer's fault: no re-ask")
        self.assertEqual(review["verdict"], "REVISE")

    def test_an_actor_without_the_capability_admits_nothing(self):
        self.start(["8"], [{"probes": ["alpha"]}])
        self.actor.acceptor_for = None
        self.round()
        review = self.reviews()[0]
        entry = review["probe_submissions"][0]
        self.assertEqual((entry["outcome"], entry["reason"]), ("NOT_ADMITTED", probes.PROBE_ACCEPTOR_UNAVAILABLE))
        self.assertEqual(review["probe_records"], [])
        self.assertEqual(review["verdict"], "REVISE")

    def test_an_accepted_probe_carries_a_bound_receipt(self):
        self.start(["8"], [{"probes": ["alpha"]}])
        self.round()
        review = self.reviews()[0]
        (record,) = review["probe_records"]
        (receipt,) = review["probe_receipts"]
        self.assertEqual(record["acceptance_receipt_id"], receipt["receipt_id"])
        self.assertEqual(
            (receipt["mode"], receipt["verdict"], receipt["probe_id"]),
            ("accept", "PASS", ALPHA),
        )
        self.assertEqual(receipt["candidate_sha"], review["candidate_sha"])
        self.assertEqual(receipt["input_digest"], review["input_digest"])
        probes.validate_receipts(review["probe_records"], review.get("probe_retired", ()), review["probe_receipts"])


class ChallengeTest(ProbeLaneCase):
    def two_rounds(self, acceptance, second):
        self.start(["8", "8"], [{"probes": ["alpha"]}, second], acceptance=acceptance)
        self.round()
        self.round()
        return self.reviews()[1]

    def test_an_upheld_challenge_retires_the_probe(self):
        review = self.two_rounds(by_mode(all_pass, all_revise), {"challenges": [ALPHA]})
        (retired,) = review["probe_retired"]
        challenge = [r for r in review["probe_receipts"] if r["mode"] == "challenge"]
        self.assertEqual(len(challenge), 1)
        self.assertEqual(retired["challenge_receipt_id"], challenge[0]["receipt_id"])
        self.assertEqual(challenge[0]["verdict"], "REVISE")
        self.assertNotIn("PROBE_FAILED", codes(review))
        self.assertNotIn("probe_commit", review, "nothing active is left to pin")
        self.round()
        self.assertIsNone(self.actor.builder_ctxs[-1].probe_texts)

    def test_a_dismissed_challenge_leaves_the_probe_open(self):
        review = self.two_rounds(by_mode(all_pass, all_pass), {"challenges": [ALPHA]})
        self.assertEqual(review["probe_retired"], [])
        self.assertIn("PROBE_FAILED", codes(review))
        self.assertEqual(list(review["probe_status"].values()), ["OPEN"])

    def test_the_reviewer_alone_cannot_retire_a_probe(self):
        self.start(["8", "8"], [{"probes": ["alpha"]}, {"challenges": [ALPHA]}])
        self.round()
        self.actor.acceptor_for = None
        self.round()
        review = self.reviews()[1]
        self.assertEqual(review["probe_retired"], [])
        self.assertIn("PROBE_FAILED", codes(review))
        entry = [s for s in review["probe_submissions"] if s.get("probe_id") == ALPHA][0]
        self.assertEqual((entry["outcome"], entry["reason"]), ("NOT_ADMITTED", probes.PROBE_ACCEPTOR_UNAVAILABLE))

    def test_a_challenge_of_a_discharged_probe_is_refused_without_asking(self):
        self.start(["8", "8 alpha"], [{"probes": ["alpha"]}, {"challenges": [ALPHA]}])
        self.round()
        self.round()
        review = self.reviews()[1]
        self.assertEqual(len(self.actor.accept_calls), 1, "only the admission was put to the acceptor")
        self.assertEqual(review["probe_retired"], [])
        self.assertEqual(list(review["probe_status"].values()), ["DISCHARGED"])


if __name__ == "__main__":
    unittest.main()
