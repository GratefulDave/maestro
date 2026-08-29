"""The finalization window (§6.5, §11.2) — §11.2's silence rule applied to
the one span with no run row.

`plan finalize` launches exactly one reviewer, and B14 is that flow's
recorded failure: a reviewer idle at its prompt having written nothing,
the verb waiting 22 minutes with no output and no diagnostic. The repair
is a bounded window with the three obligations §11.2 states:

  (a) it opens with a durable record in the store that owns the phase's
      lifecycle. Finalization has no run row, so that record is the
      tracer's reviewer-session row (§6.5) -- written here through an
      injected recorder, because the tracer schema belongs to the ledger.
  (b) it carries exactly **one** span-bounding wall-clock timeout, the
      finalization wall-clock timeout. Earlier-firing detectors inside the
      span stay legal precisely because they convert into the same kill
      and the same typed result rather than replacing the bound.
  (c) expiry converts into a durable typed result by kill and red
      outcome: `FINALIZATION_STALLED`, which is deliberately not a receipt
      (§6.5) -- a stall is a fact about the machine or the route, never a
      verdict about the plan. The typed `WindowOutcome` this returns is
      that result; recording it belongs to the caller, against the store
      that owns its phase, and this module holds no recorder for it.

**The signals are §7.6's, not a second set.** `FinalizationSignal` reuses
the watchdog's own `StallReason` strings for the two structural signals,
and the defaults for reading them are the watchdog's own functions rather
than copies, so §7.6's measured transcript contract cannot be
re-litigated here. The third member is named `WINDOW_TIMEOUT` rather than
reusing `NODE_TIMEOUT`, because finalization has no node and no attempt
row and a shared name there would be a lie about what expired.

**Arming.** Process liveness and turn count arm at *reported launch*
(`report_launched`), never at `open`. Before that, only the span bound
applies: a reviewer that has not started yet is not a dead process, and a
cold start is not a stalled turn. This is what keeps *not yet started*,
*working*, and *stopped without declaring* distinguishable, which is what
B14's fix required.

**The three answers are three signals, not two and a wall clock.** Issue
#89 was raised against `run-2a44d226e75a4be391a14f02b78a6d25`, where seven
reviews took a receipt lock and wrote no receipt. Reading them apart
required distinguishing *never started* from *stopped without declaring*
from *worked until the span ran out*, and only the third had a signal
anyone could name afterwards:

  * `NEVER_STARTED` — armed, readable, and never once observed working,
    with an empty transcript, past `start_deadline_s`. Previously this
    state had no signal at all and could only end at the span bound, which
    is the failure the issue's acceptance names.
  * `ACTOR_ABANDONED` — see below. It used to convict on a *single* `idle`
    sample.
  * `WINDOW_TIMEOUT` — the span bound, unchanged and still last.

**The turn clock is evidence of a stop only where nothing contradicts it.**
`TURN_TIMEOUT` reads transcript silence, and silence has two causes: a
reviewer that stopped, and a reviewer still thinking. It used to convict both,
unconditionally and ahead of every gated detector above it, which is the wall
clock B14 forbids wearing a structural signal's name. On
`cmo-consolidation-l-r5` it killed a reviewer whose pane was still answering
128.6 seconds in and blocked the plan with no receipt. It now fires only where
the route is not reporting the actor working — the same gating `ACTOR_ABANDONED`
carries, on observed status rather than elapsed time. It keeps its job for the
actor reported at its composer and for the window that has no status reader to
ask, and it can no longer outvote a live observation.

**Quiescence is confirmed, never sampled once.** A single `idle` reading is
not evidence that a reviewer stopped. Herdr reports a pane as `idle`
whenever the agent is between turns *or* blocked inside a tool call, and
the liveness latch is already set by then, so one sample convicts a pause
as readily as a stop. `idle` therefore has to *persist* for
`quiescence_confirm_s`, and any transcript record appearing during that
interval — or any return to a working status, or a status that cannot be
read at all — restarts the confirmation.

The transcript half is the load-bearing one, and it is deliberately
conservative in only one direction: it can delay a conviction, never
manufacture one. It cannot be used to keep a reviewer alive that has
stopped emitting records, which is what the failed reviews in
`run-2a44d226e75a4be391a14f02b78a6d25` all did — each died holding a
blocking `hub op=wait` on a sub-task it had spawned, with its transcript
silent for the whole wait (2.4s to 76.4s across the five that reached a
session). Silence is what this detector reads, so a reviewer in that shape
is still convicted on the confirmation interval rather than kept alive by
it. What ends such a review is not this module's business: delegating a
review to a sub-task is a reviewer-contract violation, and the signal here
only has to name the state honestly.

Both deadlines carry in-code defaults and are optional overrides, because
an installation's `maestro.config.yaml` predates them and a required key
would break every existing deployment. Both are also bounded by the span
at the point of use, so no setting of either can push total detection past
the finalization window — obligation (b) says that clock bounds everything
inside it, and a detector that could outlast it would not be an
earlier-firing detector at all.

**One clock, stated.** Every timeout in this module is measured in
`time.monotonic` and nothing else. The lifecycle store keeps
`last_transition_at` in **epoch** seconds, and mixing the two produced a
real defect earlier in this build, so the window stamps both separately:
`_opened_at_monotonic` is the only value any comparison here reads, while
`ReviewerSession.opened_at_epoch` exists solely to be handed to the
tracer row. No public entry point accepts a caller-supplied start time,
so a caller cannot introduce the mix from outside either.

**What this does not close.** §16.3 item 33 records that the finalization
launch path *before* the window opens is outside every window. The span
clock here is stamped before `launch` is called, so a slow launch spends
the window's own budget; a launch that never returns still never reaches
a poll, and that remains item 33's open defect rather than something this
module quietly claims to have fixed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    from typing import Protocol
except ImportError:  # pragma: no cover - py < 3.8 fallback, unused at runtime
    Protocol = object  # type: ignore

from . import watchdog as wd

#: The two structural signal readers are the watchdog's, by reference.
DEFAULT_PROCESS_ALIVE = wd.process_is_alive
DEFAULT_TRANSCRIPT_RECORD_COUNT = wd.count_complete_transcript_records
#: The key a session's transcript path lives under, shared with §7.6.
SESSION_PATH_KEY = wd.SESSION_PATH_KEY



class FinalizationSignal(str, Enum):
    """Which signal ended the window. Two are §7.6's, by value."""

    PROCESS_DEAD = wd.StallReason.PROCESS_DEAD.value
    TURN_TIMEOUT = wd.StallReason.TURN_TIMEOUT.value
    WINDOW_TIMEOUT = "WINDOW_TIMEOUT"
    #: B14: the reviewer went live and then stopped without declaring. Not a
    #: wall clock — it fires once the route has reported the agent back at its
    #: composer, with no report written and no transcript record appearing,
    #: for `quiescence_confirm_s` together.
    ACTOR_ABANDONED = "ACTOR_ABANDONED"
    #: The reviewer launched and never went live at all: a readable pane that
    #: has never once reported working, with an empty transcript, past
    #: `start_deadline_s`. Distinct from ACTOR_ABANDONED because the two want
    #: opposite handling — nothing was reviewed here, and nothing was left
    #: half-done — and distinct from WINDOW_TIMEOUT because a failed start is
    #: knowable in seconds and must not be paid for at the span bound (#89).
    NEVER_STARTED = "NEVER_STARTED"


#: Herdr statuses that mean the agent is alive and doing something. `idle` is
#: deliberately absent: it is a *live* status, and `launch()` returns while an
#: agent still sits at a fresh prompt having done nothing, so treating idle as
#: liveness is what made B14 undetectable. Quiescence detection arms only after
#: one of these has been observed at least once. Owned by the watchdog so the
#: run-side clock (#107) and this window cannot drift apart.
LIVE_WORKING_STATUSES = wd.LIVE_WORKING_STATUSES

#: The status that means "back at the composer, not doing anything". After the
#: reviewer has been observed working, this means it stopped without declaring
#: -- but only once it has held across `quiescence_confirm_s` with no
#: transcript record appearing, because a pane blocked inside a tool call reads
#: `idle` too.
QUIESCENT_STATUS = "idle"

#: How long an armed, readable reviewer may go without ever reporting a working
#: status before it is a failed start. Generous on purpose: it has to clear a
#: cold start on the slowest route, and the observed reviewers reported working
#: within seconds of the pane opening. It is also the *only* protection against
#: convicting a reviewer that is merely slow to begin, since the default record
#: counter answers 0 whenever no transcript path was recorded on the session.
#: So it is set far above any plausible start rather than as tight as the
#: signal would allow.
DEFAULT_START_DEADLINE_S = 120.0

#: The in-code default for the turn clock, and the only place it is named.
#: `maestro.py`'s `--reviewer-turn-timeout-s` takes its argparse default from
#: here rather than repeating a number, because two defaults for one clock is
#: how a raised module default comes to look like it did nothing: the CLI
#: default binds last and silently wins.
#:
#: 900s rather than the 120s this shipped with. 120s convicted a live reviewer
#: at 128.6s on `cmo-consolidation-l-r5` — a reviewer whose pane was still
#: answering and whose transcript was still growing after the verb gave up.
#: The gate in `poll` is the fix for that; this is only the width. A route that
#: reports nothing at all still has to be bounded, so the number is generous
#: rather than absent, and the span bound remains over it either way.
#:
#: Note the interaction with `maestro._validate_review_clocks`, which refuses a
#: *configured* `reviewer.turn_timeout_s` at or above
#: `reviewer.finalization_timeout_s`. A deployment that wants this width in its
#: own config must raise its span bound past it; unconfigured, the span bound
#: simply fires first, which is the same disarming reached by another route.
DEFAULT_TURN_TIMEOUT_S = 900.0

#: How long `idle` must hold, with no transcript record appearing, before the
#: reviewer is convicted of stopping without declaring. Sized against the
#: failure it exists to prevent: the reviewers killed in #89 were convicted
#: 2.4-31 seconds into a blocking `hub wait`, all of them alive and mid-review.
DEFAULT_QUIESCENCE_CONFIRM_S = 60.0


@dataclass(frozen=True)
class FinalizationConfig:
    """Configuration, never plan content — the same reason retry budgets
    are (§6.2).

    Deliberately its own type rather than a field on `SchedulerConfig`:
    §11.2 says the finalization timeout takes no part in `T`'s preflight
    inequality, because no run exists at plan time. A field there would be
    swept into `greatest_run_window_s` and would raise the bound every
    scheduler must satisfy for a window no scheduler ever enters.
    """

    finalization_timeout_s: float
    turn_timeout_s: float
    poll_interval_s: float = 1.0
    #: Both deadlines are optional overrides with in-code defaults. An
    #: installation's `maestro.config.yaml` predates them, and a key that had
    #: to be present would refuse to start every existing deployment.
    start_deadline_s: float = DEFAULT_START_DEADLINE_S
    quiescence_confirm_s: float = DEFAULT_QUIESCENCE_CONFIRM_S

    def __post_init__(self) -> None:
        for name in ("finalization_timeout_s", "turn_timeout_s",
                     "poll_interval_s", "start_deadline_s",
                     "quiescence_confirm_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} is a wall clock; it is positive")

    @property
    def effective_start_deadline_s(self) -> float:
        """Bounded by the span, so no setting outlasts obligation (b).

        Bounded here rather than refused in `__post_init__`: an installation
        may legitimately run a window shorter than this default, and refusing
        that config would turn a defaulted field into a required one — the
        thing these two fields exist to avoid.
        """
        return min(self.start_deadline_s, self.finalization_timeout_s)

    @property
    def effective_quiescence_confirm_s(self) -> float:
        """Bounded by the span, for the same reason."""
        return min(self.quiescence_confirm_s, self.finalization_timeout_s)


@dataclass
class ReviewerSession:
    """The reviewer's recorded identity and liveness state.

    `(route, model, session_id)` is what §6.5 requires the receipt to
    record and the stalled verb to print, which is how "independent
    review" becomes an auditable fact after the run rather than a promise
    before it.

    `launched_at` is `None` until the adapter reports the reviewer
    launched, exactly as `AttemptRecord.launched_at` is — the two signals
    that read it are undefined before then by construction.
    """

    route: str
    model: str
    session_id: str
    session_dir: Optional[str] = None
    harness_owned_group: bool = False
    pid: Optional[int] = None
    launched_at: Optional[float] = None
    opened_at_epoch: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def armed(self) -> bool:
        """Whether §7.6's first two signals apply yet."""
        return self.launched_at is not None


@dataclass(frozen=True)
class WindowOutcome:
    """The window's one result. Exactly one of `report` / `signal` is set."""

    completed: bool
    session: ReviewerSession
    elapsed_s: float
    report: Optional[Any] = None
    signal: Optional[FinalizationSignal] = None
    killed: bool = False
    #: Whether the route ever reported this reviewer working or blocked. The
    #: typed discriminator between a review that never began and one that
    #: began and stopped — a caller deciding what to do with a failed review
    #: needs to tell those apart, and reading it off the signal name alone
    #: would make every future signal a change to that caller.
    observed_working: bool = False


# ── injected collaborators (Protocols; the ledger owns the tracer) ──────────

class ReviewerLauncher(Protocol):
    """Launches exactly one reviewer and returns its session identity."""

    def __call__(self) -> ReviewerSession: ...


class ReportPoller(Protocol):
    """Returns the reviewer's report once it exists, else None. Opaque to
    this module: verifying it against the matrix is finalization's job."""

    def __call__(self) -> Optional[Any]: ...


class ReviewerKiller(Protocol):
    """Terminates the reviewer's process group. Called only where the
    group is harness-owned (§6.5, §8.3)."""

    def __call__(self, session: ReviewerSession) -> None: ...


class ActorStatusReader(Protocol):
    """The route's raw per-pane agent status, uncollapsed.

    B14's fix depends on the *raw* status. An adapter that collapses idle into
    RUNNING — as `HerdrLauncher.poll` must, because for a build node idle means
    "turn finished" — cannot express "went live, then stopped without
    declaring", which is the only shape that distinguishes a stalled reviewer
    from a slow one. Returns `None` when the status cannot be read, which is
    never treated as a stall: an unreadable status is a missing observation,
    and convicting on it would kill healthy reviewers whenever herdr hiccups.
    """

    def __call__(self, session: ReviewerSession) -> Optional[str]: ...


def _default_record_count(session: ReviewerSession) -> int:
    path = session.extra.get(SESSION_PATH_KEY)
    if not path:
        return 0
    return DEFAULT_TRANSCRIPT_RECORD_COUNT(path)



class FinalizationWindow:
    """One bounded span around exactly one reviewer (§6.5, §11.2).

    Owns no store and no launcher: every collaborator is injected, in the
    same shape `Watchdog` uses, so this module imports no database driver
    and spawns no process of its own.

    Drive it with `run`, or step it with `open` / `report_launched` /
    `poll` when the caller owns the loop (which is also what makes it
    testable without sleeping out a real timeout).
    """

    def __init__(
        self,
        config: FinalizationConfig,
        launch: ReviewerLauncher,
        poll_report: ReportPoller,
        kill: ReviewerKiller,
        process_alive: Callable[[int], bool] = DEFAULT_PROCESS_ALIVE,
        transcript_record_count: Callable[[ReviewerSession], int] = (
            _default_record_count),
        actor_status: Optional[ActorStatusReader] = None,
        time_source: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._launch = launch
        self._poll_report = poll_report
        self._kill = kill
        self._process_alive = process_alive
        self._transcript_record_count = transcript_record_count
        self._actor_status = actor_status
        self._time_source = time_source
        self._wall_clock = wall_clock

        self._session: Optional[ReviewerSession] = None
        self._opened_at_monotonic: Optional[float] = None
        self._outcome: Optional[WindowOutcome] = None
        # The turn clock's own state: last observed count and when it last
        # advanced. Written only by `poll`, as §7.6's heartbeat cache is.
        self._turn_observed_count = 0
        self._turn_observed_at: Optional[float] = None
        # B14's arming latch. False until the route has reported the reviewer
        # actually working at least once, because until then `idle` means "at a
        # fresh prompt, not started", which is the normal post-launch state and
        # not a stall.
        self._actor_was_working = False
        # Whether any status at all has been read. A pane whose status cannot
        # be read is an unobserved reviewer, not a failed one, so NEVER_STARTED
        # stays unreachable until the route has answered at least once.
        self._actor_status_seen = False
        # The confirmation state for quiescence: when the current unbroken run
        # of `idle` began, and the transcript record count it began at. Any
        # record appearing, any working status, or any unreadable status clears
        # it — which is what stops a reviewer blocked in a tool call from being
        # convicted on one sample.
        self._quiescent_since: Optional[float] = None
        self._quiescent_at_count = 0
        # The route's most recent *readable* status, which is what the turn
        # clock is gated on. Deliberately not `_actor_was_working`, which is a
        # latch over the whole window: a reviewer that worked once and then
        # died must stay convictable, so the turn clock reads what the route
        # says *now* rather than what it once said. An unreadable poll leaves
        # this alone rather than clearing it, for the same reason the
        # quiescence latch treats an unreadable status as a missing
        # observation — a route that hiccups says nothing about the reviewer.
        self._actor_status_current: Optional[str] = None

    # ── the span ────────────────────────────────────────────────────────

    @property
    def session(self) -> Optional[ReviewerSession]:
        return self._session

    def open(self) -> ReviewerSession:
        """Stamp the span clock and launch the reviewer.

        The clock is stamped *before* `launch` is called so that a slow
        launch spends this window's budget rather than running unbounded
        beside it.
        """
        if self._session is not None:
            raise RuntimeError(
                "a finalization window launches exactly one reviewer (§11.1)")
        self._opened_at_monotonic = self._time_source()
        session = self._launch()
        session.opened_at_epoch = self._wall_clock()
        self._session = session
        return session

    def report_launched(self, pid: Optional[int] = None,
                        session_path: Optional[Any] = None) -> ReviewerSession:
        """The adapter reporting the reviewer launched — what arms §7.6's
        process-alive and turn-count signals, and what starts the turn
        clock. The stamp is taken from this window's own clock, so a
        caller cannot hand in an epoch value.
        """
        session = self._require_open()
        session.launched_at = self._time_source()
        if pid is not None:
            session.pid = pid
        if session_path is not None:
            session.extra[SESSION_PATH_KEY] = session_path
        return session

    def poll(self) -> Optional[WindowOutcome]:
        """One evaluation of the window. Returns None while it is open."""
        session = self._require_open()
        if self._outcome is not None:
            raise RuntimeError(
                "the finalization window already converted to an outcome")

        report = self._poll_report()
        if report is not None:
            return self._finish(WindowOutcome(
                completed=True, session=session,
                elapsed_s=self._elapsed(), report=report,
                observed_working=self._actor_was_working))

        # (b) the one span-bounding wall clock, over everything.
        elapsed = self._elapsed()
        if elapsed > self._config.finalization_timeout_s:
            return self._stall(FinalizationSignal.WINDOW_TIMEOUT, elapsed)

        if not session.armed:
            # Pre-launch: no process and no transcript exist yet, by
            # construction (§7.6). Only the span bound above applies.
            return None

        if session.pid is not None and not self._process_alive(session.pid):
            return self._stall(FinalizationSignal.PROCESS_DEAD, elapsed)

        # The transcript is read *before* any status is judged, and that order
        # is deliberate. `idle` covers both "between turns" and "blocked in a
        # tool call", so the status alone cannot say whether anything stopped;
        # the transcript can. Judging this poll's status against the previous
        # poll's record count would decide that question on stale evidence.
        record_count = self._transcript_record_count(session)
        if self._turn_observed_at is None:
            # The turn clock starts at launch, not at open and not at this
            # first poll.
            self._turn_observed_count = record_count
            self._turn_observed_at = session.launched_at
        elif record_count > self._turn_observed_count:
            self._turn_observed_count = record_count
            self._turn_observed_at = self._time_source()

        now = self._time_source()
        status: Optional[str] = None
        if self._actor_status is not None:
            raw = self._actor_status(session)
            if raw is not None:
                status = raw.strip().casefold()
                self._actor_status_seen = True
                self._actor_status_current = status
            if status in LIVE_WORKING_STATUSES:
                self._actor_was_working = True

        # A failed start (#89). Armed, answering, never once working, nothing
        # written: there is no review here to wait for, and waiting for it at
        # the span bound is what the issue's acceptance refuses. Checked only
        # where a status reader exists, because without one "never reported
        # working" is not an observation about the reviewer.
        if (self._actor_status is not None
                and self._actor_status_seen
                and not self._actor_was_working
                and record_count == 0
                and session.launched_at is not None
                and (now - session.launched_at)
                > self._config.effective_start_deadline_s):
            return self._stall(FinalizationSignal.NEVER_STARTED, elapsed)

        # (c) B14 — quiescence after liveness, checked before the turn clock so
        # a reviewer that stopped without declaring is reported as what it is
        # rather than waiting out a timeout that would name the wrong cause.
        #
        # The report check at the top of this method already ran, so reaching
        # here with the actor idle means it is idle *and* has written nothing.
        # B14's fix is not a shorter timeout — a legitimate large review takes a
        # long time and any wall clock bounds honest work. It is noticing that
        # the thing stopped. What it must not do is mistake a pause for a stop:
        # the idle has to hold, and any record appearing inside it restarts the
        # confirmation from that record.
        if status == QUIESCENT_STATUS and self._actor_was_working:
            if (self._quiescent_since is None
                    or record_count > self._quiescent_at_count):
                self._quiescent_since = now
                self._quiescent_at_count = record_count
            elif (now - self._quiescent_since
                  > self._config.effective_quiescence_confirm_s):
                return self._stall(FinalizationSignal.ACTOR_ABANDONED, elapsed)
        else:
            # Working, blocked, unreadable, or a status this module does not
            # name: none of them confirm quiescence, and an unreadable status
            # is a missing observation rather than evidence of a stop.
            self._quiescent_since = None

        # The turn clock, gated the way the quiescence detector above it is:
        # on what the route reports, never on elapsed time alone.
        #
        # B14's binding lesson is "do not add a wall clock — a legitimate large
        # review takes a long time and a timeout bounds honest work", and this
        # check *was* that wall clock, sitting unconditionally underneath the
        # detector written to obey the lesson, and firing first. A reviewer
        # reasoning for longer than `turn_timeout_s` without emitting a
        # transcript record is indistinguishable from a dead one under elapsed
        # time, and B14 exists precisely because that inference is wrong. It
        # was paid for on `cmo-consolidation-l-r5`: `TURN_TIMEOUT after 128.6s`
        # against a pane that was still answering, with a growing revision
        # counter and a live transcript, and the plan left blocked with no
        # receipt written.
        #
        # So silence convicts only where the route is *not* reporting the actor
        # working. The signal keeps its whole job — an actor reported back at
        # its composer, or one the route has never once been able to describe,
        # still converts here rather than being paid for at the span bound.
        # What it can no longer do is contradict a live observation.
        #
        # Absence of a reader is not an observation of work. A window built
        # with no `actor_status` at all has only this clock and PROCESS_DEAD
        # beneath the span bound, so disarming it there would leave the span as
        # the sole detector — exactly the state B14 was recorded against. It
        # therefore still fires where nothing can be asked.
        route_reports_working = (
            self._actor_status is not None
            and self._actor_status_current in LIVE_WORKING_STATUSES)
        since_progress = self._time_source() - self._turn_observed_at
        if (since_progress > self._config.turn_timeout_s
                and not route_reports_working):
            return self._stall(FinalizationSignal.TURN_TIMEOUT, elapsed)
        return None

    def run(self, sleep: Callable[[float], None] = time.sleep) -> WindowOutcome:
        """Open, arm the recorded launch, then poll until the window converts.

        The arming step is not optional bookkeeping. `poll` returns early on
        `not session.armed`, so a `run` that never calls `report_launched`
        leaves PROCESS_DEAD, NEVER_STARTED, ACTOR_ABANDONED and TURN_TIMEOUT
        unreachable and the span bound as the *only* detector — the state
        B14 was recorded against, a reviewer idle at its prompt having written
        nothing while the verb waited out a wall clock. §6.5 requires the
        structural signals to be the working detector and the span bound to be
        the last resort; without this the order is inverted.

        It arms from what `open`'s launch already recorded rather than from
        anything a caller passes, so the stamp remains this window's own
        (§1.2: a launch reports, it does not assert a lifecycle transition).
        A launcher that captured no pid arms anyway — `launched_at` is what
        the turn clock and the quiescence latch need, and a `None` pid simply
        leaves PROCESS_DEAD inapplicable rather than leaving the window blind.
        """
        if self._session is None:
            self.open()
        session = self._require_open()
        if not session.armed:
            self.report_launched(
                pid=session.pid,
                session_path=session.extra.get(SESSION_PATH_KEY))
        while True:
            outcome = self.poll()
            if outcome is not None:
                return outcome
            sleep(self._config.poll_interval_s)

    # ── internals ───────────────────────────────────────────────────────

    def _require_open(self) -> ReviewerSession:
        if self._session is None:
            raise RuntimeError("the finalization window is not open")
        return self._session

    def _elapsed(self) -> float:
        return self._time_source() - float(self._opened_at_monotonic)

    def _stall(self, signal: FinalizationSignal,
               elapsed: float) -> WindowOutcome:
        """(c) expiry converts into a kill and a durable typed result.

        The kill is per launch path, on §8.3's honesty: where the process
        group is harness-owned it is terminated; a herdr-spawned reviewer
        under the recorded 0.8.0 surface has no group Maestro owns, so the
        verb stops waiting and reports the pane. The survivor is a leak,
        not a hazard (§7.8) — finalization measures nothing and merges
        nothing, and a report arriving after the declaration has no reader.

        The durable half is the returned `WindowOutcome` and nothing else.
        This module used to take a `StallRecorder` as well, and both of its
        call sites passed `lambda ...: None` — so the seam read as wired
        while recording nothing anywhere, which is how four distinct signals
        came to settle as one free-text reason. Each caller now records where
        it settles, against the store that actually owns that phase's
        lifecycle, from the typed `signal` on this outcome.
        """
        session = self._require_open()
        killed = False
        if session.harness_owned_group:
            self._kill(session)
            killed = True
        return self._finish(WindowOutcome(
            completed=False, session=session, elapsed_s=elapsed,
            signal=signal, killed=killed,
            observed_working=self._actor_was_working))

    def _finish(self, outcome: WindowOutcome) -> WindowOutcome:
        self._outcome = outcome
        return outcome
