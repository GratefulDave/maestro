"""A busy agent is silent, and silence is what `poll` reads as death.

An agent blocked inside a foreground tool call reports `idle` and writes
nothing to its transcript. From outside, busy-and-silent is indistinguishable
from finished-and-silent, so `poll` waits out `AGENT_QUIESCENCE_CONFIRM_S`
before calling it -- B14's rule, quiescence after liveness, never a wall clock.

**The window was exactly as long as the runtime's silence.** omp's bash tool
runs a foreground command for 60 seconds and then backgrounds it, so any
`npm install` or test run that reaches the ceiling is silent for exactly 60s.
`AGENT_QUIESCENCE_CONFIRM_S` was 60.0. The collision was guaranteed, not
unlucky.

Measured, FDAdb run `a2ea7355699c4dff93bc82ac89415475`, `lane-wp8r-route-tests`,
2026-09-05. Turn 8's transcript gap is 21:39:47.423 -> 21:40:47.432, exactly
60.0s, and the record that ends it reads `Backgrounded as job bg_3` -- the
ceiling handing the command off. The window confirmed ~66s after dispatch,
before the 30s-cadence "waiting on tester" line had printed twice, and the run
ended. The tester wrote a valid 21,394-byte `envelope-8.json` at 21:42:46, and
three lanes that were already MERGED were discarded with the run. Turn 7 ran
the same gauntlet with three gaps of >=60.0s and survived all three: the old
value was not detecting anything, it was losing a coin toss.

These cases pin the repair: the window outlasts the runtime's own
silent-tool ceiling, and outlasts a delegated subagent run.
"""

from __future__ import annotations

import json
import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

import maestro as M  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402

TOKEN = lch.role_session_token("run-a2ea7355", "lane-wp8r-route-tests", "tester")

#: The omp bash tool's foreground ceiling, and so the longest silence a
#: working agent can legitimately produce.
OMP_FOREGROUND_CEILING_S = 60.0

#: A plainly ordinary subagent run, and the bound that actually binds here.
#: Every role runs on omp, omp delegates routinely, and a delegating agent
#: writes nothing to its own transcript for the whole of the child's work --
#: it does not reliably report `working` while it waits either, so neither
#: signal covers that gap. The ceiling above bounds a shell command; nothing
#: bounds a child agent, so a window derived from the ceiling is too small
#: however many times over. 180.0 cleared the ceiling assertion below and
#: would still have called a tester dead on its first four-minute child.
OMP_SUBAGENT_SILENCE_FLOOR_S = 300.0


def _transcript(tmp: Path, records: int) -> Path:
    """A session transcript already carrying `records` finished turns."""
    path = tmp / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for index in range(records):
            handle.write(json.dumps({"type": "message", "i": index}) + "\n")
    return path


def _launcher(transcript: Path, *, confirm_s: float) -> lch.HerdrLauncher:
    """A launcher whose pane reports `idle` on every poll.

    Only the fields `poll` reaches are set, the way `_bare_launcher` in
    `test_persistent_role_agents` does it.
    """
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher.quiescence_confirm_s = confirm_s
    launcher._handles_lock = threading.RLock()
    launcher._handles = {}
    launcher._tailers = {TOKEN: lch.TranscriptTailer(transcript)}
    launcher._quiescent_since = {}
    launcher._proven_absent = {}
    launcher._herdr = (  # type: ignore[method-assign]
        lambda *args, **kwargs: {
            "result": {"pane_id": "w9:p1", "agent_status": "idle"}
        }
    )
    return launcher


def _handle(cwd: Path) -> lch.LaunchHandle:
    return lch.LaunchHandle(TOKEN, "w9:p1", lch.agent_name_for(TOKEN), cwd)


def _actor(launcher: object) -> M.HerdrStageActor:
    actor = M.HerdrStageActor.__new__(M.HerdrStageActor)
    actor.launcher = launcher
    actor.step = None
    return actor


class TheWindowOutlastsTheRuntimesSilenceTests(unittest.TestCase):
    def test_the_confirm_window_strictly_exceeds_the_foreground_ceiling(self):
        # 60.0 == 60.0 is what made run a2ea7355 a coin toss rather than a
        # measurement. A future edit that lowers this back to the ceiling
        # reintroduces the incident exactly.
        self.assertGreater(
            lch.AGENT_QUIESCENCE_CONFIRM_S, OMP_FOREGROUND_CEILING_S
        )

    def test_the_confirm_window_outlasts_a_delegated_subagent_run(self):
        # The assertion above is necessary and not sufficient: 180.0 passes it
        # and is still wrong, because the silence that has to be survived is a
        # child agent's whole run, not a backgrounded shell command. This is
        # the bound that binds, and the one to reason from if the number is
        # ever revisited.
        self.assertGreater(
            lch.AGENT_QUIESCENCE_CONFIRM_S, OMP_SUBAGENT_SILENCE_FLOOR_S
        )

    def test_a_sixty_second_tool_call_is_not_read_as_death(self):
        """The incident's threshold half, on a real clock.

        A turn shows liveness, then goes silent for the full foreground
        ceiling exactly as `Backgrounded as job bg_3` did, and declares. The
        window must outlast that silence. Time is injected rather than slept.
        """
        with TemporaryDirectory() as raw:
            tmp = Path(raw)
            transcript = _transcript(tmp, 482)
            launcher = _launcher(
                transcript, confirm_s=lch.AGENT_QUIESCENCE_CONFIRM_S
            )
            handle = _handle(tmp)
            with transcript.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"type": "message", "i": "tool-call"}) + "\n")
            self.assertEqual(launcher.poll(handle).state, lch.PollState.RUNNING)

            clock = [0.0]
            original = lch.time.monotonic
            lch.time.monotonic = lambda: clock[0]  # type: ignore[assignment]
            try:
                # Re-latch on the record above, then let the ceiling elapse in
                # total silence.
                self.assertEqual(launcher.poll(handle).state, lch.PollState.RUNNING)
                # Past the ceiling, not exactly on it. A real 60s command
                # plus its `Backgrounded as job` handoff lands here, and the
                # comparison is a strict `>`, so landing exactly on the
                # boundary is the coin toss rather than the measurement.
                clock[0] = OMP_FOREGROUND_CEILING_S + 0.5
                self.assertEqual(launcher.poll(handle).state, lch.PollState.RUNNING)
            finally:
                lch.time.monotonic = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
