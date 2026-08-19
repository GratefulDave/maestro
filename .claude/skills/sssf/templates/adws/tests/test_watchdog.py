"""Executable proof of liveness (§7.6) and the run-level backstop (§11.2).

Two mechanisms are under test here, neither owning a store:

  Watchdog     -- the single scheduler-owned thread polling every RUNNING
                  attempt, the only heartbeat writer, arming its
                  process-alive and turn-count signals at launch rather
                  than at attempt start (§7.6).
  RunBackstop  -- the run-level "no progress" timer, a timer rather than
                  an in-flight count, reading only the lifecycle tier
                  (§11.2).

The clock is always injected (`time_source`), so nothing here sleeps out a
real timeout. Where the signal under test is process liveness, a real
`subprocess.Popen` is used instead of a mock, because that signal is the
one measurement forced (§7.6, §9.4) and a mock would only assert our own
assumption back at us.

Run with:  uv run adws/adw_test.py -k watchdog
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import watchdog as wd  # noqa: E402

WATCHDOG_SOURCE_PATH = ADWS / "adw_modules" / "watchdog.py"
WATCHDOG_SOURCE = WATCHDOG_SOURCE_PATH.read_text(encoding="utf-8")


def make_config(**overrides) -> st.SchedulerConfig:
    fields = dict(
        concurrency=2,
        node_timeout_s=100.0,
        turn_timeout_s=10.0,
        final_acceptance_timeout_s=50.0,
        backstop_t_s=1_000_000.0,
        semantic_ceiling=3,
    )
    fields.update(overrides)
    return st.SchedulerConfig(**fields)


def make_attempt(**overrides) -> st.AttemptRecord:
    fields = dict(
        run_id="run-1",
        node_id="node-1",
        attempt_no=1,
        base_sha="deadbeef",
        state=st.NodeState.RUNNING,
        started_at=0.0,
        launched_at=None,
        pid=None,
        turn_count=0,
        extra={},
    )
    fields.update(overrides)
    return st.AttemptRecord(**fields)


class FakeClock:
    """An injectable `time_source`: advances only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def set(self, value: float) -> None:
        self._now = value


class Recorder:
    """A spy collaborator recording every call it receives."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def write_jsonl(path: Path, lines, trailing_newline: bool = True) -> None:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")


# ── §7.6 arming: the pre-launch segment has only the wall clock ─────────────

class ArmingTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock(0.0)
        self.kill = Recorder()
        self.fail = Recorder()
        self.heartbeat = Recorder()
        self.config = make_config(node_timeout_s=100.0, turn_timeout_s=10.0)

    def _watchdog(self, attempts):
        return wd.Watchdog(
            config=self.config,
            attempts_provider=lambda: attempts,
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            time_source=self.clock,
        )

    def test_prelaunch_first_poll_is_not_killed(self):
        """A faithful implementation must not kill every attempt at its
        first pre-launch poll (§7.6). No agent process and no transcript
        exist yet -- that is undefined by construction, not a stall."""
        attempt = make_attempt(started_at=0.0, launched_at=None, pid=None)
        watchdog = self._watchdog([attempt])
        self.clock.set(0.001)

        watchdog.check_once()

        self.assertEqual(self.kill.calls, [])
        self.assertEqual(self.fail.calls, [])

    def test_prelaunch_exceeding_node_timeout_times_out(self):
        """Elapsed wall clock beyond the node timeout applies during the
        pre-launch segment too -- the whole attempt window is bound by it,
        with or without an agent process yet."""
        attempt = make_attempt(started_at=0.0, launched_at=None, pid=None)
        watchdog = self._watchdog([attempt])
        self.clock.set(101.0)  # > node_timeout_s

        watchdog.check_once()

        self.assertEqual(len(self.kill.calls), 1)
        self.assertEqual(len(self.fail.calls), 1)
        (fail_args, _) = self.fail.calls[0]
        self.assertIs(fail_args[1], st.RetryClass.ENVIRONMENTAL)

    def test_turn_clock_starts_at_launch_not_attempt_start(self):
        """A faithful implementation must not start the turn clock over a
        cold `npm ci`: the attempt may have been started long before the
        agent launched, and the turn timeout must be measured from launch.
        """
        attempt = make_attempt(
            started_at=0.0, launched_at=100.0, pid=999999, turn_count=0)
        # A dedicated config: node_timeout_s must be well past 106s (the
        # simulated launch delay plus the turn check below), so only the
        # turn-timeout signal is under test here.
        config = make_config(node_timeout_s=10000.0, turn_timeout_s=10.0)
        watchdog = wd.Watchdog(
            config=config,
            attempts_provider=lambda: [attempt],
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            time_source=self.clock,
        )
        watchdog._process_alive = lambda pid: True  # isolate the turn signal

        # Just after launch: if the turn clock had wrongly started at
        # attempt start (100s ago), this would already exceed the 10s
        # turn timeout. It must not, because the clock starts at launch.
        self.clock.set(104.0)
        watchdog.check_once()
        self.assertEqual(self.fail.calls, [])

        # Genuinely 11s past launch with no turn completed: now it stalls.
        self.clock.set(111.0)
        watchdog.check_once()
        self.assertEqual(len(self.fail.calls), 1)
        (fail_args, _) = self.fail.calls[0]
        self.assertEqual(fail_args[2], wd.StallReason.TURN_TIMEOUT.value)


# ── §7.6 the three signals ───────────────────────────────────────────────────

class ProcessAliveSignalTests(unittest.TestCase):
    """Process liveness is the launched process polled directly (§7.6) --
    a real subprocess, never a mock, because this is the signal
    measurement forced."""

    def setUp(self):
        self.clock = FakeClock(0.0)
        self.kill = Recorder()
        self.fail = Recorder()
        self.heartbeat = Recorder()
        self.config = make_config(node_timeout_s=1000.0, turn_timeout_s=1000.0)

    def _watchdog(self, attempts):
        return wd.Watchdog(
            config=self.config,
            attempts_provider=lambda: attempts,
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            time_source=self.clock,
        )

    def test_dead_process_stalls_immediately_with_no_timeout(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            proc.wait(timeout=10)  # real process, really exited
            self.assertFalse(wd.process_is_alive(proc.pid))

            attempt = make_attempt(
                started_at=0.0, launched_at=0.0, pid=proc.pid)
            watchdog = self._watchdog([attempt])
            self.clock.set(0.001)  # barely any wall clock elapsed

            watchdog.check_once()

            self.assertEqual(len(self.kill.calls), 1)
            self.assertEqual(len(self.fail.calls), 1)
            (fail_args, _) = self.fail.calls[0]
            self.assertEqual(fail_args[2], wd.StallReason.PROCESS_DEAD.value)
        finally:
            proc.wait(timeout=10)

    def test_live_process_is_not_stalled(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            self.assertTrue(wd.process_is_alive(proc.pid))

            attempt = make_attempt(
                started_at=0.0, launched_at=0.0, pid=proc.pid)
            watchdog = self._watchdog([attempt])
            self.clock.set(0.5)

            watchdog.check_once()

            self.assertEqual(self.kill.calls, [])
            self.assertEqual(self.fail.calls, [])
        finally:
            proc.kill()
            proc.wait(timeout=10)


class TurnCountSignalTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock(0.0)
        self.kill = Recorder()
        self.fail = Recorder()
        self.heartbeat = Recorder()
        self.config = make_config(node_timeout_s=1000.0, turn_timeout_s=10.0)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.transcript = Path(self.tmpdir.name) / "session.jsonl"

    def _watchdog(self, attempts):
        watchdog = wd.Watchdog(
            config=self.config,
            attempts_provider=lambda: attempts,
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            time_source=self.clock,
        )
        watchdog._process_alive = lambda pid: True  # isolate the turn signal
        return watchdog

    def test_no_turn_completed_within_turn_timeout_stalls(self):
        write_jsonl(self.transcript, [])  # nothing written yet
        attempt = make_attempt(
            started_at=0.0, launched_at=0.0, pid=1,
            extra={wd.SESSION_PATH_KEY: str(self.transcript)})
        watchdog = self._watchdog([attempt])

        self.clock.set(11.0)  # > turn_timeout_s since launch
        watchdog.check_once()

        self.assertEqual(len(self.fail.calls), 1)
        (fail_args, _) = self.fail.calls[0]
        self.assertEqual(fail_args[2], wd.StallReason.TURN_TIMEOUT.value)

    def test_healthy_agent_midturn_is_not_stalled(self):
        """omp/Claude both write their transcript at turn granularity
        (§9.4): a healthy agent mid-turn produces an unchanging file for
        the whole turn -- 57.7s and 63.5s measured -- so an unchanging
        file within the turn timeout must not stall."""
        write_jsonl(self.transcript, ['{"role": "assistant", "n": 1}'])
        attempt = make_attempt(
            started_at=0.0, launched_at=0.0, pid=1,
            extra={wd.SESSION_PATH_KEY: str(self.transcript)})
        watchdog = self._watchdog([attempt])

        watchdog.check_once()  # observes record 1, arms the heartbeat
        self.clock.set(9.0)   # still inside the turn timeout, file unchanged
        watchdog.check_once()

        self.assertEqual(self.fail.calls, [])

    def test_turn_count_advancing_resets_the_stall_timer(self):
        write_jsonl(self.transcript, ['{"n": 1}'])
        attempt = make_attempt(
            started_at=0.0, launched_at=0.0, pid=1,
            extra={wd.SESSION_PATH_KEY: str(self.transcript)})
        watchdog = self._watchdog([attempt])

        watchdog.check_once()          # record 1 observed at t=0
        self.clock.set(9.0)
        write_jsonl(self.transcript, ['{"n": 1}', '{"n": 2}'])
        watchdog.check_once()          # record 2 observed at t=9, resets clock
        self.clock.set(17.0)           # 8s since the reset -- still healthy
        watchdog.check_once()

        self.assertEqual(self.fail.calls, [])

    def test_elapsed_wall_clock_beyond_node_timeout_times_out_regardless(self):
        """Even a steadily advancing turn count cannot outrun the node
        timeout (§7.6)."""
        config = make_config(node_timeout_s=20.0, turn_timeout_s=1000.0)
        write_jsonl(self.transcript, ['{"n": 1}'])
        attempt = make_attempt(
            started_at=0.0, launched_at=0.0, pid=1,
            extra={wd.SESSION_PATH_KEY: str(self.transcript)})
        watchdog = wd.Watchdog(
            config=config,
            attempts_provider=lambda: [attempt],
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            time_source=self.clock,
        )
        watchdog._process_alive = lambda pid: True

        watchdog.check_once()
        self.clock.set(21.0)  # > node_timeout_s, turn count still "fresh"
        watchdog.check_once()

        self.assertEqual(len(self.fail.calls), 1)
        (fail_args, _) = self.fail.calls[0]
        self.assertEqual(fail_args[2], wd.StallReason.NODE_TIMEOUT.value)


# ── §9.7 a declared result outranks both of the watchdog's clocks ───────────

class DeclaredResultOutranksTheSupervisorTests(unittest.TestCase):
    """§9.7 at the two clock signals: an artifact a worker wrote outranks
    any status a supervisor observes about that worker.

    Measured on run-9e9ac412669140039ae078601048f6c7. The worker quiesces
    the builder BEFORE it commits the work, runs the post gate, and
    dispatches the cross-vendor reviewer, so from that moment the builder's
    transcript can never grow again -- by construction, not by fault --
    while the watchdog goes on measuring exactly that file. Review latency
    tracks reviewer turn count at roughly 7s/turn over 15-64 turns: of ten
    reviews, latency ran 46s to 461s, and exactly the two that exceeded
    `turn_timeout_s=300` (461s and 368s) were killed ENVIRONMENTAL after
    their attempt had already written a success envelope, been committed by
    the scheduler, and had an adjudicated result row written. Not a race, a
    threshold -- so it recurs on any plan, and it billed an infra retry for
    a review rejection the reviewer returned seconds later, discarded the
    reviewer's findings, and burned a full review.

    It fell entirely on this branch because PROCESS_DEAD cannot convict an
    agent attempt at all: `attempt.pid` is `handle.process_group`, which
    herdr never populates (§16.3 item 17), so the two clocks are the only
    signals that reach an agent node.

    `declared_result_observed` is what closes it, and like
    `exit_status_observed` beside it the fact is structural: whether a
    typed `results` row exists for this exact `(run_id, node_id,
    attempt_no)` and what its `adjudication` enum says. Never the payload's
    prose, which §1.2 forbids any transition from keying on.
    """

    def setUp(self):
        self.clock = FakeClock(0.0)
        self.kill = Recorder()
        self.fail = Recorder()
        self.heartbeat = Recorder()
        self.config = make_config(
            node_timeout_s=1000.0, turn_timeout_s=10.0, backstop_t_s=5000.0)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.transcript = Path(self.tmpdir.name) / "session.jsonl"

    def _watchdog(self, attempts, declared, config=None):
        watchdog = wd.Watchdog(
            config=config or self.config,
            attempts_provider=lambda: attempts,
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            declared_result_observed=lambda attempt: declared,
            time_source=self.clock,
        )
        watchdog._process_alive = lambda pid: True  # isolate the clock signals
        return watchdog

    def _attempt(self):
        return make_attempt(
            started_at=0.0, launched_at=0.0, pid=None,
            extra={wd.SESSION_PATH_KEY: str(self.transcript)})

    # ── the turn clock ──────────────────────────────────────────────────────

    def test_result_holding_attempt_flatlined_past_the_turn_timeout_is_not_stalled(self):
        """The production failure, reduced: the transcript stopped growing
        because the worker quiesced the builder, and the attempt is sitting
        in review with its result already adjudicated."""
        write_jsonl(self.transcript, ['{"n": 1}'])
        watchdog = self._watchdog([self._attempt()], declared=True)

        watchdog.check_once()   # observes record 1, arms the heartbeat
        self.clock.set(500.0)   # 50x turn_timeout_s, transcript unchanged
        watchdog.check_once()

        self.assertEqual(self.kill.calls, [])
        self.assertEqual(self.fail.calls, [])

    def test_no_result_row_flatlined_past_the_turn_timeout_is_still_convicted(self):
        """The inverse, and the reason the guard reads a typed row rather
        than assuming completion. Real fixture: lane-p2-s3-inventory a1 had
        no envelope, no results row, and a worktree HEAD equal to its
        `base_sha` -- nothing was ever committed. An attempt in that shape
        must still be convicted or the guard has only turned a false kill
        into a missed one."""
        write_jsonl(self.transcript, ['{"n": 1}'])
        watchdog = self._watchdog([self._attempt()], declared=False)

        watchdog.check_once()
        self.clock.set(500.0)
        watchdog.check_once()

        self.assertEqual(len(self.kill.calls), 1)
        self.assertEqual(len(self.fail.calls), 1)
        (fail_args, _) = self.fail.calls[0]
        self.assertIs(fail_args[1], st.RetryClass.ENVIRONMENTAL)
        self.assertEqual(fail_args[2], wd.StallReason.TURN_TIMEOUT.value)

    # ── the wall clock: deferred to a proven-larger bound, never removed ────

    def test_result_holding_attempt_is_not_timed_out_at_the_ordinary_node_bound(self):
        """S3 is the same shape at the outer bound: a long review can push a
        completed, committed, adjudicated attempt past `node_timeout_s` and
        its committed work is discarded on a clock."""
        write_jsonl(self.transcript, ['{"n": 1}'])
        watchdog = self._watchdog([self._attempt()], declared=True)

        watchdog.check_once()
        self.clock.set(self.config.node_timeout_s + 1.0)
        watchdog.check_once()

        self.assertEqual(self.kill.calls, [])
        self.assertEqual(self.fail.calls, [])

    def test_a_hung_result_holding_attempt_still_terminates(self):
        """Bounded termination. The clock that still convicts is
        `config.backstop_t_s`, the run-level backstop's horizon --
        `SchedulerConfig.__post_init__` refuses to construct a config where
        it does not exceed `greatest_run_window_s`, so it is strictly later
        than `node_timeout_s` and always finite. The declared result defers
        the wall clock to that bound; it never removes it."""
        write_jsonl(self.transcript, ['{"n": 1}'])
        watchdog = self._watchdog([self._attempt()], declared=True)

        watchdog.check_once()
        self.clock.set(self.config.backstop_t_s + 1.0)
        watchdog.check_once()

        self.assertEqual(len(self.kill.calls), 1)
        self.assertEqual(len(self.fail.calls), 1)
        (fail_args, _) = self.fail.calls[0]
        self.assertIs(fail_args[1], st.RetryClass.ENVIRONMENTAL)
        self.assertEqual(fail_args[2], wd.StallReason.NODE_TIMEOUT.value)

    def test_no_result_row_still_times_out_at_the_ordinary_node_bound(self):
        """The deferral is conditional on the row, so the ordinary bound is
        untouched for every attempt that has not declared a result."""
        write_jsonl(self.transcript, ['{"n": 1}'])
        watchdog = self._watchdog([self._attempt()], declared=False)

        watchdog.check_once()
        self.clock.set(self.config.node_timeout_s + 1.0)
        watchdog.check_once()

        self.assertEqual(len(self.fail.calls), 1)
        (fail_args, _) = self.fail.calls[0]
        self.assertEqual(fail_args[2], wd.StallReason.NODE_TIMEOUT.value)

    def test_the_deferred_bound_is_structurally_later_than_the_ordinary_one(self):
        """What makes the deferral bounded rather than a hang: a config
        whose backstop does not exceed the greatest run window cannot be
        constructed at all (§11.2), so `backstop_t_s > node_timeout_s`
        holds for every config the watchdog can ever be handed."""
        with self.assertRaises(st.LivenessBoundUnsatisfied):
            make_config(node_timeout_s=100.0, final_acceptance_timeout_s=50.0,
                        backstop_t_s=100.0)
        self.assertGreater(self.config.backstop_t_s, self.config.node_timeout_s)

    # ── §1.2: the guard is a predicate, not a reader of prose ───────────────

    def test_the_predicate_is_consulted_only_when_a_clock_would_otherwise_fire(self):
        """A healthy attempt must not pay for the guard on every poll: the
        predicate reaches the ledger, and the watchdog polls once a second
        for every RUNNING attempt."""
        write_jsonl(self.transcript, ['{"n": 1}'])
        asked = []

        def declared(attempt):
            asked.append(attempt.key)
            return True

        watchdog = wd.Watchdog(
            config=self.config,
            attempts_provider=lambda: [self._attempt()],
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            declared_result_observed=declared,
            time_source=self.clock,
        )
        watchdog._process_alive = lambda pid: True

        watchdog.check_once()
        self.clock.set(5.0)   # inside both clocks
        watchdog.check_once()
        self.assertEqual(asked, [])

        self.clock.set(500.0)  # past the turn timeout
        watchdog.check_once()
        self.assertEqual(len(asked), 1)


class TranscriptRecordCountingTests(unittest.TestCase):
    """§17 item 82 -- count complete records, never observe byte size."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.transcript = Path(self.tmpdir.name) / "session.jsonl"

    def test_counts_complete_newline_terminated_records(self):
        write_jsonl(self.transcript, ['{"n": 1}', '{"n": 2}', '{"n": 3}'])
        self.assertEqual(
            wd.count_complete_transcript_records(self.transcript), 3)

    def test_partial_trailing_record_is_not_counted(self):
        """A file that grows by a partial record must not increment the
        count -- the direct repair of the size-and-mtime heartbeat §9.4
        killed."""
        write_jsonl(self.transcript, ['{"n": 1}'], trailing_newline=True)
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write('{"n": 2, "still_wri')  # no closing brace, no newline

        self.assertEqual(
            wd.count_complete_transcript_records(self.transcript), 1)

    def test_missing_file_counts_zero(self):
        missing = Path(self.tmpdir.name) / "nope.jsonl"
        self.assertEqual(wd.count_complete_transcript_records(missing), 0)


# ── the watchdog is the only heartbeat writer ────────────────────────────────

class HeartbeatOwnershipTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock(0.0)
        self.kill = Recorder()
        self.fail = Recorder()
        self.heartbeat = Recorder()
        self.config = make_config(node_timeout_s=1000.0, turn_timeout_s=10.0)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.transcript = Path(self.tmpdir.name) / "session.jsonl"

    def test_no_public_attribute_exposes_the_heartbeat_writer(self):
        """There is no `watchdog.write_heartbeat` or `watchdog.heartbeat_
        writer` accessor: the only reference to the injected writer lives
        in a private attribute, and the only caller of it is the
        watchdog's own polling code below."""
        watchdog = wd.Watchdog(
            config=self.config,
            attempts_provider=lambda: [],
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            time_source=self.clock,
        )
        self.assertFalse(hasattr(watchdog, "write_heartbeat"))
        self.assertFalse(hasattr(watchdog, "heartbeat_writer"))
        public_attrs = [a for a in dir(watchdog) if not a.startswith("_")]
        self.assertNotIn("heartbeat", [a.lower() for a in public_attrs])

    def test_source_calls_the_heartbeat_writer_only_from_inside_the_class(self):
        """AST proof: every call to `self._write_heartbeat(...)` occurs
        inside a method of `Watchdog` -- no module-level code and no other
        class in this file writes a heartbeat."""
        tree = ast.parse(WATCHDOG_SOURCE)
        watchdog_class = next(
            n for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "Watchdog")
        watchdog_method_nodes = set(ast.walk(watchdog_class))

        call_sites = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_write_heartbeat"):
                call_sites.append(node)

        self.assertTrue(call_sites, "expected at least one heartbeat write")
        for site in call_sites:
            self.assertIn(
                site, watchdog_method_nodes,
                "a heartbeat write occurred outside the Watchdog class")

    def test_writer_is_invoked_when_a_turn_is_first_observed(self):
        write_jsonl(self.transcript, ['{"n": 1}'])
        attempt = make_attempt(
            started_at=0.0, launched_at=0.0, pid=1,
            extra={wd.SESSION_PATH_KEY: str(self.transcript)})
        watchdog = wd.Watchdog(
            config=self.config,
            attempts_provider=lambda: [attempt],
            write_heartbeat=self.heartbeat,
            kill=self.kill,
            fail_attempt=self.fail,
            time_source=self.clock,
        )
        watchdog._process_alive = lambda pid: True

        watchdog.check_once()

        self.assertEqual(len(self.heartbeat.calls), 1)


# ── §9.7 pane text is never lifecycle -- an executed detector ───────────────

FORBIDDEN_PANE_FIELDS = {"agent_status", "pane_status", "foreground_cwd"}


def reads_pane_status_field(source: str) -> bool:
    """A structural detector, not a text grep: true only if the source
    actually reads one of Herdr's screen-derived pane-status fields as an
    attribute or a dict key -- never merely mentions the word in a
    docstring or comment, which `ast.parse` does not surface as either."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_PANE_FIELDS:
            return True
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Index):  # py < 3.9 compatibility shim
                key = key.value
            if (isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in FORBIDDEN_PANE_FIELDS):
                return True
    return False


class PaneTextIsNeverLifecycleTests(unittest.TestCase):

    def test_watchdog_module_never_reads_a_pane_status_field(self):
        """§9.7 -- every signal here is structural: process state, a
        record count, a clock. None of it is pane text."""
        self.assertFalse(reads_pane_status_field(WATCHDOG_SOURCE))

    def test_detector_catches_a_planted_violation(self):
        """A detector never proven red on a real violation is not a
        detector. This fixture is the violation: reading Herdr's
        screen-derived `agent_status` as a liveness signal."""
        violation_attribute = (
            "def poll(pane):\n"
            "    if pane.agent_status == 'idle':\n"
            "        return True\n"
            "    return False\n")
        self.assertTrue(reads_pane_status_field(violation_attribute))

        violation_subscript = (
            "def poll(pane):\n"
            "    if pane['agent_status'] == 'working':\n"
            "        return False\n"
            "    return True\n")
        self.assertTrue(reads_pane_status_field(violation_subscript))

    def test_detector_does_not_false_positive_on_prose_mentioning_the_words(self):
        """Docstrings and comments discussing pane status by name must not
        trip the detector -- only actual attribute/subscript reads do."""
        prose_only = (
            '"""Herdr\'s agent_status field (idle/working) is screen-\n'
            'derived and is never used as a liveness signal here."""\n'
            "def f():\n"
            "    return 1\n")
        self.assertFalse(reads_pane_status_field(prose_only))


# ── the module owns no store: no database driver, no embedded query ─────────

class NoStoreOwnershipTests(unittest.TestCase):

    def test_watchdog_module_imports_no_database_driver(self):
        """Neither mechanism owns a store (module docstring). If this
        module ever imports a DB driver, something started querying
        lifecycle state directly instead of through the injected
        callables."""
        tree = ast.parse(WATCHDOG_SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {"sqlite3", "psycopg2", "asyncpg", "pymysql"}
        self.assertEqual(imported & forbidden, set())

    def test_watchdog_module_embeds_no_sql_query(self):
        """§5.3/§11.2 -- the audit tier (`transitions`) is read at runtime
        never. `SELECT MAX(ts) FROM transitions` is the forbidden shape
        named explicitly in the spec; its absence here is checked at the
        string-literal level so a future edit cannot reintroduce it."""
        tree = ast.parse(WATCHDOG_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                self.assertNotIn("max(ts)", lowered)
                self.assertNotIn("from transitions", lowered)


# ── §11.2 the run-level backstop ─────────────────────────────────────────────

class RunBackstopTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock(0.0)
        self.stuck = Recorder()
        self.diagnostic_calls = []
        # T=500 > greatest_run_window_s (max(node=100, final=50)=100),
        # satisfying the bound SchedulerConfig already enforces.
        self.config = make_config(
            node_timeout_s=100.0, final_acceptance_timeout_s=50.0,
            backstop_t_s=500.0)

    def _backstop(self, last_transition_at):
        def diagnostic():
            self.diagnostic_calls.append(True)
            return "STUCK: no lifecycle transition in 500s"

        return wd.RunBackstop(
            config=self.config,
            last_transition_at=last_transition_at,
            on_stuck=self.stuck,
            diagnostic=diagnostic,
            time_source=self.clock,
        )

    def test_fires_when_no_transition_within_t(self):
        backstop = self._backstop(lambda: 0.0)
        self.clock.set(501.0)

        fired = backstop.check()

        self.assertTrue(fired)
        self.assertEqual(len(self.stuck.calls), 1)
        (args, _) = self.stuck.calls[0]
        self.assertEqual(args[0], "STUCK: no lifecycle transition in 500s")
        self.assertEqual(self.diagnostic_calls, [True])

    def test_does_not_fire_within_a_healthy_node_silent_working_gap(self):
        """A serial chain with one node in flight writes nothing between
        PENDING->RUNNING and RUNNING->VERIFIED, and that gap is as long
        as the node's own work -- up to node_timeout_s, well inside T."""
        backstop = self._backstop(lambda: 0.0)
        self.clock.set(99.0)  # < node_timeout_s < T

        fired = backstop.check()

        self.assertFalse(fired)
        self.assertEqual(self.stuck.calls, [])

    def test_does_not_fire_within_a_healthy_final_acceptance_window(self):
        backstop = self._backstop(lambda: 0.0)
        self.clock.set(49.0)  # < final_acceptance_timeout_s < T

        fired = backstop.check()

        self.assertFalse(fired)

    def test_fires_regardless_of_in_flight_panes_merge_thread_hang_shape(self):
        """Hang shape 1 (§8.5): the merge thread waits on a node that will
        never be verified. Something is always "in flight" (the wait
        itself); nothing is ever transitioning. The backstop has no
        in-flight parameter at all -- it is a pure function of elapsed
        time since the last transition -- so this shape cannot mute it."""
        backstop = self._backstop(lambda: 0.0)
        self.clock.set(501.0)

        fired = backstop.check()

        self.assertTrue(fired)

    def test_fires_regardless_of_a_pane_that_stopped_producing_output(self):
        """Hang shape 2: an agent stopped producing output but its pane is
        still alive. The backstop reads only the lifecycle column, never
        pane liveness, so a live pane cannot suppress it."""
        backstop = self._backstop(lambda: 0.0)
        self.clock.set(500.001)

        fired = backstop.check()

        self.assertTrue(fired)

    def test_reads_only_the_injected_last_transition_reader(self):
        """Injected-reader contract: `check()` calls exactly the supplied
        `last_transition_at` callable to learn the run's lifecycle state,
        and nothing else."""
        reads = []

        def last_transition_at():
            reads.append(True)
            return 0.0

        backstop = self._backstop(last_transition_at)
        self.clock.set(10.0)

        backstop.check()

        self.assertEqual(reads, [True])

    def test_no_in_flight_parameter_exists_on_the_backstop(self):
        """Structural proof, not behavioural: the constructor accepts no
        in-flight-count-shaped argument at all, so conditioning the timer
        on in-flight state is not merely unused -- it is not expressible.
        """
        tree = ast.parse(WATCHDOG_SOURCE)
        backstop_class = next(
            n for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "RunBackstop")
        init = next(
            n for n in backstop_class.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        arg_names = {a.arg for a in init.args.args}
        for forbidden in ("in_flight", "inflight", "running_count",
                          "pane_count"):
            self.assertNotIn(forbidden, arg_names)


# ── §7.6 the thread wrapper itself, not just check_once() ───────────────────

class ThreadWrapperTests(unittest.TestCase):
    """`start()`/`stop()` is the only part of "a single scheduler-owned
    watchdog thread" no other test exercises -- every test above drives
    `check_once()` directly. This is the one bounded, real-thread smoke
    test for the wrapper: it must actually poll repeatedly on its own, it
    must survive a poll that raises (the watchdog is the only heartbeat
    writer, so a thread that dies silently takes every attempt's liveness
    detection with it), and `stop()` must join within a bounded deadline
    with no thread left alive on any exit path.
    """

    def test_thread_polls_repeatedly_survives_a_raising_poll_and_stops_bounded(self):
        poll_times = []
        errors = []

        def flaky_attempts_provider():
            poll_times.append(time.monotonic())
            if len(poll_times) == 2:
                # A single bad poll -- e.g. a transient read against a
                # half-written attempt row -- must not kill the thread.
                raise RuntimeError("simulated transient poll failure")
            return []

        watchdog = wd.Watchdog(
            config=make_config(),
            attempts_provider=flaky_attempts_provider,
            write_heartbeat=Recorder(),
            kill=Recorder(),
            fail_attempt=Recorder(),
            poll_interval_s=0.02,
            on_error=errors.append,
        )
        # Registered before start(): if start(), an assertion, or stop()
        # itself raises, this still runs and no thread survives the test.
        self.addCleanup(watchdog.stop, 2.0)

        watchdog.start()
        deadline = time.monotonic() + 1.0
        while len(poll_times) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)

        # It actually ran the poll loop on its own, more than once.
        self.assertGreaterEqual(
            len(poll_times), 4,
            "watchdog thread did not poll repeatedly within the deadline")
        # The raising poll was observed and handled, not swallowed
        # unnoticed -- and the loop kept going past it (poll 3 and 4
        # happened after poll 2 raised).
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

        thread_ref = watchdog._thread
        self.assertIsNotNone(thread_ref)

        stop_started = time.monotonic()
        watchdog.stop(timeout=2.0)
        stop_elapsed = time.monotonic() - stop_started

        self.assertLess(
            stop_elapsed, 1.0, "stop() did not join within the bound")
        self.assertIsNone(watchdog._thread)
        self.assertFalse(thread_ref.is_alive())


if __name__ == "__main__":
    unittest.main()
