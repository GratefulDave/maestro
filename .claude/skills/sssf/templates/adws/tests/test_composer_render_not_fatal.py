"""A slow composer is not a verdict about the work the agent already declared.

On run f50638ab the tester wrote a valid envelope and the run died anyway:

    File "adws/maestro.py", line 1235, in _await_envelope
      wait(handle)
    ...
    RuntimeError: AGENT_INTERACTIVE_READY_TIMEOUT:maestro-7f5a2a969da8058a

Every gate on that path had already passed. The envelope existed, parsed, and
satisfied `_payload_ok`; the payload was in hand. The wait that killed the run
is the one whose own docstring says the envelope is written *before* the agent
finishes rendering -- it exists so the next prompt is not typed into a busy
composer, and the correction path re-checks that itself before submitting. The
agent was `idle` and `interactive_ready` when the crash was read, sixty seconds
being simply less time than that pane needed to render.

The defect was one of type as much as of policy: `wait_for_interactive_agent`
raised a bare `RuntimeError`, which no caller could distinguish from a genuine
`LaunchRefused` and which therefore no caller caught. Both of its call sites
are courtesy waits after the real work is done.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402


class _Waiter:
    """Only the two members `_await_envelope` reads off a launcher."""

    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.waits = 0

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s
        self.waits += 1
        if self.error is not None:
            raise self.error

    def poll(self, handle: object) -> object:
        del handle
        return SimpleNamespace(state=lch.PollState.EXITED)


class _ExitingRetainer:
    """Launches a declared result, then loses the agent while retaining it."""

    def __init__(self, launch_error: BaseException | None = None) -> None:
        self.launch_error = launch_error
        self.retains = 0

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        if self.launch_error is not None:
            raise self.launch_error
        spec.envelope_path.parent.mkdir(parents=True, exist_ok=True)
        spec.envelope_path.write_text('{"declared": "tests"}', encoding="utf-8")
        return SimpleNamespace(launched_cwd=spec.worktree)

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s

    def retain(self, handle: object) -> None:
        del handle
        self.retains += 1
        raise lch.HerdrCallError("agent exited", lch.AGENT_NOT_FOUND)


def _launch(launcher: _ExitingRetainer):
    """Exercise the real `_launch` tail over a declared envelope."""
    actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cwd = root / "tester" / "checkout"
        cwd.mkdir(parents=True)
        actor.launcher = launcher
        actor._roles = {}
        actor.role_routes = {
            "tester": {
                "route": "omp",
                "model": "unused",
                "effort": "low",
                "profile": "unused",
            }
        }
        actor.target = SimpleNamespace(
            target_repository_fingerprint="target",
            target_repository_root=str(root),
        )
        actor._role_key = lambda ctx, role: (ctx.lane.lane_id, role, "digest")
        actor._release_superseded = lambda key: False
        actor._prepared_cwd = lambda path, prepare: prepare(path)
        actor._materialize_role_instructions = lambda *args: root / "system.md"
        actor._prompt = lambda *args: {}
        actor._launch_environment = lambda path: {}
        actor._workspace_label = lambda ctx: "workspace"
        actor._role_cwds = lambda ctx: {}
        actor._lane_child_anchor = lambda ctx, path: None
        ctx = SimpleNamespace(
            run_id="run-1",
            lane=SimpleNamespace(lane_id="lane-a", lane_kind=None),
            stage=SimpleNamespace(value="testing"),
            input_digest="digest",
        )
        return actor._launch(ctx, "tester", cwd, {}, prepare_cwd=lambda path: None)


def _await(launcher: _Waiter, payload: dict[str, Any], role: str = "tester"):
    """Drive `_await_envelope` over a written envelope, nothing else bound."""
    actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
    object.__setattr__(actor, "launcher", launcher)
    said: list[tuple[str, str, str]] = []
    object.__setattr__(
        actor, "step", lambda lane, message, detail="": said.append(
            (lane, message, detail)
        )
    )
    with tempfile.TemporaryDirectory() as tmp:
        envelope = Path(tmp) / "envelope.json"
        envelope.write_text(json.dumps(payload), encoding="utf-8")
        handle = SimpleNamespace(launched_cwd=str(tmp), envelope_path=envelope)
        returned = actor._await_envelope(handle, envelope, role, "lane-a")
    return returned, said


class ATimeoutIsTypedSoItsCallersCanTellItApart(unittest.TestCase):
    def test_the_ready_timeout_is_agent_not_interactive(self) -> None:
        calls: list[tuple[str, ...]] = []

        def herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            calls.append(args)
            if args[:2] == ("agent", "wait"):
                return {"agent": {"agent_status": "working"}}
            return {"agent": {"agent_status": "working"}}

        with self.assertRaises(lch.AgentNotInteractive) as caught:
            lch.wait_for_interactive_agent(herdr, "maestro-abc", timeout_s=0.01)
        self.assertIn("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-abc", str(caught.exception))
        self.assertTrue(calls)

    def test_it_is_not_a_launch_refusal(self) -> None:
        # The typed render timeout remains distinct from launcher and Herdr
        # failures, even though each is now reported after a declaration.
        self.assertFalse(issubclass(lch.AgentNotInteractive, lch.LaunchRefused))
        self.assertFalse(issubclass(lch.AgentNotInteractive, lch.HerdrCallError))
        self.assertTrue(issubclass(lch.AgentNotInteractive, RuntimeError))

    def test_a_settled_agent_still_returns_without_refusing(self) -> None:
        def herdr(*args: str, **kwargs: object) -> dict:
            del args, kwargs
            return {"agent": {"agent_status": "idle"}}

        lch.wait_for_interactive_agent(herdr, "maestro-abc", timeout_s=0.01)


class ACourtesyRetentionAfterDeclaration(unittest.TestCase):
    def test_an_agent_exit_during_retention_keeps_the_declared_payload(self) -> None:
        launcher = _ExitingRetainer()

        payload, _handle, _cwd = _launch(launcher)

        self.assertEqual(payload, {"declared": "tests"})
        self.assertEqual(launcher.retains, 1)

    def test_a_refusal_before_an_envelope_is_not_courtesy(self) -> None:
        launcher = _ExitingRetainer(
            lch.LaunchRefused(lch.LaunchRefusal.BINDING_MISMATCH, "wrong checkout")
        )

        with self.assertRaises(maestro.LaunchFailed):
            _launch(launcher)

        self.assertEqual(launcher.retains, 0)


class ADeclaredEnvelopeSurvivesASlowComposer(unittest.TestCase):
    def test_the_payload_is_returned_when_the_composer_times_out(self) -> None:
        launcher = _Waiter(
            lch.AgentNotInteractive("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-7f5a")
        )
        payload, said = _await(launcher, {"declared": "tests", "sha": "abc"})
        self.assertEqual(payload, {"declared": "tests", "sha": "abc"})
        self.assertEqual(launcher.waits, 1)

    def test_the_operator_is_told_completion_could_not_be_confirmed(self) -> None:
        launcher = _Waiter(
            lch.AgentNotInteractive("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-7f5a")
        )
        _payload, said = _await(launcher, {"declared": "tests"})
        self.assertEqual(len(said), 1)
        lane, message, detail = said[0]
        self.assertEqual(lane, "lane-a")
        self.assertIn("completion could not be confirmed", message)
        self.assertIn("AGENT_INTERACTIVE_READY_TIMEOUT", detail)

    def test_a_binding_refusal_after_declaration_is_reported(self) -> None:
        launcher = _Waiter(
            lch.LaunchRefused(lch.LaunchRefusal.BINDING_MISMATCH, "a!=b")
        )
        payload, said = _await(launcher, {"declared": "tests"})
        self.assertEqual(payload, {"declared": "tests"})
        self.assertEqual(len(said), 1)
        self.assertIn("completion could not be confirmed", said[0][1])
        self.assertIn("BINDING_MISMATCH", said[0][2])

    def test_a_herdr_failure_after_declaration_is_reported(self) -> None:
        launcher = _Waiter(lch.HerdrCallError("transport unavailable", "transport"))
        payload, said = _await(launcher, {"declared": "tests"})
        self.assertEqual(payload, {"declared": "tests"})
        self.assertEqual(len(said), 1)
        self.assertIn("completion could not be confirmed", said[0][1])
        self.assertIn("transport unavailable", said[0][2])

    def test_an_agent_that_exits_after_writing_returns_its_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = lch.role_session_token("run-1", "lane-a", "tester")
            envelope = root / "envelope.json"
            payload = {"declared": "tests", "sha": "abc"}
            envelope.write_text(json.dumps(payload), encoding="utf-8")
            handle = lch.LaunchHandle(
                token, "w9:p1", lch.agent_name_for(token), root
            )
            launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
            launcher._handles_lock = threading.RLock()
            launcher._handles = {token: handle}

            def herdr(*args: str, **kwargs: object) -> dict[str, object]:
                del kwargs
                if args[:2] == ("pane", "get"):
                    return {"pane": {"pane_id": "w9:p1", "cwd": str(root)}}
                if args[:2] == ("agent", "get"):
                    raise lch.HerdrCallError("agent exited", lch.AGENT_NOT_FOUND)
                raise AssertionError(args)

            launcher._herdr = herdr  # type: ignore[method-assign]
            actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
            actor.launcher = launcher
            said: list[tuple[str, str, str]] = []
            actor.step = lambda lane, message, detail="": said.append(
                (lane, message, detail)
            )

            self.assertEqual(
                actor._await_envelope(handle, envelope, "tester", "lane-a"),
                payload,
            )
            self.assertEqual(len(said), 1)
            self.assertIn("completion could not be confirmed", said[0][1])
            self.assertIn("BINDING_MISMATCH", said[0][2])

    def test_a_reviewer_verdict_is_still_required_before_the_wait(self) -> None:
        # Tolerating the render timeout must not tolerate a payload that has
        # not declared. `MAYBE` is not a verdict, so `_payload_ok` is false and
        # the composer wait is never reached -- `waits` stays 0.
        #
        # It used to refuse `STAGE_PAYLOAD_INVALID` here, via `poll` reporting
        # EXITED. Nothing but the envelope ends this wait any more, so an
        # undeclared payload leaves it running instead. See
        # `test_absence_is_not_a_verdict`.
        launcher = _Waiter(None)
        box: dict = {}

        def run() -> None:
            try:
                box["out"] = _await(
                    launcher, {"verdict": "MAYBE"}, role="code-reviewer"
                )
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=1.0)
        self.assertTrue(thread.is_alive())
        self.assertEqual(box, {})
        self.assertEqual(launcher.waits, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
