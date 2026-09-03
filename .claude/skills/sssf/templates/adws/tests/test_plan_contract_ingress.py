"""Projection from plan-contract.v1 onto maestro-plan.artifact-factory.v1 lanes."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_contract_minimal.json"


def _ir() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _receipt_for(ir: dict, **extra) -> dict:
    payload = {
        "schema_version": "plan-contract-review.v1",
        "verdict": "PASS",
        "ir_sha256": hashlib.sha256(
            json.dumps(ir, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    payload.update(extra)
    return payload


def _write_json(path: Path, payload: dict) -> bytes:
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(raw)
    return raw


class IngressImportTests(unittest.TestCase):
    def test_module_imports(self) -> None:
        from adw_modules import plan_contract_ingress as ingress

        self.assertTrue(hasattr(ingress, "project_draft"))


class IngressProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        from adw_modules import plan_contract_ingress as ingress

        self.ingress = ingress
        self.ir = _ir()
        self.repo = Path(".")

    def _project(self, ir: dict | None = None) -> dict:
        return self.ingress.project_draft(ir if ir is not None else self.ir, self.repo)

    def _lane(self, projected: dict, lane_id: str) -> dict:
        for lane in projected["lanes"]:
            if lane["id"] == lane_id:
                return lane
        raise KeyError(lane_id)

    def test_projection_compiles(self) -> None:
        from adw_modules import plan_canonical
        from adw_modules.plan_compiler import compile_plan

        draft = self._project()
        stored = plan_canonical.canonicalize(draft)
        compile_plan(stored)
        self.assertTrue(plan_canonical.is_canonical(stored))

    def test_claims_reach_tester_and_acceptance(self) -> None:
        from adw_modules.plan_contract_ingress import _claim_sentence

        draft = self._project()
        lane_b = self._lane(draft, "lane-b")
        lane_t = self._lane(draft, "lane-t")
        claims = {item["claim_id"]: item for item in self.ir["claims"]}
        s1 = _claim_sentence(claims["claim-b1"])
        s2 = _claim_sentence(claims["claim-b2"])
        self.assertEqual(
            lane_b["acceptance"],
            ["verify-b: src/b.py meets both claims", s1, s2],
        )
        paired = lane_t["spec"]["obligations"]["for_build_lanes"]
        self.assertEqual(paired[0]["lane_id"], "lane-b")
        self.assertEqual(
            paired[0]["claims"][0]["mutation_kinds"],
            claims["claim-b1"]["mutation_kinds"],
        )
        self.assertEqual(
            paired[0]["claims"][1]["mutation_kinds"],
            claims["claim-b2"]["mutation_kinds"],
        )
        self.assertEqual(
            paired[0]["observed_baseline"][0]["record_selector"],
            "selector-b-unique",
        )
        self.assertEqual(paired[0]["acceptance"], lane_b["acceptance"])

    def test_no_private_keys_and_no_fixture_content_on_build_lane(self) -> None:
        from adw_modules import scheduler_types as st

        draft = self._project()
        lane_b = self._lane(draft, "lane-b")
        lane_t = self._lane(draft, "lane-t")
        st._reject_private_keys(lane_b["spec"])
        st._reject_private_keys(lane_t["spec"])
        dumped = json.dumps(lane_b["spec"])
        fixture_b = self.ir["fixtures"][1]
        for key in (
            "fixture_id",
            "record_selector",
            "observed_value",
            "consumer_obligation",
            "prohibited_behavior",
            "meaning",
        ):
            self.assertNotIn(fixture_b[key], dumped)
        self.assertNotIn("fixture_ids", lane_b["spec"]["bindings"])

    def test_gate_and_kind(self) -> None:
        draft = self._project()
        expected_gate = {
            "runner": "pytest",
            "argv": ["tests/test_t.py", "-q"],
            "cwd": ".",
            "min_cases": 2,
        }
        for lane_id in ("lane-t", "lane-b"):
            lane = self._lane(draft, lane_id)
            self.assertEqual(lane["spec"]["gate"], expected_gate)
        ir = copy.deepcopy(self.ir)
        del ir["lanes"][1]["lane_kind"]
        projected = self._project(ir)
        self.assertEqual(self._lane(projected, "lane-b")["lane_kind"], "build")
        lane_t = self._lane(draft, "lane-t")
        self.assertEqual(lane_t["spec"]["bindings"]["verifier_ids"], ["verify-t"])
        self.assertEqual(
            lane_t["spec"]["seams"][0]["contract"],
            self.ir["seams"][0]["contract"],
        )
        self.assertIn("seam-shared", lane_t["spec"]["instruction"])

    def test_performed_prohibited_effect_is_unmappable(self) -> None:
        ir = copy.deepcopy(self.ir)
        ir["requirements"][1]["effects"] = [
            {"effect": "network", "disposition": "performed"}
        ]
        with self.assertRaises(self.ingress.IngressError) as caught:
            self._project(ir)
        self.assertIn("UNMAPPABLE_EFFECTS:lane-b.network", str(caught.exception))

    def test_omitted_prohibited_effect_is_unmappable(self) -> None:
        ir = copy.deepcopy(self.ir)
        ir["requirements"][1]["effects"] = []
        with self.assertRaises(self.ingress.IngressError) as caught:
            self._project(ir)
        self.assertIn("UNMAPPABLE_EFFECTS:lane-b.network", str(caught.exception))

    def test_every_prohibited_effect_is_emitted(self) -> None:
        ir = copy.deepcopy(self.ir)
        ir["extensions"]["maestro"]["prohibited_effects"].append({
            "effect": "filesystem_escape",
            "meaning": "never write outside outputs",
        })
        extra = {"effect": "filesystem_escape", "disposition": "none"}
        for requirement in ir["requirements"]:
            requirement["effects"].append(extra)
        draft = self._project(ir)
        effects = {
            item["effect"]
            for item in self._lane(draft, "lane-b")["spec"]["effects"]
        }
        self.assertEqual(effects, {"network", "filesystem_escape"})

    def test_receipt_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir_path = root / "ir.json"
            ir_bytes = _write_json(ir_path, self.ir)
            bad_hash = root / "bad-hash.json"
            _write_json(
                bad_hash,
                {
                    "schema_version": "plan-contract-review.v1",
                    "verdict": "PASS",
                    "ir_sha256": "0" * 64,
                },
            )
            fail = root / "fail.json"
            _write_json(
                fail,
                {
                    "schema_version": "plan-contract-review.v1",
                    "verdict": "FAIL",
                    "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
                },
            )
            with self.assertRaises(self.ingress.IngressError) as mismatch:
                self.ingress.project_canonical_plan(
                    ir_path, bad_hash, self.repo
                )
            self.assertIn("RECEIPT_IR_MISMATCH", str(mismatch.exception))
            with self.assertRaises(self.ingress.IngressError) as not_pass:
                self.ingress.project_canonical_plan(ir_path, fail, self.repo)
            self.assertIn("RECEIPT_NOT_PASS", str(not_pass.exception))


class PlanAuthorCliTests(unittest.TestCase):
    def _author(self, root: Path, out: Path) -> subprocess.CompletedProcess[str]:
        ir = _ir()
        ir_path = root / "ir.json"
        ir_bytes = _write_json(ir_path, ir)
        receipt_path = root / "receipt.json"
        _write_json(
            receipt_path,
            {
                "schema_version": "plan-contract-review.v1",
                "verdict": "PASS",
                "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
            },
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ADWS)
        return subprocess.run(
            [
                sys.executable,
                str(ADWS / "tools" / "plan_author_cli.py"),
                "--from-plan-contract",
                str(ir_path),
                "--receipt",
                str(receipt_path),
                "--out",
                str(out),
                "--repo",
                str(root / "repo"),
            ],
            cwd=str(ADWS),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_authors_a_file(self) -> None:
        from adw_modules import plan_canonical
        from adw_modules.plan_compiler import compile_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["outcome"], "PLAN_AUTHORED")
            self.assertEqual(payload["lanes"], ["lane-t", "lane-b"])
            self.assertEqual(payload["repo"], str(root / "repo"))
            stored = out.read_bytes()
            compile_plan(stored)
            self.assertTrue(plan_canonical.is_canonical(stored))

    def test_cli_second_run_reports_plan_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            first = self._author(root, out)
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            second = self._author(root, out)
            self.assertEqual(second.returncode, 1, second.stderr + second.stdout)
            payload = json.loads(second.stdout)
            self.assertEqual(payload["outcome"], "AuthoringError")
            self.assertIn("PLAN_EXISTS", payload["detail"])


if __name__ == "__main__":
    unittest.main()
