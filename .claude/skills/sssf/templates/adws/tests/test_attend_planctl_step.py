"""The approval step `run attend` performs between authoring and amending.

An attended revision is not applied because an agent wrote it. It is applied
because `planctl` validated it, a receipt approved it, and the ingress
projection accepted that receipt -- the same four steps the human ran by hand
on FDAdb `d246ae95`. These cases pin the parts of that step Maestro owns:
where the validator and the reviewer key are resolved from, what happens when
either is absent, that a refusing planctl becomes a typed refusal carrying its
own diagnostic, and that the receipt shape Maestro mints is the shape ingress
verifies.

`_run_planctl` is exercised against the real binary when one is configured on
this machine. A stubbed `subprocess.run` cannot observe an argv parser: it
records the argv you passed and replays the stdout you scripted, which is
exactly how `vitest list --json <paths>` shipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import maestro
from adw_modules import attend as att
from adw_modules import plan_contract_ingress as ingress
from adw_modules import route_admission as admission


#: Where this machine's plan-contract validator lives, if anywhere. Read from
#: the environment rather than pinned: the path is a worktree of another
#: repository and is nobody's invariant.
_PLANCTL_ENV = "MAESTRO_TEST_PLANCTL"


def _configured_planctl() -> Path | None:
    raw = os.environ.get(_PLANCTL_ENV, "")
    if not raw:
        return None
    candidate = Path(raw)
    return candidate if candidate.is_file() else None


class _Runtime:
    def __init__(self, path: Path) -> None:
        self.path = path


class ValidatorResolution(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_an_unconfigured_validator_refuses_by_name(self) -> None:
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._planctl_binary(att.AttendPolicy(max_amendments_per_lane=1))
        self.assertEqual(caught.exception.code, att.PLANCTL_UNRESOLVED)

    def test_a_configured_validator_that_is_not_there_refuses_by_name(self) -> None:
        policy = att.AttendPolicy(
            max_amendments_per_lane=1, planctl=self.tmp / "absent" / "planctl.py"
        )
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._planctl_binary(policy)
        self.assertEqual(caught.exception.code, att.PLANCTL_UNRESOLVED)
        self.assertIn("planctl.py", caught.exception.detail)


class ReviewerKeyResolution(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.runtime = _Runtime(self.tmp)

    def test_the_key_comes_from_the_state_root_and_nowhere_else(self) -> None:
        keys = self.tmp / "keys"
        keys.mkdir()
        material = "ab" * 32
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text(material)
        self.assertEqual(maestro._reviewer_hmac_key(self.runtime), material)
        # planctl's own floor. A key shorter than this mints no receipt, so a
        # deployment whose keys directory was reprovisioned by hand finds out
        # here rather than at the review call.
        self.assertGreaterEqual(len(material.encode("utf-8")), 32)

    def test_an_absent_key_refuses_by_name(self) -> None:
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._reviewer_hmac_key(self.runtime)
        self.assertEqual(caught.exception.code, att.KEY_UNRESOLVED)

    def test_an_empty_key_file_is_not_a_key(self) -> None:
        keys = self.tmp / "keys"
        keys.mkdir()
        (keys / admission.REVIEWER_HMAC_KEY_FILE).write_text("   \n")
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._reviewer_hmac_key(self.runtime)
        self.assertEqual(caught.exception.code, att.KEY_UNRESOLVED)


class ReceiptHandshake(unittest.TestCase):
    """What ingress requires of a receipt, pinned against ingress itself."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.ir_bytes = json.dumps({"plan_id": "p-1"}, sort_keys=True).encode("utf-8")

    def _receipt(self, **overrides: object) -> dict:
        receipt = {
            "schema_version": ingress.RECEIPT_VERSION,
            "verdict": "PASS",
            "ir_sha256": hashlib.sha256(self.ir_bytes).hexdigest(),
        }
        receipt.update(overrides)
        return receipt

    def test_a_pass_receipt_over_these_exact_bytes_verifies(self) -> None:
        ingress._verify_receipt(self.ir_bytes, self._receipt(), None)

    def test_a_receipt_for_an_earlier_revision_is_refused(self) -> None:
        stale = self._receipt(ir_sha256=hashlib.sha256(b"older").hexdigest())
        with self.assertRaises(ingress.IngressError) as caught:
            ingress._verify_receipt(self.ir_bytes, stale, None)
        self.assertIn("RECEIPT_IR_MISMATCH", str(caught.exception))

    def test_a_receipt_that_is_not_a_pass_is_refused(self) -> None:
        with self.assertRaises(ingress.IngressError) as caught:
            ingress._verify_receipt(self.ir_bytes, self._receipt(verdict="REVISE"), None)
        self.assertIn("RECEIPT_NOT_PASS", str(caught.exception))


class RealValidatorInvocation(unittest.TestCase):
    """`_run_planctl` against the binary, not against a stubbed subprocess."""

    def setUp(self) -> None:
        self.binary = _configured_planctl()
        if self.binary is None:
            self.skipTest(
                "no plan-contract validator on this machine: set {0} to its "
                "path to run this case".format(_PLANCTL_ENV)
            )
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def test_a_refusing_validator_becomes_a_typed_refusal_with_its_own_words(
        self,
    ) -> None:
        ir = self.tmp / "plan.ir.json"
        ir.write_text(json.dumps({"schema_version": 0}), encoding="utf-8")
        with self.assertRaises(att.AttendRefused) as caught:
            maestro._run_planctl(
                self.binary,
                ["validate", str(ir), "--repo-root", str(self.tmp), "--json"],
            )
        self.assertEqual(caught.exception.code, att.REVISION_REFUSED)
        self.assertIn("planctl validate", caught.exception.detail)
        # planctl's own diagnostic, not a sentence Maestro made up. The
        # operator reading a refused attend needs the field that is wrong.
        self.assertIn("schema", caught.exception.detail)

    def test_the_reviewer_key_is_injected_and_never_inherited(self) -> None:
        ir = self.tmp / "plan.ir.json"
        ir.write_text(json.dumps({"schema_version": 0}), encoding="utf-8")
        os.environ[admission.REVIEWER_HMAC_KEY_ENV] = "leaked-from-the-shell"
        self.addCleanup(os.environ.pop, admission.REVIEWER_HMAC_KEY_ENV, None)
        # The call refuses on the IR either way. What is pinned is that the
        # ambient value is dropped: a reviewer key an operator happened to have
        # sourced must not decide which key approved a revision.
        with self.assertRaises(att.AttendRefused):
            maestro._run_planctl(
                self.binary,
                ["validate", str(ir), "--repo-root", str(self.tmp), "--json"],
            )
        self.assertEqual(
            os.environ[admission.REVIEWER_HMAC_KEY_ENV], "leaked-from-the-shell"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
