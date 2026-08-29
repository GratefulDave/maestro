"""Executable proof of the finalization window (§6.5, §11.2, §7.6).

The window is §11.2's silence rule applied to the one span that has no run
row: `plan finalize` launching exactly one reviewer. It must (a) open with
a durable record in the store that owns that phase — the tracer's
reviewer-session row, (b) carry exactly one span-bounding wall-clock
timeout, and (c) convert expiry into a durable typed result by kill and
red outcome. Inside the span, §7.6's structural signals apply and **arm at
reported launch**, so *not yet started*, *working*, and *stopped without
declaring* are distinguished rather than collapsed into one wall clock.

Two properties get their own tests because both are where this has failed
before:

  arming     -- B14's recorded failure is a reviewer idle at its prompt
                having written nothing. A window that armed process-alive
                and turn-count at open rather than at reported launch
                would convict every reviewer at its first poll.
  the clock  -- lifecycle stores `last_transition_at` in EPOCH seconds
                while the watchdog measures in `time.monotonic`. Mixing
                them produced a real defect earlier in this build, so the
                window owns both stamps separately and every timeout here
                is proven to be monotonic-only.

The clock is injected everywhere, so nothing sleeps out a real timeout.
Where the signal under test is process liveness, a real `subprocess.Popen`
is used rather than a mock, for the same reason `test_watchdog` does: a
mock would only assert our own assumption back at us.

Run with:  uv run adws/adw_test.py -k window
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import finalization_window as fw  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402


class FakeClock:
    """An injectable time source: advances only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def make_config(**overrides) -> fw.FinalizationConfig:
    fields = dict(finalization_timeout_s=600.0, turn_timeout_s=60.0)
    fields.update(overrides)
    return fw.FinalizationConfig(**fields)


#: Distinguishes "this window has no status reader" from "the reader answered
#: None". The two are different states and the module treats them differently:
#: the first leaves quiescence detection out of the picture entirely, the
#: second is a missing observation about a reviewer that may be perfectly fine.
_NO_STATUS_READER = object()


class Harness:
    """One window plus the collaborators it is given, all recording."""

    def __init__(self, *, config=None, report_after_polls=None,
                 harness_owned_group=True, monotonic_start=0.0,
                 epoch_start=1_760_000_000.0, alive=True, records=0,
                 pid=None, status=_NO_STATUS_READER):
        self.pid = pid
        self.calls = []
        self.killed = []
        self.launch_calls = 0
        self.alive = alive
        self.records = records
        #: The route's raw pane status, mutated by a test between polls. Left
        #: at the sentinel when the window is built with no status reader at
        #: all. Both shipped windows now pass one — `plan finalize` and the
        #: run's per-attempt review — so the readerless shape is the contract
        #: for a caller that has no route to ask, not a live call site.
        self.status = status
        self.status_reads = 0
        self._report_after_polls = report_after_polls
        self._polls = 0
        self.monotonic = FakeClock(monotonic_start)
        self.epoch = FakeClock(epoch_start)
        self.harness_owned_group = harness_owned_group
        self.window = fw.FinalizationWindow(
            config=config or make_config(),
            launch=self._launch,
            poll_report=self._poll_report,
            kill=self._kill,
            process_alive=lambda pid: self.alive,
            transcript_record_count=lambda session: self.records,
            actor_status=(None if status is _NO_STATUS_READER
                          else self._actor_status),
            time_source=self.monotonic,
            wall_clock=self.epoch,
        )

    def _actor_status(self, _session):
        self.status_reads += 1
        return self.status

    def _launch(self):
        self.launch_calls += 1
        self.calls.append("launch")
        return fw.ReviewerSession(
            route="omp", model="opus", session_id="sess-7",
            session_dir="/tmp/finalize/sess-7",
            harness_owned_group=self.harness_owned_group,
            pid=self.pid)

    def _poll_report(self):
        self._polls += 1
        if (self._report_after_polls is not None
                and self._polls >= self._report_after_polls):
            return {"plan_digest": "abc"}
        return None


    def _kill(self, session):
        self.calls.append("kill")
        self.killed.append(session)


class VocabularyIsSharedWithTheWatchdog(unittest.TestCase):
    """§6.5 says §7.6's signals apply inside the window. They are the same
    signals, so they carry the same names -- a parallel vocabulary would
    let the two drift apart silently."""

    def test_the_two_structural_signals_reuse_watchdog_names(self):
        self.assertEqual(fw.FinalizationSignal.PROCESS_DEAD.value,
                         wd.StallReason.PROCESS_DEAD.value)
        self.assertEqual(fw.FinalizationSignal.TURN_TIMEOUT.value,
                         wd.StallReason.TURN_TIMEOUT.value)

    def test_the_span_bound_is_named_for_the_window_not_a_node(self):
        """The third signal is honestly not the watchdog's NODE_TIMEOUT:
        finalization has no node and no attempt row."""
        self.assertEqual(fw.FinalizationSignal.WINDOW_TIMEOUT.value,
                         "WINDOW_TIMEOUT")
        self.assertNotIn("WINDOW_TIMEOUT",
                         [member.value for member in wd.StallReason])

    def test_the_window_reuses_the_watchdogs_own_signal_readers(self):
        """Not a copy of process liveness and record counting -- the same
        two functions, so §7.6's measured transcript contract cannot be
        re-litigated here."""
        self.assertIs(fw.DEFAULT_PROCESS_ALIVE, wd.process_is_alive)
        self.assertIs(fw.DEFAULT_TRANSCRIPT_RECORD_COUNT,
                      wd.count_complete_transcript_records)
        self.assertIs(fw.SESSION_PATH_KEY, wd.SESSION_PATH_KEY)
        self.assertIs(fw.LIVE_WORKING_STATUSES, wd.LIVE_WORKING_STATUSES)
        self.assertNotIn("idle", wd.LIVE_WORKING_STATUSES)


class TheTimeoutIsConfigurationNotPlanContent(unittest.TestCase):

    def test_the_finalization_timeout_is_not_a_scheduler_config_field(self):
        """§11.2: the finalization timeout takes no part in `T`'s
        preflight inequality, because no run exists at plan time. A field
        on `SchedulerConfig` would drag it into `greatest_run_window_s`."""
        self.assertNotIn(
            "finalization_timeout_s",
            {f.name for f in st.SchedulerConfig.__dataclass_fields__.values()})

    def test_non_positive_timeouts_are_refused(self):
        for field in ("finalization_timeout_s", "turn_timeout_s"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    make_config(**{field: 0.0})


class TheWindowOpensWithADurableRecord(unittest.TestCase):

    def test_opening_records_the_reviewer_session_row(self):
        h = Harness()
        session = h.window.open()
        self.assertEqual(session.route, "omp")
        self.assertEqual(session.model, "opus")
        self.assertEqual(session.session_id, "sess-7")

    def test_run_arms_a_launched_session_before_its_first_poll(self):
        """A launch that returned a pid must reach the process liveness check.

        `poll` returns early on `not session.armed`, so a `run` that never
        arms leaves PROCESS_DEAD, ACTOR_ABANDONED and TURN_TIMEOUT all
        unreachable and the span bound as the only detector — B14's recorded
        failure with §6.5's structural signals switched off. Asserting the
        *signal* rather than merely `armed` is what makes this test notice a
        regression that arms the session but arms it too late to matter.
        """
        h = Harness(alive=False, pid=17)
        outcome = h.window.run(sleep=lambda _seconds: h.monotonic.advance(601.0))

        self.assertTrue(outcome.session.armed)
        self.assertIs(outcome.signal, fw.FinalizationSignal.PROCESS_DEAD)

    def test_run_arms_a_session_whose_launch_captured_no_pid(self):
        """No pid is not a reason to leave the other two signals switched off.

        A herdr-spawned reviewer may legitimately have no pid; the turn clock
        and the quiescence latch still need `launched_at`, and only the
        liveness check is inapplicable.
        """
        h = Harness(records=0)
        outcome = h.window.run(sleep=lambda _seconds: h.monotonic.advance(11.0))

        self.assertTrue(outcome.session.armed)
        self.assertIs(outcome.signal, fw.FinalizationSignal.TURN_TIMEOUT)

    def test_the_span_clock_starts_before_the_launch_call(self):
        """§16.3 item 33 records that the launch path before the window
        opens is outside every window. This window narrows that gap as far
        as a caller-supplied launch allows: the span clock is stamped
        first, so a slow launch spends the window's own budget rather than
        running unbounded beside it. It does not close item 33 -- a launch
        that never returns still never reaches a poll."""
        h = Harness()
        h.monotonic.advance(0.0)
        session = h.window.open()
        self.assertEqual(session.launched_at, None)

    def test_a_window_is_opened_once(self):
        h = Harness()
        h.window.open()
        with self.assertRaises(RuntimeError):
            h.window.open()

    def test_polling_before_opening_is_refused(self):
        h = Harness()
        with self.assertRaises(RuntimeError):
            h.window.poll()


class SignalsArmAtReportedLaunch(unittest.TestCase):
    """§7.6/§6.5: *not yet started*, *working*, and *stopped without
    declaring* are three different answers."""

    def test_a_dead_process_before_arming_does_not_convict(self):
        h = Harness(alive=False)
        h.window.open()
        h.monotonic.advance(30.0)
        self.assertIsNone(h.window.poll())
        self.assertEqual(h.killed, [])

    def test_a_silent_reviewer_before_arming_does_not_start_the_turn_clock(self):
        """A cold start longer than the turn timeout is not a stalled
        turn: no turn has begun."""
        h = Harness(config=make_config(turn_timeout_s=10.0))
        h.window.open()
        h.monotonic.advance(500.0)
        self.assertIsNone(h.window.poll())

    def test_the_turn_clock_starts_at_launch_not_at_open(self):
        h = Harness(config=make_config(turn_timeout_s=10.0))
        h.window.open()
        h.monotonic.advance(500.0)
        h.window.report_launched(pid=4321)
        h.monotonic.advance(9.0)
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(2.0)
        outcome = h.window.poll()
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.signal, fw.FinalizationSignal.TURN_TIMEOUT)

    def test_reported_launch_stamps_the_windows_own_clock(self):
        h = Harness()
        h.window.open()
        h.monotonic.advance(12.0)
        session = h.window.report_launched(pid=99)
        self.assertEqual(session.launched_at, 12.0)
        self.assertTrue(session.armed)
        self.assertEqual(session.pid, 99)

    def test_an_advancing_turn_count_defers_the_turn_timeout(self):
        """A completed turn moves the clock forward; the count anchors at
        launch on the first observation, exactly as §7.6's heartbeat cache
        does, so only a *further* record defers anything."""
        h = Harness(config=make_config(turn_timeout_s=10.0))
        h.window.open()
        h.window.report_launched(pid=7)
        h.monotonic.advance(9.0)
        self.assertIsNone(h.window.poll())          # anchored at launch, 9s < 10s
        h.monotonic.advance(9.0)
        h.records = 1                                # a turn completed at t=18
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(9.0)                     # 9s since that turn
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(2.0)                     # 11s since that turn
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.TURN_TIMEOUT)


class AReviewerThatNeverGoesLiveIsAFailedStart(unittest.TestCase):
    """#89's acceptance. Seven reviews in
    `run-2a44d226e75a4be391a14f02b78a6d25` took a receipt lock and wrote no
    receipt, and the only instrument that could end one of them was the
    600-second span bound. A reviewer that has never once been reported
    working, with an empty transcript, is knowable long before that."""

    def test_a_reviewer_that_never_works_is_not_waited_out_on_the_span(self):
        """The regression this test exists for: if `NEVER_STARTED` stops
        firing, the window falls back to `finalization_timeout_s` and this
        assertion catches it by naming the signal *and* the elapsed time."""
        h = Harness(config=make_config(finalization_timeout_s=600.0,
                                       turn_timeout_s=1_000.0,
                                       start_deadline_s=30.0),
                    status="idle", pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(29.0)
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(2.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.NEVER_STARTED)
        self.assertFalse(outcome.observed_working)
        # Far below the span bound: the whole point is not paying 600s for it.
        self.assertLess(outcome.elapsed_s, 600.0)

    def test_a_reviewer_that_worked_is_never_called_a_failed_start(self):
        h = Harness(config=make_config(turn_timeout_s=1_000.0,
                                       start_deadline_s=30.0),
                    status="working", pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(31.0)
        self.assertIsNone(h.window.poll())

    def test_a_transcript_record_alone_refutes_a_failed_start(self):
        """It wrote something. Whatever else is wrong with it, it started."""
        h = Harness(config=make_config(turn_timeout_s=1_000.0,
                                       start_deadline_s=30.0),
                    status="idle", pid=5, records=1)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(31.0)
        self.assertIsNone(h.window.poll())

    def test_an_unreadable_status_is_never_a_failed_start(self):
        """A missing observation is not evidence. Convicting on one would
        kill every healthy reviewer whenever the route hiccups."""
        h = Harness(config=make_config(turn_timeout_s=1_000.0,
                                       start_deadline_s=30.0),
                    status=None, pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(31.0)
        self.assertIsNone(h.window.poll())

    def test_a_window_with_no_status_reader_cannot_reach_the_signal(self):
        """A window built without one cannot observe "never reported
        working", so it must not convict on it. Both shipped call sites now
        pass a reader; this is the contract for one that cannot."""
        h = Harness(config=make_config(turn_timeout_s=1_000.0,
                                       start_deadline_s=30.0), pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(31.0)
        self.assertIsNone(h.window.poll())

    def test_the_failed_start_deadline_does_not_apply_before_arming(self):
        h = Harness(config=make_config(start_deadline_s=30.0), status="idle")
        h.window.open()
        h.monotonic.advance(31.0)
        self.assertIsNone(h.window.poll())


class QuiescenceIsConfirmedNotSampled(unittest.TestCase):
    """One `idle` sample is not evidence that a reviewer stopped.

    Herdr reports a pane as `idle` both between turns and while the agent is
    blocked inside a tool call, and the liveness latch is already set by the
    time either happens — so a single sample cannot tell a pause from a stop
    and convicts both. What separates them is whether records are still
    appearing, which is why the confirmation reads the transcript rather than
    only the clock.
    """

    @staticmethod
    def _worked_then_idle(**config_overrides):
        h = Harness(config=make_config(turn_timeout_s=1_000.0,
                                       quiescence_confirm_s=60.0,
                                       **config_overrides),
                    status="working", pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.records = 1
        h.window.poll()                      # latches "was working"
        h.status = "idle"
        return h

    def test_one_idle_sample_does_not_convict(self):
        h = self._worked_then_idle()
        h.monotonic.advance(1.0)
        self.assertIsNone(h.window.poll())
        self.assertEqual(h.killed, [])

    def test_an_advancing_transcript_restarts_the_confirmation(self):
        """The load-bearing half. The pane says `idle` at every sample
        because the agent is blocked in a tool call, but records keep
        appearing — so the confirmation never completes and the reviewer
        is left alone."""
        h = self._worked_then_idle()
        for turn in range(2, 12):
            h.monotonic.advance(30.0)        # half the confirmation each time
            h.records = turn                 # ...but the transcript advanced
            self.assertIsNone(h.window.poll())
        self.assertEqual(h.killed, [])

    def test_returning_to_working_restarts_the_confirmation(self):
        h = self._worked_then_idle()
        self.assertIsNone(h.window.poll())   # the confirmation starts here
        h.monotonic.advance(30.0)
        h.status = "working"
        self.assertIsNone(h.window.poll())
        h.status = "idle"
        h.monotonic.advance(31.0)            # 61s after the first idle sample
        self.assertIsNone(h.window.poll())   # ...but only 31s of held idle

    def test_an_unreadable_status_restarts_the_confirmation(self):
        h = self._worked_then_idle()
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(30.0)
        h.status = None
        self.assertIsNone(h.window.poll())
        h.status = "idle"
        h.monotonic.advance(31.0)
        self.assertIsNone(h.window.poll())

    def test_a_reviewer_silent_inside_a_blocking_wait_is_still_convicted(self):
        """The confirmation must not become a way to keep a reviewer alive.

        Every failed review in `run-2a44d226e75a4be391a14f02b78a6d25` died
        holding a blocking `hub op=wait` on a sub-task it had spawned, and in
        all five that reached a session the transcript was silent for the whole
        wait — 2.4s, 14.6s, 30.9s, 35.8s and 76.4s before the exit. Silence is
        exactly what this detector reads, so that shape is convicted on the
        confirmation interval rather than deferred by it.
        """
        h = self._worked_then_idle()
        self.assertIsNone(h.window.poll())
        for _ in range(7):
            h.monotonic.advance(10.0)        # a blocking wait writes nothing
            outcome = h.window.poll()
            if outcome is not None:
                break
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.signal, fw.FinalizationSignal.ACTOR_ABANDONED)
        # One poll interval past the confirmation, and two orders of magnitude
        # inside the 600s span this replaces as the instrument of record.
        self.assertLessEqual(outcome.elapsed_s, 70.0)

    def test_idle_held_with_a_silent_transcript_still_convicts(self):
        """B14 is not repealed: a reviewer that really did stop without
        declaring is still caught, and still before any wall clock."""
        h = self._worked_then_idle()
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(61.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.ACTOR_ABANDONED)
        self.assertTrue(outcome.observed_working)
        # The harness's span bound is 600s; the point is not paying it.
        self.assertLess(outcome.elapsed_s, 600.0)

    def test_the_two_failures_are_told_apart_by_a_typed_field(self):
        """A caller deciding what to do with a failed review needs "never
        began" and "began and stopped" as data, not as a signal name it has
        to know the meaning of."""
        started = self._worked_then_idle()
        started.window.poll()
        started.monotonic.advance(61.0)
        self.assertTrue(started.window.poll().observed_working)

        never = Harness(config=make_config(turn_timeout_s=1_000.0,
                                           start_deadline_s=30.0),
                        status="idle", pid=5)
        never.window.open()
        never.window.report_launched(pid=5)
        never.monotonic.advance(31.0)
        self.assertFalse(never.window.poll().observed_working)


class TheNewDeadlinesAreOptionalOverrides(unittest.TestCase):
    """A deployment's `maestro.config.yaml` predates both keys. A config
    that does not mention them must build and behave."""

    def test_a_config_without_them_takes_the_in_code_defaults(self):
        config = fw.FinalizationConfig(finalization_timeout_s=600.0,
                                       turn_timeout_s=120.0)
        self.assertEqual(config.start_deadline_s, fw.DEFAULT_START_DEADLINE_S)
        self.assertEqual(config.quiescence_confirm_s,
                         fw.DEFAULT_QUIESCENCE_CONFIRM_S)

    def test_the_defaults_fire_well_inside_a_six_hundred_second_span(self):
        """Both defaults have to beat the span bound they exist to replace,
        or nothing changed for the run that raised #89."""
        self.assertLess(fw.DEFAULT_START_DEADLINE_S, 600.0)
        self.assertLess(fw.DEFAULT_QUIESCENCE_CONFIRM_S, 600.0)

    def test_non_positive_deadlines_are_refused_like_every_other_clock(self):
        for field in ("start_deadline_s", "quiescence_confirm_s"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    make_config(**{field: 0.0})

    def test_neither_deadline_can_outlast_the_span_it_sits_inside(self):
        """Obligation (b): the span bound is over everything. A detector
        configured longer than the window would never fire, and the window
        would be back to being the only instrument."""
        config = make_config(finalization_timeout_s=30.0,
                             start_deadline_s=120.0,
                             quiescence_confirm_s=90.0)
        self.assertEqual(config.effective_start_deadline_s, 30.0)
        self.assertEqual(config.effective_quiescence_confirm_s, 30.0)

    def test_an_over_long_setting_is_bounded_rather_than_refused(self):
        """Bounding, not refusing: an installation may run a window shorter
        than these defaults, and refusing that config would make a defaulted
        field required — which is what these defaults exist to prevent."""
        config = make_config(finalization_timeout_s=10.0)
        self.assertEqual(config.start_deadline_s, fw.DEFAULT_START_DEADLINE_S)
        self.assertEqual(config.effective_start_deadline_s, 10.0)


class ClocksAreNeverMixed(unittest.TestCase):
    """Lifecycle stores epoch seconds; the watchdog measures monotonic.
    An earlier defect in this build came from comparing one against the
    other."""

    def test_an_epoch_wall_clock_never_expires_a_monotonic_window(self):
        h = Harness(config=make_config(finalization_timeout_s=600.0),
                    monotonic_start=0.0, epoch_start=1_760_000_000.0)
        h.window.open()
        h.window.report_launched(pid=5)
        self.assertIsNone(h.window.poll())

    def test_the_session_row_carries_the_epoch_stamp_separately(self):
        h = Harness(monotonic_start=0.0, epoch_start=1_760_000_000.0)
        session = h.window.open()
        self.assertEqual(session.opened_at_epoch, 1_760_000_000.0)

    def test_the_epoch_clock_advancing_alone_never_expires_the_window(self):
        h = Harness(config=make_config(finalization_timeout_s=60.0))
        h.window.open()
        h.window.report_launched(pid=5)
        h.epoch.advance(10_000.0)
        self.assertIsNone(h.window.poll())

    def test_elapsed_is_reported_in_the_monotonic_span(self):
        h = Harness(config=make_config(finalization_timeout_s=60.0))
        h.window.open()
        h.monotonic.advance(61.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.elapsed_s, 61.0)


class ExpiryConvertsToAKillAndATypedOutcome(unittest.TestCase):

    def test_the_span_bound_fires_even_while_turns_advance(self):
        """(b) of the silence rule: one span-bounding wall clock over the
        whole window, which earlier-firing detectors never replace."""
        h = Harness(config=make_config(finalization_timeout_s=100.0,
                                       turn_timeout_s=1_000.0))
        h.window.open()
        h.window.report_launched(pid=11)
        h.monotonic.advance(101.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.WINDOW_TIMEOUT)
        self.assertFalse(outcome.completed)
        self.assertIsNone(outcome.report)

    def test_a_dead_process_after_arming_convicts_immediately(self):
        h = Harness()
        h.window.open()
        h.window.report_launched(pid=3)
        h.alive = False
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.PROCESS_DEAD)

    def test_a_real_exited_process_is_seen_as_dead(self):
        """Process liveness read from the OS, not from a mock and never
        from pane text (§9.7)."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        h = Harness()
        h.window = fw.FinalizationWindow(
            config=make_config(),
            launch=h._launch,
            poll_report=h._poll_report,
            kill=h._kill,
            time_source=h.monotonic,
            wall_clock=h.epoch,
        )
        h.window.open()
        h.window.report_launched(pid=proc.pid)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.PROCESS_DEAD)

    def test_a_harness_owned_group_is_killed(self):
        h = Harness(config=make_config(finalization_timeout_s=10.0),
                    harness_owned_group=True)
        h.window.open()
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertTrue(outcome.killed)
        self.assertEqual(len(h.killed), 1)

    def test_a_pane_maestro_does_not_own_is_not_killed(self):
        """§6.5: a herdr-spawned reviewer under the recorded 0.8.0 surface
        has no group Maestro owns, so the verb stops waiting and reports
        the pane. The survivor is a leak, not a hazard (§7.8)."""
        h = Harness(config=make_config(finalization_timeout_s=10.0),
                    harness_owned_group=False)
        h.window.open()
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertFalse(outcome.killed)
        self.assertEqual(h.killed, [])
        self.assertEqual(outcome.signal, fw.FinalizationSignal.WINDOW_TIMEOUT)

    def test_the_stall_is_returned_as_one_typed_result_not_a_side_effect(self):
        """(c)'s durable half is the outcome this returns, and there is no
        second channel for it.

        The window used to take a `StallRecorder` alongside, and both
        production call sites passed `lambda ...: None` — a seam that read as
        wired while recording nothing, which is how four distinct signals came
        to settle as one free-text reason. Recording now belongs to whoever
        called, against the store that owns its phase, and everything such a
        caller needs is on this object.
        """
        h = Harness(config=make_config(finalization_timeout_s=10.0))
        h.window.open()
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertFalse(outcome.completed)
        self.assertEqual(outcome.session.session_id, "sess-7")
        self.assertEqual(outcome.signal, fw.FinalizationSignal.WINDOW_TIMEOUT)
        self.assertEqual(outcome.elapsed_s, 11.0)

    def test_the_window_takes_no_stall_recorder(self):
        """A no-op collaborator nothing writes is the same B15 smell one
        level down: it makes an absent record look like a present one."""
        self.assertNotIn(
            "record_stall",
            inspect.signature(fw.FinalizationWindow.__init__).parameters)
        self.assertFalse(hasattr(fw, "StallRecorder"))

    def test_the_outcome_carries_the_route_model_and_session_id(self):
        """§6.5: the verb prints the reviewer's recorded (route, model,
        session id) and the signal that fired."""
        h = Harness(config=make_config(finalization_timeout_s=10.0))
        h.window.open()
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertEqual(
            (outcome.session.route, outcome.session.model,
             outcome.session.session_id),
            ("omp", "opus", "sess-7"))

    def test_polling_after_a_stall_is_refused(self):
        """The window converts expiry once; a second conversion would
        double-kill and write a second stall record."""
        h = Harness(config=make_config(finalization_timeout_s=10.0))
        h.window.open()
        h.monotonic.advance(11.0)
        h.window.poll()
        with self.assertRaises(RuntimeError):
            h.window.poll()


class ACompletedReviewLeavesTheWindow(unittest.TestCase):

    def test_a_report_ends_the_window_with_no_kill(self):
        h = Harness(report_after_polls=2)
        h.window.open()
        h.window.report_launched(pid=13)
        self.assertIsNone(h.window.poll())
        outcome = h.window.poll()
        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.report, {"plan_digest": "abc"})
        self.assertIsNone(outcome.signal)
        self.assertFalse(outcome.killed)
        self.assertEqual(h.killed, [])

    def test_run_drives_the_loop_to_completion_without_sleeping(self):
        h = Harness(report_after_polls=3)
        ticks = []
        outcome = h.window.run(sleep=lambda s: ticks.append(s))
        self.assertTrue(outcome.completed)
        self.assertGreaterEqual(len(ticks), 1)

    def test_run_returns_the_stalled_outcome_rather_than_looping_forever(self):
        h = Harness(config=make_config(finalization_timeout_s=5.0),
                    report_after_polls=None)

        def tick(seconds):
            h.monotonic.advance(2.0)

        outcome = h.window.run(sleep=tick)
        self.assertFalse(outcome.completed)
        self.assertEqual(outcome.signal, fw.FinalizationSignal.WINDOW_TIMEOUT)


class TheTurnClockCannotOutvoteALiveObservation(unittest.TestCase):
    """The regression these exist for is `cmo-consolidation-l-r5`:

        FINALIZATION_STALLED: route=omp model=openai-codex/gpt-5.6-luna
        session_id=w146:p2 signal=TURN_TIMEOUT after 128.6s

    The reviewer was not stalled. Pane `w146:p2` was still alive after the
    verb gave up, its revision counter still climbing and its transcript
    still growing. `TURN_TIMEOUT` reads transcript silence, and a reviewer
    thinking for longer than the turn timeout is silent in exactly the way a
    dead one is — which is why B14 forbids deciding this on a wall clock and
    why the check is now gated on what the route reports, the same way
    `ACTOR_ABANDONED` is.
    """

    @staticmethod
    def _armed(status, **config_overrides):
        fields = dict(finalization_timeout_s=100_000.0, turn_timeout_s=10.0)
        fields.update(config_overrides)
        h = Harness(config=make_config(**fields), status=status, pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        return h

    def test_an_actor_reported_working_is_never_convicted_by_the_turn_clock(self):
        """The defect, directly. A route saying `working` at every poll,
        with a transcript that never grows, past many multiples of the turn
        timeout: the window stays open."""
        h = self._armed("working")
        for _ in range(40):
            h.monotonic.advance(25.0)        # 2.5x turn_timeout_s each poll
            self.assertIsNone(h.window.poll())
        self.assertGreaterEqual(h.monotonic(), 100 * 10.0)

    def test_an_actor_reported_blocked_is_treated_the_same(self):
        """`blocked` is a live working status too — an agent inside a tool
        call is working, and its transcript does not grow while it waits."""
        h = self._armed("blocked")
        h.monotonic.advance(500.0)
        self.assertIsNone(h.window.poll())

    def test_an_actor_reported_not_working_still_stalls_on_the_turn_clock(self):
        """The signal keeps its job. Reported at its composer, nothing
        written, past the turn timeout: this is the state `TURN_TIMEOUT` was
        always for, and it still converts."""
        h = self._armed("idle", start_deadline_s=100_000.0,
                        quiescence_confirm_s=100_000.0)
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.signal, fw.FinalizationSignal.TURN_TIMEOUT)
        self.assertFalse(outcome.completed)

    def test_a_window_with_no_status_reader_still_stalls_on_the_turn_clock(self):
        """Absence of a reader is not an observation of work. Such a window
        has only this clock and PROCESS_DEAD beneath the span bound, and
        disarming it there would leave the span as the only detector — the
        state B14 was recorded against."""
        h = Harness(config=make_config(finalization_timeout_s=100_000.0,
                                       turn_timeout_s=10.0), pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.TURN_TIMEOUT)

    def test_a_route_that_stops_reporting_working_becomes_convictable(self):
        """The gate reads what the route says *now*, not the whole-window
        liveness latch. A reviewer that worked and then stopped must still
        convert, or the fix would trade one silent failure for another."""
        h = self._armed("working", start_deadline_s=100_000.0,
                        quiescence_confirm_s=100_000.0)
        h.monotonic.advance(500.0)
        self.assertIsNone(h.window.poll())
        h.status = "idle"
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.TURN_TIMEOUT)

    def test_an_unreadable_status_does_not_clear_a_live_observation(self):
        """A route that hiccups says nothing about the reviewer. The last
        readable status stands, exactly as it does for quiescence."""
        h = self._armed("working")
        h.monotonic.advance(500.0)
        self.assertIsNone(h.window.poll())
        h.status = None
        h.monotonic.advance(500.0)
        self.assertIsNone(h.window.poll())

    def test_a_never_readable_status_still_stalls_on_the_turn_clock(self):
        """A reader that has never once answered has produced no observation
        of work, so it cannot excuse the silence."""
        h = self._armed("idle", start_deadline_s=100_000.0,
                        quiescence_confirm_s=100_000.0)
        h.status = None
        h.monotonic.advance(11.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.TURN_TIMEOUT)

    def test_quiescence_after_liveness_still_fires_before_the_turn_clock(self):
        """B14's own detector is unchanged and still the one that names a
        reviewer which stopped without declaring."""
        h = self._armed("working", turn_timeout_s=100_000.0,
                        quiescence_confirm_s=60.0)
        h.records = 1
        h.window.poll()                       # latches "was working"
        h.status = "idle"
        h.monotonic.advance(1.0)
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(61.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal,
                         fw.FinalizationSignal.ACTOR_ABANDONED)
        self.assertTrue(outcome.observed_working)

    def test_a_new_record_still_restarts_the_quiescence_confirmation(self):
        """Any record appearing inside the confirmation interval restarts
        it from that record, so a reviewer paused inside a tool call is not
        convicted for pausing."""
        h = self._armed("working", turn_timeout_s=100_000.0,
                        quiescence_confirm_s=60.0)
        h.records = 1
        h.window.poll()
        h.status = "idle"
        h.monotonic.advance(59.0)
        self.assertIsNone(h.window.poll())
        h.records = 2                          # it is still writing
        h.monotonic.advance(59.0)
        self.assertIsNone(h.window.poll())     # confirmation restarted
        h.monotonic.advance(2.0)
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(60.0)
        self.assertEqual(h.window.poll().signal,
                         fw.FinalizationSignal.ACTOR_ABANDONED)

    def test_a_genuinely_hung_reviewer_still_converges_at_the_span_bound(self):
        """Termination, named. A route reporting `working` forever with a
        transcript that never grows now clears every gated detector — so the
        signal that ends the window is `WINDOW_TIMEOUT`, obligation (b)'s one
        span-bounding wall clock, and it still converts to a kill and a red
        outcome rather than hanging."""
        h = Harness(config=make_config(finalization_timeout_s=600.0,
                                       turn_timeout_s=10.0),
                    status="working", pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(599.0)
        self.assertIsNone(h.window.poll())
        h.monotonic.advance(2.0)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.WINDOW_TIMEOUT)
        self.assertFalse(outcome.completed)
        self.assertTrue(outcome.killed)
        self.assertEqual(h.killed, [h.window.session])

    def test_the_window_still_completes_when_the_report_arrives(self):
        """The gate must not keep a finished review open: the report check
        runs before every detector and is unaffected."""
        h = Harness(config=make_config(finalization_timeout_s=100_000.0,
                                       turn_timeout_s=10.0),
                    status="working", pid=5, report_after_polls=2)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(500.0)
        self.assertIsNone(h.window.poll())
        outcome = h.window.poll()
        self.assertTrue(outcome.completed)
        self.assertIsNone(outcome.signal)


class TheTurnClockHasOneInCodeDefault(unittest.TestCase):
    """Two literals for one clock is how a raised default comes to look like
    it did nothing: the CLI default binds last on the unconfigured path and
    silently wins whatever the module says."""

    def test_the_module_names_the_default(self):
        self.assertEqual(fw.DEFAULT_TURN_TIMEOUT_S, 900.0)

    def test_the_default_is_not_the_width_that_convicted_a_live_reviewer(self):
        """128.6s against a 120s clock is the incident. The gate above is the
        fix; this only stops the not-working case being trigger-happy."""
        self.assertGreater(fw.DEFAULT_TURN_TIMEOUT_S, 128.6)

    def test_no_cli_flag_supplies_a_second_default_for_the_turn_clock(self):
        """`plan finalize --reviewer-turn-timeout-s` was the second literal.

        It carried its own default and bound last on the unconfigured path,
        which is how a raised module constant comes to look like it did
        nothing. The flag is gone with the reviewer that verb used to launch,
        and no other verb replaced it, so the clock has exactly two writers
        left: this module's constant and an installation's
        `reviewer.turn_timeout_s`.
        """
        self.assertNotIn("--reviewer-turn-timeout-s",
                         _every_flag(maestro.build_parser()))

    def test_maestro_never_types_a_literal_for_this_clock(self):
        """The property the flag's default violated, stated over the source.

        Every assignment to `reviewer_turn_timeout_s` in maestro.py must read
        an installation's configuration. A constant there is a second literal
        wherever it sits, flag or not.
        """
        source = (Path(maestro.__file__)).read_text(encoding="utf-8")
        assignments = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Attribute)
                    and t.attr == "reviewer_turn_timeout_s"
                    for t in node.targets)
        ]
        self.assertTrue(assignments, "the run path still binds the clock")
        for node in assignments:
            self.assertIsInstance(node.value, ast.Subscript)

    def test_an_installed_configuration_still_governs_its_own_reviewer(self):
        """The in-code default is the *unconfigured* default, and saying so
        is half the fix. In an installed repository `plan finalize` binds
        `reviewer.turn_timeout_s` from `maestro.config.yaml` — a required
        key, deployment-owned and never mirrored — so it, not this constant,
        is what a deployment's reviewer runs under. A deployment wanting the
        wider clock raises its own config, and `_validate_review_clocks`
        requires it to raise `finalization_timeout_s` past it at the same
        time. Nothing in-code can reach across that boundary, which is why
        the liveness gate rather than the width is the fix that ships.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "project"
            (repo / "adws").mkdir(parents=True)
            (repo / "plans").mkdir(parents=True)
            binaries = {}
            for binary_name in ("herdr", "omp", "claude"):
                binary = root / binary_name
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)
                binaries[binary_name] = str(binary)
            config_path = repo / "adws" / "maestro.config.yaml"
            config_path.write_text(json.dumps({
                "schema": "maestro-config.v1",
                "plans_dir": "plans",
                "state_root": "../maestro-state",
                "keys": {
                    "verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
                    "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
                    "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY",
                },
                "executables": binaries,
                "route_receipts": {"omp": "route-receipts/omp.json"},
                "reviewer": {
                    "route": "omp", "model": "review-model", "effort": "high",
                    "finalization_timeout_s": 60, "turn_timeout_s": 20,
                    "poll_interval_s": 1,
                },
                "execution": {
                    "route": "omp", "model": "execution-model",
                    "effort": "medium", "concurrency": 2,
                    "node_timeout_s": 120, "turn_timeout_s": 30,
                    "final_acceptance_timeout_s": 45, "backstop_t_s": 600,
                    "semantic_ceiling": 3,
                },
            }), encoding="utf-8")

            layout = maestro._load_maestro_layout(repo, config_path)

            self.assertEqual(layout["reviewer"]["turn_timeout_s"], 20)
            self.assertNotEqual(layout["reviewer"]["turn_timeout_s"],
                                fw.DEFAULT_TURN_TIMEOUT_S)


class TheFailedStartClocksAreOperatorTunable(unittest.TestCase):
    """#100: start_deadline_s and quiescence_confirm_s are settings, so they
    belong on the tuning side of `_RUN_TUNING_OPTIONS`, and the review
    stage must actually read the override — a flag nothing consumes is
    B15 again."""

    CLOCKS = (
        ("--review-start-deadline-s", "review_start_deadline_s",
         "start_deadline_s"),
        ("--review-quiescence-confirm-s", "review_quiescence_confirm_s",
         "quiescence_confirm_s"),
    )

    def _config_call(self, tree: ast.AST) -> ast.Call:
        runner = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_code_review_runner")
        calls = [
            node for node in ast.walk(runner)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name)
                 and node.func.id == "FinalizationConfig")
                or (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "FinalizationConfig"))]
        self.assertEqual(1, len(calls), "expected one FinalizationConfig")
        return calls[0]

    def test_each_clock_is_a_tuning_flag_the_parser_declares(self):
        flags = _every_flag(maestro.build_parser())
        for option, dest, _field in self.CLOCKS:
            self.assertIn(option, flags, option + " is not a CLI flag")
            self.assertEqual(maestro._RUN_TUNING_OPTIONS[option], dest)

    def test_the_review_stage_reads_each_override_from_arguments(self):
        source = Path(maestro.__file__).read_text(encoding="utf-8")
        passed = {kw.arg: kw.value
                  for kw in self._config_call(ast.parse(source)).keywords}
        for _option, dest, field in self.CLOCKS:
            self.assertIn(field, passed, field + " is not passed")
            names = [node.attr for node in ast.walk(passed[field])
                     if isinstance(node, ast.Attribute)]
            self.assertIn(dest, names,
                          field + " is not read from args." + dest)

    def test_the_detector_convicts_a_config_that_dropped_them(self):
        planted = ast.parse(
            "def _code_review_runner(args, runner):\n"
            "    return FinalizationConfig(\n"
            "        finalization_timeout_s=args.review_timeout_s,\n"
            "        turn_timeout_s=args.reviewer_turn_timeout_s)\n")
        keywords = {kw.arg for kw in self._config_call(planted).keywords}
        self.assertNotIn("start_deadline_s", keywords)
        self.assertNotIn("quiescence_confirm_s", keywords)

    def test_a_short_start_override_is_the_number_never_started_reads(self):
        h = Harness(config=make_config(finalization_timeout_s=600.0,
                                       turn_timeout_s=1_000.0,
                                       start_deadline_s=7.0),
                    status="idle", pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.monotonic.advance(7.1)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.NEVER_STARTED)
        self.assertLess(outcome.elapsed_s, 30.0)

    def test_a_short_quiescence_override_is_the_number_abandoned_reads(self):
        h = Harness(config=make_config(turn_timeout_s=1_000.0,
                                       start_deadline_s=100_000.0,
                                       quiescence_confirm_s=4.0),
                    status="working", pid=5)
        h.window.open()
        h.window.report_launched(pid=5)
        h.records = 1
        h.window.poll()
        h.status = "idle"
        h.window.poll()
        h.monotonic.advance(4.1)
        outcome = h.window.poll()
        self.assertEqual(outcome.signal, fw.FinalizationSignal.ACTOR_ABANDONED)


def _every_flag(parser) -> "set[str]":
    """Every option string the CLI exposes, subparsers included.

    `choices` is a mapping only on a subparsers action; on an ordinary
    `--flag` with a value set it is a plain sequence. Descending on the
    mapping shape rather than assuming one is what keeps this walker from
    raising the first time any flag declares `choices=(...)`.
    """
    flags = set()
    pending = [parser]
    while pending:
        current = pending.pop()
        for action in current._actions:
            flags.update(action.option_strings)
            choices = getattr(action, "choices", None)
            if not hasattr(choices, "values"):
                continue
            for choice in choices.values():
                if hasattr(choice, "_actions"):
                    pending.append(choice)
    return flags


if __name__ == "__main__":
    unittest.main()
