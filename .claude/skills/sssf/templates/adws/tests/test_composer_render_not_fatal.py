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
        # `_await_envelope` must catch the render timeout and nothing else. A
        # binding mismatch raised by the same call is a real refusal.
        self.assertFalse(issubclass(lch.AgentNotInteractive, lch.LaunchRefused))
        self.assertFalse(issubclass(lch.AgentNotInteractive, lch.HerdrCallError))
        self.assertTrue(issubclass(lch.AgentNotInteractive, RuntimeError))

    def test_a_settled_agent_still_returns_without_refusing(self) -> None:
        def herdr(*args: str, **kwargs: object) -> dict:
            del args, kwargs
            return {"agent": {"agent_status": "idle"}}

        lch.wait_for_interactive_agent(herdr, "maestro-abc", timeout_s=0.01)


class ADeclaredEnvelopeSurvivesASlowComposer(unittest.TestCase):
    def test_the_payload_is_returned_when_the_composer_times_out(self) -> None:
        launcher = _Waiter(
            lch.AgentNotInteractive("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-7f5a")
        )
        payload, said = _await(launcher, {"declared": "tests", "sha": "abc"})
        self.assertEqual(payload, {"declared": "tests", "sha": "abc"})
        self.assertEqual(launcher.waits, 1)

    def test_the_operator_is_told_the_composer_was_still_rendering(self) -> None:
        launcher = _Waiter(
            lch.AgentNotInteractive("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-7f5a")
        )
        _payload, said = _await(launcher, {"declared": "tests"})
        self.assertEqual(len(said), 1)
        lane, message, detail = said[0]
        self.assertEqual(lane, "lane-a")
        self.assertIn("composer still rendering", message)
        self.assertIn("AGENT_INTERACTIVE_READY_TIMEOUT", detail)

    def test_a_binding_refusal_from_the_same_wait_still_ends_the_lane(self) -> None:
        # The wait calls `_verified_handle_binding` first. That refusal says the
        # handle is wrong, not that a pane is slow, and must not be swallowed.
        launcher = _Waiter(
            lch.LaunchRefused(lch.LaunchRefusal.BINDING_MISMATCH, "a!=b")
        )
        with self.assertRaises(lch.LaunchRefused):
            _await(launcher, {"declared": "tests"})

    def test_a_reviewer_verdict_is_still_required_before_the_wait(self) -> None:
        # Tolerating the render timeout must not tolerate a payload that has
        # not declared. An unusable reviewer envelope reaches `poll`, which
        # reports EXITED, and refuses -- the wait is never even reached.
        launcher = _Waiter(None)
        with self.assertRaises(Exception) as caught:
            _await(launcher, {"verdict": "MAYBE"}, role="code-reviewer")
        self.assertIn("STAGE_PAYLOAD_INVALID", str(caught.exception))
        self.assertEqual(launcher.waits, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
