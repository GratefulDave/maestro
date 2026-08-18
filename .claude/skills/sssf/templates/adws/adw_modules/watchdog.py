"""Liveness (§7.6) and the run-level backstop (§11.2).

Two mechanisms, neither owning a store:

* `Watchdog` — a single scheduler-owned thread polling every RUNNING
  attempt. It is the only heartbeat writer, because the worker thread is
  blocked reading the agent and cannot write one itself. It performs the
  kill the worker cannot, then returns the node to pending as
  ENVIRONMENTAL. Because it is the only heartbeat writer, lease expiry and
  stall detection are one mechanism and it subsumes startup detection.

* `RunBackstop` — one run-level timer, beside the watchdog's per-attempt
  timers, firing when no lifecycle transition has been written for the
  whole run within `T`, regardless of how many panes are open (§11.2).

Both take injected callables for reading attempts, writing heartbeats,
killing, and failing an attempt (`Watchdog`), and for reading the run's
last transition timestamp and reporting the stuck diagnostic
(`RunBackstop`). Neither module imports a database driver or issues a
query of its own — the store lives in lane w1's lifecycle module, wired in
by the caller.

Three signals feed the watchdog, each answering a different question
(§7.6):

  process alive        -- is the agent still there?    (the launched
                           process, polled directly -- authoritative,
                           and never pane text, per §9.7)
  turn count advancing  -- is it completing turns?      (complete records
                           in the session file, never byte size)
  wall clock elapsed    -- has it run too long anyway?  (the attempt row)

Arming (§7.6). `PENDING->RUNNING` is written before worktree creation,
provision, the pre-gate, and the baseline inventory, so the attempt window
covers all of them -- but no agent process and no transcript exist yet.
The first two signals are undefined there by construction, not omission.
From `PENDING->RUNNING` until the adapter reports launch
(`AttemptRecord.launched_at` / `.armed`), only the node wall-clock timeout
applies. Process-alive and turn-count arm at launch: "a dead process is
stalled immediately" means a process that launched and then died, never
one that has not launched yet, and the turn-timeout clock starts at
launch, not at attempt start.
"""

from __future__ import annotations

import json
import os
import threading
import time
from enum import Enum
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, NamedTuple, Optional,
                     Tuple)

try:
    from typing import Protocol
except ImportError:  # pragma: no cover - py < 3.8 fallback, unused at runtime
    Protocol = object  # type: ignore

from . import scheduler_types as st

#: The key `attempt.extra` carries the session transcript path under.
#: Owned here because the transcript-counting signal is watchdog behaviour,
#: never store behaviour -- the caller only needs to populate this key.
SESSION_PATH_KEY = "session_path"


# ── §7.6 why an attempt was declared stalled or timed out ───────────────────

class StallReason(str, Enum):
    """Which of the three signals convicted the attempt."""

    PROCESS_DEAD = "PROCESS_DEAD"
    TURN_TIMEOUT = "TURN_TIMEOUT"
    NODE_TIMEOUT = "NODE_TIMEOUT"


# ── the watchdog's private heartbeat cache ───────────────────────────────────

class _HeartbeatState(NamedTuple):
    """The last observed turn count and when it was last seen to advance.

    Private to this module. There is no constructor argument and no public
    method that lets anything but the watchdog's own polling loop write an
    entry here -- that is the whole of what "the watchdog is the only
    heartbeat writer" means at the code level.
    """

    turn_count: int
    observed_at: float


# ── injected collaborators (Protocols; the lead wires these to lane w1) ─────

class AttemptsProvider(Protocol):
    """Returns every attempt currently in RUNNING state."""

    def __call__(self) -> Iterable[st.AttemptRecord]: ...


class HeartbeatWriter(Protocol):
    """Records that the watchdog observed `turn_count` at `observed_at`
    for `attempt`. Called only by the watchdog, never by the worker."""

    def __call__(self, attempt: st.AttemptRecord, turn_count: int,
                 observed_at: float) -> None: ...


class AttemptKiller(Protocol):
    """Performs the kill the worker thread cannot, because the worker is
    blocked reading the agent."""

    def __call__(self, attempt: st.AttemptRecord) -> None: ...


class AttemptFailer(Protocol):
    """Returns the node to pending, classified by `retry_class`."""

    def __call__(self, attempt: st.AttemptRecord, retry_class: st.RetryClass,
                 reason: str) -> None: ...


class LastTransitionReader(Protocol):
    """Reads `runs.last_transition_at` -- lifecycle authority, never the
    audit tier (§5.3, §11.2)."""

    def __call__(self) -> float: ...


class StuckHandler(Protocol):
    """Declares the run STUCK, given the same diagnostic `run status`
    would print."""

    def __call__(self, diagnostic: str) -> None: ...


class DiagnosticProvider(Protocol):
    """The "why is nothing happening" text `run status` already knows how
    to print (§11.2). The backstop does not compute this itself."""

    def __call__(self) -> str: ...


# ── the three structural signals ─────────────────────────────────────────────

def process_is_alive(pid: int) -> bool:
    """Is the launched process still there? Polled directly (§7.6) --
    never pane text (§9.7). `os.kill(pid, 0)` sends no signal; it only
    asks whether the pid exists and is reachable."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else -- still alive.
        return True
    except OSError:
        return False
    return True


def count_complete_transcript_records(path: Any) -> int:
    """Count complete JSONL records in a transcript file (§7.6, §17 item 82).

    A record counts only when its line is newline-terminated and parses as
    JSON. omp and Claude both write their session transcript at turn
    granularity (§9.4): a healthy agent mid-turn produces an unchanging
    file for the whole turn, so counting complete records rather than
    observing byte size or mtime is what keeps a mid-turn agent from
    reading as stalled. The file's final line -- unterminated because it
    is still being written -- is never counted, so a partial record can
    never increment the count.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    data = p.read_text(encoding="utf-8")
    if not data:
        return 0
    lines = data.split("\n")
    # split("\n") on data ending in "\n" yields a trailing "". On data with
    # no trailing newline the last element is an in-progress, unterminated
    # line. Either way the final element is never a complete record.
    complete_lines = lines[:-1]
    count = 0
    for line in complete_lines:
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except ValueError:
            continue
        count += 1
    return count


def _default_transcript_record_count(attempt: st.AttemptRecord) -> int:
    path = attempt.extra.get(SESSION_PATH_KEY)
    if not path:
        return 0
    return count_complete_transcript_records(path)


# ── the watchdog thread ──────────────────────────────────────────────────────

class Watchdog:
    """The single scheduler-owned thread polling every RUNNING attempt.

    Owns no store. `attempts_provider`, `write_heartbeat`, `kill`, and
    `fail_attempt` are the only ways this class touches lifecycle state,
    and all four are supplied by the caller.
    """

    def __init__(
        self,
        config: st.SchedulerConfig,
        attempts_provider: AttemptsProvider,
        write_heartbeat: HeartbeatWriter,
        kill: AttemptKiller,
        fail_attempt: AttemptFailer,
        poll_interval_s: float = 1.0,
        process_alive: Callable[[int], bool] = process_is_alive,
        exit_status_observed: Callable[[st.AttemptRecord], bool] = (
            lambda attempt: False),
        transcript_record_count: Callable[[st.AttemptRecord], int] = (
            _default_transcript_record_count),
        time_source: Callable[[], float] = time.monotonic,
        on_error: Callable[[BaseException], None] = lambda exc: None,
    ) -> None:
        self._config = config
        self._attempts_provider = attempts_provider
        self._write_heartbeat = write_heartbeat
        self._kill = kill
        self._fail_attempt = fail_attempt
        self._poll_interval_s = poll_interval_s
        self._process_alive = process_alive
        self._exit_status_observed = exit_status_observed
        self._transcript_record_count = transcript_record_count
        self._time_source = time_source
        self._on_error = on_error
        # Private: the only writer of this cache is _check_attempt, below.
        self._heartbeats: Dict[Tuple[str, str, int], _HeartbeatState] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("watchdog already started")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="maestro-watchdog", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - see below
                # The watchdog is the only heartbeat writer (module
                # docstring): a poll that raises and kills this thread
                # takes every RUNNING attempt's liveness detection down
                # with it, silently. One bad poll (a transient read of a
                # half-written attempt row, a store hiccup) must not end
                # the loop. `on_error` is a pure observability hook -- it
                # cannot suppress or re-arm anything, so a caller cannot
                # use it to change this method's fail-open behaviour.
                self._on_error(exc)
            self._stop_event.wait(self._poll_interval_s)

    def check_once(self) -> None:
        """One poll pass over every RUNNING attempt. Public so a test can
        drive the watchdog deterministically without a real thread and a
        real sleep."""
        now = self._time_source()
        for attempt in list(self._attempts_provider()):
            self._check_attempt(attempt, now)

    def _check_attempt(self, attempt: st.AttemptRecord, now: float) -> None:
        key = attempt.key

        # Wall clock applies across the whole attempt window, pre-launch
        # and post-launch alike, and wins over the other two signals
        # whatever they say (§7.6).
        elapsed = now - attempt.started_at
        if elapsed > self._config.node_timeout_s:
            self._heartbeats.pop(key, None)
            self._stall(attempt, StallReason.NODE_TIMEOUT)
            return

        if not attempt.armed:
            # Pre-launch: no process and no transcript exist yet, by
            # construction. Only the wall-clock check above applies.
            return

        # §9.7's rule, which this site was the last of three to apply: **an
        # artifact a worker wrote outranks any status a supervisor observes
        # about that worker; absence of a process is not absence of output.**
        # `launcher.poll` consults the declared result before it will report
        # GONE, and `FinalizationWindow.poll` reads the reviewer's report
        # before it will read the reviewer's pid. This check read the pid
        # first and had no notion of completion at all, so a process that
        # finished its work and exited zero was indistinguishable from one
        # that died.
        #
        # Measured: a code node running `python -c "...write_text('done')"`
        # exits in milliseconds. When the poll landed after that exit the
        # attempt was failed PROCESS_DEAD, retried twice into the same race,
        # and blocked ENVIRONMENTAL_BUDGET_EXHAUSTED — roughly one run in
        # three, on the real scheduler path, for a node that had already
        # succeeded.
        #
        # `exit_status_observed` is what closes it, and it is a fact about the
        # harness rather than about the process: some other component holds
        # this process's handle and reads its exit code directly, so the
        # disappearance of the pid is not the only account of what happened
        # and this signal is not the one entitled to rule. Structural, and
        # among the facts §7.5 permits — an exit code and whether the process
        # started, never output text.
        #
        # Where the signal keeps its full force is the case §7.6 wrote it for:
        # an agent the herdr server spawned, whose handle Maestro does not
        # hold and whose exit nothing else can see, where absence genuinely is
        # the only signal there is.
        if (attempt.pid is not None
                and not self._exit_status_observed(attempt)
                and not self._process_alive(attempt.pid)):
            self._heartbeats.pop(key, None)
            self._stall(attempt, StallReason.PROCESS_DEAD)
            return

        record_count = self._transcript_record_count(attempt)
        state = self._heartbeats.get(key)
        if state is None:
            # First observation since arming: the turn clock starts at
            # launch, not at attempt start and not at this first poll.
            state = _HeartbeatState(
                turn_count=record_count, observed_at=attempt.launched_at)
            self._heartbeats[key] = state
            self._write_heartbeat(attempt, state.turn_count, state.observed_at)
        elif record_count > state.turn_count:
            state = _HeartbeatState(turn_count=record_count, observed_at=now)
            self._heartbeats[key] = state
            self._write_heartbeat(attempt, state.turn_count, state.observed_at)

        since_progress = now - state.observed_at
        if since_progress > self._config.turn_timeout_s:
            self._stall(attempt, StallReason.TURN_TIMEOUT)

    def _stall(self, attempt: st.AttemptRecord, reason: StallReason) -> None:
        self._kill(attempt)
        self._fail_attempt(attempt, st.RetryClass.ENVIRONMENTAL, reason.value)


# ── the run-level backstop ───────────────────────────────────────────────────

class RunBackstop:
    """One run-level "no progress" timer, beside the watchdog's per-attempt
    timers (§11.2).

    Fires when no lifecycle transition has been written for the whole run
    within `T` -- a timer, never an in-flight count, so it still fires in
    the two hang shapes where something is always in flight and nothing is
    ever transitioning: a merge thread waiting on a node that will never
    be verified, and an agent that stopped producing output while its pane
    stays alive.

    Reads `runs.last_transition_at` through the injected
    `last_transition_at` callable and nothing else -- this module imports
    no database driver and issues no query, so the audit tier's
    `transitions` table is unreachable from here by construction, not by
    a checked-but-avoidable choice.

    `T` (`config.backstop_t_s`) must exceed `config.greatest_run_window_s`
    or `SchedulerConfig.__post_init__` already refuses construction with
    `LivenessBoundUnsatisfied` -- that check is not duplicated here.
    """

    def __init__(
        self,
        config: st.SchedulerConfig,
        last_transition_at: LastTransitionReader,
        on_stuck: StuckHandler,
        diagnostic: DiagnosticProvider,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._last_transition_at = last_transition_at
        self._on_stuck = on_stuck
        self._diagnostic = diagnostic
        self._time_source = time_source

    def check(self) -> bool:
        """One evaluation of the timer. Returns whether it fired.

        Callers poll this the same way the watchdog polls attempts; it
        does not run its own thread, because a run has exactly one of
        these and the scheduler's own loop is a fine place to call it.
        """
        now = self._time_source()
        last = self._last_transition_at()
        if now - last > self._config.backstop_t_s:
            self._on_stuck(self._diagnostic())
            return True
        return False
