"""The acceptor must be declared independent of the code reviewer, by vendor.

Independence is the operator's declaration, compared after strip and casefold.
A different route, profile or model is not independence, and an undeclared
vendor is not guessed. Each refusal admits nothing, records NOT_ADMITTED with
the typed reason, never asks the acceptor, and leaves the reviewer's ordinary
REVISE alone. The selection is the production `probes.select_acceptor`.
"""

import unittest

from adw_modules import probes
from adw_modules import scheduler_types as st
from tests.probe_lane_support import ProbeLaneCase, probe_path

REVISE = st.ReviewerVerdict.REVISE


class IndependenceTest(ProbeLaneCase):
    def run_with(self, role_vendors):
        self.start(["8"], [{"probes": ["alpha"]}])
        self.role_vendors = role_vendors
        self.before = {k: v for k, v in role_vendors.items()}
        self.actor.acceptor_for = lambda ctx: probes.select_acceptor(role_vendors)
        self.round()
        return self.reviews()[0]

    def assert_not_independent(self, role_vendors):
        review = self.run_with(role_vendors)
        entry = review["probe_submissions"][0]
        self.assertEqual(
            (entry["path"], entry["outcome"], entry["reason"]),
            (probe_path("alpha"), "NOT_ADMITTED", probes.PROBE_ACCEPTOR_NOT_INDEPENDENT),
            role_vendors,
        )
        self.assertEqual(self.actor.accept_calls, [])
        self.assertEqual(review["probe_records"], [])
        self.assertEqual(review["probe_receipts"], [])
        self.assertEqual(review["verdict"], "REVISE")
        self.assertEqual(len(review["findings"]), 1)
        self.assertNotIn("code", review["findings"][0])
        self.assertEqual(self.role_vendors, self.before, "the declared mapping is read, never written")

    def test_the_same_vendor_is_refused_however_the_routes_differ(self):
        self.assert_not_independent({"code-reviewer": "OpenAI", "test-reviewer": " openai ", "tester": "openai"})

    def test_an_undeclared_author_is_refused(self):
        self.assert_not_independent({"test-reviewer": "xai"})

    def test_an_undeclared_acceptor_is_refused(self):
        self.assert_not_independent({"code-reviewer": "openai"})

    def test_a_blank_vendor_is_undeclared(self):
        self.assert_not_independent({"code-reviewer": "openai", "test-reviewer": "  ", "tester": ""})

    def test_nothing_declared_is_refused(self):
        self.assert_not_independent({})

    def assert_unavailable(self, returned):
        self.start(["8"], [{"probes": ["alpha"]}])
        self.actor.acceptor_for = lambda ctx: returned
        self.round()
        review = self.reviews()[0]
        self.assertEqual(review["probe_submissions"][0]["reason"], probes.PROBE_ACCEPTOR_UNAVAILABLE)
        self.assertEqual(review["probe_records"], [])
        self.assertEqual(self.actor.accept_calls, [])

    def test_none_fails_closed(self):
        self.assert_unavailable(None)

    def test_a_bare_role_fails_closed(self):
        self.assert_unavailable("test-reviewer")

    def test_a_malformed_tuple_fails_closed(self):
        self.assert_unavailable(("test-reviewer",))

    def test_different_declared_vendors_admit_and_both_are_in_the_receipt(self):
        review = self.run_with({"code-reviewer": "openai", "test-reviewer": "xai"})
        (receipt,) = review["probe_receipts"]
        self.assertEqual((receipt["author_vendor"], receipt["acceptor_vendor"]), ("openai", "xai"))
        self.assertEqual(receipt["acceptor_role"], "test-reviewer")
        self.assertEqual(self.actor.accept_calls[0][0], "test-reviewer")
        self.assertEqual(review["probe_submissions"][0]["outcome"], "ADMITTED")
        for key, other in (("author_vendor", "anthropic"), ("acceptor_vendor", "anthropic")):
            forged = dict(receipt, **{key: other})
            with self.assertRaises(probes.ProbeReceiptError, msg=key):
                probes.validate_receipts(review["probe_records"], (), [forged])

    def test_the_tester_is_the_fallback_when_only_it_differs(self):
        review = self.run_with({"code-reviewer": "openai", "test-reviewer": "openai", "tester": "xai"})
        self.assertEqual(self.actor.accept_calls[0][0], "tester")
        self.assertEqual(review["probe_receipts"][0]["acceptor_role"], "tester")


if __name__ == "__main__":
    unittest.main()
