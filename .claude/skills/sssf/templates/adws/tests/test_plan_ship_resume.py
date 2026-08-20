"""`maestro plan ship` is one command, which means it is re-runnable.

Recorded failure, 2026-08-18. `plan ship cmo-consolidation-l` authored a plan,
validated it, and failed at finalize. Re-running the same command refused at its
first step with `PLAN_AUTHORING_FAILED: PLAN_EXISTS:...` — against the file its
own author step had written moments earlier. The operator's one command could
only be resumed by deleting that file by hand, which is the manual file work the
verb exists to remove.

`plan author` stays create-once. A plan's bytes are its identity, a run and a
receipt both key on the digest computed from them, and silently rewriting them
would move a plan out from under whatever already refers to it. What was missing
is that `plan ship` never asked which situation it was in. Projection is a pure
function of the IR, the receipt, and the repository, so the ship can compute what
the author step *would* write before anything is written, and then decide:

* nothing on disk — author runs;
* identical bytes — the author step already produced exactly this plan, so it is
  skipped and the ship resumes at validate;
* different bytes — the IR moved; the old plan is kept under its own digest and
  the new one takes its place, unless a receipt approves the old bytes.

The approval guard is the one that must not be got wrong, so it fails closed:
only a receipt that is *absent* proves a plan unapproved. A receipt that cannot
be read, or a store that cannot be located, proves nothing — and the answer
decides whether a plan's bytes get replaced.

That guard bites harder than it did when it was written, and the change is
worth stating where the tests for it live. While `plan finalize` dispatched a
reviewer, a finalized plan could carry a FAIL receipt and stay replaceable.
Finalize is deterministic now: an eligible plan gets PASS and an ineligible one
gets no receipt at all, so *every* finalized plan is approved and any re-ship
whose IR moved is refused. The route back is to remove the plan directory once
no live run holds its bytes — which is what `maestro deliver` does on a re-ship
— and the refusal says so. `plan set-aside` is not that route: it reopens a
FAIL and refuses a PASS.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import maestro                                   # noqa: E402
from adw_modules import plan_digest              # noqa: E402


NEVER_APPROVED = lambda _digest: False           # noqa: E731
ALWAYS_APPROVED = lambda _digest: True           # noqa: E731


class ShipAuthoringDecision(unittest.TestCase):
    """`maestro._plan_ship_authoring`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan = Path(self._tmp.name) / "plan" / "maestro-plan.v1"
        self.plan.parent.mkdir(parents=True)

    def test_a_first_ship_leaves_the_author_step_to_write(self):
        self.assertIsNone(maestro._plan_ship_authoring(
            self.plan, b"projected", NEVER_APPROVED))
        self.assertFalse(self.plan.exists())

    def test_a_re_ship_of_the_same_plan_keeps_the_authored_bytes(self):
        # The recorded failure: this used to refuse with PLAN_EXISTS.
        self.plan.write_bytes(b"projected")
        self.assertIsNone(maestro._plan_ship_authoring(
            self.plan, b"projected", NEVER_APPROVED))
        self.assertTrue(self.plan.exists())
        self.assertEqual(self.plan.read_bytes(), b"projected")

    def test_a_changed_ir_supersedes_the_old_plan_and_keeps_its_bytes(self):
        self.plan.write_bytes(b"old plan")
        old_digest = plan_digest.digest_of(b"old plan")
        superseded = maestro._plan_ship_authoring(
            self.plan, b"new plan", NEVER_APPROVED)
        self.assertEqual(superseded, old_digest)
        # Room was made for the author step...
        self.assertFalse(self.plan.exists())
        # ...and nothing was destroyed to make it.
        archived = maestro._superseded_plan_path(self.plan, old_digest)
        self.assertEqual(archived.read_bytes(), b"old plan")

    def test_an_approved_plan_is_never_replaced(self):
        self.plan.write_bytes(b"old plan")
        with self.assertRaises(maestro._ShipSupersedeRefused):
            maestro._plan_ship_authoring(
                self.plan, b"new plan", ALWAYS_APPROVED)
        self.assertEqual(self.plan.read_bytes(), b"old plan")

    def test_the_refusal_names_a_route_back_that_exists(self):
        """An operator meets this refusal on every re-ship of a finalized plan
        now, so what it tells them to do has to be a thing they can do.

        It used to say "finalize it or remove its approval", and neither is
        available: the plan is already finalized, and `plan set-aside` refuses
        a PASS receipt outright — it exists to reopen a FAIL. Removing the
        plan's own bytes is the route, and it is the one `maestro deliver`
        takes.
        """
        self.plan.write_bytes(b"old plan")
        with self.assertRaises(maestro._ShipSupersedeRefused) as refused:
            maestro._plan_ship_authoring(
                self.plan, b"new plan", ALWAYS_APPROVED)
        detail = str(refused.exception)
        self.assertIn(plan_digest.digest_of(b"old plan"), detail)
        self.assertIn("removing this plan's directory", detail)
        self.assertIn("maestro deliver", detail)
        self.assertIn("refuses a PASS", detail)

    def test_approval_is_only_consulted_when_the_bytes_actually_differ(self):
        """A re-ship of an approved, unchanged plan still resumes.

        Otherwise the guard would make the successful case unrepeatable: ship a
        plan, have it approved, run ship again, and be refused for having
        succeeded.
        """
        self.plan.write_bytes(b"projected")
        asked = []

        def approved(digest):
            asked.append(digest)
            return True

        self.assertIsNone(maestro._plan_ship_authoring(
            self.plan, b"projected", approved))
        self.assertEqual(asked, [])


class ApprovalFailsClosed(unittest.TestCase):
    """`maestro._plan_ship_approved` under a store it cannot read."""

    def _approved_with(self, loader):
        class _Store:
            def load(self, digest):
                return loader(digest)

        original_store = maestro._plan_receipt_store
        original_step = maestro._configured_plan_step
        maestro._plan_receipt_store = lambda *a, **k: _Store()
        # The step resolver reads installed repository config, which no test
        # has; standing it in keeps this case about the receipt, which is what
        # the predicate is deciding on.
        maestro._configured_plan_step = lambda *a, **k: None
        self.addCleanup(setattr, maestro, "_plan_receipt_store", original_store)
        self.addCleanup(
            setattr, maestro, "_configured_plan_step", original_step)
        args = type("Args", (), {"plan_name": "any"})()
        return maestro._plan_ship_approved(args)

    def test_a_missing_receipt_is_the_only_proof_of_no_approval(self):
        def missing(_digest):
            raise FileNotFoundError("no receipt")
        self.assertFalse(self._approved_with(missing)("d" * 64))

    def test_an_unreadable_receipt_counts_as_approved(self):
        from adw_modules import finalization as fin

        for failure in (fin.ReceiptInvalid("corrupt"),
                        fin.SignatureMissing("unsigned"),
                        fin.SignatureInvalid("forged"),
                        ValueError("garbage"),
                        OSError("unreadable")):
            def raises(_digest, exc=failure):
                raise exc
            self.assertTrue(
                self._approved_with(raises)("d" * 64),
                "{} must not read as unapproved".format(type(failure).__name__))

    def test_an_unconfigured_receipt_store_counts_as_approved(self):
        original = maestro._plan_receipt_store

        def unconfigured(*_args, **_kwargs):
            raise maestro._PlanReceiptConfigurationError("no receipt dir")

        maestro._plan_receipt_store = unconfigured
        self.addCleanup(setattr, maestro, "_plan_receipt_store", original)
        args = type("Args", (), {"plan_name": "any"})()
        self.assertTrue(maestro._plan_ship_approved(args)("d" * 64))

    def test_a_pass_receipt_is_approved_and_a_fail_receipt_is_not(self):
        from adw_modules import finalization as fin

        for verdict, expected in ((fin.Verdict.PASS, True),
                                  (fin.Verdict.FAIL, False)):
            def load(_digest, verdict=verdict):
                return type("Receipt", (), {"verdict": verdict})()
            self.assertIs(self._approved_with(load)("d" * 64), expected)


if __name__ == "__main__":
    unittest.main()
