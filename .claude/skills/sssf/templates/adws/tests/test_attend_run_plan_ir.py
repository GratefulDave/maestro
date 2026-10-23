"""Attend amends the IR a run's own plan was projected from, never config's.

A run started from a WP4 plan had its attend root supplied from the WP1 IR
`attend.plan_ir` still named, because `_attend_plan_ir` took that path on
trust. These cases drive the attend preflight for two runs with two plans
over one deployment config and pin the replacement: the projection records
its IR (path and sha256) in the signed approval; preflight supplies the root
from it; a configured path to other bytes refuses `ATTEND_PLAN_IR_MISMATCH`;
and a root an earlier release laid out from the wrong IR is re-laid on the
next attend.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import maestro
from adw_modules import attend as att
from adw_modules import run_preflight as rp
from tests import plan_receipts
from tests.test_plan_approval import _shippable_ir_bytes
from tests.test_run_preflight import _Bed

RUN_WP1 = "a" * 32
RUN_WP4 = "b" * 32


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ir(repo: Path, name: str) -> Path:
    """An IR under the run repository citing one source by sha256."""
    source = "{0} spec\n".format(name).encode()
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / (name + ".md")).write_bytes(source)
    path = repo / ".maestro" / (name + ".plan.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "plan_id": name,
                "source_artifacts": [
                    {
                        "source_id": name,
                        "path": "docs/{0}.md".format(name),
                        "sha256": _sha(source),
                        "required": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _plan(ir: Path, repo: Path, *, record_path: bool = True) -> bytes:
    """A pinned plan whose approval names `ir` the way the projection does."""
    approval: dict = {"receipt": {"ir_sha256": _sha(ir.read_bytes())}}
    if record_path:
        approval["source_ir"] = {
            "path": str(ir.relative_to(repo)),
            "sha256": _sha(ir.read_bytes()),
        }
    return json.dumps({"schema_version": "x", "lanes": [], "approval": approval}).encode()


class TwoRunsTwoPlans(unittest.TestCase):
    def setUp(self) -> None:
        self.bed = _Bed(self, attend=True)
        self.runtime = SimpleNamespace(path=self.bed.state)
        self.base = self.bed.root / "planroots"
        self.repo = self.bed.product
        self.wp1 = _ir(self.repo, "wp1")
        self.wp4 = _ir(self.repo, "wp4")
        patcher = mock.patch.object(
            maestro,
            "plan_artifact_ref_for",
            side_effect=lambda store, run_id, revision: str(
                maestro._pinned_plan_artifact(self.runtime, run_id, revision)
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pin(self, run_id: str, plan: bytes, revision: int = 1) -> None:
        pinned = maestro._pinned_plan_artifact(self.runtime, run_id, revision)
        pinned.parent.mkdir(parents=True, exist_ok=True)
        pinned.write_bytes(plan)

    def _attend_preflight(self, run_id: str, configured: Path | None) -> rp.PreflightItem:
        policy = att.AttendPolicy(max_amendments_per_lane=1, plan_ir=configured)
        preflight = self.bed.preflight(
            plan_ir=maestro._attend_owned_ir(self.base, run_id, 1),
            plan_ir_current=lambda: maestro._attend_root_current(
                self.runtime, self.base, run_id, 1, policy
            ),
            plan_ir_supply=lambda: maestro._attend_supply_root(
                policy, self.runtime, self.base, object(), self.repo, run_id, 1
            ),
        )
        preflight.run_wide()
        return {i.item_id: i for i in preflight.items}["attend:plan-ir"]

    def _owned(self, run_id: str) -> bytes:
        return maestro._attend_owned_ir(self.base, run_id, 1).read_bytes()

    def test_each_run_is_supplied_its_own_plans_ir(self) -> None:
        self._pin(RUN_WP1, _plan(self.wp1, self.repo))
        self._pin(RUN_WP4, _plan(self.wp4, self.repo))
        for run_id, ir in ((RUN_WP1, self.wp1), (RUN_WP4, self.wp4)):
            item = self._attend_preflight(run_id, None)
            self.assertEqual(item.status, rp.ADDED, item.detail)
            self.assertEqual(self._owned(run_id), ir.read_bytes())
        root = maestro._attend_root(self.base, RUN_WP4, 1)
        self.assertTrue((root / "docs" / "wp4.md").is_file())
        self.assertFalse((root / "docs" / "wp1.md").exists())

    def test_a_configured_ir_of_the_same_bytes_is_not_a_conflict(self) -> None:
        self._pin(RUN_WP4, _plan(self.wp4, self.repo))
        item = self._attend_preflight(RUN_WP4, self.wp4)
        self.assertEqual(item.status, rp.ADDED, item.detail)
        self.assertEqual(self._owned(RUN_WP4), self.wp4.read_bytes())

    def test_a_configured_ir_of_another_plan_refuses_at_preflight(self) -> None:
        self._pin(RUN_WP4, _plan(self.wp4, self.repo))
        item = self._attend_preflight(RUN_WP4, self.wp1)
        self.assertEqual(item.status, rp.REFUSED)
        self.assertIn(att.PLAN_IR_MISMATCH, item.detail)
        self.assertFalse(maestro._attend_owned_ir(self.base, RUN_WP4, 1).exists())

    def test_a_configured_ir_never_substitutes_for_an_unrecorded_path(self) -> None:
        """A plan projected before the path was recorded still names its sha256."""
        self._pin(RUN_WP4, _plan(self.wp4, self.repo, record_path=False))
        item = self._attend_preflight(RUN_WP4, self.wp1)
        self.assertEqual(item.status, rp.REFUSED)
        self.assertIn(att.PLAN_IR_MISMATCH, item.detail)
        item = self._attend_preflight(RUN_WP4, None)
        self.assertEqual(item.status, rp.REFUSED)
        self.assertIn(_sha(self.wp4.read_bytes()), item.detail)
        item = self._attend_preflight(RUN_WP4, self.wp4)
        self.assertEqual(item.status, rp.ADDED, item.detail)

    def test_a_root_laid_from_the_wrong_ir_is_relaid_on_the_next_attend(self) -> None:
        """The a70e25b9 shape: r1 was supplied from WP1's IR for a WP4 plan."""
        self._pin(RUN_WP4, _plan(self.wp4, self.repo))
        legacy = att.AttendPolicy(max_amendments_per_lane=1)
        with mock.patch.object(maestro, "_plan_source_ir", return_value=(None, None)):
            maestro._attend_supply_root(
                att.AttendPolicy(max_amendments_per_lane=1, plan_ir=self.wp1),
                self.runtime, self.base, object(), self.repo, RUN_WP4, 1,
            )
        self.assertEqual(self._owned(RUN_WP4), self.wp1.read_bytes())
        current, detail = maestro._attend_root_current(
            self.runtime, self.base, RUN_WP4, 1, legacy
        )
        self.assertFalse(current)
        self.assertIn(_sha(self.wp4.read_bytes()), detail)
        item = self._attend_preflight(RUN_WP4, None)
        self.assertEqual(item.status, rp.ADDED, item.detail)
        self.assertEqual(self._owned(RUN_WP4), self.wp4.read_bytes())
        root = maestro._attend_root(self.base, RUN_WP4, 1)
        self.assertFalse((root / "docs" / "wp1.md").exists())


class TheProjectionRecordsItsIr(unittest.TestCase):
    def test_the_signed_approval_names_the_ir_path_and_sha256(self) -> None:
        from adw_modules import plan_approval
        from adw_modules import plan_contract_ingress as ingress

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".maestro").mkdir(parents=True)
            ir_path = repo / ".maestro" / "wp4.plan.json"
            shippable = _shippable_ir_bytes()
            ir_path.write_bytes(shippable)
            receipt = repo / ".maestro" / "wp4.plan-review.json"
            receipt.write_text(
                json.dumps(plan_receipts.signed_receipt(shippable)), encoding="utf-8"
            )
            out = repo / ".maestro" / "wp4.maestro-plan.json"
            ingress.author_from_plan_contract(
                ir_path, receipt, out, repo, reviewer_key=plan_receipts.KEY
            )
            approval = json.loads(out.read_bytes())["approval"]
            self.assertEqual(
                approval["source_ir"],
                {"path": ".maestro/wp4.plan.json", "sha256": _sha(shippable)},
            )
            self.assertEqual(
                approval["signature"], plan_approval.signature(approval, plan_receipts.KEY)
            )
            self.assertEqual(
                maestro._plan_source_ir(out), (_sha(shippable), ".maestro/wp4.plan.json")
            )


if __name__ == "__main__":
    unittest.main()
