"""`adw_modules/enforcement.py`'s detectors are executed, against this tree.

The module has held nine obligations and a bank of planted-violation fixtures
since §13.4, and the artifact-factory cutover (`e7b477e`) deliberately rewrote
its ledger for the frozen operator surface -- dropping the workspace and
coordinator obligations, narrowing `REQUIRED_VERBS` to the run verbs -- so it
describes today's invariants, not yesterday's. What that cutover also did was
delete `tests/test_step9_enforcement.py`, the only file that ran it. From
2026-08-29 to 2026-09-15 nothing imported `enforcement` at all: nine declared
safeguards, zero executions.

`base-execution-import` is what that cost. Its detector read only
`ImportFrom.module`, so `from adw_modules import agents` -- the spelling the
whole runtime uses -- was invisible to it, and it returned **zero findings
against its own planted violation**. A detector that cannot see the violation
someone wrote down for it detects nothing, and there was no reader to notice.
`digest-import-boundary` was blind the same way to
`from adw_modules import plan_model`.

So this file asserts the two-sided verdict the `Obligation` record already
declares, per obligation:

* the **planted violation** fixture is detected -- the detector can fail; and
* the **real tree** is clean -- the invariant actually holds right now.

Both halves matter. Only the first is a self-test of the detector; only the
second is a check on the runtime; a detector that passes the first and is never
pointed at the second is exactly the shape this module was in.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import maestro
from adw_modules import enforcement as en

RUNTIME_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = RUNTIME_ROOT / "tests" / "fixtures" / "enforcement"
MODULES = RUNTIME_ROOT / "adw_modules"

_SOURCE_OBLIGATIONS = tuple(
    ob for ob in en.OBLIGATION_LEDGER if ob.detector == "detect_source"
)


class LedgerIsTotal(unittest.TestCase):
    """Every obligation in the ledger is discharged by a case below."""

    def test_every_obligation_names_a_detector_this_file_runs(self) -> None:
        detectors = {ob.detector for ob in en.OBLIGATION_LEDGER}
        self.assertEqual(
            {"detect_source", "assert_installed_bytes", "assert_verbs"},
            detectors,
            "an obligation names a detector no case here executes; add the case "
            "rather than widening this set",
        )

    def test_every_source_obligation_names_a_fixture_that_exists(self) -> None:
        for ob in _SOURCE_OBLIGATIONS:
            with self.subTest(ob.check_id):
                name, _, marker = ob.planted_violation.partition(":")
                self.assertTrue(
                    (FIXTURES / name).is_file(),
                    "{}: planted violation {} is gone".format(ob.check_id, name),
                )
                self.assertTrue(marker, "planted_violation must name a marker")


class DetectorsFire(unittest.TestCase):
    """Each detector convicts its planted violation."""

    def test_planted_violations_are_detected(self) -> None:
        for ob in _SOURCE_OBLIGATIONS:
            with self.subTest(ob.check_id):
                name, _, _ = ob.planted_violation.partition(":")
                findings = en.detect_source(ob.check_id, FIXTURES / name)
                self.assertTrue(
                    findings,
                    "{}: detector found nothing in its own planted violation "
                    "{} -- it cannot convict anything".format(
                        ob.check_id, ob.planted_violation
                    ),
                )

    def test_from_package_import_module_is_an_import(self) -> None:
        """The spelling that made two detectors blind, stated on its own.

        `from adw_modules import agents` binds the module `adw_modules.agents`.
        Regression guard for the fix; without it the assertion above passes
        again the moment `_imports` forgets aliases.
        """
        import ast

        names = en._imports(ast.parse("from adw_modules import agents, gates\n"))
        self.assertIn("adw_modules.agents", names)
        self.assertIn("adw_modules.gates", names)

    def test_an_unknown_check_id_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            en.detect_source("no-such-check", FIXTURES / "violations_a.py")


class TheRuntimeIsClean(unittest.TestCase):
    """The invariants hold over `adw_modules/` as it stands."""

    def test_no_source_obligation_is_violated_in_this_tree(self) -> None:
        for ob in _SOURCE_OBLIGATIONS:
            with self.subTest(ob.check_id):
                findings = en.scan_real_tree(ob.check_id, MODULES)
                self.assertEqual(
                    (),
                    findings,
                    "{} ({}): {}".format(
                        ob.check_id, ob.green_control, ", ".join(findings)
                    ),
                )


class TheFrozenSurface(unittest.TestCase):
    """`verb-existence` and `installed-bytes`, against the real parser."""

    def test_the_parser_carries_every_required_verb(self) -> None:
        en.assert_verbs(maestro.parser_verbs(maestro.build_parser()))

    def test_a_missing_verb_is_refused_by_name(self) -> None:
        observed = [v for v in en.REQUIRED_VERBS if v != "run attend"]
        with self.assertRaises(en.EnforcementViolation) as caught:
            en.assert_verbs(observed)
        self.assertIn("run attend", str(caught.exception))

    def test_required_verbs_are_exactly_the_shipped_surface(self) -> None:
        self.assertEqual(
            ("run start", "run resume", "run amend", "run attend", "run status"),
            en.REQUIRED_VERBS,
        )

    def test_the_imported_runtime_is_the_one_under_test(self) -> None:
        en.assert_installed_bytes(RUNTIME_ROOT)

    def test_a_foreign_runtime_root_is_refused(self) -> None:
        with self.assertRaises(en.EnforcementViolation) as caught:
            en.assert_installed_bytes(RUNTIME_ROOT / "adw_modules")
        self.assertIn("INSTALLED_BYTES_MISMATCH", str(caught.exception))

    def test_every_declared_installed_file_exists(self) -> None:
        for relative in en.INSTALLED_FACTORY_FILES:
            with self.subTest(relative):
                self.assertTrue((RUNTIME_ROOT / relative).is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
