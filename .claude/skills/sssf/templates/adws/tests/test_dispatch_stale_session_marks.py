"""A dispatch that never reached its agent is recovered, not parked.

Run 646181870aef, after a resume:

* lane-wp3-reproduce-tests: a fresh Claude tester sat at an empty composer.
  The scheduler's turn counter restarted, so the prompt path was
  `prompt-1.json` again, and the replaced session's transcript beside the new
  one named it 33 times. That total read as this offer's delivery; nothing
  re-sent the prompt, and the lane parked `DISPATCH_NOT_CONSUMED` 629s later.
* lane-wp3-report-build: the code reviewer's pane still showed only its
  `claude … --remote-control` launch line when the prompt was sent three
  times. Herdr reported the agent ready ~30s before Claude drew its TUI.

Herdr is faked at the subprocess boundary: `HerdrLauncher._herdr` runs for
real and parses the scripted CLI output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules.scheduler import LaunchFailed  # noqa: E402
from tests.test_dispatch_consumed_proof import PANE, _Herdr, _Pane  # noqa: E402

#: How many times the replaced session's transcript names `prompt-1.json`.
STALE_MARKS = 33


class _ClaudeHerdr(_Herdr):
    """Reports the transcript path once the agent record carries one."""

    reports_transcript = False

    def __call__(self, *args: Any, **kwargs: Any) -> dict:
        reply = super().__call__(*args, **kwargs)
        if tuple(args[:2]) == ("agent", "get") and self.reports_transcript:
            reply["result"]["agent"]["transcript_path"] = str(self.transcript)
        return reply


def _cli(herdr: Any) -> Any:
    """`subprocess.run` for the herdr binary, answered by the scripted fake."""

    def run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        del kwargs
        payload = herdr(*[str(arg) for arg in argv[1:]])
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    return run


class _RestartedPane(_Pane):
    """A fresh session beside the transcript of the session it replaced."""

    def __init__(self, drop: int, *, bound: bool = True) -> None:
        super().__init__(drop)
        self.herdr = _ClaudeHerdr(self.prompt, self.transcript, drop)
        launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
        launcher.herdr_path = Path("herdr")
        launcher._handles_lock = threading.RLock()
        launcher._handles = {}
        launcher._tailers = {}
        launcher._clear_quiescence = lambda _t: None  # type: ignore[method-assign]
        self.launcher = launcher
        stale = self.transcript.with_name("replaced-session.jsonl")
        record = json.dumps({"type": "user", "text": "@{0}".format(self.prompt)})
        stale.write_text((record + "\n") * STALE_MARKS, encoding="utf-8")
        hour_ago = time.time() - 3600
        os.utime(stale, (hour_ago, hour_ago))
        if not bound:
            object.__setattr__(self.handle, "transcript_path", None)
            self.herdr.reports_transcript = True
        # The launch's own offer, as `HerdrLauncher.launch` records it.
        object.__setattr__(
            self.handle, "offer_marks", lch.marks_since_offer(self.handle, self.prompt)
        )

    def resubmit(self) -> None:
        """The production correction-turn path, binding checks aside."""
        with mock.patch.object(lch.subprocess, "run", _cli(self.herdr)):
            self.launcher._submit_resubmission(self.handle, self.prompt, 5.2)


class StaleSessionMarksTests(unittest.TestCase):
    def _pane(self, drop: int, *, bound: bool = True) -> _RestartedPane:
        pane = _RestartedPane(drop, bound=bound)
        self.addCleanup(pane.close)
        return pane

    def test_the_replaced_sessions_records_do_not_prove_a_fresh_offer(self) -> None:
        pane = self._pane(drop=1)
        pane.resubmit()
        self.assertTrue(pane.herdr.landed)
        self.assertEqual(pane.herdr.status, "working")
        self.assertEqual(pane.herdr.sends, 2)

    def test_a_transcript_bound_after_the_offer_does_not_prove_it(self) -> None:
        pane = self._pane(drop=1, bound=False)
        pane.resubmit()
        self.assertTrue(pane.herdr.landed)
        self.assertEqual(pane.herdr.sends, 2)

    def test_a_never_consumed_offer_is_still_refused_after_bounded_sends(self) -> None:
        pane = self._pane(drop=99)
        with self.assertRaises(lch.DispatchNotConsumed):
            pane.resubmit()
        self.assertEqual(pane.herdr.sends, lch.PROMPT_RESENDS + 1)

    def test_a_consumed_offer_is_sent_once(self) -> None:
        pane = self._pane(drop=0)
        pane.resubmit()
        self.assertEqual(pane.herdr.sends, 1)

    def test_a_record_written_since_the_offer_counts(self) -> None:
        pane = self._pane(drop=0)
        since = lch.marks_since_offer(pane.handle, pane.prompt)
        self.assertEqual(since(), 0)
        with pane.transcript.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"text": "@{0}".format(pane.prompt)}) + "\n")
        self.assertEqual(since(), 1)


class IdleBoundRedeliveryTests(unittest.TestCase):
    """The idle bound delivers a provably lost prompt again before refusing."""

    def _wait(self, pane: _RestartedPane, limit: int = 120) -> Any:
        self.clock = 1000.0
        self.steps: list[str] = []

        def sleep(_s: float) -> None:
            self.clock += 0.1

        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.launcher = pane.launcher
        actor.step = lambda _lane, message, _detail: self.steps.append(message)
        actor.dispatch_idle_timeout_s = limit
        envelope = Path(pane.handle.launched_cwd) / "results" / "envelope-1.json"
        with mock.patch.object(maestro.time, "sleep", sleep), mock.patch.object(
            maestro.time, "monotonic", lambda: self.clock
        ), mock.patch.object(lch.subprocess, "run", _cli(pane.herdr)), mock.patch.object(
            maestro.HerdrStageActor, "_payload_ok", lambda *_a: True
        ):
            return actor._await_envelope(
                pane.handle, envelope, "tester", "lane-a", pane.prompt
            )

    def _resubmit_for_real(self, pane: _RestartedPane) -> None:
        launcher = pane.launcher
        real = launcher._submit_resubmission

        def resubmit(handle: Any, prompt: Path, **kwargs: Any) -> Any:
            # Binding and readiness are proven elsewhere; delivery runs here.
            if kwargs.get("envelope_path") is not None:
                object.__setattr__(handle, "envelope_path", kwargs["envelope_path"])
            real(handle, prompt, 5.2)
            return handle

        launcher.resubmit = resubmit  # type: ignore[method-assign]

    def test_a_lost_dispatch_is_delivered_again_and_answered(self) -> None:
        pane = _RestartedPane(drop=0)
        self.addCleanup(pane.close)
        self._resubmit_for_real(pane)
        real = pane.launcher.turn_activity

        def activity(handle: Any) -> Any:
            # The redelivered prompt starts a turn that declares.
            if pane.herdr.landed:
                envelope = Path(handle.launched_cwd) / "results" / "envelope-1.json"
                envelope.parent.mkdir(parents=True, exist_ok=True)
                envelope.write_text(json.dumps({"ok": True}), encoding="utf-8")
            return real(handle)

        pane.launcher.turn_activity = activity  # type: ignore[method-assign]
        pane.launcher.wait_for_idle = lambda _h: None  # type: ignore[method-assign]
        self.assertEqual(self._wait(pane), {"ok": True})
        self.assertEqual(pane.herdr.sends, 1)
        self.assertIn("tester never received its prompt, delivering again", self.steps)

    def test_redelivery_is_bounded_then_refused(self) -> None:
        pane = _RestartedPane(drop=99)
        self.addCleanup(pane.close)
        calls: list[Path] = []
        pane.launcher.resubmit = (  # type: ignore[method-assign]
            lambda handle, prompt, **_k: calls.append(prompt) or handle
        )
        with self.assertRaises(LaunchFailed) as ctx:
            self._wait(pane)
        self.assertEqual(len(calls), maestro.DISPATCH_REDELIVERIES)
        self.assertTrue(ctx.exception.detail.startswith("DISPATCH_NOT_CONSUMED:tester"))

    def test_a_refused_redelivery_leaves_typed(self) -> None:
        pane = _RestartedPane(drop=99)
        self.addCleanup(pane.close)

        def refuse(_handle: Any, _prompt: Path, **_k: Any) -> Any:
            raise lch.LaunchRefused(
                lch.LaunchRefusal.DISPATCH_NOT_CONSUMED, "x", pane_created=True
            )

        pane.launcher.resubmit = refuse  # type: ignore[method-assign]
        with self.assertRaises(LaunchFailed) as ctx:
            self._wait(pane)
        self.assertTrue(ctx.exception.detail.startswith("DISPATCH_NOT_CONSUMED:x"))

    def test_a_delivered_prompt_is_not_delivered_again(self) -> None:
        # Pane shows the prompt: it reached the agent, which went quiet.
        pane = _RestartedPane(drop=99)
        self.addCleanup(pane.close)
        pane.herdr.screen = "> @{0}\n".format(pane.prompt)
        calls: list[Path] = []
        pane.launcher.resubmit = (  # type: ignore[method-assign]
            lambda handle, prompt, **_k: calls.append(prompt) or handle
        )
        with self.assertRaises(LaunchFailed):
            self._wait(pane)
        self.assertEqual(calls, [])


class _DrawingPane:
    """A pane that shows its launch line for `undrawn` reads, then a composer."""

    LAUNCH = "~/checkout  main  ❯ claude --model opus --effort medium --remote-control"

    def __init__(self, undrawn: int) -> None:
        self.undrawn = undrawn
        self.reads = 0

    def __call__(self, *args: Any, **kwargs: Any) -> dict:
        del kwargs
        if tuple(args[:2]) == ("pane", "read"):
            self.reads += 1
            if self.reads <= self.undrawn:
                return {"result": {"text": self.LAUNCH + "\n\n"}}
            return {"result": {"text": self.LAUNCH + "\n─────\n❯ \n─────\n  auto mode on\n"}}
        return {}


class AgentDrawnGateTests(unittest.TestCase):
    def _gate(self, pane: _DrawingPane, timeout_s: float = 180.0) -> float:
        launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
        launcher.herdr_path = Path("herdr")
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        with mock.patch.object(lch.subprocess, "run", _cli(pane)):
            lch.wait_for_agent_drawn(
                launcher._herdr,
                PANE,
                "claude",
                "--remote-control",
                timeout_s=timeout_s,
                sleep=sleep,
                monotonic=lambda: clock[0],
            )
        return clock[0]

    def test_the_first_prompt_waits_for_the_agent_to_draw(self) -> None:
        pane = _DrawingPane(undrawn=80)
        waited = self._gate(pane)
        self.assertEqual(pane.reads, 81)
        self.assertGreaterEqual(waited, 40.0)

    def test_a_drawn_agent_is_not_held(self) -> None:
        pane = _DrawingPane(undrawn=0)
        self.assertEqual(self._gate(pane), 0.0)
        self.assertEqual(pane.reads, 1)

    def test_an_agent_that_never_draws_is_refused_typed(self) -> None:
        pane = _DrawingPane(undrawn=10**6)
        with self.assertRaises(lch.AgentNotInteractive) as ctx:
            self._gate(pane, timeout_s=30.0)
        self.assertIn("AGENT_NEVER_DRAWN", str(ctx.exception))

    def test_an_unreadable_pane_never_holds_the_prompt(self) -> None:
        def unreadable(*_a: Any, **_k: Any) -> dict:
            raise lch.HerdrCallError("LAUNCH_REFUSED:x", "pane_not_found")

        self.assertFalse(
            lch.pane_shows_launch_line(unreadable, PANE, "claude", "--remote-control")
        )
        self.assertFalse(
            lch.pane_shows_launch_line(
                lambda *_a, **_k: {"result": {"text": ""}},
                PANE,
                "claude",
                "--remote-control",
            )
        )


if __name__ == "__main__":
    unittest.main()
