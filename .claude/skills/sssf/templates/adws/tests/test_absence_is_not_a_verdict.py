"""Only the envelope ends the wait for an envelope.

`_await_envelope` asks one question: did this turn declare. It used to answer
that question partly from `launcher.poll`, whose every input is a transport
observation -- does herdr still list a row under this agent's name, has a pane
been quiet, did a partial file happen to parse on one read. None of those say
whether the turn declared, and each one was allowed to end a lane.

The branch was rewritten repeatedly and each rewrite fixed one arm:

- reading the transcript for a terminal record ended every attempt on the
  agent's first message;
- reading the pane's absence before the envelope discarded three complete
  success envelopes on run `run-14b7b75944094c52ac9c0add41ae46a2`;
- one `idle` sample convicted a live builder on run
  `run-8d1a71f463e4430f92a125a8f8b3731d`, 75 seconds before it declared;
- a missing herdr row ended `lane-wp4-recalls-build` with
  `FACTORY_REFUSED:STAGE_PAYLOAD_MISSING` 245 seconds after dispatch on FDAdb
  run `2489c772d7c04ad5a2f2bcaa2f4de11c`, with an empty `results/` and a prompt
  the builder never answered.

Four incidents, one cause. These cases pin the deletion rather than any of the
four symptoms: no `PollResult` this method can be handed ends the wait, so
there is no window to tune and no state to add. An agent that never declares
leaves its lane visibly waiting, which is the operator's signal, not the
factory's decision.
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro as M  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402

TOKEN = lch.role_session_token(
    "run-2489c772", "lane-wp4-recalls-build", "builder"
)


class _Launcher:
    """A launcher that reports whatever the case wants and counts its polls."""

    def __init__(self, state: lch.PollState, detail: str = "") -> None:
        self.result = lch.PollResult(state, 1, detail)
        self.polls = 0

    def poll(self, handle: object) -> lch.PollResult:
        self.polls += 1
        return self.result


def _actor(launcher: object) -> M.HerdrStageActor:
    actor = M.HerdrStageActor.__new__(M.HerdrStageActor)
    actor.launcher = launcher
    actor.step = None
    return actor


def _await_in_thread(actor: M.HerdrStageActor, envelope: Path, role: str):
    """Run the wait off-thread; it is designed never to return on its own."""
    box: dict = {}

    def run() -> None:
        try:
            box["payload"] = actor._await_envelope(
                lch.LaunchHandle(TOKEN, "w9:p1", lch.agent_name_for(TOKEN),
                                 envelope.parent),
                envelope,
                role,
                "lane-wp4-recalls-build",
            )
        except BaseException as exc:  # pragma: no cover - failure is the point
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return box, thread


class OnlyTheEnvelopeEndsTheWaitTests(unittest.TestCase):
    def test_no_transport_state_ends_the_wait(self):
        """GONE and EXITED are the two that used to refuse. Neither does now.

        This is the whole change: not that absence is confirmed rather than
        believed, but that no observation of the transport is consulted at all.
        """
        for state in (lch.PollState.GONE, lch.PollState.EXITED):
            with self.subTest(state=state), TemporaryDirectory() as raw:
                envelope = Path(raw) / "agent-envelope.json"
                actor = _actor(_Launcher(state, "AGENT_GONE"))
                box, thread = _await_in_thread(actor, envelope, "builder")
                thread.join(timeout=1.0)
                self.assertTrue(thread.is_alive(), "the wait ended without an envelope")
                self.assertNotIn("error", box)
                self.assertNotIn("payload", box)

    def test_the_envelope_ends_it(self):
        """Written late, after every transport signal already said dead."""
        with TemporaryDirectory() as raw:
            envelope = Path(raw) / "agent-envelope.json"
            actor = _actor(_Launcher(lch.PollState.GONE, "AGENT_GONE"))
            box, thread = _await_in_thread(actor, envelope, "builder")
            thread.join(timeout=0.5)
            self.assertTrue(thread.is_alive())

            tmp = envelope.with_suffix(".part")
            tmp.write_text(json.dumps({"success": True}), encoding="utf-8")
            tmp.replace(envelope)

            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive(), "a declared envelope did not end it")
            self.assertEqual(box.get("payload", {}).get("success"), True)

    def test_a_reviewer_verdict_is_still_required(self):
        """Deleting the branch did not delete `_payload_ok`.

        A reviewer envelope carrying no verdict is not a declaration, so the
        wait continues -- the same answer as before, reached without consulting
        the pane.
        """
        with TemporaryDirectory() as raw:
            envelope = Path(raw) / "agent-envelope.json"
            actor = _actor(_Launcher(lch.PollState.RUNNING))
            envelope.write_text(json.dumps({"success": True}), encoding="utf-8")
            box, thread = _await_in_thread(actor, envelope, "code-reviewer")
            thread.join(timeout=1.0)
            self.assertTrue(thread.is_alive())

            tmp = envelope.with_suffix(".part")
            tmp.write_text(json.dumps({"verdict": "REVISE"}), encoding="utf-8")
            tmp.replace(envelope)
            thread.join(timeout=5.0)
            self.assertEqual(box.get("payload", {}).get("verdict"), "REVISE")

    def test_the_wait_never_polls(self):
        """The structural half, so the branch cannot come back one arm at a time.

        Each previous repair kept the poll and narrowed which of its answers
        was fatal. If a future edit reintroduces the call, this fails whatever
        it decides to do with the result.
        """
        with TemporaryDirectory() as raw:
            envelope = Path(raw) / "agent-envelope.json"
            launcher = _Launcher(lch.PollState.GONE, "AGENT_GONE")
            actor = _actor(launcher)
            _box, thread = _await_in_thread(actor, envelope, "builder")
            thread.join(timeout=1.0)
            self.assertTrue(thread.is_alive())
            self.assertEqual(launcher.polls, 0)


if __name__ == "__main__":
    unittest.main()
