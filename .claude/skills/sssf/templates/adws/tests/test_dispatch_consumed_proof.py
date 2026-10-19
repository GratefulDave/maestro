"""Every dispatch is proven consumed or refused within a bound.

Run 2acd2f5f: an attend operator (Claude, `--remote-control`) was dispatched,
its pane showed a fresh composer with no text in it, its transcript held no
user turn, and the lane logged "waiting on operator" for over an hour. The
paste never landed; `_offer_prompt` pressed Enter on an empty composer, read
the composer as "let go", returned offered-unproven, and `_await_envelope`
had nothing but the envelope to end on.

These drive the production predicates (`HerdrLauncher.prompt_delivery_lost`,
`HerdrLauncher.turn_activity`) against a scripted herdr, so the observations
the fix reads are the ones the launcher really makes.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
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

PANE = "w9:p1"


class _Herdr:
    """A Claude pane whose first `drop` pastes are swallowed by its startup."""

    def __init__(self, prompt: Path, transcript: Path, drop: int) -> None:
        self.prompt = prompt
        self.transcript = transcript
        self.drop = drop
        self.status = "idle"
        self.screen = '/remote-control is active\n> Try "fix lint errors"\n'
        self.landed = False
        self.sends = 0
        # A fast turn: the agent answers and is `idle` again, and its pane
        # shows the mention without the path, before the proof is read.
        self.answers_at_once = False

    def __call__(self, *args: Any, **kwargs: Any) -> dict:
        del kwargs
        verb = tuple(args[:2])
        if verb == ("pane", "send-text"):
            self.sends += 1
            if self.drop > 0:
                self.drop -= 1
            else:
                self.landed = True
                self.screen = "> " + str(args[3]) + "\n"
        elif verb in (("pane", "send-keys"), ("agent", "send-keys")):
            if args[3] == "enter" and self.landed and self.status == "idle":
                if self.answers_at_once:
                    self.screen = "> \n"
                else:
                    self.status = "working"
                    self.screen = "> @{0}\n  working\n".format(self.prompt)
                with self.transcript.open("a", encoding="utf-8") as out:
                    out.write(
                        json.dumps({"type": "user", "text": "@{0}".format(self.prompt)})
                        + "\n"
                    )
        elif verb == ("pane", "read"):
            return {"result": {"text": self.screen}}
        elif verb == ("pane", "get"):
            return {"result": {"pane": {"pane_id": PANE, "revision": self.sends}}}
        elif verb == ("agent", "get"):
            return {"result": {"agent": {"agent_status": self.status}}}
        return {}


def _launcher(herdr: _Herdr) -> lch.HerdrLauncher:
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher._handles_lock = threading.RLock()
    launcher._handles = {}
    launcher._tailers = {}
    launcher._herdr = lambda *a, **k: herdr(*a, **{x: y for x, y in k.items() if x != "env"})  # type: ignore[method-assign]
    return launcher


class _Pane:
    def __init__(self, drop: int) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.prompt = (root / "prompt-1.json").resolve()
        self.prompt.write_text("{}", encoding="utf-8")
        self.transcript = root / "session.jsonl"
        self.transcript.write_text('{"type": "system"}\n', encoding="utf-8")
        self.herdr = _Herdr(self.prompt, self.transcript, drop)
        self.launcher = _launcher(self.herdr)
        token = lch.role_session_token("run-1", "lane-a", "operator")
        self.handle = lch.LaunchHandle(
            token,
            PANE,
            lch.agent_name_for(token),
            root,
            transcript_path=self.transcript,
        )

    def close(self) -> None:
        self._tmp.cleanup()

    def submit(self) -> None:
        launcher, handle, prompt = self.launcher, self.handle, self.prompt
        lch.submit_agent_prompt(
            self.herdr,
            PANE,
            "@{0} ".format(prompt),
            handle.agent_name,
            timeout_s=5.2,
            attempts=1,
            working_proves=True,
            refuse_unproven=False,
            submission_recorded=launcher._runtime_submission_recorded(handle, prompt),
            composer_holds=launcher._composer_holds(PANE, prompt, {}),
            delivery_lost=lambda: launcher.prompt_delivery_lost(handle, prompt),
            sleep=lambda _s: None,
        )


class DeliveryProofTests(unittest.TestCase):
    def _pane(self, drop: int) -> _Pane:
        pane = _Pane(drop)
        self.addCleanup(pane.close)
        return pane

    def test_a_dropped_paste_is_resent_in_full_and_then_consumed(self) -> None:
        pane = self._pane(drop=1)
        pane.submit()
        self.assertTrue(pane.herdr.landed)
        self.assertEqual(pane.herdr.status, "working")
        self.assertEqual(pane.herdr.sends, 2)

    def test_a_prompt_never_consumed_is_refused_typed_after_bounded_sends(self) -> None:
        pane = self._pane(drop=99)
        with self.assertRaises(lch.DispatchNotConsumed) as ctx:
            pane.submit()
        self.assertIn("DISPATCH_NOT_CONSUMED", str(ctx.exception))
        self.assertEqual(pane.herdr.sends, lch.PROMPT_RESENDS + 1)

    def test_a_consumed_prompt_is_sent_once(self) -> None:
        pane = self._pane(drop=0)
        pane.submit()
        self.assertEqual(pane.herdr.sends, 1)

    def test_a_pane_showing_the_prompt_is_never_called_lost(self) -> None:
        pane = self._pane(drop=0)
        pane.herdr.screen = "> @{0}\n".format(pane.prompt)
        self.assertFalse(pane.launcher.prompt_delivery_lost(pane.handle, pane.prompt))

    def test_a_working_agent_is_never_called_lost(self) -> None:
        pane = self._pane(drop=0)
        pane.herdr.status = "working"
        self.assertFalse(pane.launcher.prompt_delivery_lost(pane.handle, pane.prompt))

    def test_resubmission_reports_the_typed_refusal(self) -> None:
        pane = self._pane(drop=0)
        launcher = pane.launcher
        with mock.patch.object(
            launcher, "_verified_handle_binding", lambda _h: None
        ), mock.patch.object(
            launcher,
            "_submit_resubmission",
            side_effect=lch.DispatchNotConsumed("DISPATCH_NOT_CONSUMED:x"),
        ):
            with self.assertRaises(lch.LaunchRefused) as ctx:
                launcher.resubmit(pane.handle, pane.prompt)
        self.assertIs(ctx.exception.refusal, lch.LaunchRefusal.DISPATCH_NOT_CONSUMED)
        self.assertTrue(ctx.exception.pane_created)


class AnsweredDispatchTests(unittest.TestCase):
    """Run 2acd2f5f, lane-spend-build code reviewer (omp).

    The agent wrote turns 3 and 4 into a second session file in its session
    directory while Herdr kept naming the first. Each prompt-4 send landed --
    the agent wrote `envelope-4.json`, then answered the re-sends with "This
    exact prompt and candidate were already reviewed" -- and the dispatch was
    refused `DISPATCH_NOT_CONSUMED ... after 3 sends`.
    """

    def _rotated(self, drop: int) -> _Pane:
        pane = _Pane(drop)
        self.addCleanup(pane.close)
        # Herdr's record still names the first file; the agent writes here.
        pane.herdr.transcript = pane.transcript.with_name("rotated.jsonl")
        pane.herdr.transcript.write_text('{"type": "session"}\n', encoding="utf-8")
        # omp renders a mention without the path, so the pane never shows it.
        pane.herdr.screen = "> \n"
        pane.herdr.answers_at_once = True
        return pane

    def test_a_prompt_recorded_in_a_rotated_transcript_is_consumed_once(self) -> None:
        pane = self._rotated(drop=0)
        pane.submit()
        self.assertEqual(pane.herdr.sends, 1)

    def test_a_dispatch_whose_envelope_exists_is_never_called_lost(self) -> None:
        pane = self._rotated(drop=99)
        envelope = Path(pane.handle.launched_cwd) / "results" / "envelope-4.json"
        envelope.parent.mkdir(parents=True)
        envelope.write_text('{"verdict": "REVISE", "findings": []}', encoding="utf-8")
        object.__setattr__(pane.handle, "envelope_path", envelope)
        self.assertFalse(pane.launcher.prompt_delivery_lost(pane.handle, pane.prompt))
        # A send the agent answers only by pointing at that envelope: consumed.
        pane.submit()
        self.assertEqual(pane.herdr.sends, 1)

    def test_a_resubmission_whose_envelope_appeared_is_returned(self) -> None:
        pane = self._rotated(drop=0)
        envelope = Path(pane.handle.launched_cwd) / "results" / "envelope-4.json"
        envelope.parent.mkdir(parents=True)
        envelope.write_text('{"verdict": "PASS", "findings": []}', encoding="utf-8")
        launcher = pane.launcher
        launcher._clear_quiescence = lambda _t: None  # type: ignore[method-assign]
        with mock.patch.object(
            launcher, "_verified_handle_binding", lambda _h: None
        ), mock.patch.object(
            lch, "wait_for_interactive_agent", lambda *_a, **_k: None
        ), mock.patch.object(
            launcher,
            "_submit_resubmission",
            side_effect=lch.DispatchNotConsumed("DISPATCH_NOT_CONSUMED:x"),
        ):
            handle = launcher.resubmit(
                pane.handle, pane.prompt, envelope_path=envelope
            )
        self.assertIs(handle, pane.handle)

    def test_a_dropped_prompt_with_a_rotated_transcript_is_still_refused(self) -> None:
        pane = self._rotated(drop=99)
        with self.assertRaises(lch.DispatchNotConsumed):
            pane.submit()
        self.assertEqual(pane.herdr.sends, lch.PROMPT_RESENDS + 1)


class IdleWaitBoundTests(unittest.TestCase):
    def _wait(self, pane: _Pane, limit: int) -> Any:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.launcher = pane.launcher
        actor.step = None
        actor.dispatch_idle_timeout_s = limit
        envelope = Path(pane.handle.launched_cwd) / "results" / "envelope-1.json"
        with mock.patch.object(maestro.time, "sleep", lambda _s: None):
            return actor._await_envelope(pane.handle, envelope, "operator", "lane-a")

    def test_an_idle_agent_with_no_envelope_ends_the_wait_typed(self) -> None:
        pane = _Pane(drop=99)
        self.addCleanup(pane.close)
        with self.assertRaises(LaunchFailed) as ctx:
            self._wait(pane, 120)
        self.assertTrue(ctx.exception.detail.startswith("DISPATCH_NOT_CONSUMED:operator"))
        self.assertTrue(ctx.exception.pane_created)

    def test_transcript_growth_restarts_the_bound(self) -> None:
        pane = _Pane(drop=99)
        self.addCleanup(pane.close)
        grown: list[int] = []
        real = pane.launcher.turn_activity

        def activity(handle: Any) -> Any:
            # The agent writes to its transcript for the first ten samples.
            if len(grown) < 10:
                with pane.transcript.open("a", encoding="utf-8") as out:
                    out.write("{}\n")
            grown.append(1)
            return real(handle)

        pane.launcher.turn_activity = activity  # type: ignore[method-assign]
        with self.assertRaises(LaunchFailed):
            self._wait(pane, 120)
        # Ten growing samples, then 120s of quiet at a 30s cadence: the bound
        # counts from the last growth, never from the dispatch.
        self.assertGreaterEqual(len(grown), 14)

    def test_a_working_agent_is_never_bounded(self) -> None:
        pane = _Pane(drop=0)
        self.addCleanup(pane.close)
        pane.herdr.status = "working"
        samples: list[int] = []
        real = pane.launcher.turn_activity

        def activity(handle: Any) -> Any:
            samples.append(1)
            if len(samples) == 40:
                envelope = Path(handle.launched_cwd) / "results" / "envelope-1.json"
                envelope.parent.mkdir(parents=True, exist_ok=True)
                envelope.write_text(json.dumps({"ok": True}), encoding="utf-8")
            return real(handle)

        pane.launcher.turn_activity = activity  # type: ignore[method-assign]
        pane.launcher.wait_for_idle = lambda _h: None  # type: ignore[method-assign]
        with mock.patch.object(
            maestro.HerdrStageActor, "_payload_ok", lambda *_a: True
        ):
            self.assertEqual(self._wait(pane, 120), {"ok": True})
        self.assertGreaterEqual(len(samples), 40)


if __name__ == "__main__":
    unittest.main()
