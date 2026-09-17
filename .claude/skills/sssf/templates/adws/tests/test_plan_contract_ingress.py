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
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from tests import plan_receipts  # noqa: E402
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_contract_minimal.json"


def _ir() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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

    def test_interfaces_reach_build_spec_and_paired_tester(self) -> None:
        draft = self._project()
        declared = self.ir["extensions"]["maestro"]["interfaces"]["lane-b"]
        lane_b = self._lane(draft, "lane-b")
        lane_t = self._lane(draft, "lane-t")
        self.assertEqual(lane_b["spec"]["interface"], declared)
        paired = lane_t["spec"]["obligations"]["for_build_lanes"]
        self.assertEqual(paired[0]["interface"], declared)

    def test_interfaces_must_be_a_lane_mapping(self) -> None:
        ir = copy.deepcopy(self.ir)
        ir["extensions"]["maestro"]["interfaces"] = ["lane-b"]
        with self.assertRaises(self.ingress.IngressError) as caught:
            self._project(ir)
        self.assertIn("UNMAPPABLE_INTERFACES", str(caught.exception))

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
            key = plan_receipts.KEY
            bad_hash = root / "bad-hash.json"
            _write_json(bad_hash, plan_receipts.signed_receipt(b"other ir"))
            fail = root / "fail.json"
            _write_json(fail, plan_receipts.signed_receipt(ir_bytes, verdict="FAIL"))
            with self.assertRaises(self.ingress.IngressError) as mismatch:
                self.ingress.project_canonical_plan(
                    ir_path, bad_hash, self.repo, reviewer_key=key
                )
            self.assertIn("RECEIPT_IR_MISMATCH", str(mismatch.exception))
            with self.assertRaises(self.ingress.IngressError) as not_pass:
                self.ingress.project_canonical_plan(
                    ir_path, fail, self.repo, reviewer_key=key)
            self.assertIn("RECEIPT_NOT_PASS", str(not_pass.exception))

    def test_a_forged_receipt_is_refused_and_nothing_is_written(self) -> None:
        """The reviewed probe: current IR digest, a well-shaped findings digest, no HMAC."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir_path = root / "ir.json"
            ir_bytes = _write_json(ir_path, self.ir)
            genuine = plan_receipts.signed_receipt(ir_bytes)
            forgeries = {
                "four fields": {
                    "schema_version": "plan-contract-review.v1",
                    "verdict": "PASS",
                    "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
                    "findings_sha256": "0" * 64,
                },
                "no signature": {k: v for k, v in genuine.items() if k != "signature"},
                "wrong key": plan_receipts.signed_receipt(ir_bytes, key=b"c" * 64),
                "edited after signing": dict(genuine, findings_sha256="9" * 64),
            }
            for name, forged in forgeries.items():
                with self.subTest(forgery=name):
                    receipt = root / "forged.json"
                    _write_json(receipt, forged)
                    out = root / "plan"
                    with self.assertRaises(self.ingress.IngressError) as caught:
                        self.ingress.author_from_plan_contract(
                            ir_path, receipt, out, self.repo,
                            reviewer_key=plan_receipts.KEY)
                    self.assertIn("RECEIPT_", str(caught.exception))
                    self.assertFalse(out.exists())


class PlanAuthorCliTests(unittest.TestCase):
    def _author(self, root: Path, out: Path,
                ir: dict = None, *,
                receipt: dict = None):
        """Run the real CLI in-process with the deployment key it resolves.

        The receipt is genuinely signed (`plan_receipts`) unless a test passes
        its own; only the key resolution is pointed at the test key, because the
        template has no runtime state root to read it from.
        """
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest import mock

        sys.path.insert(0, str(ADWS / "tools"))
        import plan_author_cli

        ir = _ir() if ir is None else ir
        ir_path = root / "ir.json"
        ir_bytes = _write_json(ir_path, ir)
        receipt_path = root / "receipt.json"
        _write_json(
            receipt_path,
            plan_receipts.signed_receipt(ir_bytes) if receipt is None else receipt,
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(plan_author_cli, "_reviewer_key", return_value=plan_receipts.KEY),
            contextlib.redirect_stdout(stdout),
        ):
            code = plan_author_cli.main([
                "--from-plan-contract", str(ir_path),
                "--receipt", str(receipt_path),
                "--out", str(out),
                "--repo", str(root / "repo"),
            ])
        return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr="")

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


    def test_cli_refuses_a_tests_claim_with_no_observation_seam(self) -> None:
        """The ship verb is where an unobservable obligation is caught.

        FDAdb run d246ae95 spent its last three rounds on an obligation no case
        could observe from the public contract. The claim is projected onto a
        gating acceptance criterion, and the objective compiler the CLI runs
        refuses a gating criterion that names no seam.
        """
        ir = _ir()
        seams = [
            claim.pop("observation_seam")
            for claim in ir["claims"]
            if claim["claim_id"] == "claim-t"
        ]
        self.assertEqual(1, len(seams))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out, ir)
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["outcome"], "AuthoringError")
            self.assertIn("OBLIGATION_UNOBSERVABLE", payload["detail"])
            self.assertFalse(out.exists())

    def test_cli_refuses_a_tests_claim_with_no_decided_by(self) -> None:
        """Ship and amendment ingress refuse an obligation with no worked examples.

        `run attend` and a hand-authored `run amend` revision both pass through
        this projection and the objective compiler, so an amendment that leaves a
        gating obligation without examples is refused here too.
        """
        ir = _ir()
        for claim in ir["claims"]:
            if claim["claim_id"] == "claim-t":
                claim.pop("decided_by")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out, ir)
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertIn("OBLIGATION_UNDECIDED", payload["detail"])
            self.assertIn("claim-t", payload["detail"])
            self.assertFalse(out.exists())

    def test_cli_requires_a_refusal_example_for_a_restricted_claim(self) -> None:
        refusal = {"input": {"module": "missing"}, "refuses": {"error": "ImportError"}}
        for field, value in (
            ("polarity", "negative"),
            ("preconditions", ["the module is on sys.path"]),
            ("exception_ids", ["claim-b1"]),
            ("witness", {"scope": "in_process", "store": "external"}),
        ):
            with self.subTest(field=field):
                ir = _ir()
                claim = next(c for c in ir["claims"] if c["claim_id"] == "claim-t")
                claim[field] = value
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "repo").mkdir()
                    result = self._author(root, root / "plan", ir)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("no refuses example", json.loads(result.stdout)["detail"])
                claim["decided_by"] = claim["decided_by"] + [refusal]
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "repo").mkdir()
                    out = root / "plan"
                    result = self._author(root, out, ir)
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(
                        "PLAN_AUTHORED", json.loads(result.stdout)["outcome"])
                    self.assertTrue(out.exists())

    def test_cli_refuses_a_receipt_not_signed_with_findings(self) -> None:
        """Every workflow ends with planctl review --findings, then this ship."""
        ir_bytes = json.dumps(_ir(), indent=2, sort_keys=True).encode("utf-8")
        unsigned_pass = plan_receipts.signed_receipt(ir_bytes)
        unsigned_pass.pop("findings_sha256")
        unsigned_pass["signature"] = plan_receipts.plan_approval.signature(
            unsigned_pass, plan_receipts.KEY)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out, receipt=unsigned_pass)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("RECEIPT_WITHOUT_FINDINGS", result.stdout + result.stderr)
            self.assertFalse(out.exists())

    def test_cli_refuses_a_forged_receipt_and_writes_no_plan(self) -> None:
        ir_bytes = json.dumps(_ir(), indent=2, sort_keys=True).encode("utf-8")
        forged = {
            "schema_version": "plan-contract-review.v1",
            "verdict": "PASS",
            "ir_sha256": hashlib.sha256(ir_bytes).hexdigest(),
            "findings_sha256": "0" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out, receipt=forged)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("RECEIPT_SIGNATURE", result.stdout)
            self.assertFalse(out.exists())

    def test_cli_writes_an_approval_record_start_can_verify(self) -> None:
        from adw_modules import plan_approval
        from adw_modules.plan_compiler import compile_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out)
            self.assertEqual(result.returncode, 0, result.stdout)
            stored = out.read_bytes()
            plan_approval.verify_approval(
                json.loads(stored)["approval"],
                compile_plan(stored).plan_digest,
                plan_receipts.KEY,
            )

    def test_cli_projects_the_examples_verbatim_into_the_public_contract(self) -> None:
        from adw_modules.plan_compiler import compile_plan

        examples = [{"input": {"module": "src.b"}, "expect": {"importable": True}}]
        rendered = json.dumps(
            examples, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        ir = _ir()
        claim_b1 = next(c for c in ir["claims"] if c["claim_id"] == "claim-b1")
        claim_b1["decided_by"] = examples
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out, ir)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            stored = json.loads(out.read_bytes())
            lanes = {lane.lane_id: lane for lane in compile_plan(out.read_bytes()).lanes}
        tests_lane = next(lane for lane in stored["lanes"] if lane["id"] == "lane-t")
        self.assertEqual(
            [examples],
            [c.get("decided_by") for c in tests_lane["spec"]["obligations"]["claims"]],
        )
        for lane_id in ("lane-t", "lane-b"):
            self.assertTrue(
                any("[decided by: {0}]".format(rendered) in item
                    for item in lanes[lane_id].public_acceptance),
                lanes[lane_id].public_acceptance,
            )

    def test_cli_carries_the_declared_seam_onto_the_acceptance(self) -> None:
        from adw_modules.plan_compiler import compile_plan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            out = root / "plan"
            result = self._author(root, out)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            lanes = {
                lane.lane_id: lane for lane in compile_plan(out.read_bytes()).lanes
            }
            self.assertTrue(
                any(
                    "[observable: src/b.py is imported by its public module path"
                    in item
                    for item in lanes["lane-t"].public_acceptance
                ),
                lanes["lane-t"].public_acceptance,
            )
            self.assertFalse(
                any("[observable:" in item
                    for item in lanes["lane-b"].public_acceptance),
                lanes["lane-b"].public_acceptance,
            )


if __name__ == "__main__":
    unittest.main()
