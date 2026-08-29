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
(§7.6). They are not interchangeable across node kinds:

  process alive        -- is the launched process still there?
                           Polled via `attempt.pid`, never pane text
                           (§9.7). A code node's pid is the harness-
                           spawned process, so this branch can fire;
                           `exit_status_observed` then outranks it
                           when the harness already holds the handle.
                           An agent node's pid is
                           `LaunchHandle.process_group`. herdr 0.8.0
                           exposes no pid and no process group
                           (§8.3, §16.3 item 17), so the field is
                           unset by design — a reserved seam pending
                           a §9.8 receipt, not a missed wire.
                           Process-alive is therefore unreachable for
                           agent nodes and is not authoritative for
                           them.

  turn count advancing  -- is it completing turns?      (complete
                           records in the session file, never byte
                           size). One of the two signals that
                           actually guard an agent node.

  wall clock elapsed    -- has it run too long anyway?  (the attempt
                           row). Applies to every node kind, pre-
                           launch and post-launch. The other signal
                           that reaches an agent node; after a
                           declared result it is the last bound left.

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

Disarming (§9.7). Both clocks stop convicting an attempt that has already
landed a typed, adjudicated result row of its own. An artifact a worker
wrote outranks any status a supervisor observes about that worker, and
this module is the supervisor: silence after a declared result is the
worker having finished, not the worker having died. `exit_status_observed`
is the same rule applied to the process signal; `declared_result_observed`
applies it to the two clocks. Neither is a fact about the agent -- both are
facts about the harness, supplied by the scheduler, because whether some
other component already holds the answer is not something the watchdog can
know about itself.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, NamedTuple, Optional, Tuple

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


#: Herdr statuses that mean the agent is alive and doing something. `idle` is
#: deliberately absent: it is a heartbeat, not completion and not a stall.
#: B14 arms on these, never on elapsed silence. Same set FinalizationWindow
#: uses — one vocabulary, so the two clocks cannot drift apart.
LIVE_WORKING_STATUSES = frozenset({"working", "blocked"})


# ── the watchdog's private heartbeat cache ───────────────────────────────────


class _HeartbeatState(NamedTuple):
    """The last observed turn count and when it was last seen to advance.

    Private to this module. There is no constructor argument and no public
    method that lets anything but the watchdog's own polling loop write an
    entry here -- that is the whole of what "the watchdog is the only
    heartbeat writer" means at the code level.

    `actor_status_current` is the last *readable* route status. An unreadable
    poll leaves it alone: a hiccup is a missing observation, not a stall.
    """

    turn_count: int
    observed_at: float
    actor_status_current: Optional[str] = None


# ── injected collaborators (Protocols; the lead wires these to lane w1) ─────


class AttemptsProvider(Protocol):
    """Returns every attempt currently in RUNNING state."""

    def __call__(self) -> Iterable[st.AttemptRecord]: ...


class HeartbeatWriter(Protocol):
    """Records that the watchdog observed `turn_count` at `observed_at`
    for `attempt`. Called only by the watchdog, never by the worker."""

    def __call__(
        self, attempt: st.AttemptRecord, turn_count: int, observed_at: float
    ) -> None: ...


class AttemptKiller(Protocol):
    """Performs the kill the worker thread cannot, because the worker is
    blocked reading the agent."""

    def __call__(self, attempt: st.AttemptRecord) -> None: ...


class AttemptFailer(Protocol):
    """Returns the node to pending, classified by `retry_class`."""

    def __call__(
        self, attempt: st.AttemptRecord, retry_class: st.RetryClass, reason: str
    ) -> None: ...


class ActorStatusReader(Protocol):
    """The route's raw per-pane agent status, uncollapsed.

    B14's fix depends on the *raw* status. `observe()` collapses idle into
    RUNNING and cannot express "went live, then stopped". Returns `None`
    when the status cannot be read, which is never a stall: an unreadable
    status is a missing observation.

    The reader is a typed route report, never scraped pane text. This
    module never reads `agent_status` off a pane object.
    """

    def __call__(self, attempt: st.AttemptRecord) -> Optional[str]: ...


class UnpublishedWorkPreserver(Protocol):
    """Publish unpublished builder work onto the attempt's durable ref.

    Called on NODE_TIMEOUT *before* kill, so a retry can find the tree
    rather than starting blind. The watchdog does not own git; the caller
    supplies this. A no-op default would re-install the discard.
    """

    def __call__(self, attempt: st.AttemptRecord) -> None: ...


class LastTransitionReader(Protocol):
    """Reads `runs.last_transition_at` -- lifecycle authority, never the
    audit tier (§5.3, §11.2)."""

    def __call__(self) -> float: ...



# ── the three structural signals ─────────────────────────────────────────────


def process_is_alive(pid: int) -> bool:
    """Return whether ``pid`` can still execute work.

    ``kill(pid, 0)`` reports a zombie as present.  A zombie cannot advance a
    scheduler or attempt and only waits for its parent to reap it, so treating
    it as live permanently fences recovery.  Linux exposes that state in
    ``/proc``; Darwin's ``proc_pidinfo`` no longer returns a complete BSD
    record for the zombie and therefore has no start epoch.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if sys.platform == "darwin":
        return _darwin_process_start_epoch(pid) is not None
    if sys.platform.startswith("linux"):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return False
        return state != "Z"
    return True


def _darwin_process_start_epoch(pid: int) -> Optional[float]:
    """Microsecond start from `proc_pidinfo`. `ps lstart` is whole seconds."""
    PROC_PIDTBSDINFO = 3

    class _ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        info = _ProcBsdInfo()
        got = libc.proc_pidinfo(
            ctypes.c_int(pid),
            ctypes.c_int(PROC_PIDTBSDINFO),
            ctypes.c_uint64(0),
            ctypes.byref(info),
            ctypes.c_int(ctypes.sizeof(info)),
        )
    except (OSError, AttributeError):
        return None
    if got != ctypes.sizeof(info) or int(info.pbi_pid) != int(pid):
        return None
    return float(info.pbi_start_tvsec) + (int(info.pbi_start_tvusec) / 1_000_000.0)


def process_start_epoch(pid: int) -> Optional[float]:
    """Wall-clock start of `pid`, or None if it cannot be said.

    Used to distinguish the process that claimed a run from a later
    occupant of the same pid. `os.kill(pid, 0)` cannot do that.
    Whole-second clocks (`ps lstart`) cannot either: a reuse in the
    same second reports the same start as the claim (#37).

    Two platforms can answer. Linux reads `/proc/<pid>/stat`, which is
    clock-tick resolution; Darwin reads `proc_pidinfo`, which is
    microsecond. Anywhere else this refuses rather than guessing, and a
    refusal is `None` -- which a caller must read as "identity
    unproven", never as "the same process". Returning a coarse or
    fabricated start off these two platforms would be worse than
    refusing: it would let a reused pid pass for the original.
    """
    if pid <= 0:
        return None
    linux = Path("/proc/{0}/stat".format(pid))
    if linux.is_file():
        try:
            body = linux.read_text().split(")", 1)[1].split()
            start_ticks = int(body[19])
            boot = None
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    boot = int(line.split()[1])
                    break
            hz = os.sysconf("SC_CLK_TCK")
            if boot is None or hz <= 0:
                return None
            return float(boot) + (start_ticks / float(hz))
        except (OSError, IndexError, ValueError):
            return None
    if sys.platform == "darwin":
        return _darwin_process_start_epoch(pid)
    return None


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
            lambda attempt: False
        ),
        declared_result_observed: Callable[[st.AttemptRecord], bool] = (
            lambda attempt: False
        ),
        transcript_record_count: Callable[[st.AttemptRecord], int] = (
            _default_transcript_record_count
        ),
        actor_status: Optional[ActorStatusReader] = None,
        preserve_unpublished: Optional[UnpublishedWorkPreserver] = None,
        time_source: Callable[[], float] = time.monotonic,
        on_error: Callable[[BaseException], None] = lambda exc: None,
        start_epoch: Callable[[int], Optional[float]] = process_start_epoch,
        host: Optional[str] = None,
    ) -> None:
        self._config = config
        self._attempts_provider = attempts_provider
        self._write_heartbeat = write_heartbeat
        self._kill = kill
        self._fail_attempt = fail_attempt
        self._poll_interval_s = poll_interval_s
        self._process_alive = process_alive
        self._exit_status_observed = exit_status_observed
        self._declared_result_observed = declared_result_observed
        self._transcript_record_count = transcript_record_count
        self._actor_status = actor_status
        self._preserve_unpublished = preserve_unpublished
        self._time_source = time_source
        self._on_error = on_error
        self._start_epoch = start_epoch
        self._host = host
        # Private: the only writer of this cache is _check_attempt, below.
        self._heartbeats: Dict[Tuple[str, str, int], _HeartbeatState] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("watchdog already started")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="maestro-watchdog", daemon=True
        )
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

        # Wall clock applies across the whole attempt window. Pre-launch it
        # is the only signal: no process and no transcript exist yet.
        # Post-launch it no longer wins over a live observation — B14, #97.
        # A builder still completing turns, or one the route reports working,
        # is not discarded on elapsed time. The clock still convicts a
        # genuinely silent attempt; it is not removed. A declared result
        # still defers it to `backstop_t_s` (§9.7).
        #
        # PROCESS_DEAD is no longer structurally unreachable for an agent
        # node: `attempt.pid` now takes `LaunchHandle.liveness_pid` -- the
        # pane's foreground process group, read from `herdr pane
        # process-info` -- where it used to take only `handle.process_group`,
        # which herdr never populates (#20, §9.8's group-membership receipt).
        # It is still not guaranteed: the launcher declines the group whenever
        # it cannot be told apart from the pane's own shell, and answers
        # `None`, which is exactly this state. So the reasoning above stands
        # as written; what changed is that an agent attempt now usually has a
        # third signal rather than never having one.
        #
        # `backstop_t_s` is the deferred bound because it is the one number
        # already proven to be both finite and strictly greater:
        # `SchedulerConfig.__post_init__` raises `LivenessBoundUnsatisfied`
        # unless it exceeds `greatest_run_window_s`, which is at least
        # `node_timeout_s`. So every config the watchdog can be handed has
        # node_timeout_s < backstop_t_s < infinity, and a result-holding
        # attempt that genuinely hangs is still convicted -- late, by design,
        # because at that horizon the run is stopping anyway.
        # The predicate is asked lazily, only once the smaller bound has
        # already been exceeded: this loop runs every poll interval over every
        # RUNNING attempt, and the overwhelmingly common case is an attempt
        # nowhere near either horizon, which must not cost a ledger read.
        elapsed = now - attempt.started_at
        if not attempt.armed:
            # Pre-launch: no process and no transcript exist yet, by
            # construction. Only the wall-clock check applies.
            if elapsed > self._config.node_timeout_s:
                bound = (
                    self._config.backstop_t_s
                    if self._declared_result_observed(attempt)
                    else self._config.node_timeout_s
                )
                if elapsed > bound:
                    self._heartbeats.pop(key, None)
                    self._stall(attempt, StallReason.NODE_TIMEOUT)
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
        # `declared_result_observed` joins the guard for #20, and it is the
        # same §9.7 rule as the two above rather than a new one. Until #20 this
        # branch could not fire at all -- a code node is spared by
        # `exit_status_observed` and an agent node had no pid -- so the
        # question of a *finished* attempt losing its process never arose here.
        # It does now. The measured cost of getting this wrong is on record for
        # the code path: a command that exited between two polls was convicted
        # PROCESS_DEAD, retried twice into the same race and blocked
        # ENVIRONMENTAL_BUDGET_EXHAUSTED, roughly one run in three, for a node
        # that had already succeeded.
        #
        # It is a belt, not the braces: a herdr-spawned agent does not exit
        # when it finishes a turn -- it returns to its composer and idles, and
        # the pane's foreground group survives with it -- so absence really is
        # death for this launch path, which is what §7.6 wrote the signal for.
        # The guard costs one ledger read on an attempt whose process is
        # already gone, and it means no route whose agent *does* exit on
        # completion can have its accepted work convicted for finishing.
        if (
            attempt.pid is not None
            and not self._exit_status_observed(attempt)
            and not self._declared_result_observed(attempt)
            and self._attempt_process_dead(attempt)
        ):
            self._heartbeats.pop(key, None)
            self._stall(attempt, StallReason.PROCESS_DEAD)
            return

        record_count = self._transcript_record_count(attempt)
        prev = self._heartbeats.get(key)
        turns_advanced = prev is not None and record_count > prev.turn_count
        if prev is None:
            # First observation since arming: the turn clock starts at
            # launch, not at attempt start and not at this first poll.
            state = _HeartbeatState(
                turn_count=record_count, observed_at=attempt.launched_at
            )
            self._heartbeats[key] = state
            self._write_heartbeat(attempt, state.turn_count, state.observed_at)
        elif turns_advanced:
            state = _HeartbeatState(
                turn_count=record_count,
                observed_at=now,
                actor_status_current=prev.actor_status_current,
            )
            self._heartbeats[key] = state
            self._write_heartbeat(attempt, state.turn_count, state.observed_at)
        else:
            state = prev

        # Raw per-pane status, never observe(). Unreadable leaves the last
        # readable value: a hiccup is not a stall. Absence of a reader is
        # not an observation of work.
        if self._actor_status is not None:
            raw = self._actor_status(attempt)
            if raw is not None:
                status = raw.strip().casefold()
                if status:
                    state = state._replace(actor_status_current=status)
                    self._heartbeats[key] = state

        route_reports_working = (
            self._actor_status is not None
            and state.actor_status_current in LIVE_WORKING_STATUSES
        )

        # Armed wall clock. Defer while turns advance or the route reports
        # the actor live — that is the 163-turn discard. Convict when
        # neither is true. A declared result still defers to backstop_t_s.
        if elapsed > self._config.node_timeout_s:
            bound = (
                self._config.backstop_t_s
                if self._declared_result_observed(attempt)
                else self._config.node_timeout_s
            )
            if elapsed > bound:
                declared = bound is self._config.backstop_t_s
                if not declared and (turns_advanced or route_reports_working):
                    return
                self._heartbeats.pop(key, None)
                self._stall(attempt, StallReason.NODE_TIMEOUT)
                return

        # §9.7 again, and this is the site that fired. The worker quiesces
        # the builder BEFORE it commits the work, runs the post gate, and
        # dispatches the cross-vendor reviewer, so from that moment this exact
        # file cannot grow again by construction -- while this loop goes on
        # measuring it. Review latency tracks reviewer turn count at roughly
        # 7s/turn over 15-64 turns, so it is unbounded and this is a threshold
        # rather than a race.
        #
        # Measured on run-9e9ac412669140039ae078601048f6c7: ten reviews, 46s
        # to 461s, and exactly the two that exceeded turn_timeout_s=300 were
        # killed ENVIRONMENTAL -- attempts that had written a success
        # envelope, been committed by the scheduler, had an adjudicated result
        # row written, and were legitimately sitting in review. The cost was
        # not only the kill: an infra retry was debited for what the reviewer
        # returned as a rejection 164s and 72s later, the reviewer's findings
        # were discarded so the retry relaunched blind, and a full review was
        # burned. `attempts.extra_json.review_rejected` present on all six
        # correctly-settled rejections and absent on all three of these is how
        # the distortion was confirmed.
        #
        # The predicate is asked only once a clock would otherwise fire: it
        # reaches the ledger, and this loop runs once a second over every
        # RUNNING attempt.
        #
        # #107 / M30: silence convicts only where the route is *not*
        # reporting the actor working. A builder thinking inside one long
        # turn produces no transcript growth and no result row; elapsed
        # silence is not quiescence. Absence of a reader is not an
        # observation of work, so the clock still fires there.
        since_progress = now - state.observed_at
        if (
            since_progress > self._config.turn_timeout_s
            and not self._declared_result_observed(attempt)
            and not route_reports_working
        ):
            self._stall(attempt, StallReason.TURN_TIMEOUT)

    def _attempt_process_dead(self, attempt: st.AttemptRecord) -> bool:
        """True only when this attempt's own process is proven absent.

        Lazy import: this module must not import `lifecycle` at load time
        (`sqlite3` is a store driver; the store is injected). The predicate
        itself lives in `lifecycle.attempt_liveness` so watchdog and salvage
        cannot drift into two answers.
        """
        from . import lifecycle as lc

        return (
            lc.attempt_liveness(
                attempt,
                is_alive=self._process_alive,
                start_epoch=self._start_epoch,
                host=self._host,
            )
            is False
        )

    def _stall(self, attempt: st.AttemptRecord, reason: StallReason) -> None:
        self._kill(attempt)
        if reason is StallReason.NODE_TIMEOUT:
            self._preserve_unpublished_work(attempt)
        self._fail_attempt(attempt, st.RetryClass.ENVIRONMENTAL, reason.value)

    def _preserve_unpublished_work(self, attempt: st.AttemptRecord) -> None:
        """Commit unpublished builder work onto the attempt ref before fail.

        Kill already ran, so the writer is stopped. A missing callback is
        a missing wire, not a skip of the stall. Preserve errors are
        observed, never used to dodge the timeout.
        """
        if self._preserve_unpublished is None:
            return
        try:
            self._preserve_unpublished(attempt)
        except Exception as exc:  # noqa: BLE001 — stall must still complete
            self._on_error(exc)


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
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._last_transition_at = last_transition_at
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
            return True
        return False
