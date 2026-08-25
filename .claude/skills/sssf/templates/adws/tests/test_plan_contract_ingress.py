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
from adw_modules import plan_author as pa
from adw_modules import plan_contract_ingress as pci
from adw_modules import plan_digest as pd
from adw_modules import plan_model as pm

from test_step2_plan_validation import README, make_repo


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
            # The real hash of the bytes `make_repo` commits. It used to be
            # `"a" * 64` — a knowingly wrong value that projected clean for as
            # long as this fixture existed, which is the proof that nothing
            # read it. `plan_author.fill_git_facts` overwrote the declared pin
            # with the hash of the object it was meant to be checked against,
            # so no declaration could ever be wrong. Computed here rather than
            # pasted, because a pasted digest is the same hole one edit later.
            "sha256": hashlib.sha256(README.encode("utf-8")).hexdigest(),
            "required": True,
        }],
        "requirements": [{
            "requirement_id": "req-freeze",
            "text": "Freeze the writers behind a greeting module.",
            # Where the requirement's behaviour lives, declared as paths and
            # mutation kinds rather than left to the prose above. `written`
            # must be one of this lane's own outputs; `unmodified` must be a
            # pinned source artifact no lane rewrites.
            "surface": [
                {"path": "src/greeting.py", "mutation": "written"},
                {"path": "README.md", "mutation": "unmodified"},
            ],
            # Required, like `surface`: a plan states what external acts it
            # forbids and every requirement states its disposition toward each
            # one. This fixture forbids nothing, which is a declaration rather
            # than an omission.
            "effects": [],
        }],
        "lanes": [{
            "lane_id": "lane-freeze",
            "title": "Freeze writers",
            "execution_context": ".",
            "requirement_ids": ["req-freeze"],
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
                "prohibited_effects": [],
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
        self.assertEqual(plan.title, ir["title"])
        self.assertEqual(plan.intent, ir["title"])
        self.assertEqual(plan.nodes[0].node_id, "lane-freeze")
        self.assertEqual(trace["lanes"], ["lane-freeze"])
        self.assertEqual(pd.digest_of(stored), hashlib.sha256(stored).hexdigest())

    def test_project_canonical_plan_refuses_an_untitled_ir(self):
        """Ship's first production call. Untitled is IR_SCHEMA:title, not a
        parse default — B8 makes a later field optional forever."""
        ir = _ir()
        ir.pop("title")
        ir_path = _write(self.root / "untitled.plan.json", ir)
        receipt_path = _write(
            self.root / "untitled.plan-review.json",
            _receipt(ir_path.read_bytes()))
        with self.assertRaisesRegex(pci.IngressError, "IR_SCHEMA:title"):
            pci.project_canonical_plan(
                ir_path, receipt_path, self.repo)

    def test_a_real_ir_projects_its_title_onto_the_plan(self):
        """The recorded p5 IR. Admission later required a witness the file
        predates; that one field is filled so `project_draft` can run. Title
        is untouched."""
        ir_path = TESTS / "fixtures" / "cmo-consolidation-l-r6-p5.plan.json"
        if not ir_path.is_file():
            self.skipTest("the recorded p5 IR is not checked out")
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
        recorded_title = ir["title"]
        for claim in ir.get("claims") or []:
            if isinstance(claim, dict) and "witness" not in claim:
                claim["witness"] = {"scope": "in_process", "store": "in_memory"}
        draft = pci.project_draft(ir, self.repo)
        self.assertEqual(draft["title"], recorded_title)
        self.assertEqual(draft["intent"], recorded_title)

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

    def test_refuses_a_source_whose_pinned_digest_does_not_match(self):
        """End to end: the IR's pin now reaches the object it names.

        The IR declares a hash, the projection carries it, and
        `plan_author.fill_git_facts` compares it against the blob at the base
        commit instead of overwriting it. Before this, the pin was theatre —
        and this file's own `_ir()` fixture proved it, because it carried
        `"a" * 64` for a README whose real content is `"fixture repository\\n"`
        and authored a plan from it without complaint.
        """
        ir = _ir()
        ir["source_artifacts"][0]["sha256"] = "0" * 64
        ir_path = _write(self.root / "phase-1.plan.json", ir)
        receipt_path = _write(
            self.root / "phase-1.plan-review.json",
            _receipt(ir_path.read_bytes()))
        with self.assertRaisesRegex(pa.AuthoringError,
                                    "OBSERVED_DIGEST_MISMATCH"):
            pci.author_from_plan_contract(
                ir_path, receipt_path, self.root / "maestro-plan.v1", self.repo)

    def test_no_plan_file_is_written_when_the_pin_does_not_match(self):
        """`write_canonical_plan` is create-once (`PLAN_EXISTS`), so a plan
        authored before the refusal would have to be deleted by hand before
        the corrected IR could be authored at all."""
        ir = _ir()
        ir["source_artifacts"][0]["sha256"] = "0" * 64
        ir_path = _write(self.root / "phase-1.plan.json", ir)
        receipt_path = _write(
            self.root / "phase-1.plan-review.json",
            _receipt(ir_path.read_bytes()))
        destination = self.root / "maestro-plan.v1"
        with self.assertRaises(pa.AuthoringError):
            pci.author_from_plan_contract(
                ir_path, receipt_path, destination, self.repo)
        self.assertFalse(destination.exists())

    def test_refuses_unmappable_outputs(self):
        ir = _ir()
        ir["extensions"]["maestro"]["outputs"] = {}
        with self.assertRaisesRegex(pci.IngressError, "UNMAPPABLE_OUTPUTS"):
            pci.project_draft(ir, self.repo)

    def test_projects_a_tests_lane_as_maestro_plan_v3(self):
        ir = _ir()
        ir["lanes"][0]["lane_kind"] = "tests"
        ir["requirements"][0]["surface"][0]["path"] = (
            "tests/test_existing.py")
        ir["extensions"]["maestro"]["outputs"]["lane-freeze"] = [
            "tests/test_existing.py"]

        draft = pci.project_draft(ir, self.repo)

        self.assertEqual(pm.SCHEMA_V3, draft["schema_version"])
        self.assertEqual("tests", draft["nodes"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
