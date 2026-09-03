"""Ingress projection totality: every IR field is carried or named as exempt."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_contract_minimal.json"

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import checkout_layout  # noqa: E402


def _ir() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class IngressTotalityTests(unittest.TestCase):
    def setUp(self) -> None:
        from adw_modules import plan_contract_ingress as ingress

        self.ingress = ingress
        self.ir = _ir()
        self.repo = Path(".")

    def test_extra_lane_key_names_lane_projection(self) -> None:
        ir = copy.deepcopy(self.ir)
        ir["lanes"][0]["owner"] = "x"
        with self.assertRaises(self.ingress.IngressProjectionIncomplete) as caught:
            self.ingress.project_draft(ir, self.repo)
        self.assertIn("_LANE_PROJECTION", str(caught.exception))

    def test_extra_extension_key_names_extension_projection(self) -> None:
        ir = copy.deepcopy(self.ir)
        ir["extensions"]["maestro"]["unexpected"] = "x"
        with self.assertRaises(self.ingress.IngressProjectionIncomplete) as caught:
            self.ingress.project_draft(ir, self.repo)
        self.assertIn("_EXTENSION_PROJECTION", str(caught.exception))

    def test_dropped_claim_ids_binding_is_named(self) -> None:
        real = self.ingress._bindings

        def drop(lane, kind):
            data = real(lane, kind)
            data.pop("claim_ids", None)
            return data

        with mock.patch.object(self.ingress, "_bindings", drop):
            with self.assertRaises(self.ingress.IngressProjectionIncomplete) as caught:
                self.ingress.project_draft(self.ir, self.repo)
        self.assertIn("claim_ids", str(caught.exception))

    def test_obligations_private_key_is_named(self) -> None:
        real = self.ingress._obligations

        def leak(*args, **kwargs):
            data = real(*args, **kwargs)
            data["fixtures"] = []
            return data

        with mock.patch.object(self.ingress, "_obligations", leak):
            with self.assertRaises(self.ingress.IngressProjectionIncomplete) as caught:
                self.ingress.project_draft(self.ir, self.repo)
        self.assertIn("private key", str(caught.exception))

    def test_empty_table_reason_is_refused(self) -> None:
        with mock.patch.dict(
            self.ingress._LANE_PROJECTION_EXEMPT, {"fixture_ids": " "}
        ):
            with self.assertRaises(self.ingress.IngressProjectionIncomplete) as caught:
                self.ingress.project_draft(self.ir, self.repo)
        self.assertIn("_LANE_PROJECTION", str(caught.exception))


def _schema_path() -> Path:
    maestro_root = Path(__file__).resolve().parents[6]
    return (
        maestro_root.parent
        / "the-library"
        / "skills"
        / "plan-contract"
        / "schemas"
        / "plan-ir-v1.schema.json"
    )


class SchemaKeySetTests(unittest.TestCase):
    def test_projection_tables_match_schema_defs(self) -> None:
        from adw_modules import plan_contract_ingress as ingress

        schema_path = _schema_path()
        if not schema_path.is_file():
            # A deployed instance has no the-library beside it. The schema is the
            # only authority for these key sets, so without it there is nothing to
            # compare -- the same shape test_template_parity uses for an absent
            # peer checkout, and visible in a default run rather than silent.
            checkout_layout.skip_visibly(
                "the plan-contract schema is not on this machine at {path}, so "
                "the projection tables cannot be compared against it; the check "
                "runs where the-library is checked out beside this "
                "repository".format(path=schema_path)
            )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        pairs = (
            ("lane", ingress._LANE_PROJECTION),
            ("verifier", ingress._VERIFIER_PROJECTION),
            ("claim", ingress._CLAIM_PROJECTION),
            ("seam", ingress._SEAM_PROJECTION),
            ("fixture", ingress._FIXTURE_PROJECTION),
            ("requirement", ingress._REQUIREMENT_PROJECTION),
            ("maestroExtension", ingress._EXTENSION_PROJECTION),
        )
        mismatches = []
        for def_name, table in pairs:
            expected = set(schema["$defs"][def_name]["properties"])
            actual = set(table)
            if expected != actual:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                mismatches.append(
                    "{0}: missing {1}; extra {2}".format(def_name, missing, extra)
                )
        self.assertEqual(mismatches, [])
        self.assertEqual(
            set(ingress._IR_PROJECTION), set(schema["properties"])
        )
        self.assertEqual(
            set(ingress._EXTENSIONS_PROJECTION),
            set(schema["properties"]["extensions"]["properties"]),
        )

    def test_extra_ir_key_names_ir_projection(self) -> None:
        from adw_modules import plan_contract_ingress as ingress

        ir = _ir()
        ir["unexpected_top"] = "x"
        with self.assertRaises(ingress.IngressProjectionIncomplete) as caught:
            ingress.project_draft(ir, Path("."))
        self.assertIn("_IR_PROJECTION", str(caught.exception))

    def test_extra_extensions_key_names_extensions_projection(self) -> None:
        from adw_modules import plan_contract_ingress as ingress

        ir = _ir()
        ir["extensions"]["claim_kinds"] = []
        ir["extensions"]["other"] = {}
        with self.assertRaises(ingress.IngressProjectionIncomplete) as caught:
            ingress.project_draft(ir, Path("."))
        self.assertIn("_EXTENSIONS_PROJECTION", str(caught.exception))

    def test_unbound_claim_is_named(self) -> None:
        from adw_modules import plan_contract_ingress as ingress

        ir = _ir()
        ir["claims"].append({
            "claim_id": "claim-unbound",
            "kind": "behavior",
            "subject": "x",
            "predicate": "y",
            "object": "z",
            "polarity": "positive",
        })
        with self.assertRaises(ingress.IngressProjectionIncomplete) as caught:
            ingress.project_draft(ir, Path("."))
        self.assertIn("claim-unbound", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
