"""The findings schema shows an object, and a misshapen envelope is named.

Run 7a80027d4a3c43a9ac0b1a34b028609f died here. `_schema` rendered the
findings shape as `list(REVISE_FINDING_KEYS)` -- four bare key names -- which
reads as "findings is an array of four strings". The lane-wp1-sections-tests
reviewer read it that way and filled the array positionally:

    ["services/label-batch/tests/sections/test_section_extraction.py — ...",
     "The case supplies only a subset of the mapped codes, ...",
     "Exercise every entry in the pinned mapping in one document, ...",
     "The suite must fix the behavior that only the thirteen mapped ..."]

That is one correct, complete finding in key order, serialized flat. Three
sibling reviewers guessed objects and were fine. `require_revise_findings`
refused it several frames inside `tests_chain.review_test_draft`, and the
CanonicalIdentityError left the scheduler, ending the run and taking four
healthy in-flight lanes with it.

Two things are pinned. The schema a reviewer is handed must be unambiguous
about the shape, and a misshapen envelope must be refused where the lane and
role are still known rather than as a bare identity error from the writer.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

#: The envelope, verbatim from the run.
SECTIONS_FINDINGS = [
    "services/label-batch/tests/sections/test_section_extraction.py — mapped-code filtering case",
    "The case supplies only a subset of the mapped codes, so an extractor that "
    "recognizes only the mapped codes used elsewhere in the draft can pass while "
    "silently dropping other valid mapped sections.",
    "Exercise every entry in the pinned mapping in one document, include at least "
    "one unmapped section, and assert that all and only the mapped entries are "
    "returned.",
    "The suite must fix the behavior that only the thirteen mapped LOINC codes are "
    "kept; the current assertions prove rejection of unmapped codes but do not "
    "prove retention of all thirteen mapped codes.",
]

_REVIEW_ROLES = ("test-reviewer", "code-reviewer", "integration-reviewer")


def _schema(role: str) -> dict:
    return maestro.HerdrStageActor._schema(None, role)  # type: ignore[arg-type]


def _payload(payload):
    return maestro.HerdrStageActor._review_payload(None, payload)  # type: ignore[arg-type]


class FindingsSchemaShape(unittest.TestCase):
    def test_every_reviewer_is_shown_an_object_not_a_list_of_key_names(self) -> None:
        for role in _REVIEW_ROLES:
            with self.subTest(role=role):
                findings = _schema(role)["findings"]
                self.assertEqual(len(findings), 1, "show exactly one example")
                self.assertIsInstance(
                    findings[0], dict, "a string here is what caused the flattening"
                )
                self.assertEqual(set(findings[0]), set(st.REVISE_FINDING_KEYS))

    def test_no_reviewer_schema_serializes_findings_as_bare_strings(self) -> None:
        """The exact rendering the reviewer read off its prompt."""
        for role in _REVIEW_ROLES:
            with self.subTest(role=role):
                rendered = json.dumps(_schema(role), sort_keys=True)
                self.assertNotIn('"findings":["implementation_area"', rendered)

    def test_the_placeholder_names_the_key_it_stands_for(self) -> None:
        finding = _schema("test-reviewer")["findings"][0]
        for key, value in finding.items():
            self.assertEqual(value, "<{0}>".format(key))


class MalformedEnvelopeIsNamed(unittest.TestCase):
    def test_the_sections_envelope_is_refused_by_name_not_by_identity_error(
        self,
    ) -> None:
        with self.assertRaises(maestro.FactoryRefused) as caught:
            _payload({"verdict": "REVISE", "findings": SECTIONS_FINDINGS})
        detail = str(caught.exception)
        self.assertIn("REVIEW_FINDINGS_MALFORMED", detail)
        self.assertIn("findings[0]", detail, "say which one")
        self.assertIn("str", detail, "say what it actually was")
        self.assertIn("implementation_area", detail, "say what was wanted")

    def test_a_well_formed_revise_still_passes_through_untouched(self) -> None:
        finding = {key: "text" for key in st.REVISE_FINDING_KEYS}
        verdict, findings = _payload({"verdict": "REVISE", "findings": [finding]})
        self.assertIs(verdict, st.ReviewerVerdict.REVISE)
        self.assertEqual(findings, [finding])

    def test_a_pass_with_no_findings_still_passes_through(self) -> None:
        verdict, findings = _payload({"verdict": "PASS", "findings": []})
        self.assertIs(verdict, st.ReviewerVerdict.PASS)
        self.assertEqual(findings, [])

    def test_a_missing_verdict_still_refuses_as_before(self) -> None:
        with self.assertRaises(maestro.FactoryRefused) as caught:
            _payload({"findings": []})
        self.assertIn("REVIEW_VERDICT_MISSING", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
