"""A probe failure reaches the builder as a typed, harness-synthesized finding.

`code` is the one field a transition may key on without reading reviewer
prose, so it is exactly the field a reviewer must never be able to set. The
end-to-end cases drive `review_builder_output` over a real candidate, a real
accepted suite and a real probe execution.
"""

from __future__ import annotations

import json
import unittest

from probe_support import (
    FAILING_PROBE, PRODUCT, REQUIREMENT, RepoCase,
)

from adw_modules import code_review as cr
from adw_modules import probes
from adw_modules import review_contract as rc
from adw_modules import scheduler_types as st

PROBE = "tests/probes/test_zero.py"
REVIEWER_FINDING = {
    "implementation_area": "refund.py",
    "observed_behavior": "zero is accepted",
    "required_behavior": "refuse zero",
    "violated_requirement": REQUIREMENT,
}


class FindingShape(unittest.TestCase):
    RECORD = {
        "path": PROBE,
        "intent": "zero must be refused too",
        "requirement": REQUIREMENT,
    }

    def test_the_harness_finding_carries_the_code_the_path_and_the_cited_requirement(self) -> None:
        finding = probes.probe_failed_finding(self.RECORD, ["FAILED a::b - assert 0 is None"])
        self.assertEqual(finding["code"], st.PROBE_FAILED)
        self.assertEqual(finding["implementation_area"], PROBE)
        self.assertEqual(finding["required_behavior"], "zero must be refused too")
        self.assertEqual(finding["violated_requirement"], REQUIREMENT)
        self.assertIn("assert 0 is None", finding["observed_behavior"])
        self.assertEqual(finding["axis"], st.FINDING_AXIS_SPEC)

    def test_it_survives_the_normalizer_with_its_code(self) -> None:
        finding = probes.probe_failed_finding(self.RECORD, [])
        (normalized,) = st.require_revise_findings([finding])
        self.assertEqual(normalized["code"], st.PROBE_FAILED)
        self.assertTrue(finding["observed_behavior"].strip())

    def test_an_unknown_code_is_refused(self) -> None:
        finding = probes.probe_failed_finding(self.RECORD, [])
        for bad in ("PROBE_OTHER", "", 7):
            with self.subTest(code=bad):
                with self.assertRaises(st.CanonicalIdentityError):
                    st.require_revise_findings([{**finding, "code": bad}])

    def test_the_code_is_not_advertised_as_an_optional_reviewer_key(self) -> None:
        self.assertNotIn("code", st.FINDING_OPTIONAL_KEYS)
        self.assertEqual(st.FINDING_CODES, ("PROBE_FAILED",))


class ThroughTheReview(RepoCase):
    def _setup(self):
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base, PRODUCT)
        admitted = self.admit(measurement.tree, PROBE, FAILING_PROBE, candidate_sha=sha)
        return accepted, base, sha, ref, measurement, admitted

    def _probe(self, measurement, admitted):
        return cr.measure_probes(
            tree=measurement.tree,
            records=[admitted["record"]],
            receipts=[admitted["receipt"]],
            scratch_root=self.state / "scratch",
        )

    def test_a_failing_probe_promotes_pass_to_revise_with_a_coded_finding(self) -> None:
        accepted, base, sha, ref, measurement, admitted = self._setup()
        probe = self._probe(measurement, admitted)
        artifact = self.review("r1", accepted, base, sha, ref, measurement,
                               st.ReviewerVerdict.PASS, probe=probe)
        self.assertIs(artifact.verdict, st.ReviewerVerdict.REVISE)
        (finding,) = artifact.payload["findings"]
        self.assertEqual(finding["code"], st.PROBE_FAILED)
        self.assertEqual(finding["implementation_area"], PROBE)
        self.assertTrue(
            any(line.startswith("[probe {0}]".format(PROBE)) for line in artifact.payload["failure_output"]),
            artifact.payload["failure_output"],
        )
        self.assertEqual(artifact.payload["probe_summary"], {"active": 1, "discharged": 0, "open": 1})
        self.assertEqual(artifact.payload["public_result_summary"]["failed"], 0)

    def test_the_reviewers_own_findings_stay_beside_the_probe_finding(self) -> None:
        accepted, base, sha, ref, measurement, admitted = self._setup()
        artifact = self.review("r1", accepted, base, sha, ref, measurement,
                               st.ReviewerVerdict.REVISE, (REVIEWER_FINDING,),
                               probe=self._probe(measurement, admitted))
        codes = [f.get("code") for f in artifact.payload["findings"]]
        self.assertEqual(codes, [None, st.PROBE_FAILED])

    def test_a_discharged_probe_adds_no_finding_and_a_pass_stays_pass(self) -> None:
        accepted, base, sha, ref, measurement, admitted = self._setup()
        both_zero_and_negative = (
            "def refund(a):\n    if a <= 0:\n        return None\n    return a\n"
        )
        sha3, ref3, both = self.measure("m3", accepted, base, both_zero_and_negative)
        probes.write_probe_tree(both.tree, {PROBE: FAILING_PROBE})
        probe = cr.measure_probes(
            tree=both.tree, records=[admitted["record"]], receipts=[admitted["receipt"]],
            scratch_root=self.state / "scratch",
        )
        self.assertEqual(probe.status, {admitted["record"]["probe_id"]: probes.STATUS_DISCHARGED})
        artifact = self.review("r3", accepted, base, sha3, ref3, both,
                               st.ReviewerVerdict.PASS, probe=probe)
        self.assertIs(artifact.verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(artifact.payload["findings"], [])
        self.assertEqual(artifact.payload["probe_summary"], {"active": 1, "discharged": 1, "open": 0})
        self.assertEqual(cr.review_progress(artifact.payload)[1], (admitted["record"]["probe_id"],))

    def test_the_builder_sees_the_probe_source_its_status_and_the_framing(self) -> None:
        accepted, base, sha, ref, measurement, admitted = self._setup()
        artifact = self.review("r1", accepted, base, sha, ref, measurement,
                               st.ReviewerVerdict.PASS, probe=self._probe(measurement, admitted))
        view = cr.builder_view(
            public_contract=accepted.payload["public_contract"],
            architecture_constraints=("c",),
            test_suite_digest=str(accepted.payload["test_suite_digest"]),
            test_paths=("tests/test_refund.py",),
            prior_code_review=artifact,
            probe_texts={PROBE: FAILING_PROBE},
        )
        (shown,) = view["probes"]
        self.assertEqual(shown["source"], FAILING_PROBE)
        self.assertEqual(shown["status"], probes.STATUS_OPEN)
        self.assertEqual(shown["requirement"], REQUIREMENT)
        self.assertIn("PROBE_FAILED", view["prior_code_review"]["findings"][0])
        self.assertTrue(
            any(line.startswith("[probe ") for line in view["prior_code_review"]["failure_output"])
        )

    def test_a_view_without_probes_has_no_probes_key(self) -> None:
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base, PRODUCT)
        artifact = self.review("r1", accepted, base, sha, ref, measurement,
                               st.ReviewerVerdict.REVISE, (REVIEWER_FINDING,))
        view = cr.builder_view(
            public_contract=accepted.payload["public_contract"],
            architecture_constraints=("c",),
            test_suite_digest=str(accepted.payload["test_suite_digest"]),
            prior_code_review=artifact,
        )
        self.assertNotIn("probes", view)

    def test_a_reviewer_submitted_code_is_refused(self) -> None:
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base, PRODUCT)
        forged = {**REVIEWER_FINDING, "code": st.PROBE_FAILED}
        with self.assertRaises(rc.ReviewContractError):
            self.review("r1", accepted, base, sha, ref, measurement,
                        st.ReviewerVerdict.REVISE, (forged,))

    def test_a_probe_less_artifact_is_byte_identical_to_one_with_an_empty_measurement(self) -> None:
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base, PRODUCT)
        plain = self.review("r1", accepted, base, sha, ref, measurement,
                            st.ReviewerVerdict.REVISE, (REVIEWER_FINDING,))
        empty = self.review("r1", accepted, base, sha, ref, measurement,
                            st.ReviewerVerdict.REVISE, (REVIEWER_FINDING,),
                            probe=cr.ProbeMeasurement())
        self.assertEqual(plain.output_digest, empty.output_digest)
        self.assertEqual(plain.artifact_ref, empty.artifact_ref)
        self.assertEqual(json.dumps(plain.payload, sort_keys=True), json.dumps(empty.payload, sort_keys=True))
        self.assertEqual(
            sorted(plain.payload),
            sorted(
                ["builder_base_sha", "candidate_ref", "candidate_sha", "failure_output", "findings",
                 "input_artifact_ids", "input_digest", "public_result_summary", "results_digest",
                 "test_suite_digest", "verdict"]
            ),
        )
        self.assertIsInstance(cr.review_progress(plain.payload), int)


if __name__ == "__main__":
    unittest.main()
