"""A suite environment refusal carries the measurement that caused it.

`SUITE_RUNNER_UNUSABLE:vitest` plus "repair the review environment and
resume" cannot tell UNRESOLVED (the runner was never installed; the repair is
`provision_argv`) from INCAPABLE (the runner is installed and cannot resolve
this project's config; the repair is the config or its deps). Those are
opposite repairs, so the operator gets `RunnerUnusable.detail` verbatim: the
suite is visible, and the probe's own words name the repair.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adw_modules import review_contract as rc
from adw_modules import runner_resolution as rr
from adw_modules import tests_chain as tc


class SuiteRefusalCarriesItsMeasurementTest(unittest.TestCase):
    SEALED = "tests/test_kryptonite_sealed_case.py"

    def _refuse(self, unusable: rr.RunnerUnusable, *, body: str = "") -> str:
        with tempfile.TemporaryDirectory() as tree:
            if body:
                sealed = Path(tree) / self.SEALED
                sealed.parent.mkdir(parents=True, exist_ok=True)
                sealed.write_text(body, encoding="utf-8")
            with mock.patch.object(rr, "resolve", side_effect=unusable):
                with self.assertRaises(rc.SuiteEnvironmentError) as caught:
                    tc.run_suite(Path(tree), (self.SEALED,))
        return str(caught.exception)

    def test_an_incapable_runner_reports_its_probe(self) -> None:
        message = self._refuse(
            rr.RunnerUnusable(
                "pytest",
                rr.Reason.INCAPABLE,
                ".",
                resolved="/candidate/.venv/bin/pytest",
                probe_exit=4,
                probe_output="ERR_MODULE_NOT_FOUND: cannot resolve 'vitest/config'",
            )
        )
        # The operator boundary matches on the prefix; it stays first.
        self.assertTrue(
            message.startswith("SUITE_RUNNER_UNUSABLE:pytest"), message
        )
        # INCAPABLE, distinguishably: a binary was resolved and it started.
        self.assertIn("could not collect", message)
        self.assertIn("/candidate/.venv/bin/pytest", message)
        self.assertIn("probe exit 4", message)
        # The probe's own words, which name the actual repair.
        self.assertIn("ERR_MODULE_NOT_FOUND", message)
        self.assertIn("vitest/config", message)

    def test_an_unresolved_runner_reads_differently(self) -> None:
        message = self._refuse(
            rr.RunnerUnusable(
                "pytest",
                rr.Reason.UNRESOLVED,
                ".",
                candidates=(".venv/bin/pytest", "uv run pytest"),
            )
        )
        self.assertTrue(
            message.startswith("SUITE_RUNNER_UNUSABLE:pytest"), message
        )
        self.assertIn("no usable pytest was found", message)
        self.assertIn("uv run pytest", message)
        self.assertNotIn("could not collect", message)

    def test_the_probe_output_names_the_file_it_could_not_collect(self) -> None:
        message = self._refuse(
            rr.RunnerUnusable(
                "pytest",
                rr.Reason.INCAPABLE,
                ".",
                resolved="/candidate/.venv/bin/pytest",
                probe_exit=2,
                probe_output=(
                    "ERROR collecting {0} - ImportError".format(self.SEALED)
                ),
            )
        )
        # Nothing is redacted: the file is the operator's to open.
        self.assertIn(self.SEALED, message)
        self.assertNotIn("[redacted]", message)
        self.assertIn("ImportError", message)
        self.assertIn("probe exit 2", message)

    def test_source_quoted_by_the_probe_reaches_the_operator(self) -> None:
        literal = "EXPECTED_CLEARANCE_LITERAL = 'K-9-provenance'"
        message = self._refuse(
            rr.RunnerUnusable(
                "pytest",
                rr.Reason.INCAPABLE,
                ".",
                resolved="/candidate/.venv/bin/pytest",
                probe_exit=2,
                probe_output="E   {0}\nE   ImportError: no module named app".format(
                    literal
                ),
            ),
            body="import app\n{0}\n".format(literal),
        )
        self.assertIn(literal, message)
        self.assertIn("ImportError", message)
        self.assertIn("probe exit 2", message)


if __name__ == "__main__":
    unittest.main()
