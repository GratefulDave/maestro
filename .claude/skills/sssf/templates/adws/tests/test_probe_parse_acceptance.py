"""The acceptor's answer is parsed strictly before anything changes.

A malformed answer raises once for the whole batch; the reviewer's ordinary
verdict is untouched whether the acceptor is unavailable, not independent, or
wrong-shaped.
"""

from __future__ import annotations

import unittest

from probe_support import RepoCase

from adw_modules import code_review as cr
from adw_modules import probes
from adw_modules import scheduler_types as st

IDS = ["p-aaa", "p-bbb"]


def _ok(**override):
    raw = {
        "verdict": "PASS",
        "probe_verdicts": [
            {"probe_id": "p-aaa", "verdict": "PASS", "reason": "follows"},
            {"probe_id": "p-bbb", "verdict": "PASS", "reason": "follows"},
        ],
    }
    raw.update(override)
    return raw


class StrictParser(unittest.TestCase):
    def test_a_complete_answer_parses_to_verdicts_and_reasons(self) -> None:
        self.assertEqual(
            probes.parse_acceptance(_ok(), IDS, probes.MODE_ACCEPT),
            {"p-aaa": ("PASS", "follows"), "p-bbb": ("PASS", "follows")},
        )

    def test_one_revise_item_requires_a_revise_aggregate(self) -> None:
        raw = _ok(verdict="REVISE")
        raw["probe_verdicts"][1]["verdict"] = "REVISE"
        parsed = probes.parse_acceptance(raw, IDS, probes.MODE_CHALLENGE)
        self.assertEqual(parsed["p-bbb"][0], "REVISE")

    def test_every_malformation_raises_the_one_typed_error(self) -> None:
        contradictory = _ok()
        contradictory["probe_verdicts"][1]["verdict"] = "REVISE"
        cases = {
            "not a mapping": ["PASS"],
            "no list": {"verdict": "PASS", "probe_verdicts": "PASS"},
            "partial": _ok(probe_verdicts=[_ok()["probe_verdicts"][0]]),
            "duplicate": _ok(probe_verdicts=[_ok()["probe_verdicts"][0]] * 2),
            "unknown id": _ok(
                probe_verdicts=[
                    _ok()["probe_verdicts"][0],
                    {"probe_id": "p-zzz", "verdict": "PASS", "reason": ""},
                ]
            ),
            "bad verdict": _ok(
                probe_verdicts=[
                    _ok()["probe_verdicts"][0],
                    {"probe_id": "p-bbb", "verdict": "MAYBE", "reason": ""},
                ]
            ),
            "extra key": _ok(
                probe_verdicts=[
                    _ok()["probe_verdicts"][0],
                    {"probe_id": "p-bbb", "verdict": "PASS", "reason": "", "x": 1},
                ]
            ),
            "contradictory aggregate": contradictory,
            "missing aggregate": {"probe_verdicts": _ok()["probe_verdicts"]},
            "non-string reason": _ok(
                probe_verdicts=[
                    _ok()["probe_verdicts"][0],
                    {"probe_id": "p-bbb", "verdict": "PASS", "reason": 3},
                ]
            ),
        }
        for name, raw in cases.items():
            with self.subTest(name):
                with self.assertRaises(probes.ProbeAcceptanceMalformed) as caught:
                    probes.parse_acceptance(raw, IDS, probes.MODE_ACCEPT)
                self.assertEqual(caught.exception.code, probes.PROBE_ACCEPTANCE_MALFORMED)

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(probes.ProbeAcceptanceMalformed):
            probes.parse_acceptance(_ok(), IDS, "approve")


class IndependenceByDeclaredVendor(unittest.TestCase):
    def test_different_declared_vendors_select_the_test_reviewer_first(self) -> None:
        picked = probes.select_acceptor(
            {"code-reviewer": " OpenAI ", "test-reviewer": "xai", "tester": "anthropic"}
        )
        self.assertEqual(picked, ("test-reviewer", "openai", "xai"))

    def test_the_tester_is_the_fallback_when_only_it_differs(self) -> None:
        picked = probes.select_acceptor(
            {"code-reviewer": "openai", "test-reviewer": "openai", "tester": "xai"}
        )
        self.assertEqual(picked, ("tester", "openai", "xai"))

    def test_anything_else_is_not_independent(self) -> None:
        for name, vendors in {
            "none": None,
            "empty": {},
            "author undeclared": {"test-reviewer": "xai"},
            "acceptor undeclared": {"code-reviewer": "openai"},
            "equal": {"code-reviewer": "openai", "test-reviewer": "OpenAI", "tester": "openai"},
            "blank": {"code-reviewer": "openai", "test-reviewer": "  ", "tester": ""},
            "non-string": {"code-reviewer": "openai", "test-reviewer": 5, "tester": None},
        }.items():
            with self.subTest(name):
                picked = probes.select_acceptor(vendors)
                self.assertIsInstance(picked, probes.Rejection)
                self.assertEqual(picked.code, probes.PROBE_ACCEPTOR_NOT_INDEPENDENT)


class VerdictPreservedWhenNothingIsAdmitted(RepoCase):
    def test_a_not_admitted_submission_leaves_the_ordinary_revise_untouched(self) -> None:
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base)
        finding = {
            "implementation_area": "refund.py",
            "observed_behavior": "zero accepted",
            "required_behavior": "refuse zero",
            "violated_requirement": "negative amounts are refused",
        }
        plain = self.review("r1", accepted, base, sha, ref, measurement,
                            st.ReviewerVerdict.REVISE, (finding,))
        rejected = self.review(
            "r1", accepted, base, sha, ref, measurement, st.ReviewerVerdict.REVISE, (finding,),
            probe=cr.ProbeMeasurement(
                submissions=(
                    {"path": "tests/p.py", "outcome": "NOT_ADMITTED",
                     "reason": probes.PROBE_ACCEPTOR_NOT_INDEPENDENT},
                )
            ),
        )
        self.assertIs(rejected.verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(rejected.payload["findings"], plain.payload["findings"])
        self.assertEqual(rejected.payload["failure_output"], plain.payload["failure_output"])
        self.assertEqual(rejected.payload["probe_submissions"][0]["outcome"], "NOT_ADMITTED")
        self.assertEqual(rejected.payload["probe_records"], [])
        self.assertIsInstance(cr.review_progress(rejected.payload), int)

    def test_a_pass_stays_a_pass_when_the_only_submission_was_not_admitted(self) -> None:
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base, "def refund(a):\n    return a\n")
        artifact = self.review(
            "r1", accepted, base, sha, ref, measurement, st.ReviewerVerdict.PASS,
            probe=cr.ProbeMeasurement(
                submissions=({"path": "tests/p.py", "outcome": "REJECTED", "reason": probes.PROBE_UNCITED},)
            ),
        )
        self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(artifact.payload["findings"], [])


if __name__ == "__main__":
    unittest.main()
