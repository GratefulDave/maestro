"""An operator interrupt ends every in-flight wait, and the process can exit.

FDAdb run `d246ae95`, 2026-09-10 09:13-09:34 UTC. The operator pressed Ctrl-C
on `run resume` while `lane-faq-release-tests` was waiting on its test
reviewer. Everything the scheduler owns did the right thing: a `USER_WAIT` with
`wait_reason` PAUSE and `resume_stage` REVIEWING_TESTS was recorded, and the
console printed `run finished waiting`. The process then stayed alive for
twenty more minutes, printing

    waiting on test-reviewer  1230s elapsed

every thirty seconds. Closing the reviewer's Herdr pane -- so the envelope it
was waiting for could never be written by anyone -- changed nothing, and
neither did `pkill -TERM`. SIGKILL ended it.

Two facts compose into that. `signal` delivers SIGINT to the **main** thread,
so with `concurrency` above 1 the `KeyboardInterrupt` unwound the scheduler
thread and left the worker inside `HerdrStageActor._await_envelope`, which by
design ends on the envelope and on nothing else. And `ThreadPoolExecutor`
workers are not daemon threads: `concurrent.futures.thread._python_exit` joins
them at interpreter shutdown, so one polling worker holds the whole process
open however cleanly the scheduler returned.

`pool.shutdown(wait=False, cancel_futures=True)` does not touch this. It
cancels futures that were never started; a running worker is reached only by
something the worker itself reads. That is `adw_modules/interrupt`.

The interrupt flag is emphatically not a transport observation, and does not
reopen the question four incidents closed beside `_await_envelope`: a closed
pane, a missing herdr record and a quiet composer still end no wait. An
operator pressing Ctrl-C is a decision about the run.
"""

from __future__ import annotations

import json
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import interrupt  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from tests import test_factory_cutover as cutover  # noqa: E402
from tests import test_lane_concurrency as conc  # noqa: E402


class _StuckOnAnEnvelopeActor(cutover.ScriptedActor):
    """Authoring blocks in the real envelope poll, on a worker thread.

    Not a `threading.Event().wait()` stand-in: the loop is the one
    `_await_envelope` runs, spending its interval in `interrupt.sleep`, so
    what this case observes is the shipped wait ending rather than a fake
    agreeing to stop.
    """

    first_candidate_is_draft = False

    def __init__(self, repo: Path, worktrees: Path) -> None:
        super().__init__(repo, worktrees)
        self.arrived = threading.Barrier(len(conc.LANES) + 1, timeout=30)
        self.workers: list[threading.Thread] = []
        self.ended_at: dict[str, float] = {}
        self.refusals: list[BaseException] = []
        self._lock = threading.Lock()

    def write_tests(self, ctx: sch.LaneContext) -> dict:
        with self._lock:
            self.workers.append(threading.current_thread())
        self.arrived.wait()
        try:
            while True:
                interrupt.sleep(0.1, "envelope:test-reviewer")
        except interrupt.AgentWaitInterrupted as exc:
            with self._lock:
                self.ended_at[ctx.lane.lane_id] = time.monotonic()
                self.refusals.append(exc)
            raise

    def review_tests(self, ctx: sch.LaneContext):
        return st.ReviewerVerdict.PASS, ()

    def review_code(self, ctx: sch.LaneContext):
        return st.ReviewerVerdict.PASS, ()


class AnInterruptEndsEveryInFlightWait(conc._Run):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(interrupt.clear_stop)

    def test_run_returns_waiting_and_no_worker_is_left_polling(self) -> None:
        actor = _StuckOnAnEnvelopeActor(self.repo, self.runtime.path / "worktrees")
        scheduler = self.scheduler(actor, concurrency=2)
        main_ident = threading.main_thread().ident
        assert main_ident is not None
        fired: list[float] = []

        def interrupt_once_both_are_waiting() -> None:
            actor.arrived.wait()
            # The scheduler thread is parked on its futures by now.
            time.sleep(0.05)
            fired.append(time.monotonic())
            signal.pthread_kill(main_ident, signal.SIGINT)

        trigger = threading.Thread(target=interrupt_once_both_are_waiting)
        trigger.start()
        try:
            status = scheduler.run()
        finally:
            trigger.join(30)
        returned = time.monotonic()

        # The ledger says what the operator asked for.
        self.assertIs(status, st.RunStatus.WAITING)
        self.assertEqual(
            {lane: self.store.lane_stage(conc.RUN_ID, lane) for lane in conc.LANES},
            {lane: st.LaneStage.WAITING_FOR_USER for lane in conc.LANES},
        )
        for lane in conc.LANES:
            wait = sch._latest(
                self.store, conc.RUN_ID, lane, st.ArtifactKind.USER_WAIT
            )
            assert wait is not None, lane
            self.assertEqual(
                wait.payload["resume_stage"], st.LaneStage.WRITING_TESTS.value
            )

        # And every worker that was waiting has stopped waiting. Before the
        # fix these threads were still alive here, and `_python_exit` would
        # hold the process open behind them for as long as they polled.
        for worker in actor.workers:
            worker.join(5)
            self.assertFalse(worker.is_alive(), worker.name)
        self.assertEqual(len(actor.workers), len(conc.LANES))
        self.assertNotIn(threading.main_thread(), actor.workers)
        self.assertEqual(set(actor.ended_at), set(conc.LANES))

        # Within one poll interval of the interrupt, not at some deadline.
        # A generous bound: the point is that it is bounded by the poll and
        # not by the envelope, which on this run could never arrive.
        for lane, ended in actor.ended_at.items():
            self.assertLess(ended - fired[0], 2.0, lane)
        self.assertLess(returned - fired[0], 10.0)

        # The refusal is typed, and says only that the operator stopped.
        self.assertTrue(actor.refusals)
        for exc in actor.refusals:
            self.assertIsInstance(exc, interrupt.AgentWaitInterrupted)
            self.assertIn("OPERATOR_INTERRUPT:", str(exc))
        self.assertIsNone(scheduler._pool)

    def test_no_further_stage_is_dispatched_after_the_interrupt(self) -> None:
        interrupt.request_stop()
        scheduler = self.scheduler(
            _StuckOnAnEnvelopeActor(self.repo, self.runtime.path / "worktrees"),
            concurrency=2,
        )
        with self.assertRaises(interrupt.AgentWaitInterrupted) as caught:
            scheduler._advance(conc.LANES[0])
        self.assertIn("dispatch:{0}".format(conc.LANES[0]), str(caught.exception))

    def test_a_fresh_run_starts_unstopped(self) -> None:
        interrupt.request_stop()
        self.assertTrue(interrupt.stop_requested())
        actor = conc._PassingActor(self.repo, self.runtime.path / "worktrees")
        self.assertIs(self.scheduler(actor, concurrency=1).run(), st.RunStatus.COMPLETE)


class TheEnvelopeWaitReadsTheFlag(unittest.TestCase):
    """The shipped `_await_envelope` loop, driven directly."""

    def setUp(self) -> None:
        self.addCleanup(interrupt.clear_stop)

    def _actor(self) -> maestro.HerdrStageActor:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        object.__setattr__(actor, "launcher", SimpleNamespace())
        object.__setattr__(actor, "step", lambda *a, **k: None)
        return actor

    def test_it_ends_on_the_interrupt_with_no_envelope_written(self) -> None:
        actor = self._actor()
        with tempfile.TemporaryDirectory() as tmp:
            envelope = Path(tmp) / "envelope.json"
            handle = SimpleNamespace(launched_cwd=str(tmp))
            timer = threading.Timer(0.2, interrupt.request_stop)
            timer.start()
            self.addCleanup(timer.cancel)
            started = time.monotonic()
            with self.assertRaises(interrupt.AgentWaitInterrupted) as caught:
                actor._await_envelope(handle, envelope, "test-reviewer", "lane-a")
            self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("envelope:test-reviewer", str(caught.exception))

    def test_a_declared_envelope_still_wins_over_a_pending_stop(self) -> None:
        # Ordering that matters: the payload is read before the interval is
        # spent, so work already on disk is returned rather than discarded.
        # This is the f50638ab rule -- a wait must not throw away a
        # declaration it is holding.
        actor = self._actor()
        interrupt.request_stop()
        with tempfile.TemporaryDirectory() as tmp:
            envelope = Path(tmp) / "envelope.json"
            envelope.write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
            handle = SimpleNamespace(launched_cwd=str(tmp))
            payload = actor._await_envelope(
                handle, envelope, "test-reviewer", "lane-a"
            )
        self.assertEqual(payload["verdict"], "PASS")


class AVanishedPaneEndsTheIdleWait(unittest.TestCase):
    """The other wait on this line, and why it was never the hang.

    `wait_for_idle` is reached only *after* an envelope has been read and
    accepted, and it is bounded: a pane the operator has closed makes every
    `herdr agent get` refuse, so it ends with `AgentNotInteractive` rather
    than looping. That typing is what run f50638ab bought. Recorded here
    because the incident report asked whether a closed pane was a second
    unbounded wait on the same line; it is not.
    """

    def test_a_closed_pane_refuses_rather_than_polling(self) -> None:
        calls: list[tuple[str, ...]] = []

        def herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            calls.append(args)
            raise RuntimeError('{"error": {"code": "agent_not_found"}}')

        started = time.monotonic()
        with self.assertRaises(lch.AgentNotInteractive) as caught:
            lch.wait_for_interactive_agent(herdr, "maestro-gone", timeout_s=0.01)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn("AGENT_INTERACTIVE_READY_TIMEOUT:maestro-gone", str(caught.exception))
        self.assertIn("probe=", str(caught.exception))
        self.assertTrue(calls)

    def test_it_is_not_confusable_with_a_launch_refusal(self) -> None:
        self.assertFalse(issubclass(lch.AgentNotInteractive, lch.LaunchRefused))
        self.assertFalse(
            issubclass(interrupt.AgentWaitInterrupted, lch.AgentNotInteractive)
        )
        self.assertTrue(issubclass(interrupt.AgentWaitInterrupted, RuntimeError))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
