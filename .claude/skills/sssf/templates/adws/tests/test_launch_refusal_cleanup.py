"""Every launch exit that registers no handle must state its own cleanup.

`HerdrLauncher.launch` splits a pane and then has five ways to fail with that
pane open. Each of them cleaned up after itself and then said nothing typed
about what the cleanup achieved: two raised bare `RuntimeError`, one raised
`ValueError` without closing the pane at all, and the widest one re-raised
herdr's own `HerdrCallError`. None of those is a `LaunchRefused`, so
`scheduler.LaunchFailed.pane_created` fell through to its fail-closed `True`,
the scheduler ran §8.3's quiesce step over an attempt whose handle had never
been registered, `quiesce_attempt` raised `PROCESS_GROUP_UNTRACKED` from inside
the except arm, and that `QuiescenceFailure` *replaced* the retryable
`LaunchFailed`. Terminal BLOCKED, `retry_class` NULL, launch never retried —
on run-1907d9c1f9d84def80272cb39b5fc137, for a pane herdr had refused with
`agent_pane_busy` while the sibling lane's split succeeded 30ms later.

The tests below convict each exit separately, and the two that matter for §8.3
are the pair: a close herdr accepted reports `pane_created=False`, a close
herdr refused reports `True`. The second is the fail-closed control — without
it the first is indistinguishable from "always say False", which is the lie
§8.3 exists to prevent.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import maestro
from adw_modules import launcher as lch
from adw_modules import retry_policy as rp
from adw_modules import scheduler as sch
from adw_modules import worktree as worktree_module
from adw_modules.route_receipts import (AdmittedRouteSet, load_admitted_routes,
                                        load_public_key)


class _AnyRoute(AdmittedRouteSet):
    """An admitted-route set that admits a route the launcher cannot build.

    The only way to reach `launch`'s UNSUPPORTED_ROUTE exit is to pass route
    admission with a route neither `build_omp_argv` nor `build_claude_argv`
    handles. Constructed through `object.__new__` because the real constructor
    demands the verification token, and `admits` is overridden so the unset
    slot is never read.
    """

    def admits(self, route: str) -> bool:  # noqa: D102 - see class docstring
        return True


class FakeHerdr:
    """A herdr stand-in that records argv and fails exactly where told."""

    def __init__(self, *, worktree: Path, transcript: Path) -> None:
        self.calls = []
        self.worktree = worktree
        self.transcript = transcript
        self.current_pane_id = "w0:p0"
        self.split_pane_id = "w1:p2"
        #: cwd `pane get` reports, per call index, falling back to the last.
        self.get_cwds = [str(worktree)]
        self.start_error = None
        self.close_error = None
        self.closed = []

    def __call__(self, *args, env=None, timeout=30.0):
        self.calls.append(list(args))
        head = tuple(args[:2])
        if head == ("pane", "current"):
            if self.current_pane_id is None:
                raise lch.HerdrCallError("LAUNCH_REFUSED:no current", "no_pane")
            return {"result": {"pane": {"pane_id": self.current_pane_id}}}
        if head == ("pane", "split"):
            if self.split_pane_id is None:
                return {"result": {"pane": {}}}
            return {"result": {"pane": {"pane_id": self.split_pane_id,
                                        "cwd": str(self.worktree)}}}
        if head == ("pane", "get"):
            index = min(len([c for c in self.calls if c[:2] == ["pane", "get"]]),
                        len(self.get_cwds)) - 1
            return {"result": {"pane": {"pane_id": self.split_pane_id,
                                        "cwd": self.get_cwds[index]}}}
        if head == ("pane", "process-info"):
            return {"result": {"process_info": {
                "pane_id": self.split_pane_id,
                "foreground_processes": [
                    {"name": "zsh", "argv0": "zsh", "argv": ["zsh"]}]}}}
        if head == ("pane", "close"):
            if self.close_error is not None:
                raise self.close_error
            self.closed.append(args[2])
            return {"result": {"type": "ok"}}
        if head == ("agent", "start"):
            if self.start_error is not None:
                raise self.start_error
            return {"result": {"agent": {
                "name": args[2], "status": "idle",
                "transcript_path": str(self.transcript)}}}
        if head == ("agent", "wait"):
            return {"result": {"ok": True, "status": "idle"}}
        if head == ("agent", "get"):
            if self.closed:
                return {"result": {}}
            return {"result": {"agent": {"name": args[2], "status": "idle",
                                         "transcript_path": str(self.transcript)}}}
        return {"result": {}}

    def argv_for(self, verb):
        return [call for call in self.calls if call[:2] == list(verb)]


class RefusalCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("do the work")
        self.envelope = self.root / "envelope.json"
        self.transcript = self.root / "session.jsonl"
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = load_public_key(fixtures / "route_receipts.pub")
        self.admitted_routes = load_admitted_routes(
            {"omp": fixtures / "omp.json", "claude": fixtures / "claude.json"},
            verify_keys=(key,))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def spec(self, route: str = "omp") -> lch.LaunchSpec:
        return lch.LaunchSpec(
            correlation_token="run1-node_a-1",
            worktree=self.worktree,
            prompt_path=self.prompt,
            envelope_path=self.envelope,
            route=route,
            model="openai-codex/gpt-5.6-sol",
            effort="high",
            profile="openai-performance",
            session_dir=self.root / "session",
            environment=worktree_module.launch_env(self.scratch))

    def build(self, admitted=None):
        harness = lch.HerdrLauncher(
            herdr_path=self.root / "herdr", omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=admitted or self.admitted_routes)
        fake = FakeHerdr(worktree=self.worktree, transcript=self.transcript)
        harness._herdr = fake
        return harness, fake

    @staticmethod
    def busy_pane():
        """What herdr answers when the pane cannot host an agent.

        The `code` is herdr's own typed `error.code` field, not a substring of
        the message: §1.2 forbids classifying on prose and the message is text
        herdr may reword at any release.
        """
        return lch.HerdrCallError(
            'LAUNCH_REFUSED:{"error":{"code":"agent_pane_busy",'
            '"message":"agent target pane w13A:pA is not an available shell"}}',
            "agent_pane_busy")

    # ── the incident: `agent start` refused after a successful split ────────

    def test_agent_start_refusal_is_a_typed_refusal_that_reaped_its_pane(self):
        harness, fake = self.build()
        fake.start_error = self.busy_pane()
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec())
        refusal = caught.exception
        self.assertIs(refusal.refusal, lch.LaunchRefusal.AGENT_START_REFUSED)
        self.assertEqual(fake.closed, ["w1:p2"])
        # herdr accepted the close, so the pane is gone and the refusal says
        # so. This is the field §8.3's quiesce step reads.
        self.assertFalse(refusal.pane_created)
        self.assertIs(refusal.__cause__, fake.start_error)

    def test_the_refusal_stays_a_retryable_launch_failure(self):
        """The end of the causal chain the incident took.

        `_launch_left_nothing_to_reap` is the scheduler's only gate on whether
        `_quiesce` runs for a failed launch. `True` here means no
        `PROCESS_GROUP_UNTRACKED`, so no `QuiescenceFailure` replaces the
        launch failure and the attempt spends a retry budget instead of
        blocking terminally.
        """
        harness, fake = self.build()
        fake.start_error = self.busy_pane()
        with self.assertRaises(sch.LaunchFailed) as caught:
            maestro._typed_launch_pane(harness, self.spec())
        failed = caught.exception
        self.assertFalse(failed.pane_created)
        self.assertTrue(sch._launch_left_nothing_to_reap(failed))
        # Sized as contention rather than as a broken launcher, and outside
        # the zero-budget partition so the STARTUP budget is actually spent.
        self.assertIs(failed.classified_failure,
                      rp.LauncherFailure.TRANSPORT)
        self.assertNotIn(failed.classified_failure,
                         rp.DETERMINISTIC_LAUNCHER_FAILURES)

    def test_a_busy_pane_is_classified_from_herdrs_typed_code(self):
        """Contention, not a broken launcher: TRANSIENT rather than EXECUTION.

        Read from `HerdrCallError.code`, which herdr populates from its
        `{"error": {"code": ...}}` envelope. The same exception carrying no
        code falls through to the structural default.
        """
        harness, fake = self.build()
        fake.start_error = self.busy_pane()
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec())
        self.assertIs(lch.classify_error(caught.exception),
                      lch.ErrorClass.TRANSIENT)
        self.assertIs(
            lch.classify_error(lch.HerdrCallError("LAUNCH_REFUSED:x", "")),
            lch.ErrorClass.EXECUTION)

    def test_a_close_herdr_refused_reports_a_pane_that_may_survive(self):
        """The fail-closed control on the pair. §8.3 never reports an absence
        nobody measured, and a close that raised measured nothing."""
        harness, fake = self.build()
        fake.start_error = self.busy_pane()
        fake.close_error = lch.HerdrCallError("LAUNCH_REFUSED:nope", "close_failed")
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec())
        self.assertTrue(caught.exception.pane_created)
        with self.assertRaises(sch.LaunchFailed) as wrapped:
            maestro._typed_launch_pane(harness, self.spec())
        self.assertTrue(wrapped.exception.pane_created)
        self.assertFalse(sch._launch_left_nothing_to_reap(wrapped.exception))

    # ── the other three post-split exits ────────────────────────────────────

    def test_unsupported_route_after_the_split_reaps_its_pane(self):
        harness, fake = self.build(admitted=object.__new__(_AnyRoute))
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(replace(self.spec(), route="gemini"))
        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.UNSUPPORTED_ROUTE)
        # The pane leak this exit used to be: it raised `ValueError` with the
        # split's pane still open and nothing tracking it.
        self.assertEqual(fake.closed, ["w1:p2"])
        self.assertFalse(caught.exception.pane_created)

    def test_binding_mismatch_before_the_agent_is_a_typed_refusal(self):
        harness, fake = self.build()
        fake.get_cwds = [str(self.root / "wrong")]
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec())
        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertEqual(fake.argv_for(("agent", "start")), [])
        self.assertEqual(fake.closed, ["w1:p2"])
        self.assertFalse(caught.exception.pane_created)

    def test_binding_mismatch_after_the_agent_is_a_typed_refusal(self):
        harness, fake = self.build()
        fake.get_cwds = [str(self.worktree), str(self.root / "wrong")]
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec())
        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.BINDING_MISMATCH)
        # `cancel` reaped it: the pane held a started agent, so the process
        # group goes first and the pane close is proved by `_agent_absent`.
        self.assertEqual(fake.closed, ["w1:p2"])
        self.assertFalse(caught.exception.pane_created)

    def test_no_pane_id_reported_still_demands_the_proof(self):
        """The one post-split exit with nothing to reap: no id, no close."""
        harness, fake = self.build()
        fake.split_pane_id = None
        with self.assertRaises(lch.LaunchRefused) as caught:
            harness.launch(self.spec())
        self.assertIs(caught.exception.refusal, lch.LaunchRefusal.NO_PANE)
        self.assertEqual(fake.closed, [])
        self.assertTrue(caught.exception.pane_created)

    # ── the split's parent, and the demoted readiness gate ──────────────────

    def test_the_split_names_a_pane_instead_of_the_shared_selector(self):
        """`--current` is a server-side selector over mutable focus state, so
        two concurrent launches can resolve it to two different panes — and
        one of them to a pane the other has just created."""
        harness, fake = self.build()
        harness.launch(self.spec())
        harness.launch(replace(self.spec(), correlation_token="run1-node_b-1"))
        splits = fake.argv_for(("pane", "split"))
        self.assertEqual(len(splits), 2)
        for split in splits:
            self.assertEqual(split[2], "w0:p0")
            self.assertNotIn("--current", split)
        # Resolved once. Re-asking would reintroduce the moving target.
        self.assertEqual(len(fake.argv_for(("pane", "current"))), 1)

    def test_an_unanswerable_selector_falls_back_rather_than_guessing(self):
        harness, fake = self.build()
        fake.current_pane_id = None
        harness.launch(self.spec())
        self.assertEqual(fake.argv_for(("pane", "split"))[0][2], "--current")

    def test_the_readiness_wait_is_advisory_and_no_longer_refuses(self):
        """A wall clock over a separate RPC cannot prove a pane is free when
        `agent start` reaches the server. herdr's own precondition check is
        the authority, and it now arrives as a typed retryable refusal."""
        calls = []

        def busy(*args, **kwargs):
            calls.append(list(args))
            return {"result": {"process_info": {
                "foreground_processes": [
                    {"name": "python", "argv0": "python", "argv": ["python"]}]}}}

        self.assertIsNone(
            lch._wait_for_available_shell(busy, "w1:p2", timeout_s=0.05))
        self.assertTrue(calls)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
