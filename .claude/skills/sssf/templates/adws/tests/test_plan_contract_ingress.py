"""Fail-closed projection from plan-contract.v1 to maestro-plan.v1."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from adw_modules import plan_canonical as pc
from adw_modules import plan_contract_ingress as pci
from adw_modules import plan_digest as pd
from adw_modules import plan_model as pm

from test_step2_plan_validation import make_repo


def _write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ir(**overrides):
    data = {
        "schema_version": "plan-contract.v1",
        "plan_id": "phase-1",
        "title": "Phase 1 freeze",
        "plan_kind": "brownfield",
        "source_artifacts": [{
            "source_id": "src-readme",
            "path": "README.md",
            "sha256": "a" * 64,
            "required": True,
        }],
        "lanes": [{
            "lane_id": "lane-freeze",
            "title": "Freeze writers",
            "execution_context": ".",
            "depends_on": [],
            "verifier_ids": ["verify-freeze"],
        }],
        "verifiers": [{
            "verifier_id": "verify-freeze",
            "lane_ids": ["lane-freeze"],
            "source_ids": ["src-readme"],
            "command": "python3 -m pytest tests/test_existing.py",
            "min_executed": 1,
        }],
        "extensions": {
            "maestro": {
                "repo": "example",
                "outputs": {"lane-freeze": ["src/greeting.py"]},
                "integration_branch": "main",
                "integration_gate": {
                    "runner": "pytest",
                    "argv": ["tests"],
                    "cwd": ".",
                    "min_cases": 1,
                },
            }
        },
    }
    data.update(overrides)
    return data


def _receipt(ir_bytes: bytes, **overrides):
    data = {
        "schema_version": "plan-contract-review.v1",
        "verdict": "PASS",
        "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
    }
    data.update(overrides)
    return data


class PlanContractIngressTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = make_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_authors_canonical_plan_from_approved_package(self):
        ir = _ir()
        ir_path = _write(self.root / "phase-1.plan.json", ir)
        receipt_path = _write(
            self.root / "phase-1.plan-review.json",
            _receipt(ir_path.read_bytes()))
        destination = self.root / "maestro-plan.v1"
        stored, trace = pci.author_from_plan_contract(
            ir_path, receipt_path, destination, self.repo)
        plan = pm.parse_bytes(stored)
        self.assertTrue(pc.is_canonical(stored))
        self.assertEqual(plan.plan_id, "phase-1")
        self.assertEqual(plan.nodes[0].node_id, "lane-freeze")
        self.assertEqual(trace["lanes"], ["lane-freeze"])
        self.assertEqual(pd.digest_of(stored), hashlib.sha256(stored).hexdigest())

    def test_refuses_receiptless_and_stale_digest(self):
        ir_path = _write(self.root / "ir.json", _ir())
        receipt_path = _write(
            self.root / "receipt.json",
            _receipt(b"not-the-ir"))
        with self.assertRaisesRegex(pci.IngressError, "RECEIPT_IR_MISMATCH"):
            pci.author_from_plan_contract(
                ir_path, receipt_path, self.root / "out", self.repo)

    def test_refuses_architecture_kind(self):
        ir = _ir(plan_kind="architecture")
        with self.assertRaisesRegex(pci.IngressError, "ARCHITECTURE_NOT_EXECUTABLE"):
            pci.project_draft(ir, self.repo)

    def test_refuses_ambient_source_path(self):
        ir = _ir()
        ir["source_artifacts"][0]["path"] = "../secret"
        with self.assertRaisesRegex(pci.IngressError, "AMBIENT_PATH"):
            pci.project_draft(ir, self.repo)

    def test_refuses_broad_gate(self):
        ir = _ir()
        ir["verifiers"][0]["command"] = "python3 -m pytest"
        with self.assertRaisesRegex(pci.IngressError, "BROAD_GATE"):
            pci.project_draft(ir, self.repo)

    def test_refuses_unmappable_outputs(self):
        ir = _ir()
        ir["extensions"]["maestro"]["outputs"] = {}
        with self.assertRaisesRegex(pci.IngressError, "UNMAPPABLE_OUTPUTS"):
            pci.project_draft(ir, self.repo)


if __name__ == "__main__":
    unittest.main()
