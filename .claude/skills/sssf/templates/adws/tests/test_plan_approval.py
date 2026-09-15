"""A new run binds only a plan bound to authenticated approval evidence.

Cross-vendor review of #275 forged a four-field receipt (`verdict: PASS`, the
current IR digest, a well-shaped `findings_sha256`) and `plan_author_cli.py`
wrote an executable plan from it; `run start` accepted any compiler-valid plan
at all. Receipts are now authenticated with planctl's own HMAC format and the
deployment's reviewer key, the projection signs a binding from plan digest to
receipt, and `run start` verifies it. `run amend` still accepts a scripted edit,
and a run already bound is never re-checked.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import maestro
from adw_modules import plan_approval
from adw_modules import plan_author
from adw_modules import plan_compiler
from tests import plan_receipts
from tests.test_run_status import _plan_bytes

#: Computed with the-library planctl.py `receipt_signature` / `reviewer_key_id`
#: (feat/claim-decided-by) over this record and key. A second, drifting HMAC
#: scheme would fail here even where planctl is not checked out.
_VECTOR_KEY = ("ab" * 32).encode("utf-8")
_VECTOR_RECORD = {
    "schema_version": "plan-contract-review.v1",
    "verdict": "PASS",
    "ir_sha256": "0" * 64,
    "note": "é ✓",
}
_VECTOR_SIGNATURE = "90093cbb89d9760f1c884f449eb5d83bb8da9835455aabed831e34222398df26"
_VECTOR_KEY_ID = "271a413bd339c5709fdceaec41f14f11e9fbfb5042d72d331c65f32b284cd09a"


def _planctl_path() -> Path | None:
    raw = os.environ.get("MAESTRO_TEST_PLANCTL", "")
    if raw and Path(raw).is_file():
        return Path(raw)
    beside = (
        Path(__file__).resolve().parents[6].parent
        / "the-library" / "skills" / "plan-contract" / "scripts" / "planctl.py"
    )
    return beside if beside.is_file() else None


class SameSignatureAsPlanctl(unittest.TestCase):
    def test_the_pinned_vector(self) -> None:
        self.assertEqual(_VECTOR_SIGNATURE, plan_approval.signature(_VECTOR_RECORD, _VECTOR_KEY))
        self.assertEqual(_VECTOR_KEY_ID, plan_approval.key_id(_VECTOR_KEY))

    def test_against_the_real_planctl_when_it_is_here(self) -> None:
        path = _planctl_path()
        if path is None:
            self.skipTest("planctl.py is not checked out beside this repository")
        spec = importlib.util.spec_from_file_location("planctl_parity", path)
        assert spec is not None and spec.loader is not None
        planctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planctl)
        receipt = plan_receipts.signed_receipt(b'{"plan_id": "p"}')
        self.assertEqual(
            planctl.receipt_signature(receipt, plan_receipts.KEY), receipt["signature"]
        )
        self.assertEqual(planctl.reviewer_key_id(plan_receipts.KEY), receipt["reviewer_key_id"])


class RequireApprovedPlan(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.runtime = SimpleNamespace(path=self.root / "state")
        plan_receipts.install_key(self.runtime.path)
        self.plan = self.root / "plan.json"

    def _check(self, stored: bytes) -> None:
        self.plan.write_bytes(stored)
        compiled = plan_compiler.compile_plan(stored)
        maestro._require_approved_plan(self.plan, compiled, self.runtime)

    def _refusal(self, stored: bytes) -> str:
        with self.assertRaises(maestro._RunRefused) as caught:
            self._check(stored)
        return caught.exception.outcome

    def test_a_bare_compiler_valid_plan_is_refused(self) -> None:
        self.assertEqual("PLAN_UNAPPROVED", self._refusal(plan_author.author_plan(json.loads(_plan_bytes()))))

    def test_an_approved_projection_is_accepted(self) -> None:
        self._check(plan_receipts.approve_plan_bytes(_plan_bytes()))

    def test_the_approval_does_not_move_the_plan_digest(self) -> None:
        bare = plan_compiler.compile_plan(_plan_bytes()).plan_digest
        approved = plan_compiler.compile_plan(plan_receipts.approve_plan_bytes(_plan_bytes()))
        self.assertEqual(bare, approved.plan_digest)

    def test_a_plan_edited_after_projection_is_refused(self) -> None:
        document = json.loads(plan_receipts.approve_plan_bytes(_plan_bytes()))
        document["lanes"][0]["acceptance"] = ["a.txt is written, and nothing else"]
        self.assertEqual(
            "PLAN_APPROVAL_DIGEST", self._refusal(plan_author.author_plan(document))
        )

    def test_approval_signed_with_another_key_is_refused(self) -> None:
        other = b"c" * 64
        stored = plan_receipts.approve_plan_bytes(
            _plan_bytes(), key=other,
            receipt=plan_receipts.signed_receipt(b"{}", key=other),
        )
        self.assertEqual("PLAN_APPROVAL_KEY_ID", self._refusal(stored))

    def test_a_forged_approval_record_is_refused(self) -> None:
        document = json.loads(plan_receipts.approve_plan_bytes(_plan_bytes()))
        document["approval"]["receipt"]["findings_sha256"] = "9" * 64
        self.assertEqual("PLAN_APPROVAL_SIGNATURE", self._refusal(plan_author.author_plan(document)))

    def test_a_deployment_without_a_reviewer_key_refuses(self) -> None:
        (self.runtime.path / "keys" / maestro.admission.REVIEWER_HMAC_KEY_FILE).unlink()
        self.assertEqual(
            "PLAN_UNAPPROVED", self._refusal(plan_receipts.approve_plan_bytes(_plan_bytes()))
        )


class StartAndAmendWiring(unittest.TestCase):
    """`run start` refuses before a run exists; `run amend` does not ask."""

    def _start(self, plan: Path, runtime: SimpleNamespace, create: mock.Mock) -> None:
        args = argparse.Namespace(plan=str(plan), repo="/product", main_ref="refs/heads/main", run_id="r1")
        with (
            mock.patch.object(maestro, "_executing_maestro_file", return_value=Path("/d/maestro.py")),
            mock.patch.object(maestro, "_load_deployment_config", return_value={}),
            mock.patch.object(maestro, "require_deployment"),
            mock.patch.object(maestro, "_open_runtime", return_value=runtime),
            mock.patch.object(maestro.gitpub, "bind_target_worktree", return_value=mock.Mock()),
            mock.patch.object(maestro, "_open_store", return_value=mock.Mock()),
            mock.patch.object(maestro, "create_factory_run", create),
        ):
            maestro._run_start(args)

    def test_start_refuses_a_bare_plan_before_creating_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = mock.Mock(path=root / "state")
            plan_receipts.install_key(runtime.path)
            plan = root / "plan.json"
            plan.write_bytes(_plan_bytes())
            create = mock.Mock(side_effect=AssertionError("a run was created"))
            with self.assertRaises(maestro._RunRefused) as caught:
                self._start(plan, runtime, create)
            self.assertEqual("PLAN_UNAPPROVED", caught.exception.outcome)
            create.assert_not_called()

    def test_start_binds_an_approved_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = mock.Mock(path=root / "state")
            plan_receipts.install_key(runtime.path)
            plan = root / "plan.json"
            plan.write_bytes(plan_receipts.approve_plan_bytes(_plan_bytes()))
            create = mock.Mock(side_effect=RuntimeError("created"))
            with self.assertRaisesRegex(RuntimeError, "created"):
                self._start(plan, runtime, create)
            create.assert_called_once()

    def test_a_scripted_amendment_is_not_asked_for_approval(self) -> None:
        compiled = plan_compiler.compile_plan(_plan_bytes())
        applied = mock.Mock(side_effect=RuntimeError("applied"))
        store = mock.Mock()
        runtime = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "bare.json"
            plan.write_bytes(_plan_bytes())
            with (
                mock.patch.object(
                    maestro, "_bind_existing_run",
                    return_value=({}, runtime, store, {"plan_revision": 1}, mock.Mock(), compiled),
                ),
                mock.patch.object(
                    maestro, "_require_approved_plan",
                    side_effect=AssertionError("amend asked for approval"),
                ),
                mock.patch.object(maestro, "apply_factory_amendment", applied),
            ):
                with self.assertRaisesRegex(RuntimeError, "applied"):
                    maestro._run_amend(argparse.Namespace(run_id="r1", plan=str(plan)))
        applied.assert_called_once()


if __name__ == "__main__":
    unittest.main()
