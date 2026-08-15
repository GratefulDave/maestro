"""Step 9: every static detector proves both conviction and acquittal."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import enforcement


ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "adw_modules"
FIXTURES = Path(__file__).parent / "fixtures" / "enforcement"

WORKSPACE_SOURCE_FIXTURES = {
    "workspace-foundation-execution-boundary": (
        FIXTURES / "workspace_foundation_bad.py",
        FIXTURES / "workspace_foundation_control.py",
        ("execution import:workspace_runtime",),
    ),
    "workspace-receipt-execution-boundary": (
        FIXTURES / "workspace_receipt_bad.py",
        FIXTURES / "workspace_receipt_control.py",
        ("receipt execution import:coordinator",),
    ),
    "coordinator-execution-boundary": (
        FIXTURES / "coordinator_execution_bad.py",
        FIXTURES / "coordinator_execution_control.py",
        ("direct sqlite import", "direct subprocess import"),
    ),
    "publication-authority-boundary": (
        FIXTURES / "publication_authority_bad.py",
        FIXTURES / "publication_authority_control.py",
        ("workspace reconstruction import:workspace_canonical",
         "workspace reconstruction import:workspace_digest"),
    ),
    "participant-independence-boundary": (
        FIXTURES / "participant_independence_bad.py",
        FIXTURES / "participant_independence_control.py",
        ("participant execution import:coordinator",
         "participant execution import:publication"),
    ),
}

WORKSPACE_VERBS = (
    "workspace validate", "workspace finalize", "workspace start",
    "workspace status", "workspace cancel", "workspace resume",
    "workspace publish", "workspace rollback",
)

WORKSPACE_INSTALLED_FILES = (
    "maestro.py",
    "adw_modules/workspace_model.py",
    "adw_modules/workspace_canonical.py",
    "adw_modules/workspace_digest.py",
    "adw_modules/workspace_receipt.py",
    "adw_modules/coordinator_store.py",
    "adw_modules/participant.py",
    "adw_modules/workspace_runtime.py",
    "adw_modules/coordinator.py",
    "adw_modules/publication.py",
)


class EnforcementLedgerTest(unittest.TestCase):
    def test_ledger_has_exactly_sixteen_complete_unique_pairs(self):
        rows = enforcement.OBLIGATION_LEDGER
        self.assertEqual(len(rows), 16)
        self.assertEqual(len({row.check_id for row in rows}), 16)
        self.assertTrue(all(row.detector and row.planted_violation and row.green_control for row in rows))

    def test_each_source_detector_convicts_only_its_explicit_fixture(self):
        planted = {
            "route-boundary": FIXTURES / "violations_a.py",
            "digest-over-audit": FIXTURES / "violations_a.py",
            "model-from-audit": FIXTURES / "violations_a.py",
            "stderr-classification": FIXTURES / "violations_b.py",
            "status-write-boundary": FIXTURES / "violations_b.py",
            "finalization-import-boundary": FIXTURES / "violations_a.py",
            "digest-import-boundary": FIXTURES / "violations_b.py",
            "base-execution-import": FIXTURES / "violations_b.py",
        }
        for check_id, fixture in planted.items():
            with self.subTest(check_id=check_id):
                self.assertTrue(enforcement.detect_source(check_id, fixture))
                self.assertEqual(enforcement.scan_real_tree(check_id, MODULES), ())

    def test_workspace_source_detectors_convict_bad_and_acquit_controls(self):
        for check_id, (bad, control, expected) in WORKSPACE_SOURCE_FIXTURES.items():
            with self.subTest(check_id=check_id, kind="bad"):
                self.assertEqual(enforcement.detect_source(check_id, bad), expected)
            with self.subTest(check_id=check_id, kind="control"):
                self.assertEqual(enforcement.detect_source(check_id, control), ())
                self.assertEqual(enforcement.scan_real_tree(check_id, MODULES), ())

    def test_installed_bytes_detector_convicts_wrong_tree_and_acquits_runtime(self):
        self.assertEqual(enforcement.INSTALLED_WORKSPACE_FILES, WORKSPACE_INSTALLED_FILES)
        with self.assertRaises(enforcement.EnforcementViolation):
            enforcement.assert_installed_bytes(ROOT / "not-the-runtime")
        enforcement.assert_installed_bytes(ROOT)

    def test_legacy_verb_detector_convicts_missing_and_acquits_complete_parser(self):
        observed = maestro.parser_verbs(maestro.build_parser())
        missing_legacy_verb = tuple(
            verb for verb in observed if verb != "run resume")
        with self.assertRaises(enforcement.EnforcementViolation):
            enforcement.assert_verbs(missing_legacy_verb)
        enforcement.assert_verbs(observed)

    def test_workspace_verb_detector_requires_every_nested_verb(self):
        observed = maestro.parser_verbs(maestro.build_parser())
        workspace_verbs = tuple(
            verb for verb in observed if verb.startswith("workspace "))
        self.assertEqual(enforcement.WORKSPACE_REQUIRED_VERBS, WORKSPACE_VERBS)
        self.assertEqual(workspace_verbs, WORKSPACE_VERBS)
        for missing in WORKSPACE_VERBS:
            with self.subTest(missing=missing):
                partial = tuple(verb for verb in observed if verb != missing)
                with self.assertRaises(enforcement.EnforcementViolation):
                    enforcement.assert_workspace_verbs(partial)
        enforcement.assert_workspace_verbs(observed)


if __name__ == "__main__":
    unittest.main()
