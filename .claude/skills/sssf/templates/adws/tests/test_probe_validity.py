"""Mechanical validity: each check rejects exactly its own violation.

A probe that passes every check here is *mechanically* valid, not correct; the
rest is the acceptor's judgment. Each test below is red if its check is deleted
(the falsification sweep removes them one at a time).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from probe_support import CONTRACT, INTERFACE, REQUIREMENT, write_tree

from adw_modules import probes
from adw_modules import scheduler_types as st

CONTRACT_TEXT = " ".join(
    CONTRACT["acceptance_criteria"] + CONTRACT["declared_outputs"]
) + " " + str(INTERFACE)
GOOD = "from refund import refund\n\n\ndef test_a():\n    assert refund(0) is None\n"


def _sub(path="tests/test_p.py", source=GOOD, requirement=REQUIREMENT):
    return probes.ProbeSubmission(path, source, requirement, "intent")


class Validity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = write_tree(
            Path(self.tmp.name) / "tree",
            {"refund.py": "def refund(a):\n    return a\n", "internal.py": "x = 1\n",
             "web/lib.ts": "export const a = 1\n", "web/hidden.ts": "export const b = 1\n"},
        )

    def _check(self, submission, runner="pytest", interface=INTERFACE):
        return probes.check_submission(
            submission,
            contract_text=CONTRACT_TEXT,
            interface=interface,
            runner=runner,
            tree=self.tree,
        )

    def _codes(self, *args, **kwargs):
        return [r.code for r in self._check(*args, **kwargs)]

    def test_a_valid_probe_has_no_rejection(self) -> None:
        self.assertEqual(self._check(_sub()), ())

    def test_the_file_must_belong_to_the_suites_runner(self) -> None:
        self.assertEqual(self._codes(_sub("web/p.test.ts")), [probes.PROBE_RUNNER_MISMATCH])
        self.assertEqual(self._codes(_sub("tests/notes.txt")), [probes.PROBE_RUNNER_MISMATCH])
        self.assertEqual(self._codes(_sub(), runner="vitest"), [probes.PROBE_RUNNER_MISMATCH])

    def test_the_requirement_must_quote_the_contract_verbatim(self) -> None:
        self.assertEqual(
            self._codes(_sub(requirement="amounts below zero are rejected loudly")),
            [probes.PROBE_UNCITED],
        )

    def test_a_citation_shorter_than_the_minimum_is_not_a_citation(self) -> None:
        self.assertLess(len("zero amount"), st.MIN_CONTRACT_QUOTE)
        self.assertEqual(self._codes(_sub(requirement="zero amount")), [probes.PROBE_UNCITED])

    def test_quotes_whitespace_and_case_are_normalized_like_a_finding_citation(self) -> None:
        self.assertEqual(
            self._check(_sub(requirement='  "Negative   AMOUNTS are refused"  ')), ()
        )

    def test_an_unparseable_python_probe_is_rejected(self) -> None:
        self.assertEqual(
            self._codes(_sub(source="def test_a(:\n    pass\n")), [probes.PROBE_NOT_PARSEABLE]
        )

    def test_importing_an_undeclared_in_tree_module_is_rejected(self) -> None:
        source = "import internal\n\n\ndef test_a():\n    assert internal.x == 2\n"
        self.assertEqual(self._codes(_sub(source=source)), [probes.PROBE_IMPORT_UNDECLARED])

    def test_importing_an_undeclared_name_of_a_declared_module_is_rejected(self) -> None:
        source = "from refund import refund, _secret_helper\n\n\ndef test_a():\n    pass\n"
        rejected = self._check(_sub(source=source))
        self.assertEqual([r.code for r in rejected], [probes.PROBE_IMPORT_UNDECLARED])
        self.assertIn("_secret_helper", rejected[0].detail)

    def test_a_relative_import_is_rejected(self) -> None:
        source = "from . import internal\n\n\ndef test_a():\n    pass\n"
        self.assertEqual(self._codes(_sub(source=source)), [probes.PROBE_IMPORT_UNDECLARED])

    def test_third_party_and_stdlib_imports_are_not_the_interfaces_business(self) -> None:
        source = "import json\nimport pytest\nfrom refund import refund\n\n\ndef test_a():\n    pass\n"
        self.assertEqual(self._check(_sub(source=source)), ())

    def test_with_no_declared_interface_an_in_tree_import_is_undeclared(self) -> None:
        self.assertEqual(self._codes(_sub(), interface=()), [probes.PROBE_IMPORT_UNDECLARED])

    def test_a_javascript_relative_import_must_be_a_declared_module(self) -> None:
        interface = ({"kind": "module", "module": "web/lib", "name": "a"},)
        ok = "import { a } from './lib'\nimport { test } from 'vitest'\n"
        hidden = "import { b } from './hidden'\n"
        self.assertEqual(self._check(_sub("web/p.test.ts", ok), "vitest", interface), ())
        self.assertEqual(
            self._codes(_sub("web/p.test.ts", hidden), "vitest", interface),
            [probes.PROBE_IMPORT_UNDECLARED],
        )

    def test_a_javascript_import_that_leaves_the_repository_is_rejected(self) -> None:
        source = "import { a } from '../../etc/thing'\n"
        self.assertEqual(
            self._codes(_sub("web/p.test.ts", source), "vitest", ()),
            [probes.PROBE_IMPORT_UNDECLARED],
        )


if __name__ == "__main__":
    unittest.main()
