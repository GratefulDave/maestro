"""A count cannot tell a happy-path suite from a discharged contract.

`gate.min_cases` counts collected cases. Eleven positive cases satisfy a floor of
eleven whether or not the contract they answer also requires a refusal, so a
suite can be arithmetically correct and assert nothing about the obligation it
exists for.

Observed on run a33d5e9b4a404f5889785cb1c9ca5f6f. `claim-wp1-provenance` says
every observation carries release_id, method_version and code_sha256. Eleven
cases asserted that a correctly-called store records them; none asserted that an
incorrectly-called store refuses. The builder wrote
`record(observation, *, raw_spl=None)` -- legitimate under those cases -- and
passed 11/11. Plan revision 2 asked for refusal cases in prose; the tester
received that text (`prompt-4.json`, `prompt-5.json`) and sealed the same eleven
case names. Revision 3 raised the floor to 15 and the four refusal cases appeared
immediately, but only because a human knew the number was 15.

`gate.required_cases` names them instead. These cases pin that:

* a missing required case is refused even when the count is satisfied;
* the correction names the missing cases rather than asking for "more";
* names are matched inside runner identifiers (`path::name`, `path > title`);
* a lane declaring none behaves exactly as before.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import scheduler as sch  # noqa: E402

PYTEST_IDS = (
    "services/label-batch/tests/observations/test_store.py::test_appends_a_row",
    "services/label-batch/tests/observations/test_store.py::test_same_key_twice",
)
VITEST_IDS = ("src/checkout.test.ts > refuses a negative amount",)


class MissingRequiredCaseTests(unittest.TestCase):
    def test_a_required_case_present_in_a_pytest_identifier_is_found(self):
        self.assertEqual(
            sch._missing_required_cases(PYTEST_IDS, ("test_appends_a_row",)), ()
        )

    def test_a_required_case_present_in_a_vitest_identifier_is_found(self):
        self.assertEqual(
            sch._missing_required_cases(VITEST_IDS, ("refuses a negative amount",)),
            (),
        )

    def test_a_required_case_with_no_identifier_is_reported(self):
        self.assertEqual(
            sch._missing_required_cases(
                PYTEST_IDS, ("test_appends_a_row", "test_without_raw_spl_is_refused")
            ),
            ("test_without_raw_spl_is_refused",),
        )

    def test_requiring_nothing_reports_nothing(self):
        self.assertEqual(sch._missing_required_cases(PYTEST_IDS, ()), ())

    def test_the_correction_names_every_missing_case(self):
        findings = sch._draft_required_cases_findings(
            ("test_a_is_refused", "test_b_is_refused")
        )
        self.assertEqual(len(findings), 1)
        observed = findings[0]["observed_behavior"]
        self.assertIn("test_a_is_refused", observed)
        self.assertIn("test_b_is_refused", observed)
        self.assertEqual(findings[0]["violated_requirement"], "gate.required_cases")


class GateParseTests(unittest.TestCase):
    def test_required_cases_are_parsed_and_carried_to_the_collect_gate(self):
        gate = SimpleNamespace(
            runner="pytest",
            argv=("tests/observations",),
            cwd=".",
            min_cases=15,
            required_cases=("test_without_raw_spl_is_refused",),
        )
        collect = sch._collect_gate(gate, {"tests/observations/test_x.py": "x"})
        self.assertEqual(
            collect.required_cases, ("test_without_raw_spl_is_refused",)
        )

    def test_a_gate_declaring_none_carries_an_empty_tuple(self):
        gate = SimpleNamespace(
            runner="pytest", argv=("tests",), cwd=".", min_cases=1
        )
        collect = sch._collect_gate(gate, {"tests/test_x.py": "x"})
        self.assertEqual(collect.required_cases, ())


if __name__ == "__main__":
    unittest.main()
