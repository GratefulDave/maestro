"""A dispatch prepares a tree once; an adopted cwd it already prepared is not
prepared again.

`HerdrStageActor._launch` calls `_prepared_cwd` (materialize, then provision)
on `cwd` up front, then hands the launcher a `prepare_adopted_cwd` callback
that `HerdrLauncher.launch` invokes on the two paths that reach a pane which
already exists: a reused role pane, and an adopted agent. On the reused-pane
path the launcher has already verified `actual == worktree` before calling the
callback, so when the cwd handed back to `prepare_adopted_cwd` is the same
path `_launch` already materialized and provisioned, calling `_prepared_cwd`
again is a byte-identical repeat of the work a few lines up -- on FDAdb this
is `npm ci` + `uv venv`, roughly two minutes, wasted on every reused-pane
dispatch, which is the common case for a long-running lane.

The fix is not a gate: `_launch` remembers the path it prepared for this
dispatch, and `prepare_adopted_cwd` skips materialization and provisioning
only when the adopted path resolves to that same path. A path that differs
(the true adopted-agent case, a pane bound somewhere `_launch` did not
prepare) is still prepared exactly as before, and provisioning stays the last
thing done to it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, cast
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import git_publication as gitpub
from adw_modules import launcher as lch
from adw_modules import provisioning
from adw_modules.scheduler import LaneContext
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


class _AdoptingLauncher:
    """A launcher whose reused-pane path adopts a caller-chosen cwd.

    Mirrors what `HerdrLauncher.launch` does on the reused-role-pane and
    adopted-agent paths: after the ordinary preflight, it calls
    `spec.prepare_adopted_cwd` with the cwd the pane is actually bound to.
    `target_cwd_fn` lets a case choose whether that is the same path the
    dispatch already prepared, or a different one -- exactly the choice
    `HerdrLauncher.launch` makes based on `actual == worktree`.
    """

    def __init__(self, *, target_cwd_fn) -> None:
        self.target_cwd_fn = target_cwd_fn
        self.provision_argv = _PROVISION_ARGV
        self.provision_timeout_s = 120.0
        self.specs: list[lch.LaunchSpec] = []

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        self.specs.append(spec)
        target = self.target_cwd_fn(Path(spec.worktree).resolve())
        if spec.prepare_adopted_cwd is not None:
            spec.prepare_adopted_cwd(target)
        spec.envelope_path.parent.mkdir(parents=True, exist_ok=True)
        spec.envelope_path.write_text(
            '{"verdict": "PASS", "findings": []}', encoding="utf-8"
        )
        return SimpleNamespace(
            correlation_token=spec.correlation_token,
            launched_cwd=target,
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

    def __init__(self, stack: unittest.TestCase, launcher: _AdoptingLauncher) -> None:
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
            run_id="run-once",
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
        self.materialize_calls: list[Path] = []

    def materialize(self, tree: Path) -> None:
        """Stand in for `hv.refresh_materialized_commit`: unlink every child."""
        tree = Path(tree)
        self.materialize_calls.append(tree)
        tree.mkdir(parents=True, exist_ok=True)
        for child in tree.iterdir():
            if child.is_dir():
                for inner in child.rglob("*"):
                    if inner.is_file() or inner.is_symlink():
                        inner.unlink()
                continue
            child.unlink()
        (tree / "vitest.config.ts").write_text("export default {};\n", encoding="utf-8")

    def launch(self, role: str = "test-reviewer"):
        return self.actor._launch(
            self.ctx, role, self.cwd, {}, prepare_cwd=self.materialize
        )


class ADispatchPreparesATreeOnce(unittest.TestCase):
    def test_reused_pane_with_the_same_cwd_is_not_prepared_twice(self) -> None:
        # The launcher hands `prepare_adopted_cwd` the exact same path
        # `_launch` already materialized and provisioned -- the ordinary
        # reused-role-pane path, where the launcher has already asserted
        # `actual == worktree` before calling back.
        launcher = _AdoptingLauncher(target_cwd_fn=lambda worktree: worktree)
        bench = _Bench(self, launcher)
        with mock.patch.object(
            provisioning, "provision_tree", wraps=provisioning.provision_tree
        ) as provision_spy:
            bench.launch()
        self.assertEqual(provision_spy.call_count, 1)
        self.assertEqual(len(bench.materialize_calls), 1)
        # The marker still reaches the agent, from the one provisioning run.
        self.assertTrue((bench.cwd / _MARKER).exists())

    def test_an_adopted_cwd_that_differs_is_still_prepared(self) -> None:
        # `_await_envelope` reads the envelope back from under
        # `handle.launched_cwd`, so a launch whose adopted cwd is a wholly
        # different directory from the envelope's own root cannot complete
        # end to end without also relocating the envelope -- a second,
        # unrelated invariant this fix does not touch. Drive the same-path
        # launch to completion first (exercising the closure exactly as
        # `_launch` builds it), then invoke the captured `prepare_adopted_cwd`
        # a second time with a genuinely different path, the way the
        # adopted-agent path (a stale handle whose recorded cwd is not this
        # dispatch's cwd) would.
        launcher = _AdoptingLauncher(target_cwd_fn=lambda worktree: worktree)
        bench = _Bench(self, launcher)
        with mock.patch.object(
            provisioning, "provision_tree", wraps=provisioning.provision_tree
        ) as provision_spy:
            bench.launch()
            self.assertEqual(provision_spy.call_count, 1)
            self.assertEqual(len(bench.materialize_calls), 1)

            adopted_path = bench.cwd.parent / "adopted-checkout"
            adopted_path.mkdir(parents=True)
            spec = launcher.specs[-1]
            assert spec.prepare_adopted_cwd is not None
            spec.prepare_adopted_cwd(adopted_path)

        # The differing path was prepared too -- materialized, then
        # provisioned, in that order -- on top of the one same-path
        # preparation already counted above.
        self.assertEqual(provision_spy.call_count, 2)
        self.assertEqual(len(bench.materialize_calls), 2)
        self.assertTrue((adopted_path / _MARKER).exists())
        # Provisioning is still the last thing done to the adopted tree.
        self.assertTrue((adopted_path / "vitest.config.ts").exists())


if __name__ == "__main__":
    unittest.main()
