"""Every tree the factory hands to an actor or a measurement crosses one provisioner.

FDAdb run d246ae9592be478396ad5146a89f00ae, `lane-faq-producer-tests`: the
harness's draft-collect tree symlinked the product repository's `node_modules`
into itself, so the round-1 runner preflight and the draft collect both passed
in a deployment whose `provision_argv` installed no JS dependencies -- while the
tester, reviewer and builder trees, which had no bridge, could not resolve
`vitest/config`. Three rounds of a harness fault were filed against the tester.

Two properties restore the invariant, and both are asserted here rather than
grepped for:

* the scheduler's preflight and draft-collect trees, the launcher's actor
  worktree, and the code-review tree all call `provisioning.provision_tree`,
  so a patch of that one function observes every site; and
* with nothing bridged in, a provisioned tree whose runner cannot load is
  refused `RUNNER_PREFLIGHT_REFUSED` at round 1, where the answer is the
  deployment's `provision_argv` and no agent is asked to fix it.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adw_modules import code_review as cr
from adw_modules import launcher as lch
from adw_modules import provisioning
from adw_modules import runner_resolution as rr
from adw_modules import scheduler
from adw_modules.scheduler import RunnerPreflightRefused


#: Stands in for vitest 3.x loading `vitest.config.ts`: without the `vitest`
#: package installed in the tree it is run from, the config import fails before
#: any subcommand is read. The text is the shape measured against FDAdb
#: integration on 2026-08-30 and again on 2026-09-09.
_FAKE_VITEST = """#!/bin/sh
if [ ! -f node_modules/vitest/package.json ]; then
  echo "failed to load config from $PWD/vitest.config.ts" >&2
  echo "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'vitest' imported from $PWD/vitest.config.ts" >&2
  exit 1
fi
if [ "$1" = "--version" ]; then echo "vitest/3.2.7"; exit 0; fi
exit 0
"""


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _Recorder:
    """A stand-in for the one provisioner that records every tree it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, tuple[str, ...], object]] = []

    def __call__(self, dest, provision_argv, timeout_s=None) -> None:
        self.calls.append((Path(dest), tuple(provision_argv), timeout_s))


class _FactoryFixture(unittest.TestCase):
    """A `FactoryScheduler` with vault and checkout isolated; nothing else patched."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = self.root / "checkout"
        self.tree.mkdir()
        self.factory = scheduler.FactoryScheduler.__new__(scheduler.FactoryScheduler)
        self.factory.run_id = "one-provisioner"
        self.factory.store = SimpleNamespace(
            active_projection=lambda _: [SimpleNamespace(lane_id="lane")]
        )
        self.factory.actor = SimpleNamespace(
            lane_specs={
                "lane": {
                    "gate": {"runner": "vitest", "argv": ["src/a.test.ts"], "min_cases": 1}
                }
            }
        )
        self.factory.target = SimpleNamespace(target_repository_root=str(self.tree))
        self.factory.runtime = SimpleNamespace(path=self.root / "state")
        self.factory._provision_argv = ("provisioner", "--install")
        self.factory._provision_timeout_s = 7
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
            scheduler,
            "_remove_collect_tree",
            side_effect=lambda tree, _: shutil.rmtree(tree, ignore_errors=True),
        )
        patch.start()
        self.addCleanup(patch.stop)


class EverySiteCallsTheOneProvisioner(_FactoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.recorder = _Recorder()
        patch = mock.patch.object(provisioning, "provision_tree", self.recorder)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_runner_preflight_tree(self) -> None:
        with mock.patch.object(scheduler.rr, "resolve"):
            self.factory._assert_runners_usable()
        self.assertEqual(
            self.recorder.calls, [(self.tree, ("provisioner", "--install"), 7)]
        )

    def test_the_draft_collect_tree(self) -> None:
        ctx = SimpleNamespace(run_id="one-provisioner", lane=SimpleNamespace(lane_id="lane"))
        gate = SimpleNamespace(runner="vitest", argv=("src/a.test.ts",), cwd=".", min_cases=1)
        resolved = rr.ResolvedRunner(runner="vitest", executable="/nonexistent/vitest")
        with (
            mock.patch.object(scheduler.rr, "resolve", return_value=resolved),
            mock.patch.object(scheduler.prv, "write_files"),
            mock.patch.object(
                scheduler.rr, "collect_cases", return_value=("src/a.test.ts > a",)
            ) as collect,
        ):
            ids = self.factory._collect_private_draft(ctx, gate, {"src/a.test.ts": "it()"})
        self.assertEqual(ids, ("src/a.test.ts > a",))
        self.assertEqual(
            self.recorder.calls, [(self.tree, ("provisioner", "--install"), 7)]
        )
        # And nothing is bridged in after provisioning: the collect is asked
        # about the tree alone.
        self.assertNotIn("runtime_root", collect.call_args.kwargs)
        self.assertFalse((self.tree / "node_modules").exists())

    def test_the_actor_worktree(self) -> None:
        launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
        launcher.provision_argv = ("provisioner", "--install")
        launcher.provision_timeout_s = 7.0
        launcher.provision(self.tree)
        self.assertEqual(
            self.recorder.calls, [(self.tree, ("provisioner", "--install"), 7.0)]
        )

    def test_the_review_tree(self) -> None:
        dest = self.root / "review"
        with mock.patch.object(cr.hv, "materialize_commit", return_value=dest) as materialize:
            cr._review_tree(self.root / "repo", "a" * 40, dest, ("provisioner", "--install"), 7)
        materialize.assert_called_once()
        self.assertEqual(
            self.recorder.calls, [(dest, ("provisioner", "--install"), 7)]
        )

    def test_the_harness_has_no_bridge_left_to_reach_for(self) -> None:
        for name in ("prepare_collect_tree", "COLLECT_RUNTIME_DIRS"):
            self.assertFalse(hasattr(rr, name), name)
        import inspect

        for fn in (rr.collect_cases, rr.execute_cases, scheduler.tc.run_private_suite):
            self.assertNotIn("runtime_root", inspect.signature(fn).parameters, fn.__name__)


class TheLauncherRefusesTyped(unittest.TestCase):
    def test_a_failing_provisioner_is_a_launch_refusal_not_a_bare_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
            launcher.provision_argv = ("sh", "-c", "echo 'lockfile is stale' >&2; exit 3")
            launcher.provision_timeout_s = 30.0
            with self.assertRaises(lch.LaunchRefused) as caught:
                launcher.provision(Path(tmp))
        exc = caught.exception
        self.assertIs(exc.refusal, lch.LaunchRefusal.PROVISION_FAILED)
        self.assertTrue(str(exc).startswith("LAUNCH_REFUSED:PROVISION_FAILED:"))
        self.assertIn("lockfile is stale", str(exc))
        # Raised before any herdr call, so §8.3's quiesce must not go looking
        # for a pane; and the command is the deployment's, so another attempt
        # cannot change the answer.
        self.assertFalse(exc.pane_created)
        self.assertTrue(exc.deterministic)
        self.assertIsInstance(exc.__cause__, provisioning.ReviewProvisioningError)
        self.assertEqual(exc.__cause__.returncode, 3)

    def test_the_review_path_still_sees_the_same_typed_error(self) -> None:
        # One error class for every site: the code review imports it from
        # `provisioning`, and its tests keep addressing it as `cr.ReviewProvisioningError`.
        self.assertIs(cr.ReviewProvisioningError, provisioning.ReviewProvisioningError)
        self.assertIs(cr.provision_tree, provisioning.provision_tree)


class AProvisioningGapIsRefusedAtRoundOne(_FactoryFixture):
    """Real resolver, real probe, a vitest that fails the way vitest 3 does."""

    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.root / "bin"
        _executable(bin_dir / "vitest", _FAKE_VITEST)
        patch = mock.patch.dict(os.environ, {"PATH": str(bin_dir)})
        patch.start()
        self.addCleanup(patch.stop)
        (self.tree / "vitest.config.ts").write_text(
            'import { defineConfig } from "vitest/config";\nexport default defineConfig({});\n',
            encoding="utf-8",
        )

    def test_a_provision_argv_that_installs_nothing_for_js_refuses_the_run(self) -> None:
        # The deployment's command: exits 0 and leaves no `node_modules`,
        # which is exactly what FDAdb's did.
        self.factory._provision_argv = ("/bin/sh", "-c", "true")

        with self.assertRaises(RunnerPreflightRefused) as caught:
            self.factory._assert_runners_usable()

        message = str(caught.exception)
        self.assertIn("ERR_MODULE_NOT_FOUND", message)
        self.assertIn("vitest", message)
        # The measured tree is kept and named, and nothing repaired it.
        self.assertIn(str(self.tree), message)
        self.assertFalse((self.tree / "node_modules").exists())

    def test_the_same_tree_passes_when_provisioning_installs_the_runner(self) -> None:
        self.factory._provision_argv = (
            "/bin/sh",
            "-c",
            "/bin/mkdir -p node_modules/vitest && echo '{}' > node_modules/vitest/package.json",
        )

        self.factory._assert_runners_usable()

        # A capable probe discards its scratch tree.
        self.assertFalse(self.tree.exists())


if __name__ == "__main__":
    unittest.main()
