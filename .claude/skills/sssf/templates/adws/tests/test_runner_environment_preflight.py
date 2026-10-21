"""Runner preflight failures name the unusable environment, not a lane."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adw_modules import provisioning as prov
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
    """Keep tree setup isolated, but execute the production resolver and probe."""

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
        self.factory._integration_head = lambda: "test-base"
        for name, value in (
            ("scratch_tree_path", self.tree),
            ("materialize_commit", None),
        ):
            patch = mock.patch.object(scheduler.tm, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(
            scheduler, "_remove_collect_tree",
            side_effect=lambda tree: shutil.rmtree(tree),
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

    def test_real_probe_refusal_reports_runner_output_and_where_it_ran(self) -> None:
        # The probe runs on the spot fleet, so the local tree it was named
        # after is not where it was measured and is not kept.
        self._runner(4)

        with self.assertRaises(RunnerPreflightRefused) as caught:
            self.factory._assert_runners_usable()

        self.assertIn("--collect-only", self.marker.read_text())
        self.assertFalse(self.tree.exists())
        self.assertIn("preflight dependency unavailable", str(caught.exception))
        self.assertIn("measured on the spot fleet at", str(caught.exception))

    def _require(self, *requirements: dict) -> None:
        prov.set_suite_requirements(requirements)
        self.addCleanup(prov.set_suite_requirements, ())

    def test_a_requirement_with_no_install_refuses_the_run_naming_it(self) -> None:
        # MPS on a Linux runner: nothing can install it, so the run is refused
        # before any lane is dispatched, and the runner is never reached.
        self._runner(5)
        self._require({"name": "mps", "probe": ["/bin/sh", "-c", "exit 1"]})

        with self.assertRaises(RunnerPreflightRefused) as caught:
            self.factory._assert_runners_usable()

        self.assertIn("SUITE_REQUIREMENT_MISSING mps", str(caught.exception))
        self.assertIn("no install is declared", str(caught.exception))
        self.assertFalse(self.marker.exists())

    def test_a_failed_install_refuses_the_run_naming_the_requirement(self) -> None:
        self._runner(5)
        self._require({
            "name": "just",
            "probe": ["/bin/sh", "-c", "exit 1"],
            "install": ["/bin/sh", "-c", "echo E: Unable to locate package just >&2; exit 100"],
        })

        with self.assertRaises(RunnerPreflightRefused) as caught:
            self.factory._assert_runners_usable()

        self.assertIn("SUITE_REQUIREMENT_MISSING just: install failed", str(caught.exception))
        self.assertIn("Unable to locate package just", str(caught.exception))

    def test_a_missing_tool_is_installed_then_proven_before_the_runner(self) -> None:
        self._runner(5)
        installed = self.root / "ffmpeg-installed"
        self._require({
            "name": "ffmpeg",
            "probe": ["/bin/sh", "-c", 'test -f "{0}"'.format(installed)],
            "install": ["/bin/sh", "-c", ': > "{0}"'.format(installed)],
        })

        self.factory._assert_runners_usable()

        self.assertTrue(installed.exists())
        self.assertIn("--collect-only", self.marker.read_text())


class SuiteRequirementsConfig(unittest.TestCase):
    def test_entries_parse_and_malformed_ones_are_refused_by_name(self) -> None:
        parsed = prov.parse_suite_requirements(
            [{"name": "ffmpeg", "probe": ["ffmpeg", "-version"], "install": ["apt-get", "install", "ffmpeg"]},
             {"name": "mps", "probe": ["python", "-c", "pass"]}]
        )
        self.assertEqual([item["name"] for item in parsed], ["ffmpeg", "mps"])
        self.assertNotIn("install", parsed[1])
        self.assertEqual(prov.parse_suite_requirements(None), ())
        for bad in ({"name": "x"}, [{"name": "x", "probe": "ffmpeg -version"}],
                    [{"name": "", "probe": ["x"]}], [{"name": "x", "probe": ["x"], "via": ["y"]}]):
            with self.assertRaises(ValueError):
                prov.parse_suite_requirements(bad)


if __name__ == "__main__":
    unittest.main()
