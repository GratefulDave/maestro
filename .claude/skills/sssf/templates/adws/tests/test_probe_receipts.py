"""Admission and retirement are valid only against a receipt that binds them.

The receipts live inside CODE_REVIEW, so the last test rebuilds the whole
picture from a stored artifact's JSON alone, with every in-memory object gone.
"""

from __future__ import annotations

import copy
import json
import unittest

from probe_support import FAILING_PROBE, PRODUCT, REQUIREMENT, RepoCase, digest

from adw_modules import code_review as cr
from adw_modules import probes
from adw_modules import review_contract as rc
from adw_modules import scheduler_types as st

PROBE = "tests/probes/test_zero.py"


def _receipt(record, **override):
    body = dict(
        mode=probes.MODE_ACCEPT,
        probe_id=record["probe_id"],
        source_sha256=record["source_sha256"],
        candidate_sha="a" * 40,
        input_digest=digest("x"),
        requirement_sha256=probes.requirement_digest(record["requirement"]),
        failure_sha256=digest("failure"),
        verdict="PASS",
        acceptor_role="test-reviewer",
        author_vendor="openai",
        acceptor_vendor="xai",
        reason="fine",
    )
    body.update(override)
    return probes.make_receipt(**body)


def _record(source="def test_a():\n    assert False\n", requirement=REQUIREMENT, receipt_id="r"):
    return probes.make_record(
        path=PROBE, source=source, blob_oid="b" * 40, requirement=requirement,
        intent="intent", case_ids=["tests.probes.test_zero::test_a"],
        admitted_in=digest("review"), acceptance_receipt_id=receipt_id,
    )


class ReceiptForm(unittest.TestCase):
    def test_the_id_is_the_digest_of_the_body_and_the_reason_is_outside_it(self) -> None:
        record = _record()
        one = _receipt(record, reason="one")
        two = _receipt(record, reason="two")
        self.assertEqual(one["receipt_id"], two["receipt_id"])
        self.assertNotEqual(one["reason"], two["reason"])

    def test_every_bound_field_moves_the_id(self) -> None:
        record = _record()
        base = _receipt(record)["receipt_id"]
        for field, value in {
            "probe_id": "p-other",
            "source_sha256": digest("s"),
            "candidate_sha": "c" * 40,
            "input_digest": digest("y"),
            "requirement_sha256": digest("q"),
            "failure_sha256": digest("f"),
            "verdict": "REVISE",
            "mode": probes.MODE_CHALLENGE,
            "acceptor_role": "tester",
            "author_vendor": "anthropic",
            "acceptor_vendor": "google",
        }.items():
            with self.subTest(field=field):
                self.assertNotEqual(_receipt(record, **{field: value})["receipt_id"], base)

    def test_a_bad_mode_or_verdict_cannot_be_made(self) -> None:
        record = _record()
        with self.assertRaises(rc.ReviewContractError):
            _receipt(record, mode="approve")
        with self.assertRaises(rc.ReviewContractError):
            _receipt(record, verdict="MAYBE")


class Binding(unittest.TestCase):
    def setUp(self) -> None:
        provisional = _record()
        self.accept = _receipt(provisional)
        self.record = _record(receipt_id=self.accept["receipt_id"])
        # Accepting the real record id (the id depends on path, source, requirement only).
        self.assertEqual(self.record["probe_id"], provisional["probe_id"])

    def _validate(self, records=None, retired=(), receipts=None):
        probes.validate_receipts(
            [self.record] if records is None else records,
            retired,
            [self.accept] if receipts is None else receipts,
        )

    def test_a_bound_admission_validates(self) -> None:
        self._validate()

    def test_an_admission_with_no_receipt_is_refused(self) -> None:
        with self.assertRaises(probes.ProbeReceiptError):
            self._validate(receipts=[])

    def test_a_receipt_bound_to_another_probe_source_or_requirement_is_refused(self) -> None:
        other_source = _record(source="def test_a():\n    assert 1\n")
        for name, receipt_for in {
            "probe": _receipt(other_source),
            "requirement": _receipt(self.record, requirement_sha256=digest("another")),
            "source": _receipt(self.record, source_sha256=digest("another")),
        }.items():
            with self.subTest(name):
                record = {**self.record, "acceptance_receipt_id": receipt_for["receipt_id"]}
                with self.assertRaises(probes.ProbeReceiptError):
                    self._validate(records=[record], receipts=[receipt_for])

    def test_a_revise_or_challenge_receipt_cannot_admit(self) -> None:
        for override in ({"verdict": "REVISE"}, {"mode": probes.MODE_CHALLENGE}):
            with self.subTest(override=override):
                receipt = _receipt(self.record, **override)
                record = {**self.record, "acceptance_receipt_id": receipt["receipt_id"]}
                with self.assertRaises(probes.ProbeReceiptError):
                    self._validate(records=[record], receipts=[receipt])

    def test_a_tampered_receipt_body_is_refused(self) -> None:
        forged = {**self.accept, "verdict": "PASS", "acceptor_vendor": "someone-else"}
        with self.assertRaises(probes.ProbeReceiptError):
            self._validate(receipts=[forged])

    def test_a_record_whose_id_does_not_match_its_fields_is_refused(self) -> None:
        record = {**self.record, "path": "tests/probes/test_other.py"}
        with self.assertRaises(probes.ProbeReceiptError):
            self._validate(records=[record])

    def test_a_duplicate_record_or_receipt_is_refused(self) -> None:
        with self.assertRaises(probes.ProbeReceiptError):
            self._validate(records=[self.record, self.record])
        with self.assertRaises(probes.ProbeReceiptError):
            self._validate(receipts=[self.accept, self.accept])

    def test_retirement_needs_a_revise_challenge_receipt_for_that_probe(self) -> None:
        challenge = _receipt(self.record, mode=probes.MODE_CHALLENGE, verdict="REVISE")
        retired = [{"probe_id": self.record["probe_id"], "retired_in": digest("r2"),
                    "challenge_receipt_id": challenge["receipt_id"]}]
        self._validate(retired=retired, receipts=[self.accept, challenge])
        self.assertEqual(probes.active_records([self.record], retired), ())
        for name, (receipts, entry) in {
            "no receipt": ([self.accept], retired[0]),
            "accept receipt": ([self.accept], {**retired[0], "challenge_receipt_id": self.accept["receipt_id"]}),
            "pass challenge": (
                [self.accept, _receipt(self.record, mode=probes.MODE_CHALLENGE, verdict="PASS")],
                {**retired[0], "challenge_receipt_id": _receipt(
                    self.record, mode=probes.MODE_CHALLENGE, verdict="PASS")["receipt_id"]},
            ),
            "unknown probe": ([self.accept, challenge], {**retired[0], "probe_id": "p-unknown"}),
        }.items():
            with self.subTest(name):
                with self.assertRaises(probes.ProbeReceiptError):
                    self._validate(retired=[entry], receipts=receipts)

    def test_a_probe_cannot_be_retired_twice(self) -> None:
        challenge = _receipt(self.record, mode=probes.MODE_CHALLENGE, verdict="REVISE")
        entry = {"probe_id": self.record["probe_id"], "retired_in": digest("r2"),
                 "challenge_receipt_id": challenge["receipt_id"]}
        with self.assertRaises(probes.ProbeReceiptError):
            self._validate(retired=[entry, entry], receipts=[self.accept, challenge])


class FromTheArtifactAlone(RepoCase):
    def test_the_picture_rebuilds_from_the_stored_json_with_all_transport_state_gone(self) -> None:
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base, PRODUCT)
        admitted = self.admit(measurement.tree, PROBE, FAILING_PROBE, candidate_sha=sha)
        probe = cr.measure_probes(
            tree=measurement.tree, records=[admitted["record"]], receipts=[admitted["receipt"]],
            scratch_root=self.state / "scratch",
        )
        artifact = self.review("r1", accepted, base, sha, ref, measurement,
                               st.ReviewerVerdict.PASS, probe=probe)
        stored = json.loads(json.dumps(artifact.payload))
        del probe, admitted, measurement
        probes.validate_receipts(
            stored["probe_records"], stored["probe_retired"], stored["probe_receipts"]
        )
        (record,) = stored["probe_records"]
        (receipt,) = stored["probe_receipts"]
        self.assertEqual(record["acceptance_receipt_id"], receipt["receipt_id"])
        self.assertEqual(receipt["author_vendor"], "openai")
        self.assertEqual(receipt["acceptor_vendor"], "xai")
        self.assertEqual(
            [probes.probe_id(record["path"], record["source_sha256"], record["requirement"])],
            [record["probe_id"]],
        )
        forged = copy.deepcopy(stored)
        forged["probe_receipts"][0]["acceptor_vendor"] = "openai"
        with self.assertRaises(probes.ProbeReceiptError):
            probes.validate_receipts(
                forged["probe_records"], forged["probe_retired"], forged["probe_receipts"]
            )

    def test_building_a_review_over_a_forged_receipt_is_refused(self) -> None:
        accepted, base = self.sealed()
        sha, ref, measurement = self.measure("m1", accepted, base, PRODUCT)
        admitted = self.admit(measurement.tree, PROBE, FAILING_PROBE, candidate_sha=sha)
        forged = {**admitted["receipt"], "verdict": "REVISE"}
        with self.assertRaises(probes.ProbeReceiptError):
            cr.measure_probes(
                tree=measurement.tree, records=[admitted["record"]], receipts=[forged],
                scratch_root=self.state / "scratch",
            )
        probe = cr.ProbeMeasurement(
            records=(admitted["record"],), receipts=(forged,),
            status={admitted["record"]["probe_id"]: probes.STATUS_OPEN},
        )
        with self.assertRaises(probes.ProbeReceiptError):
            self.review("r1", accepted, base, sha, ref, measurement,
                        st.ReviewerVerdict.REVISE, probe=probe)


if __name__ == "__main__":
    unittest.main()
