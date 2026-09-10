"""The operator's interrupt, as one flag every in-flight wait can read.

An operator who presses Ctrl-C is asking for the run to stop. That request is
recorded in the ledger by `FactoryScheduler._pause_on_interrupt` -- a
`USER_WAIT` with `wait_reason` PAUSE on every lane that was executing -- and
the scheduler then returns `WAITING`. Until this module existed, that was the
whole of it, and it was only half a stop.

`signal` delivers SIGINT to the **main** thread. With `concurrency` above 1 a
lane's stage runs on a `ThreadPoolExecutor` worker, so the `KeyboardInterrupt`
unwinds the main thread while the worker sits inside its wait, which is
`HerdrStageActor._await_envelope` -- a loop that by design ends on the
envelope and on nothing else. `ThreadPoolExecutor` threads are not daemons and
`concurrent.futures.thread._python_exit` joins them at interpreter shutdown, so
the process cannot exit while one of them polls. On FDAdb run `d246ae95`
(2026-09-10) that is exactly what happened: the ledger recorded PAUSE, the
console printed `run finished waiting`, and the process went on printing
`waiting on test-reviewer <N>s elapsed` every 30 seconds for twenty minutes
until it was SIGKILLed.

The flag here is the second half. The signal handler sets it after the pause is
recorded; every wait on the dispatch path reads it and ends with
`AgentWaitInterrupted`, a typed refusal whose only meaning is "the operator
stopped this run". It is deliberately **not** a transport observation. A closed
pane, a missing herdr record and a quiet composer still end no wait -- four
incidents are recorded beside `_await_envelope` explaining why. An operator
pressing Ctrl-C is not a signal about the agent; it is a decision about the run,
and it is the only thing besides the envelope that may end that wait.

Process-wide rather than scheduler-owned because the waits it must reach are
spread across `maestro.py` and `adw_modules/launcher.py` and are handed no
scheduler. One run executes per process, so one flag is exactly the scope.
"""

from __future__ import annotations

import threading


class AgentWaitInterrupted(RuntimeError):
    """A wait ended because the operator interrupted the run.

    Never a statement about the agent, the pane, or the work. The lane it
    belongs to is already paused in the ledger by the time this is raised --
    the handler records the pause first -- so a caller that sees it has
    nothing left to record and must not treat it as a lane failure.
    """


_STOP = threading.Event()


def request_stop() -> None:
    """Ask every in-flight wait to end. Called after the pause is recorded."""
    _STOP.set()


def clear_stop() -> None:
    """Forget a previous interrupt, so the next `run()` starts unstopped."""
    _STOP.clear()


def stop_requested() -> bool:
    return _STOP.is_set()


def raise_if_stopped(what: str) -> None:
    """End here if the operator has interrupted; otherwise return."""
    if _STOP.is_set():
        raise AgentWaitInterrupted("OPERATOR_INTERRUPT:{0}".format(what))


def sleep(seconds: float, what: str) -> None:
    """`time.sleep`, except that an operator interrupt ends it immediately.

    A poll loop that spends its interval here ends on the interrupt rather
    than one interval later, and needs no cancellation branch of its own.
    """
    if _STOP.wait(max(0.0, seconds)):
        raise AgentWaitInterrupted("OPERATOR_INTERRUPT:{0}".format(what))
