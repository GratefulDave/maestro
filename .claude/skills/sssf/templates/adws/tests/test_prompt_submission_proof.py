"""§7.6 — a prompt is submitted when the pane consumed it, not when it says `idle`.

Recorded failure, 2026-08-18, run-5aa698ce4753498ebc71a6b29b8245aa. Two depth-0
lanes launched, both panes sat holding an unsubmitted `@<prompt-path>` in the
composer, both attempts were cancelled `LAUNCHER_TRANSIENT` at **0 turns**, and
the run burned attempt after attempt without a single agent turn ever starting.

`submit_agent_prompt` already had the whole recovery: `herdr agent prompt` sends
the text plus an encoded Enter atomically, `--wait` demands an observed lifecycle
change, and a four-round loop presses Enter on a composer that swallowed it. None
of it ran. The launch site asked for `until=("working", "idle")`, and `idle` is
precisely what a pane reports when it never accepted the prompt — so the wait
that existed to prove submission was satisfied by the failure it was meant to
catch, and the function returned before pressing Enter even once.

`idle` was not added carelessly: a short task can finish and be back at idle
before `working` is ever sampled, so demanding `working` alone would fail a
perfectly good launch. The repair is therefore not to drop `idle` from the wait
but to stop believing a status word at all. `pane get` carries a monotonic
`revision` counter — a typed integer the terminal maintains, which §1.2 permits
reading — and an unsubmitted composer cannot advance it. Both the fast-task case
and the stalled case are then distinguishable, because the fast task consumed a
turn and the stall consumed nothing.

Unreadable revisions read as *not submitted*. Pressing Enter again on a prompt
that did go through costs a keystroke; believing an unproven submission costs the
attempt, and then the node's whole retry budget.
"""

from __future__ import annotations

import inspect
import sys
import threading
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch  # noqa: E402


class FakeHerdr:
    """A pane whose revision advances when the composer repaints.

    Pasting the prompt is a repaint, so `send-text` may tick the counter
    without the turn having started. Tests must not treat that tick as
    submission. `stalls` is how many `send-text` / `send-keys` rounds are
    swallowed before the composer starts accepting. `status_ok` mimics herdr
    answering the lifecycle wait successfully — which the stalled pane does,
    reporting `idle`, and which is exactly why the wait alone proves nothing.
    """

    def __init__(
        self,
        stalls: int = 0,
        status_ok: bool = True,
        revision: object = 0,
        agent_status: str = "idle",
    ):
        self.stalls = stalls
        self.status_ok = status_ok
        self.revision = revision
        self.agent_status = agent_status
        self.calls: list = []

    def __call__(self, *argv, **_kwargs):
        self.calls.append(argv)
        verb = argv[:2]
        if verb == ("pane", "get"):
            return {"result": {"pane": {"revision": self.revision}}}
        if verb == ("agent", "get"):
            # herdr's field is `agent_status`; `status` is always None.
            return {"result": {"agent": {
                "agent_status": self.agent_status, "status": None,
            }}}
        if verb in (("pane", "send-text"), ("agent", "send-keys")):
            if self.stalls > 0:
                self.stalls -= 1
                if verb == ("pane", "send-text"):
                    raise RuntimeError("agent_prompt_stalled")
                return {}
            if isinstance(self.revision, int):
                self.revision += 1
            return {}
        if verb == ("agent", "wait"):
            if not self.status_ok:
                raise RuntimeError("timeout")
            return {}
        return {}

    def count(self, *verb):
        return sum(1 for call in self.calls if call[: len(verb)] == verb)



def _recorded_after_pane_enter(herdr: FakeHerdr):
    """True once the offer's Enter has been issued. Not a meter. Not paste."""

    def recorded() -> bool:
        return any(
            call[:2] == ("pane", "send-keys")
            and len(call) > 3
            and call[3] == "enter"
            for call in herdr.calls
        )

    return recorded


def _recorded_after_agent_enters(herdr: FakeHerdr, n: int):
    """True after `n` recovery Enters. Send-text repaint never counts."""

    def recorded() -> bool:
        return herdr.count("agent", "send-keys") >= n

    return recorded

class SubmissionProof(unittest.TestCase):
    def test_the_completion_popup_is_dismissed_before_the_enter(self):
        """Recorded failure, 2026-08-27, run-faa4dc49ac954899a7445d9b447c0443.

        `lane-routing-chemical-tests` held
        `@/Users/.../agent-prompt.txt` in its omp composer, unsubmitted, at
        pane revision 2, while the launcher pressed Enter. Measured directly
        against a live omp composer that same day: typing `@/etc/hosts` leaves
        the file-completion popup open (the pane renders `hosts` and
        `hosts.equiv` beneath the composer), the next Enter is consumed to
        accept a completion rather than to submit, and only a *second* Enter
        starts the turn. `esc` closes the popup and leaves the composed text
        intact, after which one Enter submits.

        So the Enter must never be the first key after the text. Asserted as
        an order over the argv the function actually issues, because a count
        of Enters is exactly what a popup-eaten Enter satisfies.
        """
        herdr = FakeHerdr()
        try:
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        except lch.PromptNotSubmitted:
            pass
        keys = [
            call for call in herdr.calls
            if call[:2] in (("pane", "send-text"), ("pane", "send-keys"))
        ]
        verbs = [(call[1], call[3] if len(call) > 3 else "") for call in keys]
        self.assertEqual(verbs[0][0], "send-text")
        self.assertEqual(verbs[1], ("send-keys", "esc"))
        self.assertEqual(verbs[2], ("send-keys", "enter"))

    def test_paste_settle_sleeps_between_send_text_and_enter(self):
        """send-text returns when herdr writes bytes, not when the composer
        takes them. PASTE_SETTLE_S must land between those two calls."""
        slept = []
        herdr = FakeHerdr()
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=slept.append,
            submission_recorded=_recorded_after_pane_enter(herdr),
        )
        keys = [
            call for call in herdr.calls
            if call[:2] in (("pane", "send-text"), ("pane", "send-keys"))
        ]
        text_i = next(
            i for i, call in enumerate(keys) if call[:2] == ("pane", "send-text")
        )
        enter_i = next(
            i for i, call in enumerate(keys)
            if call[:2] == ("pane", "send-keys") and call[3] == "enter"
        )
        self.assertLess(text_i, enter_i)
        self.assertGreaterEqual(slept.count(lch.PASTE_SETTLE_S), 1)

    def test_esc_is_pressed_once_and_never_after_the_prompt_lands(self):
        """`esc` is omp's INTERRUPT key, not just a popup dismissal.

        The composer renders `Working... <esc>` while a turn runs. Pressing
        `esc` on every recovery round therefore killed the turn it was meant
        to rescue: run-c672e173f33044489d37f12527c5b251 attempts 1 and 2 each
        ran nine turns and then died `ENVIRONMENTAL` with the agent stopped
        mid-work, which is a strictly worse failure than the unsubmitted
        prompt the key was added to fix.

        One `esc`, before the first Enter, when nothing can be running yet.
        """
        herdr = FakeHerdr(stalls=99, revision=0)
        with self.assertRaises(lch.PromptNotSubmitted):
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                attempts=4,
                sleep=lambda _s: None,
            )
        escapes = [
            call for call in herdr.calls
            if call[:2] in (("pane", "send-keys"), ("agent", "send-keys"))
            and len(call) > 3 and call[3] == "esc"
        ]
        self.assertEqual(len(escapes), 1)
        enters = [
            call for call in herdr.calls
            if call[:2] in (("pane", "send-keys"), ("agent", "send-keys"))
            and len(call) > 3 and call[3] == "enter"
        ]
        self.assertGreater(len(enters), 1)
        self.assertLess(herdr.calls.index(escapes[0]), herdr.calls.index(enters[0]))

    def test_an_accepted_prompt_needs_no_recovery(self):
        herdr = FakeHerdr()
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=lambda _s: None,
            submission_recorded=_recorded_after_pane_enter(herdr),
        )
        self.assertEqual(herdr.count("agent", "send-keys"), 0)

    def test_a_send_text_repaint_without_a_record_is_not_submitted(self):
        """Meter fallback guard. FakeHerdr ticks revision on paste; that is
        not proof. If `current > baseline` starts returning True again, this
        assertion fails because the call would succeed with zero recovery."""
        herdr = FakeHerdr()
        with self.assertRaises(lch.PromptNotSubmitted):
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )

    def test_a_working_agent_does_not_outrank_a_legible_static_meter(self):
        """Recorded failure, 2026-08-27, run-8d1a71f463e4430f92a125a8f8b3731d.

        This test asserted the opposite until production falsified it. A
        reviewer on the `claude` route sat at revision 1 holding
        `@<prompt>.md` in its composer while `working` was believed, the
        launcher returned a handle, and the finalization window waited forty
        minutes for a report no actor was writing. The node blocked
        `ENVIRONMENTAL_BUDGET_EXHAUSTED` with receipt `NEVER_STARTED` and the
        operator found the prompt still on screen.

        Claude Code reports `working` while it *boots*. The status therefore
        cannot separate "took the prompt" from "starting up" — exactly the
        blindness `idle` had in the original incident, one word over. A
        legible counter can, so where it can be read it decides, and Enter is
        pressed on a composer the status was vouching for.
        """

        class StaticWorking(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "send-text"):
                    self.calls.append(argv)
                    return {}
                return super().__call__(*argv, **kwargs)

        herdr = StaticWorking(stalls=99, revision=41, agent_status="working")
        with self.assertRaises(lch.PromptNotSubmitted):
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                working_proves=True,
                sleep=lambda _s: None,
            )
        self.assertEqual(herdr.revision, 41)
        self.assertEqual(herdr.count("agent", "send-keys"), lch.SUBMIT_ATTEMPTS)

    def test_a_working_agent_still_proves_it_when_the_meter_is_unreadable(self):
        """Typed working remains the fallback when the meter cannot be read.
        herdr's field is `agent_status`; a fake that only set `status` would
        not convict a regression that forgot that field."""

        class UnreadableWorking(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "send-text"):
                    self.calls.append(argv)
                    return {}
                return super().__call__(*argv, **kwargs)

        herdr = UnreadableWorking(revision=None, agent_status="working")
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            working_proves=True,
            sleep=lambda _s: None,
        )
        self.assertEqual(herdr.count("agent", "send-keys"), 0)

    def test_a_swallowed_prompt_is_not_believed_because_the_pane_says_idle(self):
        """The recorded failure, and the case that used to return success.

        The lifecycle wait answers happily throughout — that is what `idle`
        does — so only the revision keeps the loop honest.
        """
        herdr = FakeHerdr(stalls=2)
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=lambda _s: None,
            submission_recorded=_recorded_after_agent_enters(herdr, 2),
        )
        # Enter was actually pressed, which is the whole point.
        self.assertGreaterEqual(herdr.count("agent", "send-keys"), 1)

    def test_recovery_wait_cannot_match_the_still_idle_composer(self):
        herdr = FakeHerdr(stalls=2)

        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=lambda _s: None,
            submission_recorded=_recorded_after_agent_enters(herdr, 2),
        )

        recovery_waits = [call for call in herdr.calls if call[:2] == ("agent", "wait")]
        self.assertTrue(recovery_waits)
        for call in recovery_waits:
            self.assertIn("working", call)
            self.assertNotIn("idle", call)

    def test_a_composer_that_never_accepts_is_refused_rather_than_reported_ok(self):
        herdr = FakeHerdr(stalls=99)
        with self.assertRaises(lch.PromptNotSubmitted):
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertEqual(herdr.count("agent", "send-keys"), lch.SUBMIT_ATTEMPTS)

    def test_a_prompt_the_agent_finished_before_working_was_sampled_still_passes(self):
        """Why `idle` cannot simply be dropped from the wait.

        The task is done and the pane is idle again — but it consumed a turn.
        Proof is the rising record (here: the offer Enter), not the meter.
        """
        herdr = FakeHerdr()
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=lambda _s: None,
            submission_recorded=_recorded_after_pane_enter(herdr),
        )
        self.assertEqual(herdr.count("agent", "send-keys"), 0)

    def test_a_legible_counter_that_never_moves_is_a_genuine_refusal(self):
        """The meter was readable the whole time and did not move.

        That is a fact about the composer, so it stays terminal and stays
        classified as EXECUTION.
        """
        herdr = FakeHerdr(stalls=99, revision=41)
        with self.assertRaises(lch.PromptNotSubmitted) as caught:
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertIn("AGENT_PROMPT_UNSUBMITTED", str(caught.exception))
        self.assertEqual(lch.classify_error(caught.exception), lch.ErrorClass.EXECUTION)

    def test_an_unreadable_revision_is_unproven_rather_than_unsubmitted(self):
        """D9. "I could not read the meter" is not "the meter did not move".

        Both fail closed -- nothing here ever reports the prompt as submitted,
        and Enter is still pressed every round -- but a herdr that cannot be
        read is an environmental condition the next attempt survives, not a
        wedged composer that has earned the node's terminal verdict.
        """
        herdr = FakeHerdr(revision=None)
        with self.assertRaises(lch.PromptSubmissionUnobservable) as caught:
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertIn("AGENT_PROMPT_UNOBSERVED", str(caught.exception))
        self.assertNotIsInstance(caught.exception, lch.PromptNotSubmitted)
        # Reuses the existing transient class; no new retry class exists.
        self.assertEqual(lch.classify_error(caught.exception), lch.ErrorClass.TRANSIENT)
        # Fails closed all the same: recovery was attempted every round.
        self.assertEqual(herdr.count("agent", "send-keys"), lch.SUBMIT_ATTEMPTS)

    def test_a_baseline_read_that_fails_outright_is_also_unproven(self):
        """The recorded co-cause shape: one transient `pane get` at baseline.

        The prompt may well have landed; the launcher simply never saw it. A
        counter that is legible again afterwards does not retroactively supply
        the missing baseline, so this is still unproven -- but transiently so.
        """

        class BlindBaseline(FakeHerdr):
            def __init__(self) -> None:
                super().__init__(revision=5)
                self.reads = 0

            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "get"):
                    self.reads += 1
                    if self.reads == 1:
                        raise RuntimeError("herdr timeout")
                return super().__call__(*argv, **kwargs)

        herdr = BlindBaseline()
        with self.assertRaises(lch.PromptSubmissionUnobservable):
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )

    def test_a_counter_that_becomes_unreadable_after_a_good_baseline_is_unproven(self):
        """The mirror case: baseline legible, every later read fails.

        There is still no before/after pair, so there is still no fact about
        the prompt -- only one about herdr.
        """

        class GoesBlind(FakeHerdr):
            def __init__(self) -> None:
                super().__init__(stalls=99, revision=5)
                self.reads = 0

            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "get"):
                    self.reads += 1
                    if self.reads > 1:
                        raise RuntimeError("herdr timeout")
                return super().__call__(*argv, **kwargs)

        with self.assertRaises(lch.PromptSubmissionUnobservable):
            lch.submit_agent_prompt(
                GoesBlind(),
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )

    def test_a_non_stall_failure_is_raised_rather_than_retried(self):
        class Broken(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "send-text"):
                    raise RuntimeError("pane_not_found")
                return super().__call__(*argv, **kwargs)

        with self.assertRaises(RuntimeError) as caught:
            lch.submit_agent_prompt(
                Broken(), "w1:p1", "@prompt", "agent", sleep=lambda _s: None
            )
        self.assertIn("pane_not_found", str(caught.exception))

    def test_the_exact_prompt_path_is_recoverable_from_a_persistent_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "review" / "digest" / "prompt.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("review", encoding="utf-8")
            transcript = root / "session.jsonl"
            transcript.write_text(
                '{"type":"message","message":{"role":"user","content":'
                '[{"type":"text","text":"@' + str(prompt.resolve()) + '"}]}}\n',
                encoding="utf-8",
            )
            handle = lch.LaunchHandle(
                "review-run-lane-a1",
                "w1:p1",
                "agent",
                root,
                transcript_path=transcript,
            )

            self.assertTrue(lch.prompt_submission_recorded(handle, prompt))
            self.assertFalse(
                lch.prompt_submission_recorded(
                    handle, prompt.parent.parent / "other" / "prompt.md"
                )
            )

    def test_prompt_path_match_crosses_a_transcript_chunk_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "review" / "prompt.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("review", encoding="utf-8")
            marker = ("@" + str(prompt.resolve())).encode("utf-8")
            transcript = root / "session.jsonl"
            transcript.write_bytes(b"x" * 29 + marker + b"\n")
            handle = lch.LaunchHandle(
                "review-run-lane-a1",
                "w1:p1",
                "agent",
                root,
                transcript_path=transcript,
            )

            self.assertTrue(
                lch.prompt_submission_recorded(handle, prompt, chunk_size=32)
            )


class OmpStartsAtInteractiveComposer(unittest.TestCase):
    """OMP startup argv never races the immutable prompt against readiness."""

    def _spec(self, tmp):
        return lch.LaunchSpec(
            correlation_token="t",
            worktree=Path(tmp),
            prompt_path=Path(tmp) / "p.txt",
            envelope_path=Path(tmp) / "e.json",
            route="omp",
            model="x-ai/grok-4.6",
            effort="high",
            profile="grok",
            session_dir=Path(tmp) / "session",
        )

    def test_startup_argv_contains_no_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = self._spec(tmp)
            argv = lch.build_omp_argv(Path("/bin/omp"), spec)
            self.assertFalse(any(arg.startswith("@") for arg in argv))
            self.assertIn("--profile", argv)
            self.assertEqual(argv[argv.index("--profile") + 1], "grok")

    def test_a_resumed_session_restores_before_prompt_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = self._spec(tmp)
            spec.session_dir.mkdir(parents=True)
            (spec.session_dir / "prior.jsonl").write_text("{}\n", encoding="utf-8")
            argv = lch.build_omp_argv(Path("/bin/omp"), spec)
            self.assertEqual(argv[-1], "-c")
            self.assertFalse(any(arg.startswith("@") for arg in argv))


class PersistentHandleSubmission(unittest.TestCase):
    """A repair turn reuses a proven actor rather than launching a lookalike."""

    def _runtime(self, herdr, handle):
        runtime = object.__new__(lch.HerdrLauncher)
        runtime._handles_lock = threading.RLock()
        runtime._handles = {handle.correlation_token: handle}
        runtime._quiescent_since = {}
        runtime._herdr = herdr
        return runtime

    def test_resubmission_requires_the_registered_pane_actor_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "repair.md"
            prompt.write_text("repair", encoding="utf-8")
            transcript = root / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            token = "run-build-a1"
            name = lch.agent_name_for(token)
            marker = "@" + str(prompt.resolve())

            class BoundHerdr(FakeHerdr):
                def __call__(self, *argv, **kwargs):
                    if argv[:2] == ("pane", "get"):
                        self.calls.append(argv)
                        return {
                            "result": {
                                "pane": {
                                    "pane_id": "w1:p1",
                                    "cwd": str(root),
                                    "revision": self.revision,
                                }
                            }
                        }
                    if argv[:2] == ("agent", "get"):
                        self.calls.append(argv)
                        return {
                            "result": {
                                "agent": {
                                    "name": name,
                                    "pane_id": "w1:p1",
                                    "agent_status": self.agent_status,
                                    "status": None,
                                }
                            }
                        }
                    if (
                        argv[:2] == ("pane", "send-keys")
                        and len(argv) > 3
                        and argv[3] == "enter"
                    ):
                        self.calls.append(argv)
                        with transcript.open("a", encoding="utf-8") as sink:
                            sink.write(
                                '{"role":"user","text":"%s"}\n' % marker
                            )
                        return {}
                    return super().__call__(*argv, **kwargs)

            handle = lch.LaunchHandle(
                token,
                "w1:p1",
                name,
                root,
                environment={},
                transcript_path=transcript,
            )
            herdr = BoundHerdr()
            runtime = self._runtime(herdr, handle)
            self.assertIs(runtime.resubmit(handle, prompt), handle)
            self.assertEqual(herdr.count("pane", "send-text"), 1)

            wrong = lch.LaunchHandle(
                token,
                "w1:p1",
                name,
                root / "other",
                environment={},
                transcript_path=transcript,
            )
            with self.assertRaises(lch.HandleAdoptionRefused):
                runtime.resubmit(wrong, prompt)

    def test_completed_turn_waits_for_idle_instead_of_sampling_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "run-build-a1"
            handle = lch.LaunchHandle(
                token,
                "w1:p1",
                lch.agent_name_for(token),
                root,
                environment={"MAESTRO_TEST_ENV": "retained"},
            )

            class SettlingHerdr(FakeHerdr):
                def __call__(self, *argv, **kwargs):
                    result = super().__call__(*argv, **kwargs)
                    if argv[:2] == ("agent", "wait"):
                        self.agent_status = "done"
                    return result

            herdr = SettlingHerdr(agent_status="working")
            runtime = self._runtime(herdr, handle)
            runtime._verified_handle_binding = lambda _handle: None
            observed = []
            call = runtime._herdr
            runtime._herdr = lambda *args, **kwargs: (
                observed.append(kwargs.get("env")) or call(*args, **kwargs)
            )

            runtime.wait_for_idle(handle, timeout_s=7.0)

            waits = [call for call in herdr.calls if call[:2] == ("agent", "wait")]
            self.assertEqual(len(waits), 1)
            self.assertEqual(waits[0][2], handle.agent_name)
            self.assertNotIn("--until", waits[0])
            self.assertIn("7000", waits[0])
            self.assertTrue(observed)
            self.assertTrue(all(env == handle.environment for env in observed))

    def test_completed_done_turn_is_already_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "run-build-a1"
            handle = lch.LaunchHandle(
                token, "w1:p1", lch.agent_name_for(token), root, environment={}
            )
            herdr = FakeHerdr(agent_status="done")
            runtime = self._runtime(herdr, handle)
            runtime._verified_handle_binding = lambda _handle: None

            runtime.wait_for_idle(handle, timeout_s=7.0)

            waits = [call for call in herdr.calls if call[:2] == ("agent", "wait")]
            self.assertEqual(waits, [])


class PaneRevision(unittest.TestCase):
    def test_the_counter_is_read_from_the_typed_payload(self):
        self.assertEqual(lch.pane_revision(FakeHerdr(revision=7), "w1:p1"), 7)

    def test_anything_unreadable_is_none_rather_than_a_guess(self):
        def broken(*_argv, **_kwargs):
            raise RuntimeError("herdr down")

        self.assertIsNone(lch.pane_revision(broken, "w1:p1"))
        self.assertIsNone(lch.pane_revision(lambda *a, **k: {"result": {}}, "w1:p1"))
        self.assertIsNone(lch.pane_revision(FakeHerdr(revision="1"), "w1:p1"))


if __name__ == "__main__":
    unittest.main()


class WaitFailureIsNotUnobservability(unittest.TestCase):
    """The wait is a delay. Reading the meter is the observation.

    Recorded failure, 2026-08-27, run-8d1a71f463e4430f92a125a8f8b3731d.
    `lane-routing-chemical-tests` spent twelve launcher attempts on an
    identical `AGENT_PROMPT_UNOBSERVED:... after 4 submit attempts`, each
    lasting the full sixty-second budget, against panes whose `revision` field
    every other reader could see perfectly well.

    `consumed()` was reachable through exactly two doors and the stall locks
    both: it ran after `agent prompt --wait` *returned*, which a stalled offer
    does not, and inside `wait_for` after `agent wait` *returned*, which it
    does not either — herdr raises when the agent has not reached `working`
    inside the budget, and a composer holding an unsubmitted `@<path>` never
    does. So the loop pressed Enter four times and never once looked at the
    meter, `readings` stayed empty, and the caller reported "the counter could
    not be read" about a counter it had not read since baseline.

    That inverts the distinction `PromptSubmissionUnobservable` exists to
    make. D9 separates "the meter did not move" from "I could not read the
    meter"; this manufactured the second from a failure of the *wait*, which
    is a statement about neither. And because `agent wait --until working`
    fails identically for a fast turn, a prompt that had already landed was
    reported unobservable too — the launch was refused over work that was
    already under way.

    Every case below fails a `wait` and asserts on what is concluded from it.
    The existing `FakeHerdr` has always been able to fail one (`status_ok`);
    nothing had ever combined that with a stall, which is why the whole family
    was invisible.
    """

    def test_a_failing_wait_never_reports_a_legible_meter_as_unreadable(self):
        """The incident. Wait failure is not Unobservable when the meter was
        readable. Proof is a rising record (recovery Enter), not the paste."""
        herdr = FakeHerdr(stalls=1, status_ok=False)
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=lambda _s: None,
            submission_recorded=_recorded_after_agent_enters(herdr, 1),
        )
        self.assertEqual(herdr.count("agent", "send-keys"), 1)
        # Baseline plus at least one reading taken after the prompt was
        # offered. One `pane get` in the whole call is the defect's signature.
        self.assertGreater(herdr.count("pane", "get"), 1)

    def test_a_prompt_that_landed_despite_a_stalled_offer_needs_no_enter(self):
        """A send-text repaint plus stall is NOT submitted.

        herdr's stall means it did not *observe* a lifecycle change. The paste
        still repaints, so the revision ticks — and that used to return
        success with zero recovery Enter. Recovery Enter or a transcript rise
        is required; without either, fail closed.
        """

        class StalledButAccepted(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "send-text"):
                    self.calls.append(argv)
                    self.revision += 1
                    raise RuntimeError("agent_prompt_stalled")
                return super().__call__(*argv, **kwargs)

        herdr = StalledButAccepted(status_ok=False)
        with self.assertRaises(lch.PromptNotSubmitted):
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertGreaterEqual(herdr.count("agent", "send-keys"), 1)
        self.assertEqual(herdr.count("pane", "send-text"), 1)

    def test_a_wedged_composer_is_unsubmitted_rather_than_unobservable(self):
        """The half a careless repair gets wrong. The meter was legible every
        round and did not move: that is a fact about the composer, and it must
        keep its terminal EXECUTION verdict rather than being laundered into a
        transient one."""
        herdr = FakeHerdr(stalls=99, status_ok=False, revision=41)
        with self.assertRaises(lch.PromptNotSubmitted) as caught:
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertIn("AGENT_PROMPT_UNSUBMITTED", str(caught.exception))
        self.assertEqual(lch.classify_error(caught.exception), lch.ErrorClass.EXECUTION)
        self.assertEqual(herdr.count("agent", "send-keys"), lch.SUBMIT_ATTEMPTS)

    def test_a_genuinely_unreadable_meter_is_still_unobservable(self):
        """D9's own case, with the wait failing too. The class survives; only
        its false positives are gone."""
        herdr = FakeHerdr(stalls=99, status_ok=False, revision=None)
        with self.assertRaises(lch.PromptSubmissionUnobservable) as caught:
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertIn("AGENT_PROMPT_UNOBSERVED", str(caught.exception))
        self.assertEqual(lch.classify_error(caught.exception), lch.ErrorClass.TRANSIENT)

    def test_the_prompt_is_offered_exactly_once_however_the_wait_ends(self):
        """A second `agent prompt` would append its text to the unsubmitted
        line and send both as one garbled turn. Recovery presses Enter on what
        is already on screen and never re-offers."""
        for label, herdr in (
            ("lands on enter", FakeHerdr(stalls=1, status_ok=False)),
            ("wedged", FakeHerdr(stalls=99, status_ok=False, revision=41)),
            ("unreadable", FakeHerdr(stalls=99, status_ok=False, revision=None)),
        ):
            with self.subTest(case=label):
                try:
                    lch.submit_agent_prompt(
                        herdr,
                        "w1:p1",
                        "@prompt",
                        "agent",
                        sleep=lambda _s: None,
                    )
                except (lch.PromptNotSubmitted, lch.PromptSubmissionUnobservable):
                    pass
                self.assertEqual(herdr.count("pane", "send-text"), 1)

    def test_observation_stops_at_the_first_proof(self):
        """Observed exactly once, then nothing further is spent: no extra
        Enter, no extra wait, no extra round. Proof is the record, not the
        revision tick from paste."""
        herdr = FakeHerdr(stalls=1, status_ok=False)
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=lambda _s: None,
            submission_recorded=_recorded_after_agent_enters(herdr, 1),
        )
        self.assertEqual(herdr.count("agent", "send-keys"), 1)
        self.assertEqual(herdr.count("agent", "wait"), 1)

    def test_a_failure_that_is_not_a_stall_still_propagates(self):
        """The offer's other failures are not submission facts and are not
        swallowed into the recovery loop."""

        class Broken(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "send-text"):
                    self.calls.append(argv)
                    raise RuntimeError("LAUNCH_REFUSED:agent_not_found")
                return super().__call__(*argv, **kwargs)

        herdr = Broken(status_ok=False)
        with self.assertRaises(RuntimeError) as caught:
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertIn("agent_not_found", str(caught.exception))
        self.assertEqual(herdr.count("agent", "send-keys"), 0)


class PasteRepaintIsNotSubmissionTests(unittest.TestCase):
    """§7.6 — the pane revision cannot prove submission, because the paste moves it.

    Recorded failure, 2026-08-27, run-8d1a71f463e4430f92a125a8f8b3731d. A grok
    builder pane sat at revision **1** holding `@<prompt>` in its composer,
    unsubmitted, while the console showed the lane RUNNING/BUILDING and the
    scheduler waited on a turn no actor had started. The node had already
    blocked once as `ENVIRONMENTAL_BUDGET_EXHAUSTED` with receipt
    `NEVER_STARTED` on the same mechanism.

    The revision was not stuck. It was *correct*: `pane get` counts repaints,
    and pasting the prompt into a composer is a repaint. Baseline 0 on an empty
    booted composer, paste, read 1 -- `current > baseline` -- and
    `submit_agent_prompt` returned success without pressing Enter even once.
    One Enter pressed by hand afterwards started the turn and drove that same
    counter past 1000 in four seconds.

    So the counter separates nothing, and no threshold over it can: a stalled
    pane and a submitted one both show `+1` immediately after the paste. This
    is the same shape as the `idle` failure this file opens with and the
    `working` failure that replaced it -- a signal that moves for both
    outcomes. The proof has to be an artifact only an accepted turn produces,
    and the agent runtime's own transcript record is one.
    """

    def setUp(self):
        self._grace = lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S
        lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S = 0.05

    def tearDown(self):
        lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S = self._grace

    class RepaintingPane:
        """A pane whose revision ticks once for the paste and never again.

        This is the production observation, not a contrivance: a composer that
        swallowed the text renders it, and renders nothing further.
        """

        def __init__(self, transcript: Path, marker: str, submit_on_enter: bool):
            self.transcript = transcript
            self.marker = marker
            self.submit_on_enter = submit_on_enter
            self.revision = 0
            self.enters = 0

        def __call__(self, *argv, **kwargs):
            head = tuple(argv[:2])
            if head == ("pane", "send-text"):
                self.revision += 1  # the paste repaints the pane
                return {"result": {"type": "ok"}}
            if head == ("agent", "send-keys"):
                self.enters += 1
                if self.submit_on_enter:
                    with self.transcript.open("a", encoding="utf-8") as sink:
                        sink.write('{"role":"user","text":"%s"}\n' % self.marker)
                return {"result": {"type": "ok"}}
            if head == ("pane", "get"):
                return {"result": {"pane": {"revision": self.revision}}}
            if head == ("agent", "get"):
                return {"result": {"agent": {
                    "agent_status": "working", "status": None,
                }}}
            if head == ("agent", "wait"):
                return {"result": {"type": "ok"}}
            return {"result": {"type": "ok"}}

    def _handle(self, transcript: Path) -> lch.LaunchHandle:
        return lch.LaunchHandle("tok", "w1:p1", "maestro-test", transcript.parent,
                                transcript_path=transcript)

    def test_a_repaint_tick_is_refused_when_the_transcript_never_records_the_turn(self):
        with tempfile.TemporaryDirectory() as root:
            prompt = Path(root) / "prompt.md"
            prompt.write_text("do the work", encoding="utf-8")
            transcript = Path(root) / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            handle = self._handle(transcript)
            herdr = self.RepaintingPane(
                transcript, "@" + str(prompt.resolve()), submit_on_enter=False
            )
            with self.assertRaises(lch.PromptNotSubmitted):
                lch.submit_agent_prompt(
                    herdr,
                    "w1:p1",
                    "@" + str(prompt.resolve()),
                    "maestro-test",
                    timeout_s=6.0,
                    working_proves=True,
                    sleep=lambda _: None,
                    submission_recorded=lch._rising_submission_record(handle, prompt),
                )
            # The revision did advance, and it proved nothing.
            self.assertEqual(herdr.revision, 1)

    def test_the_recovery_enter_runs_instead_of_being_skipped_by_the_repaint(self):
        with tempfile.TemporaryDirectory() as root:
            prompt = Path(root) / "prompt.md"
            prompt.write_text("do the work", encoding="utf-8")
            transcript = Path(root) / "session.jsonl"
            transcript.write_text("", encoding="utf-8")
            handle = self._handle(transcript)
            herdr = self.RepaintingPane(
                transcript, "@" + str(prompt.resolve()), submit_on_enter=True
            )
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@" + str(prompt.resolve()),
                "maestro-test",
                timeout_s=6.0,
                working_proves=True,
                sleep=lambda _: None,
                submission_recorded=lch._rising_submission_record(handle, prompt),
            )
            self.assertGreaterEqual(herdr.enters, 1)

    def test_a_previous_turns_record_for_the_same_prompt_path_is_not_this_turns_proof(self):
        """One actor is re-prompted across a correction cycle, reusing the path."""
        with tempfile.TemporaryDirectory() as root:
            prompt = Path(root) / "prompt.md"
            prompt.write_text("do the work", encoding="utf-8")
            transcript = Path(root) / "session.jsonl"
            marker = "@" + str(prompt.resolve())
            transcript.write_text(
                '{"role":"user","text":"%s"}\n' % marker, encoding="utf-8"
            )
            handle = self._handle(transcript)
            herdr = self.RepaintingPane(transcript, marker, submit_on_enter=False)
            with self.assertRaises(lch.PromptNotSubmitted):
                lch.submit_agent_prompt(
                    herdr,
                    "w1:p1",
                    marker,
                    "maestro-test",
                    timeout_s=6.0,
                    working_proves=True,
                    sleep=lambda _: None,
                    submission_recorded=lch._rising_submission_record(handle, prompt),
                )


class ColdBootAdmissionFacts(unittest.TestCase):
    """Regression locks for the 2026-08-27 cold-boot admission facts."""

    def test_admission_prompt_is_the_literal_marker_not_the_team_prefix(self):
        from adw_modules import route_admission as ra

        source = inspect.getsource(ra.capture_route)
        self.assertIn("FIRST_PROMPT.format", source)
        self.assertNotIn("prepare_route_prompt_text(", source)
        self.assertNotIn("/team", ra.FIRST_PROMPT)
        prompt_turn = inspect.getsource(ra._prompt_turn)
        self.assertNotIn("prepare_route_prompt_text(", prompt_turn)

    def test_admission_herdr_treats_empty_stdout_as_success(self):
        from adw_modules import route_admission as ra

        source = inspect.getsource(ra._herdr)
        self.assertIn('or "{}"', source)

    def test_claude_admission_keeps_typed_working_as_fallback(self):
        from adw_modules import route_admission as ra

        source = inspect.getsource(ra._prompt_turn)
        self.assertIn("working_proves", source)
        self.assertIn('agent.get("agent_status")', source)
        self.assertIn("submission_recorded=_submitted", source)
        self.assertNotIn("submission_recorded = _submitted if working_proves else None", source)


def _recorded_after_pane_enters(herdr: FakeHerdr, n: int):
    """True after `n` pane-scope Enters were issued. Not a meter."""

    def recorded() -> bool:
        return sum(
            1 for call in herdr.calls
            if call[:2] == ("pane", "send-keys")
            and len(call) > 3
            and call[3] == "enter"
        ) >= n

    return recorded


class SwallowedFailuresAreEvidence(unittest.TestCase):
    """A discarded failure on the submission path is a fabricated conclusion.

    Measured instance, 2026-08-27: every `agent send-keys` in the recovery
    loop died on herdr's agent-registry lookup for a just-registered admission
    agent — `agent_not_found`, typed — and every failure was swallowed by
    `except Exception: pass`. The loop "pressed Enter four times" having
    pressed nothing, waited out its whole budget, and refused
    `AGENT_PROMPT_UNSUBMITTED`: a statement about the composer that was
    actually a statement about a name lookup. `pane send-keys <pane_id>` was
    the scope proven to deliver on both routes the whole time, and that
    difference was invisible precisely because the failure was discarded.
    """

    def test_a_registry_miss_falls_back_to_the_pane_scope_and_submits(self):
        """The incident, rescued: `agent_not_found` is herdr's typed statement
        that the agent-scope verb cannot resolve the target, so the recovery
        presses the pane id — ground truth this function was handed."""

        class RegistryMiss(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("agent", "send-keys"):
                    self.calls.append(argv)
                    raise lch.HerdrCallError(
                        "LAUNCH_REFUSED:agent not found", lch.AGENT_NOT_FOUND
                    )
                return super().__call__(*argv, **kwargs)

        herdr = RegistryMiss()
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            sleep=lambda _s: None,
            submission_recorded=_recorded_after_pane_enters(herdr, 2),
        )
        # One agent-scope attempt, then the pane-scope fallback delivered:
        # the offer's Enter plus the recovery round's fallback Enter.
        self.assertEqual(herdr.count("agent", "send-keys"), 1)
        self.assertTrue(_recorded_after_pane_enters(herdr, 2)())

    def test_zero_delivered_enters_never_claims_the_composer_refused(self):
        """When herdr accepted no Enter at all, `AGENT_PROMPT_UNSUBMITTED`
        asserts a fact about the composer the code never established. The
        refusal is UNDELIVERED, transient, and names the calls that died."""

        class NoKeyDelivered(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if (
                    argv[:2] in (("agent", "send-keys"), ("pane", "send-keys"))
                    and len(argv) > 3
                    and argv[3] == "enter"
                ):
                    self.calls.append(argv)
                    raise lch.HerdrCallError(
                        "LAUNCH_REFUSED:agent not found", lch.AGENT_NOT_FOUND
                    )
                return super().__call__(*argv, **kwargs)

        herdr = NoKeyDelivered()
        with self.assertRaises(lch.PromptSubmissionUnobservable) as caught:
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        message = str(caught.exception)
        self.assertIn("AGENT_PROMPT_UNDELIVERED", message)
        self.assertIn("agent_not_found", message)
        phases = {failure.phase for failure in caught.exception.failures}
        self.assertIn("offer-enter", phases)
        self.assertIn("recovery-enter", phases)
        self.assertIn("recovery-enter-pane", phases)
        self.assertIn(
            lch.AGENT_NOT_FOUND,
            {failure.code for failure in caught.exception.failures},
        )
        self.assertEqual(
            lch.classify_error(caught.exception), lch.ErrorClass.TRANSIENT
        )

    def test_a_dead_proof_channel_is_unobservable_not_unsubmitted(self):
        """A `submission_recorded` that raises on every consultation observed
        nothing about the transcript; the old code silently read each raise as
        False and refused UNSUBMITTED with no trace of the dead probe."""

        def broken_probe() -> bool:
            raise OSError("transcript unreadable")

        herdr = FakeHerdr(status_ok=False)
        grace = lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S
        lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S = 0.0
        try:
            with self.assertRaises(lch.PromptSubmissionUnobservable) as caught:
                lch.submit_agent_prompt(
                    herdr,
                    "w1:p1",
                    "@prompt",
                    "agent",
                    sleep=lambda _s: None,
                    submission_recorded=broken_probe,
                )
        finally:
            lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S = grace
        self.assertIn("AGENT_PROMPT_UNOBSERVED", str(caught.exception))
        phases = {failure.phase for failure in caught.exception.failures}
        self.assertIn("proof-probe", phases)
        self.assertIn(
            "OSError",
            {failure.error for failure in caught.exception.failures},
        )

    def test_meter_read_failures_ride_the_unobservable_refusal(self):
        """D9's unobservable case now says WHY the meter was unreadable."""

        class GoesBlind(FakeHerdr):
            def __init__(self) -> None:
                super().__init__(stalls=99, status_ok=False, revision=5)
                self.reads = 0

            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("pane", "get"):
                    self.reads += 1
                    if self.reads > 1:
                        raise RuntimeError("herdr timeout")
                return super().__call__(*argv, **kwargs)

        with self.assertRaises(lch.PromptSubmissionUnobservable) as caught:
            lch.submit_agent_prompt(
                GoesBlind(),
                "w1:p1",
                "@prompt",
                "agent",
                sleep=lambda _s: None,
            )
        self.assertIn("swallowed=[", str(caught.exception))
        phases = {failure.phase for failure in caught.exception.failures}
        self.assertIn("meter-read", phases)

    def test_an_aborted_baseline_cannot_promote_a_previous_turns_record(self):
        """`prompt_submission_marks` returns its count-so-far when the read
        aborts. Snapshotting that partial count as the baseline let a
        transcript that already contained the marker prove a submission that
        never happened: 0-from-a-failed-read < 1-from-the-old-turn."""
        import os as _os
        import types

        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            transcript = Path(tmp) / "transcript.jsonl"
            marker = "@" + str(prompt.resolve())
            # The transcript EXISTS and already carries a PREVIOUS turn's
            # record — the reuse case the baseline snapshot exists for.
            transcript.write_text(marker + "\n", encoding="utf-8")
            handle = types.SimpleNamespace(transcript_path=transcript)
            # The baseline scan aborts mid-observation: the file is present
            # but unreadable, so the count-so-far (0) is partial, not zero.
            _os.chmod(transcript, 0)
            try:
                recorded = lch._rising_submission_record(handle, prompt)
            finally:
                _os.chmod(transcript, 0o600)
            # The old turn's record must not count as this offer's proof.
            self.assertFalse(recorded())
            # Only a rise observed between clean reads is this offer's proof.
            with transcript.open("a", encoding="utf-8") as sink:
                sink.write(marker + "\n")
            self.assertTrue(recorded())

    def test_a_transcript_not_yet_created_baselines_at_exactly_zero(self):
        """A missing file is not a partial read: its zero is exact, and the
        first record written after the offer proves it on the first clean
        consultation — no recovery round, no extra Enter."""
        import types

        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("x", encoding="utf-8")
            transcript = Path(tmp) / "transcript.jsonl"
            handle = types.SimpleNamespace(transcript_path=transcript)
            recorded = lch._rising_submission_record(handle, prompt)
            self.assertFalse(recorded())
            transcript.write_text(
                "@" + str(prompt.resolve()) + "\n", encoding="utf-8"
            )
            self.assertTrue(recorded())

    def test_interactive_ready_timeout_names_the_dead_probe(self):
        """`wait_for_interactive_agent` polled a probe whose every failure was
        discarded, then reported "never became ready" about an agent record it
        had never read."""

        def call(*argv, **kwargs):
            if argv[:2] == ("agent", "get"):
                raise lch.HerdrCallError(
                    "LAUNCH_REFUSED:down", lch.AGENT_NOT_FOUND
                )
            raise RuntimeError("timeout")

        with self.assertRaises(RuntimeError) as caught:
            lch.wait_for_interactive_agent(call, "agent-x", timeout_s=0.001)
        message = str(caught.exception)
        self.assertIn("AGENT_INTERACTIVE_READY_TIMEOUT:agent-x", message)
        self.assertIn("probe=HerdrCallError/agent_not_found", message)


class VirtualClock:
    """A monotonic clock the test moves, so a turn can take three minutes.

    §16.3 item 62 — a fake that cannot express the failure is not coverage.
    Before this seam existed `submit_agent_prompt` read `time.monotonic()`
    directly for its grace deadline, so "the transcript flushes one turn after
    the prompt landed" could only be written by literally waiting a turn. The
    defect it hides is therefore the one no test could state.

    Time advances only where the function under test would really have spent
    it: an injected `sleep`, and the herdr wait that blocks for its budget.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class BlockingWaitHerdr(FakeHerdr):
    """`agent wait --timeout N` consumes N milliseconds, as herdr's does."""

    def __init__(self, clock: VirtualClock, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock

    def __call__(self, *argv, **kwargs):
        if argv[:2] == ("agent", "wait") and "--timeout" in argv:
            self.clock.sleep(int(argv[argv.index("--timeout") + 1]) / 1000.0)
        return super().__call__(*argv, **kwargs)


#: Longer than any window this function could have invented, and shorter than
#: plenty of real turns. §7.6 measured omp writing its transcript at TURN
#: granularity — 57.7s in the measured case — and records that a turn doing
#: real work runs far longer.
TURN_LENGTH_S = 180.0


class ABoundedLookIsNotAVerdict(unittest.TestCase):
    """Recorded failure, 2026-08-27, run-8a200af7f9044ce7a11a51b6908f37e3.

    `lane-wp6-tests` attempt a4: the actor's transcript hit disk carrying the
    submission marker at 11:44:22 UTC, and `AGENT_PROMPT_UNSUBMITTED` was
    recorded at 11:44:22.707 — after roughly four rounds of >=5.1s plus a 10s
    grace. The paste had worked. The refusal cancelled the handle and spent the
    attempt.

    No larger number fixes this, which is why the number is not what changed.
    Turn length is unbounded, so every window over "has the transcript recorded
    the submission yet" is wrong for some turn, and the end of one is a fact
    about Maestro's clock wearing the costume of a fact about the composer.
    """

    def _submit(self, clock, herdr, **kwargs):
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            timeout_s=60.0,
            working_proves=True,
            sleep=clock.sleep,
            monotonic=clock,
            submission_recorded=lambda: clock.now >= TURN_LENGTH_S,
            **kwargs,
        )

    def test_a_turn_longer_than_the_window_is_not_refused_on_the_lane_path(self):
        clock = VirtualClock()
        herdr = BlockingWaitHerdr(clock)

        self._submit(clock, herdr, refuse_unproven=False)

        # It really did the work it can do: text, popup dismissal, Enter.
        self.assertGreaterEqual(
            sum(
                1 for call in herdr.calls
                if call[:2] == ("pane", "send-keys") and call[3] == "enter"
            ),
            1,
        )
        # And it gave up looking long before the turn would have been recorded,
        # which is exactly why it must not have convicted.
        self.assertLess(clock.now, TURN_LENGTH_S)

    def test_the_same_composer_is_still_refused_where_a_receipt_is_at_stake(self):
        """Route admission's turn is bounded by construction, so it convicts.

        Its prompt is one sentence asking for one marker back, and its output
        is a SIGNED receipt. "Offered, unproven" is not a thing a receipt can
        say, so admission keeps the terminal verdict the lane path gave up.
        """
        clock = VirtualClock()
        herdr = BlockingWaitHerdr(clock)

        with self.assertRaises(lch.PromptNotSubmitted):
            self._submit(clock, herdr, refuse_unproven=True)

    def test_undelivered_and_unobserved_still_refuse_on_the_lane_path(self):
        """Suppressing the verdict about the composer suppresses only that.

        `AGENT_PROMPT_UNDELIVERED` is a statement about herdr — not one Enter
        was accepted, so "the composer will not submit" was never tested. It
        fails closed on every path, and it is D9's distinction, not this one.
        """
        clock = VirtualClock()

        class DeafHerdr(BlockingWaitHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] in (("pane", "send-keys"), ("agent", "send-keys")):
                    self.calls.append(argv)
                    raise lch.HerdrCallError("LAUNCH_REFUSED:no", "pane_not_found")
                return super().__call__(*argv, **kwargs)

        with self.assertRaises(lch.PromptSubmissionUnobservable) as caught:
            self._submit(clock, DeafHerdr(clock), refuse_unproven=False)
        self.assertIn("AGENT_PROMPT_UNDELIVERED", str(caught.exception))

    def test_the_grace_window_is_not_spent_where_it_cannot_change_an_answer(self):
        """The grace is the tail of the window this function convicted at.

        Where it no longer convicts, waiting it out only delays the handoff to
        the node's own machinery. Measured on the virtual clock so the saving
        is a fact rather than a claim.
        """
        lane, admission = VirtualClock(), VirtualClock()
        self._submit(lane, BlockingWaitHerdr(lane), refuse_unproven=False)
        with self.assertRaises(lch.PromptNotSubmitted):
            self._submit(
                admission, BlockingWaitHerdr(admission), refuse_unproven=True
            )
        # The grace polls every 0.1s, so it overshoots by at most one tick.
        saved = admission.now - lane.now
        self.assertGreaterEqual(saved, lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S)
        self.assertLess(saved, lch.TRANSCRIPT_SUBMISSION_OBSERVE_TIMEOUT_S + 0.5)


class TheRecoveryWaitEndsOnAnyLivenessOrSettledState(unittest.TestCase):
    """`--until working` alone raised on two unrelated facts.

    A composer that never took the prompt and a turn that had already finished
    both fail that wait, and so does an actor stopped at a permission prompt.
    Herdr 0.8.2's `agent wait --until` accepts idle, working, blocked, done and
    unknown; the round is over as soon as the actor is alive or settled.
    """

    def test_the_wait_names_working_done_and_blocked_and_never_idle(self):
        herdr = FakeHerdr(stalls=99)
        with self.assertRaises(lch.PromptNotSubmitted):
            lch.submit_agent_prompt(
                herdr, "w1:p1", "@prompt", "agent", sleep=lambda _s: None
            )
        waits = [call for call in herdr.calls if call[:2] == ("agent", "wait")]
        self.assertTrue(waits)
        for call in waits:
            for status in ("working", "done", "blocked"):
                self.assertIn(status, call)
            # `idle` is the state of a composer holding an unsubmitted prompt,
            # so waiting on it would return instantly and spend every recovery
            # Enter back-to-back — the 2026-08-18 shape.
            self.assertNotIn("idle", call)
            self.assertNotIn("unknown", call)

    def test_a_wait_that_answers_instantly_still_spaces_the_keystrokes(self):
        """Widening the state set must not collapse the round.

        An actor herdr reports `done` satisfies the wait at once, and without a
        floor under the round every remaining Enter would be pressed in the
        same millisecond — which is the failure the old `--until working` was
        accidentally preventing by always timing out.
        """
        clock = VirtualClock()
        # This herdr answers the wait immediately: it never advances the clock.
        herdr = FakeHerdr(stalls=99, agent_status="done")

        with self.assertRaises(lch.PromptNotSubmitted):
            lch.submit_agent_prompt(
                herdr,
                "w1:p1",
                "@prompt",
                "agent",
                timeout_s=60.0,
                sleep=clock.sleep,
                monotonic=clock,
            )

        # Two paste settles at the offer, then one settle's worth of spacing
        # under every recovery round — none of which the instant wait supplied.
        rounds = lch.SUBMIT_ATTEMPTS
        self.assertGreaterEqual(
            clock.now, lch.PASTE_SETTLE_S * (2 + rounds)
        )
