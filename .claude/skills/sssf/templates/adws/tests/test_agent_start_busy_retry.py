"""A pane whose shell has not settled must not cost an attempt.

`herdr pane split` returns as soon as the pane exists. Its shell may still be
running login hooks — direnv, a keychain lookup — for a moment afterwards, and
`agent start` arriving inside that moment is refused `agent_pane_busy`. On
run-2a44d226e75a4be391a14f02b78a6d25 that timing accident cost node
`lane-p1-freeze-and-run-log` its fourth attempt at zero turns, and because the
refusal was then masked by a quiescence error (see
`test_quiesce_undispatched_attempt.py`) it cost the whole run.

Classifying the refusal ENVIRONMENTAL makes the run survive it and is not
enough on its own: the lane still burns an attempt and an operator still reads
a failure with nothing in its diff. The remedy belongs where the authority is.

**Why the client-side gate was not restored.** `_wait_for_available_shell`
used to raise `SHELL_NOT_READY` at its deadline and was deliberately demoted
to advisory, because a wall clock over a separate RPC cannot prove what it is
asked to prove: its last snapshot is already stale when `agent start` reaches
the server, so a pane it calls ready can be busy a millisecond later and a
pane it calls busy can be free. That reasoning still holds and
`test_launch_refusal_cleanup.py::test_the_readiness_wait_is_advisory_and_no_longer_refuses`
still holds it. So the fix re-offers the pane to herdr's own server-side
precondition — the one check with no gap — inside a bounded window, and the
advisory wait stays advisory.

Two bounds, both finite and both needed: the window here spends seconds inside
one launch, and the refusal it eventually raises is retryable, so the
attempt-level budget spends launches into a *fresh* pane — the only remedy
left if this pane is durably occupied.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import launcher as lch          # noqa: E402
from adw_modules import worktree as worktree_module  # noqa: E402
from adw_modules.route_receipts import (load_admitted_routes,  # noqa: E402
                                        load_public_key)

from test_launch_refusal_cleanup import FakeHerdr   # noqa: E402


#: The refusal herdr returned in run-2a44d226e75a4be391a14f02b78a6d25, kept
#: verbatim: the decision is read off the typed `error.code` parsed from this
#: envelope, never matched out of the message (§1.2).
BUSY_PAYLOAD = (
    'LAUNCH_REFUSED:{"error":{"code":"agent_pane_busy","message":"agent '
    'target pane w13A:p29 is not an available shell"},"id":"cli:agent:start"}')


def busy() -> lch.HerdrCallError:
    return lch.HerdrCallError(BUSY_PAYLOAD, "agent_pane_busy")


class _SettlingHerdr(FakeHerdr):
    """A pane that answers `agent_pane_busy` for its first `n` starts."""

    def __init__(self, *, worktree: Path, transcript: Path,
                 busy_starts: int) -> None:
        super().__init__(worktree=worktree, transcript=transcript)
        self.busy_starts = busy_starts
        self.start_attempts = 0

    def __call__(self, *args, env=None, timeout=30.0):
        if tuple(args[:2]) == ("agent", "start"):
            self.start_attempts += 1
            if self.start_attempts <= self.busy_starts:
                raise busy()
        return super().__call__(*args, env=env, timeout=timeout)


class _Clock:
    """A monotonic source the test advances, so no case sleeps for real."""

    def __init__(self, step: float = 0.5) -> None:
        self.now = 0.0
        self.step = step
        self.slept = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class ReOfferTests(unittest.TestCase):
    """`_start_agent_when_free` in isolation, on a driven clock."""

    def test_a_pane_that_settles_starts_without_costing_an_attempt(self):
        clock = _Clock()
        calls = []

        def start():
            calls.append(len(calls))
            if len(calls) <= 3:
                raise busy()
            return {"result": {"agent": {"name": "a"}}}

        result = lch._start_agent_when_free(
            start, window_s=10.0, poll_s=0.5,
            sleep=clock.sleep, monotonic=clock.monotonic)

        self.assertEqual(result, {"result": {"agent": {"name": "a"}}})
        self.assertEqual(len(calls), 4)
        self.assertEqual(clock.slept, [0.5, 0.5, 0.5])

    def test_the_window_is_finite_and_the_refusal_still_arrives(self):
        """The termination control. A durably occupied pane must not loop."""
        clock = _Clock()
        calls = []

        def start():
            calls.append(len(calls))
            raise busy()

        with self.assertRaises(lch.HerdrCallError) as caught:
            lch._start_agent_when_free(
                start, window_s=2.0, poll_s=0.5,
                sleep=clock.sleep, monotonic=clock.monotonic)

        self.assertEqual(caught.exception.code, "agent_pane_busy")
        # Bounded by the window, not by the number of refusals.
        self.assertLessEqual(clock.now, 2.0 + 0.5)
        self.assertGreater(len(calls), 1)

    def test_no_start_is_offered_at_or_past_the_deadline(self):
        """The bound is on the offers, not merely on the loop's exit.

        The deadline was read only *before* the poll, so the poll carried the
        clock past it and bought one more `agent start` at `deadline +
        poll_s`. A window that authorises an offer outside itself is a window
        in name only: the point of bounding this loop is that the attempt-level
        retry gets its fresh pane on schedule, and a launch that keeps talking
        to a pane herdr has already called busy past its own budget is the
        cost the bound exists to cap.
        """
        clock = _Clock()
        offered = []

        def start():
            offered.append(clock.now)
            raise busy()

        with self.assertRaises(lch.HerdrCallError):
            lch._start_agent_when_free(
                start, window_s=2.0, poll_s=0.5,
                sleep=clock.sleep, monotonic=clock.monotonic)

        self.assertTrue(offered)
        self.assertTrue(all(at < 2.0 for at in offered), offered)

    def test_a_refusal_herdr_does_not_call_survivable_raises_at_once(self):
        """Only herdr's own transient vocabulary is re-offered. Everything
        else is a refusal about this launch and is raised on the first no."""
        clock = _Clock()
        calls = []

        def start():
            calls.append(len(calls))
            raise lch.HerdrCallError("LAUNCH_REFUSED:nope", "no_such_pane")

        with self.assertRaises(lch.HerdrCallError):
            lch._start_agent_when_free(
                start, window_s=10.0, poll_s=0.5,
                sleep=clock.sleep, monotonic=clock.monotonic)

        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.slept, [])

    def test_an_untyped_failure_is_not_re_offered(self):
        """A `HerdrCallError` carrying no code says nothing survivable."""
        clock = _Clock()
        calls = []

        def start():
            calls.append(len(calls))
            raise lch.HerdrCallError("LAUNCH_REFUSED:x", "")

        with self.assertRaises(lch.HerdrCallError):
            lch._start_agent_when_free(
                start, window_s=10.0, poll_s=0.5,
                sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(len(calls), 1)

    def test_a_zero_window_refuses_on_the_first_answer(self):
        calls = []

        def start():
            calls.append(len(calls))
            raise busy()

        with self.assertRaises(lch.HerdrCallError):
            lch._start_agent_when_free(start, window_s=0.0, poll_s=0.5)
        self.assertEqual(len(calls), 1)


class SettlingPaneLaunchTests(unittest.TestCase):
    """The same, through `HerdrLauncher.launch`: no failed attempt."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("do the work")
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = load_public_key(fixtures / "route_receipts.pub")
        self.admitted = load_admitted_routes(
            {"omp": fixtures / "omp.json", "claude": fixtures / "claude.json"},
            verify_keys=(key,))

    def spec(self) -> lch.LaunchSpec:
        return lch.LaunchSpec(
            correlation_token="run1-lane_p1-4", worktree=self.worktree,
            prompt_path=self.prompt, envelope_path=self.root / "envelope.json",
            route="omp", model="openai-codex/gpt-5.6-sol", effort="high",
            profile="openai-performance", session_dir=self.root / "session",
            context_window_tokens=400_000,
            environment=worktree_module.launch_env(self.scratch))

    def build(self, busy_starts: int, window_s: float):
        harness = lch.HerdrLauncher(
            herdr_path=self.root / "herdr", omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"), admitted_routes=self.admitted)
        harness.agent_start_busy_window_s = window_s
        fake = _SettlingHerdr(worktree=self.worktree,
                              transcript=self.root / "session.jsonl",
                              busy_starts=busy_starts)
        harness._herdr = fake
        return harness, fake

    def test_a_pane_still_settling_produces_a_handle_not_a_refusal(self):
        """The incident's shape, with the pane settling as it normally does."""
        harness, fake = self.build(busy_starts=2, window_s=5.0)

        handle = harness.launch(self.spec())

        self.assertEqual(handle.pane_id, fake.split_pane_id)
        self.assertEqual(fake.start_attempts, 3)
        # Nothing was reaped: the pane that eventually started is the pane the
        # split produced, so no attempt and no pane were spent.
        self.assertEqual(fake.closed, [])

    def test_a_pane_that_never_frees_still_refuses_and_reaps(self):
        """The negative control. The window cannot become an unbounded wait,
        and when it expires the launch refuses exactly as it did before —
        typed, retryable, and with the pane closed behind it."""
        harness, fake = self.build(busy_starts=99, window_s=0.0)

        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec())

        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.AGENT_START_REFUSED)
        self.assertEqual(fake.start_attempts, 1)
        self.assertEqual(fake.closed, [fake.split_pane_id])
        self.assertFalse(caught.exception.pane_created)
        self.assertIs(lch.classify_error(caught.exception),
                      lch.ErrorClass.TRANSIENT)

    def test_the_production_window_is_finite(self):
        self.assertGreater(lch.AGENT_START_BUSY_WINDOW_S, 0.0)
        self.assertLess(lch.AGENT_START_BUSY_WINDOW_S, 60.0)
        self.assertGreater(lch.AGENT_START_BUSY_POLL_S, 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
