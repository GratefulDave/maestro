"""Launch failures are transport. They are not retry-class authority."""

from __future__ import annotations

import json
import unittest
from io import StringIO
from unittest import mock

import maestro
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


if __name__ == "__main__":
    unittest.main()
