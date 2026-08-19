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
      verdict about the plan.

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

**One clock, stated.** Every timeout in this module is measured in
`time.monotonic` and nothing else. The lifecycle store keeps
`last_transition_at` in **epoch** seconds, and mixing the two produced a
real defect earlier in this build, so the window stamps both separately:
`opened_at_monotonic` is the only value any comparison here reads, while
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
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

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


class SessionNotFresh(RuntimeError):
    """§6.5: the review runs in a fresh session directory, never the
    authoring node's, so context cannot leak through session continuity."""


class FinalizationSignal(str, Enum):
    """Which signal ended the window. Two are §7.6's, by value."""

    PROCESS_DEAD = wd.StallReason.PROCESS_DEAD.value
    TURN_TIMEOUT = wd.StallReason.TURN_TIMEOUT.value
    WINDOW_TIMEOUT = "WINDOW_TIMEOUT"
    #: B14: the reviewer went live and then stopped without declaring. Not a
    #: wall clock — it fires the moment the route reports the agent back at its
    #: composer with no report written, however long or short the review was.
    ACTOR_ABANDONED = "ACTOR_ABANDONED"


#: Herdr statuses that mean the agent is alive and doing something. `idle` is
#: deliberately absent: it is a *live* status, and `launch()` returns while an
#: agent still sits at a fresh prompt having done nothing, so treating idle as
#: liveness is what made B14 undetectable. Quiescence detection arms only after
#: one of these has been observed at least once.
LIVE_WORKING_STATUSES = frozenset({"working", "blocked"})

#: The status that means "back at the composer, not doing anything". After the
#: reviewer has been observed working, this means it stopped without declaring.
QUIESCENT_STATUS = "idle"


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

    def __post_init__(self) -> None:
        for name in ("finalization_timeout_s", "turn_timeout_s",
                     "poll_interval_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} is a wall clock; it is positive")


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
    turn_count: int = 0
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


# ── injected collaborators (Protocols; the ledger owns the tracer) ──────────

class ReviewerLauncher(Protocol):
    """Launches exactly one reviewer and returns its session identity."""

    def __call__(self) -> ReviewerSession: ...


class ReportPoller(Protocol):
    """Returns the reviewer's report once it exists, else None. Opaque to
    this module: verifying it against the matrix is finalization's job."""

    def __call__(self) -> Optional[Any]: ...


class SessionRecorder(Protocol):
    """Writes the tracer's reviewer-session row — the durable record that
    opens the window (§6.5, §11.2)."""

    def __call__(self, session: ReviewerSession) -> None: ...


class StallRecorder(Protocol):
    """Records `FINALIZATION_STALLED` durably beside the session row."""

    def __call__(self, session: ReviewerSession, signal: FinalizationSignal,
                 elapsed_s: float) -> None: ...


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


def require_fresh_session_dir(
    session_dir,
    authoring_dirs: Iterable[Any] = (),
) -> Path:
    """§6.5's structural half of "independent review is recorded".

    Refuses a directory that is, or is inside, any authoring session
    directory, and refuses one that already holds files — either would let
    the authoring context reach the reviewer through session continuity,
    which is the leak the fresh directory exists to prevent.
    """
    target = Path(session_dir).resolve()
    for authoring in authoring_dirs:
        authoring_resolved = Path(authoring).resolve()
        if target == authoring_resolved:
            raise SessionNotFresh(
                f"the reviewer's session directory is the authoring one: {target}")
        try:
            target.relative_to(authoring_resolved)
        except ValueError:
            continue
        raise SessionNotFresh(
            f"the reviewer's session directory {target} is inside the authoring "
            f"session {authoring_resolved}")
    if target.exists() and any(target.iterdir()):
        raise SessionNotFresh(
            f"the reviewer's session directory is not fresh: {target}")
    return target


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
        record_reviewer_session: SessionRecorder,
        record_stall: StallRecorder,
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
        self._record_reviewer_session = record_reviewer_session
        self._record_stall = record_stall
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

    # ── the span ────────────────────────────────────────────────────────

    @property
    def opened_at_monotonic(self) -> Optional[float]:
        """The only stamp any timeout in this module compares against."""
        return self._opened_at_monotonic

    @property
    def session(self) -> Optional[ReviewerSession]:
        return self._session

    def open(self) -> ReviewerSession:
        """Stamp the span clock, launch the reviewer, record the row.

        The clock is stamped *before* `launch` is called so that a slow
        launch spends this window's budget rather than running unbounded
        beside it. The tracer row is written immediately after the launch
        returns, because the row carries the identity only the launch can
        supply — and it is written before any poll, so a stall is
        diagnosable from the store even though no run row exists (§6.5).
        """
        if self._session is not None:
            raise RuntimeError(
                "a finalization window launches exactly one reviewer (§11.1)")
        self._opened_at_monotonic = self._time_source()
        session = self._launch()
        session.opened_at_epoch = self._wall_clock()
        self._session = session
        self._record_reviewer_session(session)
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
                elapsed_s=self._elapsed(), report=report))

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

        # (c) B14 — quiescence after liveness, checked before either clock so a
        # reviewer that stopped without declaring is reported as what it is
        # rather than waiting out a timeout that would name the wrong cause.
        #
        # The order matters and is the whole point: the report check at the top
        # of this method already ran, so reaching here with the actor idle means
        # it is idle *and* has written nothing. B14's fix is not a shorter
        # timeout — a legitimate large review takes a long time and any wall
        # clock bounds honest work. It is noticing that the thing stopped.
        if self._actor_status is not None:
            status = (self._actor_status(session) or "").strip().casefold()
            if status in LIVE_WORKING_STATUSES:
                self._actor_was_working = True
            elif status == QUIESCENT_STATUS and self._actor_was_working:
                return self._stall(FinalizationSignal.ACTOR_ABANDONED, elapsed)

        record_count = self._transcript_record_count(session)
        if self._turn_observed_at is None:
            # The turn clock starts at launch, not at open and not at this
            # first poll.
            self._turn_observed_count = record_count
            self._turn_observed_at = session.launched_at
        elif record_count > self._turn_observed_count:
            self._turn_observed_count = record_count
            self._turn_observed_at = self._time_source()
        session.turn_count = self._turn_observed_count

        since_progress = self._time_source() - self._turn_observed_at
        if since_progress > self._config.turn_timeout_s:
            return self._stall(FinalizationSignal.TURN_TIMEOUT, elapsed)
        return None

    def run(self, sleep: Callable[[float], None] = time.sleep) -> WindowOutcome:
        """Open, arm the recorded launch, then poll until the window converts.

        The arming step is not optional bookkeeping. `poll` returns early on
        `not session.armed`, so a `run` that never calls `report_launched`
        leaves PROCESS_DEAD, ACTOR_ABANDONED and TURN_TIMEOUT unreachable and
        the span bound as the *only* detector — which is precisely the state
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
        """
        session = self._require_open()
        killed = False
        if session.harness_owned_group:
            self._kill(session)
            killed = True
        self._record_stall(session, signal, elapsed)
        return self._finish(WindowOutcome(
            completed=False, session=session, elapsed_s=elapsed,
            signal=signal, killed=killed))

    def _finish(self, outcome: WindowOutcome) -> WindowOutcome:
        self._outcome = outcome
        return outcome
