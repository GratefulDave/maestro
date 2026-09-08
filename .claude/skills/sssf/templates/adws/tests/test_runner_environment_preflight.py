"""Runner preflight failures name the unusable environment, not a lane."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adw_modules import scheduler
from adw_modules.scheduler import RunnerPreflightRefused


class PreflightRefusal(unittest.TestCase):
    def test_the_refusal_is_typed_and_names_the_harness_not_a_lane(self) -> None:
        self.assertEqual(RunnerPreflightRefused.code, "RUNNER_PREFLIGHT_REFUSED")

    def test_it_carries_what_was_unusable(self) -> None:
        exc = RunnerPreflightRefused("pytest in .: no usable pytest was found")
        self.assertIn("pytest", str(exc))
        self.assertIn("no usable pytest", str(exc))


class RealResolverPreflight(unittest.TestCase):
    """Keep vault setup isolated, but execute the production resolver and probe."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = self.root / "checkout"
        self.tree.mkdir()
        self.marker = self.root / "probe-ran"
        self.factory = scheduler.FactoryScheduler.__new__(scheduler.FactoryScheduler)
        self.factory.run_id = "preflight-regression"
        self.factory.store = SimpleNamespace(
            active_projection=lambda _: [SimpleNamespace(lane_id="lane")]
        )
        self.factory.actor = SimpleNamespace(
            lane_specs={"lane": {"gate": {"runner": "pytest", "min_cases": 1}}}
        )
        self.factory.target = SimpleNamespace(target_repository_root=str(self.tree))
        self.factory.runtime = SimpleNamespace(path=self.root / "state")
        self.factory._provision_argv = ()
        self.factory._provision_timeout_s = 1
        self.factory._say = lambda *_: None
        for name, value in (
            ("ensure_vault", self.root / "vault"),
            ("seed", "test-base"),
            ("scratch_worktree_path", self.tree),
            ("checkout_vault_worktree", None),
        ):
            patch = mock.patch.object(scheduler.hv, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            scheduler, "_remove_collect_tree",
            side_effect=lambda tree, _: shutil.rmtree(tree),
        )
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"PATH": ""})
        patch.start()
        self.addCleanup(patch.stop)

    def _runner(self, exit_code: int) -> None:
        binary = self.tree / ".venv" / "bin" / "pytest"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo pytest; exit 0; fi\n'
            'printf "%s\\n" "$@" > "{0}"\n'
            "echo 'preflight dependency unavailable' >&2\n"
            "exit {1}\n".format(self.marker, exit_code),
            encoding="utf-8",
        )
        binary.chmod(0o755)

    def test_real_probe_accepts_capable_runner_and_discards_scratch_tree(self) -> None:
        self._runner(5)

        self.factory._assert_runners_usable()

        self.assertIn("--collect-only", self.marker.read_text())
        self.assertFalse(self.tree.exists())

    def test_real_probe_refusal_keeps_measured_tree_and_reports_runner_output(self) -> None:
        self._runner(4)

        with self.assertRaises(RunnerPreflightRefused) as caught:
            self.factory._assert_runners_usable()

        self.assertIn("--collect-only", self.marker.read_text())
        self.assertTrue(self.tree.is_dir())
        self.assertIn("preflight dependency unavailable", str(caught.exception))
        self.assertIn(str(self.tree), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
