"""A second attend dispatch over a tree and a pane an earlier one left.

The earlier dispatch's tree was a plain directory and its Claude pane was
still running in it. Converting that tree into a worktree renamed it aside,
the pane's working directory went with the rename, and every later launch
found the stable operator agent bound to `.checkout-<hex>` and refused
`BINDING_MISMATCH:<...>/.checkout-<hex>!=<...>/checkout`.

Three parts, all on the path: the tree is converted where it stands, so a
live agent's directory is never moved; an attend agent whose pane is bound
to another tree is closed and replaced rather than refused, because an
attend role holds no session a later dispatch needs; and each attend
dispatch closes the pane it launched once it is answered or has failed, so
no later dispatch finds one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import attend as att  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules.scheduler import LaneContext, LaunchFailed  # noqa: E402
from adw_modules.scheduler_types import (  # noqa: E402
    LaneProjection,
    LaneStage,
    WaitReason,
    lane_projection_digest,
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class APlainTreeIsConvertedWhereItStands(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name).resolve()
        self.repo = root / "product"
        _git("init", "-q", "-b", "main", str(self.repo))
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git("-C", str(self.repo), "add", "seed.txt")
        _git(
            "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "-m", "seed",
        )
        self.head = _git("-C", str(self.repo), "rev-parse", "HEAD")
        self.actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        self.actor.target = type(
            "T", (), {"target_repository_root": str(self.repo)}
        )()
        self.actor._base_sha = lambda ctx, role: self.head  # type: ignore[method-assign]
        self.cwd = root / "operator" / "checkout"
        self.kept = self.cwd / "revisions" / "r2.round1.ir.json"
        self.kept.parent.mkdir(parents=True)
        self.kept.write_text("{}", encoding="utf-8")

    def _assert_worktree(self) -> None:
        listed = _git("-C", str(self.repo), "worktree", "list", "--porcelain")
        self.assertIn("worktree {0}".format(self.cwd), listed)
        self.assertNotIn(".checkout-", listed)
        self.assertEqual(
            Path(_git("-C", str(self.cwd), "rev-parse", "--git-common-dir")).resolve(),
            (self.repo / ".git").resolve(),
        )
        self.assertFalse((self.cwd / "seed.txt").exists())
        self.assertEqual(self.kept.read_text(), "{}")
        self.assertEqual(
            sorted(p.name for p in self.cwd.parent.iterdir()), ["checkout"]
        )

    def test_the_directory_a_live_agent_sits_in_is_never_moved(self) -> None:
        before = os.stat(self.cwd).st_ino
        # A process whose working directory is the tree: the agent's pane.
        sitting = subprocess.Popen(
            ["sleep", "30"], cwd=str(self.cwd)
        )
        self.addCleanup(sitting.kill)
        self.actor._attend_checkout(None, self.cwd)  # type: ignore[arg-type]
        self.assertEqual(os.stat(self.cwd).st_ino, before)
        self._assert_worktree()

    def test_a_tree_that_is_already_a_worktree_is_left_alone(self) -> None:
        self.actor._attend_checkout(None, self.cwd)  # type: ignore[arg-type]
        before = os.stat(self.cwd / ".git").st_ino
        self.actor._attend_checkout(None, self.cwd)  # type: ignore[arg-type]
        self.assertEqual(os.stat(self.cwd / ".git").st_ino, before)
        self._assert_worktree()


def _launcher() -> lch.HerdrLauncher:
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher._handles_lock = threading.RLock()
    launcher._handles = {}
    launcher._tailers = {}
    launcher._proven_absent = {}
    launcher._role_handles = {}
    launcher._tabs = {}
    return launcher


class AnAttendAgentBoundElsewhereIsReplaced(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name).resolve()
        self.moved = root / "operator" / ".checkout-b60504fe"
        self.current = root / "operator" / "checkout"
        self.current.mkdir(parents=True)
        self.calls: list[tuple[str, ...]] = []
        self.closed = False

    def _spec(self, role: str) -> lch.LaunchSpec:
        return lch.LaunchSpec(
            correlation_token=lch.role_session_token("run-1", "lane-a", role),
            worktree=self.current,
            prompt_path=self.current / "prompt.json",
            envelope_path=self.current / "envelope.json",
            route="claude",
            model="opus",
            effort="medium",
            profile="",
            session_dir=self.current.parent / "session",
            lane_key="lane-a",
            pane_role=role,
            run_id="run-1",
        )

    def _herdr(self, status: str):
        def fake(*args: str, **kwargs: object) -> dict:
            del kwargs
            self.calls.append(args)
            if args[:2] == ("agent", "get"):
                if self.closed:
                    raise lch.HerdrCallError("missing", lch.AGENT_NOT_FOUND)
                return {"result": {"agent": {
                    "name": args[2], "pane_id": "w25B:p4", "agent_status": status,
                }}}
            if args[:2] == ("pane", "get"):
                return {"result": {"pane": {
                    "pane_id": "w25B:p4", "cwd": str(self.moved),
                }}}
            if args[:2] == ("pane", "close"):
                self.closed = True
                return {"result": {"type": "ok"}}
            raise AssertionError(args)

        return fake

    def test_the_operator_pane_left_in_a_moved_tree_is_closed(self) -> None:
        for status in ("idle", "blocked"):
            with self.subTest(status=status):
                self.calls.clear()
                self.closed = False
                launcher = _launcher()
                launcher._herdr = self._herdr(status)  # type: ignore[method-assign]
                for role in lch.ATTEND_PANE_ROLES:
                    self.closed = False
                    self.assertIsNone(
                        launcher._reconnect_live_agent(self._spec(role), {})
                    )
                    self.assertIn(("pane", "close", "w25B:p4"), self.calls)

    def test_a_lane_role_bound_elsewhere_still_refuses(self) -> None:
        launcher = _launcher()
        launcher._herdr = self._herdr("idle")  # type: ignore[method-assign]
        with self.assertRaises(lch.LaunchRefused) as raised:
            launcher._reconnect_live_agent(self._spec("tester"), {})
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertNotIn(("pane", "close", "w25B:p4"), self.calls)


class CloseAgent(unittest.TestCase):
    """`HerdrLauncher.close_agent`: the pane a stable agent sits in, closed."""

    def setUp(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.closed = False
        self.token = lch.role_session_token("run-1", "lane-a", "operator")

    def _herdr(self, *args: str, **kwargs: object) -> dict:
        del kwargs
        self.calls.append(args)
        if args[:2] == ("agent", "get"):
            if self.closed:
                raise lch.HerdrCallError("missing", lch.AGENT_NOT_FOUND)
            return {"result": {"agent": {
                "name": args[2], "pane_id": "w25B:p4", "agent_status": "blocked",
            }}}
        if args[:2] == ("pane", "close"):
            self.closed = True
            return {"result": {"type": "ok"}}
        raise AssertionError(args)

    def test_a_blocked_start_left_with_no_handle_is_closed_by_name(self) -> None:
        launcher = _launcher()
        launcher._herdr = self._herdr  # type: ignore[method-assign]
        self.assertTrue(launcher.close_agent(self.token, {}))
        self.assertIn(("pane", "close", "w25B:p4"), self.calls)

    def test_no_agent_closes_nothing(self) -> None:
        self.closed = True
        launcher = _launcher()
        launcher._herdr = self._herdr  # type: ignore[method-assign]
        self.assertFalse(launcher.close_agent(self.token, {}))
        self.assertNotIn("close", {call[1] for call in self.calls})


def _ctx(run_id: str, lane_id: str) -> LaneContext:
    digest = "ab" * 32
    return LaneContext(
        run_id=run_id,
        lane=LaneProjection(
            lane_id=lane_id,
            needs=(),
            spec_digest=digest,
            declared_outputs=("a.txt",),
            lane_projection_digest=lane_projection_digest(digest, (), ("a.txt",)),
            public_acceptance=("a.txt is written",),
        ),
        plan_revision=1,
        plan_digest="cd" * 32,
        plan_artifact_ref="plan:x",
        input_digest="11" * 32,
        stage=LaneStage.WAITING_FOR_USER,
        artifacts={},
    )


def _evidence(lane_id: str) -> att.LaneEvidence:
    return att.LaneEvidence(
        lane_id=lane_id,
        wait_reason=WaitReason.NO_PROGRESS.value,
        stage=LaneStage.WAITING_FOR_USER.value,
        round_number=3,
        public_contract={"acceptance_criteria": ["a.txt is written"]},
        reviews=(),
        failure_output=(),
        lane_gates="",
        suite_files={},
    )


class EachAttendDispatchClosesThePaneItLaunched(unittest.TestCase):
    """Answered or failed, the dispatcher's one `finally` closes its pane.

    A launch that stopped `blocked` keeps its pane on purpose, so a failed
    attend launch used to leave one open for the next dispatch to reconnect
    to; the operator pane left on the live run was exactly that.
    """

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name).resolve()
        self.ir = self.root / "current.ir.json"
        self.ir.write_text("{}", encoding="utf-8")
        self.revision = self.root / "revision.ir.json"
        self.revision.write_text("{}", encoding="utf-8")
        self.closed: list[str] = []
        self.ctx = _ctx("run-1", "lane-a")

    def _actor(self, outcome: object) -> maestro.HerdrStageActor:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.worktrees = self.root / "worktrees"
        actor._roles = {}
        actor.launcher = type(
            "L", (), {"close_agent": lambda _, token, env: self.closed.append(token)}
        )()
        actor._attend_checkout = (  # type: ignore[method-assign]
            lambda ctx, cwd: cwd.mkdir(parents=True, exist_ok=True)
        )
        actor._bind_checkout = lambda *args: None  # type: ignore[method-assign]
        actor._say = lambda *args: None  # type: ignore[method-assign]

        def launch(ctx, role, cwd, extra, **kwargs):
            del ctx, role, extra, kwargs
            if isinstance(outcome, BaseException):
                raise outcome
            return {"ok": True}, object(), cwd

        actor._launch = launch  # type: ignore[method-assign]
        return actor

    def _dispatch(self, actor: maestro.HerdrStageActor, role: str) -> None:
        if role == "operator":
            actor.attend_operator(self.ctx, att.OperatorRequest(
                run_id="run-1", plan_revision=1, next_plan_revision=2,
                lanes=(_evidence("lane-a"),), ir_path=str(self.ir),
                revision_out_path=str(self.root / "out.ir.json"),
                amendment_rules="", allowed_lane_ids=("lane-a",),
            ))
        else:
            actor.attend_reviewer(self.ctx, att.ReviewRequest(
                run_id="run-1", plan_revision=1, next_plan_revision=2,
                review_round=1, lanes=(_evidence("lane-a"),),
                current_ir_path=str(self.ir), revision_ir_path=str(self.revision),
                question_surface="{}", rationales=(),
                findings_out_path=str(self.root / "findings.json"),
                amendment_rules="", allowed_lane_ids=("lane-a",),
                revision_sha256=att.sha256_hex(self.revision.read_bytes()),
            ))

    def test_answered_and_failed_dispatches_both_close(self) -> None:
        for role in lch.ATTEND_PANE_ROLES:
            token = lch.role_session_token("run-1", "lane-a", role)
            with self.subTest(role=role, outcome="answered"):
                self.closed.clear()
                self._dispatch(self._actor(None), role)
                self.assertEqual(self.closed, [token])
            with self.subTest(role=role, outcome="failed"):
                self.closed.clear()
                with self.assertRaises(LaunchFailed):
                    self._dispatch(
                        self._actor(LaunchFailed("AGENT_START_BLOCKED:pane w25B:p4")),
                        role,
                    )
                self.assertEqual(self.closed, [token])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
