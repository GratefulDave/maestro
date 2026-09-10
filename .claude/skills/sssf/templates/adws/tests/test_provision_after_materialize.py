"""A tree is provisioned after its final materialization, never before one.

FDAdb run d246ae9592be478396ad5146a89f00ae, `lane-faq-producer-tests`: the test
reviewer reported `ERR_MODULE_NOT_FOUND` for `vitest/config` on ten consecutive
rounds and was right every time. Its tree is a materialized private tree, and
the ordering was:

1. `HerdrStageActor._launch` materialized it (`prepare_cwd`), which unlinks
   every child of the tree;
2. `HerdrLauncher.launch` provisioned it, so `node_modules` existed;
3. still inside `launch`, the reused-role-pane and adopted-agent paths called
   `spec.prepare_adopted_cwd`, which materializes the tree *again* -- unlinking
   the `node_modules` step 2 had just installed -- and then the agent started.

The fix is an ordering, not a gate: both preparation paths go through
`HerdrStageActor._prepared_cwd`, which materializes and then provisions, so
provisioning is the last thing done to the tree before an agent reads it. The
launcher provisions nothing; `HerdrLauncher.provision` is deleted.

These cases fail against the pre-fix code: the marker a provisioning run writes
into the tree is absent at the moment the agent would start.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules.scheduler import LaneContext, LaunchFailed
from adw_modules.scheduler_types import LaneProjection, LaneStage, lane_projection_digest


_MARKER = "node_modules_marker"

#: Stands in for a deployment's `provision_argv`. Writes one file into the tree
#: it is run in, the way `npm ci` writes `node_modules`.
_PROVISION_ARGV = (
    sys.executable,
    "-c",
    "import pathlib;pathlib.Path('{0}').write_text('installed')".format(_MARKER),
)

_ROLE_ROUTES: Mapping[str, Mapping[str, str]] = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")
    return _git(path, "rev-parse", "HEAD")


def _lane() -> LaneProjection:
    spec_digest = "ab" * 32
    return LaneProjection(
        lane_id="lane-a",
        needs=(),
        spec_digest=spec_digest,
        declared_outputs=("a.txt",),
        lane_projection_digest=lane_projection_digest(spec_digest, (), ("a.txt",)),
        public_acceptance=("a.txt is written",),
    )


class _RematerializingLauncher:
    """A launcher that re-materializes the tree the way the real one does.

    `HerdrLauncher.launch` calls `spec.prepare_adopted_cwd` on two paths that
    reach a pane which already exists: a reused role pane and an adopted agent.
    This fake takes that path on demand, and records the tree exactly as the
    agent would find it -- after every callback the launcher runs.
    """

    def __init__(self, *, adopted: bool, provision_argv=_PROVISION_ARGV) -> None:
        self.adopted = adopted
        self.provision_argv = tuple(provision_argv)
        self.provision_timeout_s = 120.0
        #: Whether the provisioning marker was present in the tree at the
        #: moment the agent would be started.
        self.marker_at_agent_start: bool | None = None
        self.specs: list[lch.LaunchSpec] = []

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        self.specs.append(spec)
        worktree = Path(spec.worktree).resolve()
        # Where `HerdrLauncher.launch` provisioned until 2026-09-09, and in the
        # order it did: before `prepare_adopted_cwd`, which materializes the
        # tree again. Kept here deliberately so these cases falsify the
        # *ordering* rather than the absence of a provisioner -- against the
        # pre-fix actor this is the only provisioning that happens, and the
        # callback below unlinks it.
        if self.provision_argv and self.adopted:
            subprocess.run(
                list(self.provision_argv), cwd=worktree, capture_output=True
            )
        if self.adopted and spec.prepare_adopted_cwd is not None:
            spec.prepare_adopted_cwd(worktree)
        self.marker_at_agent_start = (worktree / _MARKER).exists()
        spec.envelope_path.parent.mkdir(parents=True, exist_ok=True)
        spec.envelope_path.write_text('{"verdict": "PASS", "findings": []}', encoding="utf-8")
        return SimpleNamespace(
            correlation_token=spec.correlation_token,
            launched_cwd=worktree,
            pane_id="pane-1",
            workspace_id="ws-1",
        )

    def invoking_repository(self, environment: Mapping[str, str]) -> Path | None:
        return None

    def poll(self, handle: object) -> lch.PollResult:
        return lch.PollResult(lch.PollState.IDLE)

    def classify(self, exc: BaseException) -> lch.ErrorClass:
        return lch.classify_error(exc)


class _Bench:
    """`HerdrStageActor._launch` over a real checkout, with a fake launcher."""

    def __init__(self, stack: unittest.TestCase, launcher: _RematerializingLauncher) -> None:
        temporary = tempfile.TemporaryDirectory()
        stack.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        product = self.root / "product"
        self.head = _init_repo(product)
        target = gitpub.bind_target_worktree(product, "refs/heads/main")
        self.actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, launcher), state, target, _ROLE_ROUTES
        )
        self.ctx = LaneContext(
            run_id="run-provision",
            lane=_lane(),
            plan_revision=1,
            plan_digest="cd" * 32,
            plan_artifact_ref="plan:x",
            input_digest="11" * 32,
            stage=LaneStage.REVIEWING_TESTS,
            artifacts={},
            builder_base_sha=self.head,
        )
        self.cwd = self.root / "role" / "checkout"
        self.cwd.mkdir(parents=True)

    def materialize(self, tree: Path) -> None:
        """Stand in for `hv.refresh_materialized_commit`: unlink every child."""
        tree = Path(tree)
        tree.mkdir(parents=True, exist_ok=True)
        for child in tree.iterdir():
            if child.is_dir():
                for inner in child.rglob("*"):
                    if inner.is_file() or inner.is_symlink():
                        inner.unlink()
                continue
            child.unlink()
        (tree / "vitest.config.ts").write_text("export default {};\n", encoding="utf-8")

    def launch(self, role: str = "test-reviewer") -> Any:
        return self.actor._launch(
            self.ctx, role, self.cwd, {}, prepare_cwd=self.materialize
        )


class ProvisioningSurvivesToTheAgent(unittest.TestCase):
    def test_the_reused_pane_path_leaves_the_tree_provisioned(self) -> None:
        launcher = _RematerializingLauncher(adopted=True)
        bench = _Bench(self, launcher)
        bench.launch()
        # The defect: `prepare_adopted_cwd` re-materialized the tree after the
        # launcher provisioned it, so the agent started with nothing installed.
        self.assertIs(launcher.marker_at_agent_start, True)
        self.assertTrue((bench.cwd / _MARKER).exists())

    def test_the_first_launch_path_leaves_the_tree_provisioned(self) -> None:
        launcher = _RematerializingLauncher(adopted=False)
        bench = _Bench(self, launcher)
        bench.launch()
        self.assertIs(launcher.marker_at_agent_start, True)

    def test_a_deployment_that_declares_no_provisioning_is_not_forced_into_one(
        self,
    ) -> None:
        launcher = _RematerializingLauncher(adopted=True, provision_argv=())
        bench = _Bench(self, launcher)
        bench.launch()
        self.assertIs(launcher.marker_at_agent_start, False)


class AFailingProvisionerIsTypedAtBothSites(unittest.TestCase):
    """The refusal the launcher used to raise, raised where provisioning moved.

    `_launch` turns it into `LaunchFailed` with the same `PROVISION_FAILED:`
    text, so nothing downstream reads a bare `RuntimeError` on either path.
    """

    _FAILING = ("sh", "-c", "echo 'lockfile is stale' >&2; exit 3")

    def _assert_refused(self, *, adopted: bool) -> None:
        launcher = _RematerializingLauncher(adopted=adopted, provision_argv=self._FAILING)
        bench = _Bench(self, launcher)
        with self.assertRaises(LaunchFailed) as caught:
            bench.launch()
        self.assertTrue(str(caught.exception).startswith("PROVISION_FAILED:"))
        self.assertIn("lockfile is stale", str(caught.exception))
        self.assertFalse(caught.exception.pane_created)

    def test_the_first_launch_path(self) -> None:
        self._assert_refused(adopted=False)

    def test_the_adopted_path(self) -> None:
        self._assert_refused(adopted=True)


class TheLauncherProvisionsNothing(unittest.TestCase):
    """A named deletion, asserted rather than remembered.

    `HerdrLauncher.provision`, `FakeLauncher.provision` and the
    `LauncherAdapter` protocol member are gone. Leaving the method behind would
    leave a second provisioner whose docstring claimed to be the only one, at
    the one point in the dispatch where provisioning is wiped by the next
    callback.
    """

    def test_no_launcher_carries_a_provision_method(self) -> None:
        for owner in (lch.HerdrLauncher, lch.FakeLauncher, lch.LauncherAdapter):
            self.assertFalse(hasattr(owner, "provision"), owner.__name__)

    def test_the_binding_the_actor_reads_is_still_on_the_launcher(self) -> None:
        # Deleting the method must not delete the deployment's binding: the
        # actor resolves the argv off the launcher it was admitted with.
        launcher = _RematerializingLauncher(adopted=False)
        from adw_modules import scheduler

        actor = SimpleNamespace(launcher=launcher)
        self.assertEqual(
            scheduler._resolved_provision_argv(actor, None), _PROVISION_ARGV
        )
        self.assertEqual(scheduler._resolved_provision_timeout(actor), 120.0)


if __name__ == "__main__":
    unittest.main()
