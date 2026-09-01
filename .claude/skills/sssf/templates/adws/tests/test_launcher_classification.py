"""Launch failures are transport. They are not retry-class authority."""

from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import maestro
from adw_modules import launcher as lch
from adw_modules.scheduler import LaunchFailed


class LaunchFailedIsTransportOnlyTest(unittest.TestCase):
    def test_launch_failed_has_no_retry_class_or_budget(self) -> None:
        failed = LaunchFailed("pane refused", pane_created=False)
        self.assertIsInstance(failed, RuntimeError)
        self.assertEqual(failed.detail, "pane refused")
        self.assertFalse(failed.pane_created)
        self.assertFalse(hasattr(failed, "retry_class"))
        self.assertFalse(hasattr(LaunchFailed, "retry_class"))

    def test_cli_maps_launch_failed_to_typed_json_without_retry(self) -> None:
        stdout = StringIO()
        with mock.patch.object(
            maestro, "_run_start", side_effect=LaunchFailed("herdr gone")
        ):
            with mock.patch("sys.stdout", stdout):
                code = maestro.main(
                    [
                        "run",
                        "start",
                        "plan.json",
                        "--repo",
                        "/abs/product",
                        "--main-ref",
                        "refs/heads/main",
                    ]
                )
        self.assertEqual(code, 3)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["outcome"], "LAUNCH_FAILED")
        self.assertEqual(payload["detail"], "herdr gone")
        self.assertNotIn("retry_class", payload)
        self.assertNotIn("ceiling", payload)

    def test_unreachable_herdr_binary_maps_to_launch_failed_json(self) -> None:
        """`executables.herdr` naming a missing binary is HERDR_UNAVAILABLE,
        carried to the operator as typed LAUNCH_FAILED JSON with no traceback."""
        launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
        with tempfile.TemporaryDirectory() as tmp:
            launcher.herdr_path = Path(tmp) / "no-such-herdr"
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._herdr("workspace", "list")
        self.assertIs(raised.exception.refusal, lch.LaunchRefusal.HERDR_UNAVAILABLE)
        failed = maestro.HerdrStageActor._launch_failed(None, raised.exception)  # type: ignore[arg-type]
        self.assertIsInstance(failed, LaunchFailed)
        stdout, stderr = StringIO(), StringIO()
        with mock.patch.object(maestro, "_run_start", side_effect=failed):
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                code = maestro.main(
                    [
                        "run",
                        "start",
                        "plan.json",
                        "--repo",
                        "/abs/product",
                        "--main-ref",
                        "refs/heads/main",
                    ]
                )
        self.assertEqual(code, 3)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["outcome"], "LAUNCH_FAILED")
        # `_launch_failed` prefixes the bare refusal code. `LaunchRefusal` is
        # an Enum whose `.value` is the whole `(code, pane_created,
        # deterministic)` tuple, so rendering the member instead of `.code`
        # put `('HERDR_UNAVAILABLE', None, True):...` in front of an
        # operator; the detail must start with the code and carry no tuple.
        self.assertTrue(
            payload["detail"].startswith("HERDR_UNAVAILABLE:"), payload["detail"]
        )
        self.assertNotIn("('", payload["detail"])
        self.assertIn("no-such-herdr", payload["detail"])
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
