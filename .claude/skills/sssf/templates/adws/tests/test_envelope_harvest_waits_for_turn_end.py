"""The harvested envelope is the one the agent finished with.

Run 2acd2f5f, `lane-device-tests`, tester turn 14: the tester wrote
`envelope-14.json` at about 06:28:00 and kept working. `_await_envelope` read
it, waited the 60s courtesy idle wait, logged

    tester completion could not be confirmed  AGENT_INTERACTIVE_READY_TIMEOUT

and returned the first read at 06:29:05. The tester's last edit ran from
06:29:05 to 06:29:11, rewrote the envelope, and its final message came at
06:29:17. TEST_DRAFT seq 41 missed that edit.

These drive `_await_envelope` itself with a launcher that does what that
tester did during the wait.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import maestro
from adw_modules import launcher as lch

FIRST = {"test_files": {"t.py": "first"}}
FINAL = {"test_files": {"t.py": "final"}}


class _StillWorking:
    """Times out `busy` times, rewriting the envelope during the first."""

    def __init__(self, envelope: Path, busy: int, *, grow: bool, rewrite_on_idle=False):
        self.envelope = envelope
        self.busy = busy
        self.grow = grow
        self.rewrite_on_idle = rewrite_on_idle
        self.transcript = 100
        self.waits = 0

    def turn_activity(self, handle: object) -> tuple[str, int]:
        del handle
        return ("working", self.transcript)

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s
        self.waits += 1
        if self.waits == 1 and (self.busy or self.rewrite_on_idle):
            self.envelope.write_text(json.dumps(FINAL), encoding="utf-8")
        if self.waits <= self.busy:
            if self.grow:
                self.transcript += 50
            raise lch.AgentNotInteractive("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-x")


def _await(make) -> tuple[object, list[str], _StillWorking]:
    actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
    said: list[str] = []
    actor.step = lambda lane, message, detail="": said.append(message)
    with tempfile.TemporaryDirectory() as tmp:
        envelope = Path(tmp) / "envelope.json"
        envelope.write_text(json.dumps(FIRST), encoding="utf-8")
        launcher = make(envelope)
        actor.launcher = launcher
        handle = SimpleNamespace(launched_cwd=str(tmp), envelope_path=envelope)
        payload = actor._await_envelope(handle, envelope, "tester", "lane-a")
    return payload, said, launcher


class AnAgentStillWorkingAfterDeclaringIsWaitedFor(unittest.TestCase):
    def test_a_rewritten_envelope_during_a_timed_out_wait_is_the_one_returned(self):
        payload, said, launcher = _await(
            lambda envelope: _StillWorking(envelope, busy=1, grow=False)
        )
        self.assertEqual(payload, FINAL)
        self.assertEqual(launcher.waits, 2)
        self.assertIn("tester still working after declaring", said)
        self.assertNotIn("tester completion could not be confirmed", said)

    def test_a_growing_transcript_keeps_the_wait_open_until_idle(self):
        payload, said, launcher = _await(
            lambda envelope: _StillWorking(envelope, busy=3, grow=True)
        )
        self.assertEqual(payload, FINAL)
        self.assertEqual(launcher.waits, 4)

    def test_an_envelope_rewritten_while_finishing_is_reread_after_idle(self):
        payload, _said, launcher = _await(
            lambda envelope: _StillWorking(
                envelope, busy=0, grow=False, rewrite_on_idle=True
            )
        )
        self.assertEqual(payload, FINAL)
        self.assertEqual(launcher.waits, 2)


class ASlowComposerOverFinishedWorkStillReturns(unittest.TestCase):
    def test_no_growth_and_no_rewrite_keeps_the_declaration(self):
        class Quiet(_StillWorking):
            def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
                del handle, timeout_s
                self.waits += 1
                raise lch.AgentNotInteractive("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-x")

        payload, said, launcher = _await(
            lambda envelope: Quiet(envelope, busy=0, grow=False)
        )
        self.assertEqual(payload, FIRST)
        self.assertEqual(launcher.waits, 1)
        self.assertIn("tester completion could not be confirmed", said)


if __name__ == "__main__":
    unittest.main()
