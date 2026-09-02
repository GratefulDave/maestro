"""A plan revision replaces the contract; it has to replace the reader too.

`HerdrStageActor` keeps one long-lived agent session per role and resubmits
into it turn after turn. That is the right shape while the lane's contract is
fixed and the wrong shape the moment it is not: on run f50638ab a code reviewer
went from turn 46 to turn 51 across an amendment, in one continuous context,
and kept quoting the acceptance sentence the amendment had already corrected.
The document was fixed and the reader was not.

So the role key carries the lane's `spec_digest`. Keying it -- rather than
special-casing the `amend` verb -- is what makes a fresh session fall out of
every path that changes a spec. Two things have to hold beside it:

  * the superseded pane is closed, because the launcher's own role registry is
    keyed by `(lane_key, pane_role)` with no digest in it and will otherwise
    adopt the live agent and resubmit into the very context window the new key
    exists to leave behind; and
  * the checkout is bound by path and survives, with its candidate commits,
    because a revision changes what the lane must satisfy and not what the
    lane has already built.

The fake below is faithful to those two launcher behaviours specifically: it
adopts a live `(lane, role)` agent on `launch`, and `cancel` drops it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules.scheduler import LaneContext  # noqa: E402
from adw_modules.scheduler_types import (  # noqa: E402
    LaneProjection,
    LaneStage,
    lane_projection_digest,
)

_ROLE_ROUTES: Mapping[str, Mapping[str, str]] = {
    "tester": {"route": "omp", "profile": "grok"},
    "test-reviewer": {"route": "omp", "profile": "openai-performance"},
    "builder": {"route": "claude", "model": "opus", "effort": "high"},
    "code-reviewer": {"route": "omp", "profile": "openai-performance"},
    "integration-reviewer": {"route": "omp", "profile": "openai-performance"},
}

_SPEC_A = "ab" * 32
_SPEC_B = "cd" * 32


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "factory@example.test")
    _git(path, "config", "user.name", "factory")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-m", "seed")
    return _git(path, "rev-parse", "HEAD")


def _lane(
    *,
    lane_id: str = "lane-a",
    spec_digest: str = _SPEC_A,
    outputs: tuple[str, ...] = ("a.txt",),
) -> LaneProjection:
    return LaneProjection(
        lane_id=lane_id,
        needs=(),
        spec_digest=spec_digest,
        declared_outputs=outputs,
        lane_projection_digest=lane_projection_digest(
            spec_digest, (), outputs, lane_kind=None
        ),
        public_acceptance=("a.txt is written",),
    )


def _tester_ctx(head: str, lane: LaneProjection) -> LaneContext:
    return LaneContext(
        run_id="run-1",
        lane=lane,
        plan_revision=1,
        plan_digest="ef" * 32,
        plan_artifact_ref="plan:x",
        input_digest="11" * 32,
        stage=LaneStage.WRITING_TESTS,
        artifacts={},
        builder_base_sha=head,
    )


def _builder_ctx(head: str, lane: LaneProjection) -> LaneContext:
    return LaneContext(
        run_id="run-1",
        lane=lane,
        plan_revision=1,
        plan_digest="ef" * 32,
        plan_artifact_ref="plan:x",
        input_digest="22" * 32,
        stage=LaneStage.BUILDING,
        artifacts={},
        builder_base_sha=head,
        public_contract={
            "acceptance_criteria": ["a.txt is written"],
            "declared_outputs": ["a.txt"],
        },
        sealed_digest="33" * 32,
    )


class _RoleLauncher:
    """Adopts a live `(lane, role)` agent the way `HerdrLauncher.launch` does."""

    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        cancel_error: Exception | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.cancel_error = cancel_error
        self.launches: list[dict[str, Any]] = []
        self.resubmits: list[object] = []
        self.cancels: list[object] = []
        self.handles: list[SimpleNamespace] = []
        self._live: dict[tuple[str, str], SimpleNamespace] = {}
        self._by_token: dict[str, SimpleNamespace] = {}

    def _write_worktree(self, worktree: Path) -> None:
        for rel, body in self.files.items():
            path = worktree / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def _reply(self, envelope: Path) -> None:
        envelope.parent.mkdir(parents=True, exist_ok=True)
        envelope.write_text(json.dumps({}), encoding="utf-8")

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        key = (str(spec.lane_key or ""), str(spec.pane_role or ""))
        live = self._live.get(key)
        if live is not None and key[1]:
            if spec.prepare_adopted_cwd is not None:
                spec.prepare_adopted_cwd(Path(live.launched_cwd))
            return self.resubmit(
                live, spec.prompt_path, envelope_path=spec.envelope_path
            )
        worktree = Path(spec.worktree)
        tracked = worktree / "a.txt"
        self.launches.append(
            {
                "head": (
                    _git(worktree, "rev-parse", "HEAD")
                    if (worktree / ".git").exists()
                    else ""
                ),
                "lane_key": key[0],
                "pane_role": key[1],
                "spec": spec,
                "tracked_at_launch": (
                    tracked.read_text(encoding="utf-8") if tracked.is_file() else None
                ),
                "worktree": worktree.resolve(),
            }
        )
        self._write_worktree(worktree)
        self._reply(Path(spec.envelope_path))
        handle = SimpleNamespace(
            correlation_token=spec.correlation_token,
            envelope_path=spec.envelope_path,
            lane_key=key[0],
            launched_cwd=worktree.resolve(),
            pane_id="pane-{0}".format(len(self.launches)),
            pane_role=key[1],
        )
        self.handles.append(handle)
        self._by_token[spec.correlation_token] = handle
        if key[1]:
            self._live[key] = handle
        return handle

    def resubmit(
        self,
        handle: SimpleNamespace,
        prompt_path: Path,
        *,
        route: str = "",
        expected_token: str | None = None,
        timeout_s: float = 60.0,
        envelope_path: Path | None = None,
    ) -> SimpleNamespace:
        del prompt_path, route, expected_token, timeout_s
        self.resubmits.append(handle)
        dest = Path(envelope_path or handle.envelope_path)
        self._write_worktree(Path(handle.launched_cwd))
        self._reply(dest)
        handle.envelope_path = dest
        return handle

    def cancel(self, handle: SimpleNamespace, deadline: float) -> None:
        del deadline
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancels.append(handle)
        key = (str(handle.lane_key or ""), str(handle.pane_role or ""))
        if self._live.get(key) is handle:
            self._live.pop(key)
        self._by_token.pop(handle.correlation_token, None)

    def poll(self, handle: object) -> object:
        del handle
        return SimpleNamespace(state=lch.PollState.EXITED)

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s

    def retain(self, handle: SimpleNamespace) -> None:
        if self._by_token.get(handle.correlation_token) is not handle:
            raise lch.LaunchRefused(
                lch.LaunchRefusal.BINDING_MISMATCH, handle.correlation_token
            )


class _Bench:
    """A real product repository, a real state root, and a bound actor."""

    def __init__(self, tmp: str, *, launcher: _RoleLauncher) -> None:
        root = Path(tmp)
        self.product = root / "product"
        self.state = root / "state"
        self.state.mkdir(mode=0o700)
        self.head = _init_repo(self.product)
        self.target = gitpub.bind_target_worktree(self.product, "refs/heads/main")
        self.launcher = launcher
        self.steps: list[tuple[str, str, str]] = []
        self.actor = maestro.HerdrStageActor(
            cast(lch.LauncherAdapter, launcher),
            self.state,
            self.target,
            _ROLE_ROUTES,
        )
        self.actor.step = lambda lane, message, detail="": self.steps.append(
            (lane, message, detail)
        )

    def checkout(self, lane_id: str, role: str) -> Path:
        return (
            self.state / "worktrees" / "run-1" / lane_id / role / "checkout"
        ).resolve()


class TheRoleKeyCarriesTheSpecDigest(unittest.TestCase):
    def test_the_key_is_lane_role_and_digest(self) -> None:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        ctx = _tester_ctx("0" * 40, _lane())
        self.assertEqual(
            actor._role_key(ctx, "code-reviewer"),
            ("lane-a", "code-reviewer", _SPEC_A),
        )

    def test_only_the_digest_moves_when_only_the_digest_moves(self) -> None:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        head = "0" * 40
        base = actor._role_key(_tester_ctx(head, _lane()), "builder")
        revised = actor._role_key(
            _tester_ctx(head, _lane(spec_digest=_SPEC_B)), "builder"
        )
        other_role = actor._role_key(_tester_ctx(head, _lane()), "tester")
        other_lane = actor._role_key(
            _tester_ctx(head, _lane(lane_id="lane-b")), "builder"
        )
        self.assertEqual(base[:2], revised[:2])
        self.assertNotEqual(base, revised)
        self.assertNotEqual(base, other_role)
        self.assertNotEqual(base, other_lane)
        self.assertEqual(len({base, revised, other_role, other_lane}), 4)


class ASteadySpecKeepsItsSession(unittest.TestCase):
    def test_two_dispatches_at_one_digest_reuse_the_session_and_the_pane(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp, launcher=_RoleLauncher())
            ctx = _tester_ctx(bench.head, _lane())
            bench.actor.write_tests(ctx)
            bench.actor.write_tests(ctx)

            self.assertEqual(len(bench.launcher.launches), 1)
            self.assertEqual(len(bench.launcher.resubmits), 1)
            self.assertEqual(bench.launcher.cancels, [])
            self.assertEqual(
                list(bench.actor._roles),
                [("lane-a", "tester", _SPEC_A)],
            )


class ARevisedSpecGetsAFreshSession(unittest.TestCase):
    def test_a_changed_digest_relaunches_and_closes_the_previous_pane_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp, launcher=_RoleLauncher())
            bench.actor.write_tests(_tester_ctx(bench.head, _lane()))
            first = bench.launcher.handles[0]
            bench.actor.write_tests(
                _tester_ctx(bench.head, _lane(spec_digest=_SPEC_B))
            )

            self.assertEqual(len(bench.launcher.launches), 2)
            self.assertEqual(bench.launcher.resubmits, [])
            self.assertEqual(bench.launcher.cancels, [first])
            self.assertIsNot(bench.launcher.handles[1], first)
            self.assertEqual(
                list(bench.actor._roles),
                [("lane-a", "tester", _SPEC_B)],
            )

    def test_a_third_dispatch_does_not_close_the_pane_a_second_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp, launcher=_RoleLauncher())
            bench.actor.write_tests(_tester_ctx(bench.head, _lane()))
            revised = _tester_ctx(bench.head, _lane(spec_digest=_SPEC_B))
            bench.actor.write_tests(revised)
            bench.actor.write_tests(revised)

            self.assertEqual(len(bench.launcher.cancels), 1)
            self.assertEqual(len(bench.launcher.launches), 2)
            self.assertEqual(len(bench.launcher.resubmits), 1)

    def test_the_other_roles_on_the_lane_keep_their_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = _Bench(tmp, launcher=_RoleLauncher(files={"a.txt": "a\n"}))
            bench.actor.write_tests(_tester_ctx(bench.head, _lane()))
            bench.actor.build(_builder_ctx(bench.head, _lane()))
            builder_handle = bench.launcher.handles[1]
            self.assertEqual(builder_handle.pane_role, "builder")

            bench.actor.build(_builder_ctx(bench.head, _lane(spec_digest=_SPEC_B)))

            self.assertEqual(bench.launcher.cancels, [builder_handle])
            self.assertIn(("lane-a", "tester", _SPEC_A), bench.actor._roles)
            self.assertIn(("lane-a", "builder", _SPEC_B), bench.actor._roles)
            self.assertNotIn(("lane-a", "builder", _SPEC_A), bench.actor._roles)


class TheCheckoutOutlivesTheSession(unittest.TestCase):
    def test_the_path_is_the_same_and_the_candidate_commit_is_still_there(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launcher = _RoleLauncher(files={"a.txt": "first\n"})
            bench = _Bench(tmp, launcher=launcher)
            checkout = bench.checkout("lane-a", "builder")

            first = bench.actor.build(_builder_ctx(bench.head, _lane()))
            candidate = str(first["candidate_sha"])
            self.assertTrue(first["changed"])
            self.assertNotEqual(candidate, bench.head)
            self.assertEqual(_git(checkout, "rev-parse", "HEAD"), candidate)

            launcher.files = {"a.txt": "second\n"}
            second = bench.actor.build(
                _builder_ctx(bench.head, _lane(spec_digest=_SPEC_B))
            )

            self.assertEqual(len(launcher.launches), 2)
            self.assertEqual(
                launcher.launches[1]["worktree"],
                launcher.launches[0]["worktree"],
            )
            self.assertEqual(launcher.launches[1]["worktree"], checkout)
            self.assertEqual(launcher.launches[0]["head"], bench.head)
            # The fresh session opens on the work the previous one committed,
            # not on the base the lane started from. `_refresh_builder_checkout`
            # re-commits the declared outputs whenever the tree is not clean --
            # the role's own `.maestro-agent` scratch is untracked and always
            # makes it so -- which is why the sha is re-derived rather than
            # carried. What must survive is the content under it.
            adopted = str(launcher.launches[1]["head"])
            self.assertNotEqual(adopted, bench.head)
            self.assertEqual(_git(checkout, "rev-parse", adopted + "^"), bench.head)
            self.assertEqual(_git(checkout, "show", adopted + ":a.txt"), "first")
            self.assertEqual(launcher.launches[1]["tracked_at_launch"], "first\n")
            self.assertEqual(
                _git(checkout, "cat-file", "-t", candidate + "^{commit}"),
                "commit",
            )
            self.assertNotEqual(str(second["candidate_sha"]), candidate)
            self.assertEqual(_git(checkout, "show", str(second["candidate_sha"]) + ":a.txt"), "second")


class AReviewerReadsTheTreeItWasDispatchedAgainst(unittest.TestCase):
    def test_a_fresh_session_refreshes_the_adopted_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launcher = _RoleLauncher()
            bench = _Bench(tmp, launcher=launcher)
            checkout = bench.checkout("lane-a", "tester")

            bench.actor.write_tests(_tester_ctx(bench.head, _lane()))
            stray = checkout / "stray.txt"
            stray.write_text("left behind\n", encoding="utf-8")

            bench.actor.write_tests(
                _tester_ctx(bench.head, _lane(spec_digest=_SPEC_B))
            )

            self.assertFalse(stray.exists())
            self.assertEqual(launcher.launches[1]["head"], bench.head)


class ARefusedCloseIsReportedAndNotFatal(unittest.TestCase):
    def test_the_lane_still_runs_and_the_step_log_says_the_pane_is_open(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launcher = _RoleLauncher(
                cancel_error=RuntimeError("HERDR_QUIESCENCE_UNPROVEN:tok")
            )
            bench = _Bench(tmp, launcher=launcher)
            bench.actor.write_tests(_tester_ctx(bench.head, _lane()))

            result = bench.actor.write_tests(
                _tester_ctx(bench.head, _lane(spec_digest=_SPEC_B))
            )

            self.assertIn("private_files", result)
            self.assertEqual(launcher.cancels, [])
            # The dispatch still happened. Whether it reached a new pane is the
            # launcher's business: a close that could not be proven leaves the
            # agent adoptable, and this fake models that worst case rather than
            # pretending the refusal had no consequence.
            self.assertEqual(
                len(launcher.launches) + len(launcher.resubmits), 2
            )
            self.assertEqual(
                list(bench.actor._roles),
                [("lane-a", "tester", _SPEC_B)],
            )
            reported = [
                entry for entry in bench.steps if "not closed" in entry[1]
            ]
            self.assertEqual(len(reported), 1)
            self.assertEqual(reported[0][0], "lane-a")
            self.assertIn("tester", reported[0][1])
            self.assertIn("HERDR_QUIESCENCE_UNPROVEN", reported[0][2])


if __name__ == "__main__":
    unittest.main()
