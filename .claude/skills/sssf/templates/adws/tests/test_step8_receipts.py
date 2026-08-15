"""A route is admitted only by a complete, executed receipt."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules.route_receipts import (
    ReceiptInvalid,
    ReceiptSignatureInvalid,
    ReceiptSignatureMissing,
    load_admitted_routes,
    load_public_key,
    load_route_receipt,
)


FIXTURES = Path(__file__).parent / "fixtures" / "step8"


class RouteReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.key = load_public_key(FIXTURES / "route_receipts.pub")

    def test_real_signed_route_receipts_prove_binding_exit_and_continuity(self):
        for route in ("omp", "claude"):
            receipt = load_route_receipt(
                FIXTURES / (route + ".json"), verify_keys=(self.key,))
            self.assertEqual(receipt.route, route)
            self.assertEqual(receipt.first_exit_code, 0)
            self.assertEqual(receipt.continuation_exit_code, 0)
            self.assertTrue(receipt.continuity_proven)
            self.assertTrue(receipt.reported_model)
            self.assertTrue(receipt.visible_pane_cwd_verified)
            self.assertTrue(receipt.cancellation_clean)

    def test_admission_binds_each_configured_route_to_matching_signed_bytes(self):
        admitted = load_admitted_routes(
            {"omp": FIXTURES / "omp.json", "claude": FIXTURES / "claude.json"},
            verify_keys=(self.key,))
        self.assertTrue(admitted.admits("omp"))
        self.assertTrue(admitted.admits("claude"))
        self.assertFalse(admitted.admits("other"))
        with self.assertRaises(AttributeError):
            admitted.routes = frozenset()
        with self.assertRaisesRegex(ReceiptInvalid, "ROUTE_MISMATCH"):
            load_admitted_routes(
                {"omp": FIXTURES / "claude.json"}, verify_keys=(self.key,))

    def test_tampered_receipt_bytes_and_wrong_key_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "omp.json"
            signature_path = Path(str(receipt_path) + ".sig")
            data = bytearray((FIXTURES / "omp.json").read_bytes())
            data[0] ^= 1
            receipt_path.write_bytes(data)
            signature_path.write_bytes((FIXTURES / "omp.json.sig").read_bytes())
            with self.assertRaises(ReceiptSignatureInvalid):
                load_route_receipt(receipt_path, verify_keys=(self.key,))
        wrong_key = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325"
            "af021a68f707511a")
        with self.assertRaises(ReceiptSignatureInvalid):
            load_route_receipt(FIXTURES / "omp.json", verify_keys=(wrong_key,))

    def test_missing_signature_and_incomplete_capture_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_text(json.dumps({"route": "omp", "continuity_proven": False}))
            with self.assertRaises(ReceiptSignatureMissing):
                load_route_receipt(path, verify_keys=(self.key,))


if __name__ == "__main__":
    unittest.main()
