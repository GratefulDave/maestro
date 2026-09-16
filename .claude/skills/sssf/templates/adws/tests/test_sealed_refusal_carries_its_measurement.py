"""A sealed-suite environment refusal carries the measurement that caused it.

`SEALED_SUITE_RUNNER_UNUSABLE:vitest` plus "repair the review environment and
resume" cannot tell UNRESOLVED (the runner was never installed; the repair is
`provision_argv`) from INCAPABLE (the runner is installed and cannot resolve
this project's config; the repair is the config or its deps). Those are
opposite repairs, so the operator gets `RunnerUnusable.detail` -- redacted,
because the probe's own output can quote sealed test source and the tree path
names the vault checkout.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adw_modules import private_review as pr
from adw_modules import runner_resolution as rr
from adw_modules import tests_chain as tc


class SealedRefusalCarriesItsMeasurementTest(unittest.TestCase):
    SEALED = "tests/test_kryptonite_sealed_case.py"

    def _refuse(self, unusable: rr.RunnerUnusable, *, body: str = "") -> str:
        with tempfile.TemporaryDirectory() as tree:
            if body:
                sealed = Path(tree) / self.SEALED
                sealed.parent.mkdir(parents=True, exist_ok=True)
                sealed.write_text(body, encoding="utf-8")
            with mock.patch.object(rr, "resolve", side_effect=unusable):
                with self.assertRaises(pr.SealedEnvironmentError) as caught:
                    tc.run_private_suite(Path(tree), (self.SEALED,))
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
            message.startswith("SEALED_SUITE_RUNNER_UNUSABLE:pytest"), message
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
            message.startswith("SEALED_SUITE_RUNNER_UNUSABLE:pytest"), message
        )
        self.assertIn("no usable pytest was found", message)
        self.assertIn("uv run pytest", message)
        self.assertNotIn("could not collect", message)

    def test_private_tokens_in_the_probe_output_are_redacted(self) -> None:
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
        self.assertNotIn(self.SEALED, message)
        self.assertIn("[redacted]", message)
        # Redaction removes the sealed path, not the diagnosis.
        self.assertIn("ImportError", message)
        self.assertIn("probe exit 2", message)


    def test_sealed_source_quoted_by_the_probe_is_redacted(self) -> None:
        """The probe quotes source, not just file names.

        A collect error echoes the line that failed to import, and
        `code_review._run_sealed_suite` puts `str(exc)` into the run's
        `output`, so that line leaves this boundary. Redacting the sealed
        *path* alone does not redact the sealed *source*: the tokens have to
        be built from the bodies, which is what `files=` does and `extra=`
        does not.
        """
        secret = "EXPECTED_CLEARANCE_LITERAL = 'K-9-provenance'"
        message = self._refuse(
            rr.RunnerUnusable(
                "pytest",
                rr.Reason.INCAPABLE,
                ".",
                resolved="/candidate/.venv/bin/pytest",
                probe_exit=2,
                probe_output="E   {0}\nE   ImportError: no module named app".format(
                    secret
                ),
            ),
            body="import app\n{0}\n".format(secret),
        )
        self.assertNotIn(secret, message)
        self.assertNotIn("K-9-provenance", message)
        # The diagnosis survives the redaction.
        self.assertIn("ImportError", message)
        self.assertIn("probe exit 2", message)


if __name__ == "__main__":
    unittest.main()
