"""The attend operator and reviewer launched through a real Herdr, end to end.

Every other attend test replaces the launcher, and three launch defects
reached a live run one after another through exactly that gap: a tree
provisioned like a checkout, a tree Claude Code would not trust, and a tree
moved out from under a live agent's pane. This drives the real dispatchers,
`HerdrStageActor.attend_operator` and `attend_reviewer`, through the real
`HerdrLauncher` into a real pane, against a scratch target repository and a
scratch runtime root, and stops each dispatch once the agent is ready: the
answer the agent would write is not what is under test.

For each role, two shapes of a second attempt over a stale tree:

* `A` -- an earlier dispatch left a plain tree with a live agent sitting in
  it. The next dispatch converts the tree in place and reconnects to the
  agent, which must still be bound to the tree, and closes its pane.
* `B` -- the same plain tree, converted the way the runtime used to convert
  it (renamed aside, worktree added, children moved back), which carries the
  agent's pane to the temporary name. The next dispatch must close that
  agent, start a ready one in the tree, and close its pane.

Opt-in, because it starts real agents and opens a Herdr Space:
`MAESTRO_REAL_HERDR=1` inside a Herdr session (`HERDR_ENV=1`), with `herdr`,
`claude` and `omp` on PATH. Anywhere else, including CI, it skips. It opens
one Space for the whole module, and closes it with `--group` at teardown.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import attend as att  # noqa: E402
from adw_modules import git_publication as gitpub  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import route_receipts as rr  # noqa: E402
from adw_modules.scheduler import LaneContext, LaunchFailed  # noqa: E402
from adw_modules.scheduler_types import (  # noqa: E402
    LaneProjection,
    LaneStage,
    WaitReason,
    lane_projection_digest,
)

_ENABLED = (
    os.environ.get("MAESTRO_REAL_HERDR") == "1"
    and os.environ.get("HERDR_ENV") == "1"
    and not os.environ.get("CI")
    and all(shutil.which(tool) for tool in ("herdr", "claude", "omp"))
)

_ROLE_ROUTES = {
    "tester": {"route": "omp", "profile": "openai-astra"},
    "test-reviewer": {"route": "omp", "profile": "openai-astra"},
    "builder": {"route": "claude", "model": "haiku", "effort": "low"},
    "code-reviewer": {"route": "omp", "profile": "openai-astra"},
    "integration-reviewer": {"route": "omp", "profile": "openai-astra"},
}
#: The deployment shape: the author on claude, the reviewer on omp, each
#: canonicalized as the config loader does.
_OPERATOR_ROUTE = maestro._canonical_role_route(
    {"route": "claude", "model": "haiku", "effort": "low"}, "attend.route"
)
_REVIEWER_ROUTE = maestro._canonical_role_route(
    {"route": "omp", "profile": "openai-astra"}, "attend.reviewer_route"
)

_READY_S = 180.0


class _Ready(Exception):
    """Raised from the envelope wait once the agent is proven ready."""


class _Blocked(Exception):
    """A start Herdr refused because the agent waits on a startup prompt."""


def _herdr(*args: str) -> dict[str, Any]:
    done = subprocess.run(
        ["herdr", *args], capture_output=True, text=True, timeout=60
    )
    if done.returncode != 0:
        raise RuntimeError("herdr {0}: {1}".format(" ".join(args), done.stderr))
    return json.loads(done.stdout or "{}")


def _agent(name: str) -> dict[str, Any] | None:
    try:
        payload = _herdr("agent", "get", name)
    except RuntimeError:
        return None
    return lch._agent_record(payload)


def _pane(pane_id: str) -> dict[str, Any]:
    return _herdr("pane", "get", pane_id)["result"]["pane"]


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError("git {0}: {1}".format(" ".join(args), done.stderr))
    return done.stdout.strip()


def _lane(lane_id: str) -> LaneProjection:
    digest = "ab" * 32
    return LaneProjection(
        lane_id=lane_id,
        needs=(),
        spec_digest=digest,
        declared_outputs=("a.txt",),
        lane_projection_digest=lane_projection_digest(digest, (), ("a.txt",)),
        public_acceptance=("a.txt is written",),
    )


def _evidence(lane_id: str) -> att.LaneEvidence:
    return att.LaneEvidence(
        lane_id=lane_id,
        wait_reason=WaitReason.NO_PROGRESS.value,
        stage=LaneStage.WAITING_FOR_USER.value,
        round_number=3,
        public_contract={"acceptance_criteria": ["a.txt is written"]},
        reviews=({"kind": "CODE_REVIEW", "verdict": "REVISE"},),
        failure_output=("1 failed",),
        lane_gates="stage WAITING_FOR_USER",
        suite_files={"tests/test_a.py": "def test_a():\n    assert True\n"},
    )


@unittest.skipUnless(_ENABLED, "needs MAESTRO_REAL_HERDR=1 in a Herdr session")
class AttendLaunchesThroughRealHerdr(unittest.TestCase):
    #: The one Space this module opens. Every lane child the launcher hangs
    #: under it goes with it when it is closed with `--group`.
    source_space = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.home = Path(tempfile.mkdtemp(prefix="attend-real-", dir="/tmp")).resolve()
        cls.product = cls.home / "product"
        _git("init", "-q", "-b", "main", str(cls.product))
        (cls.product / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git("-C", str(cls.product), "add", "seed.txt")
        _git(
            "-C", str(cls.product), "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "-m", "seed",
        )
        cls.head = _git("-C", str(cls.product), "rev-parse", "HEAD")
        # The operator's own Space on the repository, where `run attend` is
        # invoked from: the launcher hangs every lane child under it.
        created = _herdr(
            "workspace", "create", "--cwd", str(cls.product),
            "--label", "attend-real-source", "--no-focus",
        )
        cls.source_space = str(created["result"]["workspace"]["workspace_id"])

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls.source_space:
                subprocess.run(
                    ["herdr", "workspace", "close", cls.source_space, "--group"],
                    capture_output=True, timeout=60,
                )
        finally:
            shutil.rmtree(cls.home, True)

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=self.home)).resolve()
        #: The attend panes' closes, run by the case once it has looked at
        #: the pane; see `_actor`.
        self.closes: list[Any] = []
        self.addCleanup(self._close_attend_panes)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.target = gitpub.bind_target_worktree(self.product, "refs/heads/main")
        self.run_id = uuid.uuid4().hex
        self.ir = self.root / "current.ir.json"
        self.ir.write_text('{"plan_id": "scratch", "revision": 1}', encoding="utf-8")
        self.events: list[str] = []
        #: The agent session and pane cwd each ready dispatch found, in order.
        self.sessions: list[str] = []
        self.bound: list[Path] = []

    # -- harness ---------------------------------------------------------

    def _close_attend_panes(self) -> None:
        """Run the dispatchers' own pane closes, deferred until now."""
        while self.closes:
            self.closes.pop(0)()

    def _actor(self) -> maestro.HerdrStageActor:
        """A fresh actor and launcher, the way every `run resume` builds them."""
        admitted = rr.AdmittedRouteSet(
            frozenset({"claude", "omp"}),
            _verification_token=rr._ADMISSION_CONSTRUCTION_TOKEN,
        )
        launcher = lch.HerdrLauncher(
            herdr_path=Path("herdr"),
            omp_path=Path("omp"),
            claude_path=Path("claude"),
            admitted_routes=admitted,
            workspace_label=lch.workspace_label_for(
                maestro._project_identity(self.target), self.run_id
            ),
            parent_workspace_id=self.source_space,
        )
        actor = maestro.HerdrStageActor(
            launcher,
            self.state,
            self.target,
            _ROLE_ROUTES,
            operator_route=_OPERATOR_ROUTE,
            attend_reviewer_route=_REVIEWER_ROUTE,
        )
        actor._await_envelope = self._await_ready  # type: ignore[method-assign]
        # The dispatcher closes its pane in its `finally`; deferred here so a
        # stale agent can be set up, and a trust prompt in this untrusted
        # scratch repository approved, before the real close runs.
        close = actor._close_attend_pane
        actor._close_attend_pane = (  # type: ignore[method-assign]
            lambda ctx, role, cwd: self.closes.append(lambda: close(ctx, role, cwd))
        )
        return actor

    def _ctx(self, lane_id: str) -> LaneContext:
        return LaneContext(
            run_id=self.run_id,
            lane=_lane(lane_id),
            plan_revision=1,
            plan_digest="cd" * 32,
            plan_artifact_ref="plan:x",
            input_digest="11" * 32,
            stage=LaneStage.WAITING_FOR_USER,
            artifacts={},
            builder_base_sha=self.head,
        )

    def _await_ready(self, handle: Any, envelope: Path, role: str, lane_id: str):
        """Stand in for the envelope wait: prove the agent ready, then stop.

        Ready is Herdr's own record: the stable agent exists, is not
        `blocked` (a trust or permission prompt), and its pane is bound to
        the tree the dispatch prepared.
        """
        del envelope
        deadline = time.monotonic() + _READY_S
        status = ""
        while time.monotonic() < deadline:
            agent = _agent(handle.agent_name)
            status = lch._agent_status_of(agent) if agent else "<absent>"
            if status == "blocked":
                raise AssertionError("{0} {1}: agent blocked".format(lane_id, role))
            if status in ("working", "idle", "done"):
                break
            time.sleep(2.0)
        else:
            raise AssertionError("{0} {1}: never ready ({2})".format(lane_id, role, status))
        pane = _pane(handle.pane_id)
        session = (agent or {}).get("agent_session") or {}
        self.sessions.append(str(session.get("value") or ""))
        self.bound.append(Path(str(pane["cwd"])).resolve())
        event = "{0} {1}: agent {2} {3} in pane {4} cwd {5}".format(
            lane_id, role, handle.agent_name, status, handle.pane_id, pane["cwd"]
        )
        self.events.append(event)
        print(event, flush=True)
        raise _Ready(handle.pane_id)

    def _quiet(self, name: str) -> None:
        """Interrupt the agent's turn and wait for it to sit idle.

        The live failure's stale agent sat idle in its tree; an agent still
        working on a real operator prompt would be minutes from that.
        """
        agent = _agent(name)
        assert agent is not None
        pane_id = str(agent["pane_id"])
        seen: list[str] = []
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            agent = _agent(name)
            status = lch._agent_status_of(agent) if agent else "<absent>"
            seen.append(status)
            if status in ("idle", "done"):
                print("{0} quiet after {1}".format(name, seen), flush=True)
                return
            subprocess.run(
                ["herdr", "pane", "send-keys", pane_id, "esc"],
                capture_output=True, timeout=30,
            )
            time.sleep(4.0)
        screen = subprocess.run(
            ["herdr", "pane", "read", pane_id], capture_output=True, text=True
        ).stdout[-1500:]
        raise AssertionError(
            "{0} never went idle: {1}\n{2}".format(name, seen, screen)
        )

    def _dispatch(self, actor: maestro.HerdrStageActor, role: str, lane_id: str) -> str:
        """Dispatch once; a start blocked on Claude's folder-trust prompt is
        approved, as the refusal's remedy says, and dispatched again, as
        attend's own retry does.

        Claude Code reads trust for a worktree from its repository, and this
        scratch repository is not one the user trusted, so a fresh start in it
        can stop at the prompt (the deployment's repository is trusted).
        """
        try:
            return self._dispatch_once(actor, role, lane_id)
        except _Blocked as blocked:
            pane_id, screen = blocked.args
            self.assertIn("trust this folder", screen)
            self.events.append(
                "{0} {1}: AGENT_START_BLOCKED at the trust prompt in pane {2}; "
                "approved, dispatched again".format(lane_id, role, pane_id)
            )
            print(self.events[-1], flush=True)
            subprocess.run(["herdr", "pane", "send-keys", pane_id, "down"], timeout=30)
            subprocess.run(["herdr", "pane", "send-keys", pane_id, "enter"], timeout=30)
            time.sleep(5.0)
            return self._dispatch_once(self._actor(), role, lane_id)

    def _dispatch_once(
        self, actor: maestro.HerdrStageActor, role: str, lane_id: str
    ) -> str:
        ctx = self._ctx(lane_id)
        cwd = actor._role_dir(ctx, role) / "checkout"
        revisions = cwd / "revisions"
        if role == "operator":
            request: Any = att.OperatorRequest(
                run_id=self.run_id, plan_revision=1, next_plan_revision=2,
                lanes=(_evidence(lane_id),), ir_path=str(self.ir),
                revision_out_path=str(revisions / "r2.round1.ir.json"),
                amendment_rules="Edit only the parked lane.",
                allowed_lane_ids=(lane_id,),
            )
            call = actor.attend_operator
        else:
            revision = self.root / "{0}.r2.ir.json".format(lane_id)
            revision.write_bytes(b'{"plan_id": "scratch", "revision": 2}')
            request = att.ReviewRequest(
                run_id=self.run_id, plan_revision=1, next_plan_revision=2,
                review_round=1, lanes=(_evidence(lane_id),),
                current_ir_path=str(self.ir), revision_ir_path=str(revision),
                question_surface="{}", rationales=(),
                findings_out_path=str(revisions / "r2.round1.findings.json"),
                amendment_rules="Edit only the parked lane.",
                allowed_lane_ids=(lane_id,),
                revision_sha256=att.sha256_hex(revision.read_bytes()),
            )
            call = actor.attend_reviewer
        try:
            call(ctx, request)
        except _Ready as ready:
            return str(ready.args[0])
        except LaunchFailed as failed:
            pane = re.search(r"pane (w\w+:p\w+)", str(failed))
            screen = subprocess.run(
                ["herdr", "pane", "read", pane.group(1)] if pane else ["true"],
                capture_output=True, text=True,
            ).stdout[-2000:]
            if pane and str(failed).startswith("AGENT_START_BLOCKED"):
                raise _Blocked(pane.group(1), screen) from failed
            raise AssertionError("{0}\n{1}".format(failed, screen)) from failed
        raise AssertionError("{0} {1}: dispatch returned".format(lane_id, role))

    def _plain_tree_with_live_agent(self, role: str, lane_id: str) -> tuple[Path, str]:
        """What a pre-worktree dispatch left: a plain tree and an idle agent in it."""
        actor = self._actor()
        ctx = self._ctx(lane_id)
        cwd = actor._role_dir(ctx, role) / "checkout"
        (cwd / "revisions").mkdir(parents=True)
        (cwd / "revisions" / "r2.round0.ir.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(actor, "_attend_checkout"):
            pane_id = self._dispatch(actor, role, lane_id)
        self.assertFalse((cwd / ".git").exists())
        self.assertEqual(Path(_pane(pane_id)["cwd"]).resolve(), cwd.resolve())
        token = lch.role_session_token(self.run_id, lane_id, role)
        self._quiet(lch.agent_name_for(token))
        return cwd, pane_id

    def _assert_bound(self, role: str, lane_id: str, pane_id: str) -> None:
        cwd = self.state / "worktrees" / self.run_id / lane_id / role / "checkout"
        self.assertEqual(Path(_pane(pane_id)["cwd"]).resolve(), cwd.resolve())
        self.assertEqual(self.bound[-1], cwd.resolve())
        self._close_attend_panes()
        token = lch.role_session_token(self.run_id, lane_id, role)
        self.assertIsNone(_agent(lch.agent_name_for(token)))
        self.assertEqual(
            Path(_git("-C", str(cwd), "rev-parse", "--git-common-dir")).resolve(),
            (self.product / ".git").resolve(),
        )
        self.assertEqual(
            (cwd / "revisions" / "r2.round0.ir.json").read_text(), "{}"
        )
        self.assertFalse((cwd / "seed.txt").exists())

    # -- cases -----------------------------------------------------------

    def _a_live_agent_in_a_plain_tree_is_reconnected(self, role: str) -> None:
        lane_id = "lane-a-{0}".format(role)
        cwd, stale_pane = self._plain_tree_with_live_agent(role, lane_id)
        inode = os.stat(cwd).st_ino
        pane_id = self._dispatch(self._actor(), role, lane_id)
        self.assertEqual(os.stat(cwd).st_ino, inode)
        # Reconnected: the same pane and the same agent session, not a new
        # agent started in a reused pane.
        self.assertEqual(pane_id, stale_pane)
        self.assertTrue(self.sessions[-1])
        self.assertEqual(self.sessions[-1], self.sessions[0])
        self._assert_bound(role, lane_id, pane_id)

    def _an_agent_carried_off_by_a_moved_tree_is_replaced(self, role: str) -> None:
        lane_id = "lane-b-{0}".format(role)
        cwd, stale_pane = self._plain_tree_with_live_agent(role, lane_id)
        # The conversion this runtime used to make, step for step.
        held = cwd.parent / ".checkout-{0}".format(uuid.uuid4().hex)
        cwd.rename(held)
        for attempt in range(5):
            # A live Claude agent recreates its own working path by name, so
            # the old conversion's `worktree add` could find it occupied.
            if cwd.exists():
                shutil.rmtree(cwd)
            try:
                _git(
                    "-C", str(self.product), "worktree", "add", "--detach",
                    "--no-checkout", str(cwd), self.head,
                )
                break
            except AssertionError:
                if attempt == 4:
                    raise
                time.sleep(1.0)
        for child in held.iterdir():
            child.rename(cwd / child.name)
        held.rmdir()
        self.assertEqual(Path(_pane(stale_pane)["cwd"]), held)
        pane_id = self._dispatch(self._actor(), role, lane_id)
        self.assertNotEqual(pane_id, stale_pane)
        self._assert_bound(role, lane_id, pane_id)

    def test_operator_reconnects_to_its_agent_over_a_converted_tree(self) -> None:
        self._a_live_agent_in_a_plain_tree_is_reconnected("operator")

    def test_operator_replaces_an_agent_a_moved_tree_carried_off(self) -> None:
        self._an_agent_carried_off_by_a_moved_tree_is_replaced("operator")

    def test_reviewer_reconnects_to_its_agent_over_a_converted_tree(self) -> None:
        self._a_live_agent_in_a_plain_tree_is_reconnected(maestro._ATTEND_REVIEWER_ROLE)

    def test_reviewer_replaces_an_agent_a_moved_tree_carried_off(self) -> None:
        self._an_agent_carried_off_by_a_moved_tree_is_replaced(
            maestro._ATTEND_REVIEWER_ROLE
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
