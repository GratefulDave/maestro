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

import sys
import threading
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch  # noqa: E402


class FakeHerdr:
    """A pane whose revision only advances when the prompt is truly accepted.

    `stalls` is how many `agent prompt` / `send-keys` rounds are swallowed
    before the composer starts accepting. `status_ok` mimics herdr answering
    the lifecycle wait successfully — which the stalled pane does, reporting
    `idle`, and which is exactly why the wait alone proves nothing.
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
            return {"result": {"agent": {"status": self.agent_status}}}
        if verb in (("agent", "prompt"), ("agent", "send-keys")):
            if self.stalls > 0:
                self.stalls -= 1
                if verb == ("agent", "prompt"):
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


class SubmissionProof(unittest.TestCase):
    def test_an_accepted_prompt_needs_no_recovery(self):
        herdr = FakeHerdr()
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            until=("working", "idle"),
            sleep=lambda _s: None,
        )
        self.assertEqual(herdr.count("agent", "send-keys"), 0)

    def test_a_working_agent_proves_acceptance_when_revision_is_static(self):
        class StaticWorking(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("agent", "prompt"):
                    self.calls.append(argv)
                    return {}
                return super().__call__(*argv, **kwargs)

        herdr = StaticWorking(revision=41, agent_status="working")
        lch.submit_agent_prompt(
            herdr,
            "w1:p1",
            "@prompt",
            "agent",
            until=("working", "idle"),
            working_proves=True,
            sleep=lambda _s: None,
        )
        self.assertEqual(herdr.revision, 41)
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
            until=("working", "idle"),
            sleep=lambda _s: None,
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
            until=("working", "idle"),
            sleep=lambda _s: None,
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
                until=("working", "idle"),
                sleep=lambda _s: None,
            )
        self.assertEqual(herdr.count("agent", "send-keys"), lch.SUBMIT_ATTEMPTS)

    def test_a_prompt_the_agent_finished_before_working_was_sampled_still_passes(self):
        """Why `idle` cannot simply be dropped from the wait.

        The task is done and the pane is idle again — but it consumed a turn,
        so the revision moved and the launch is not failed for being fast.
        """
        herdr = FakeHerdr()
        lch.submit_agent_prompt(
            herdr, "w1:p1", "@prompt", "agent", until=("idle",), sleep=lambda _s: None
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
                until=("working", "idle"),
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
                until=("working", "idle"),
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
                until=("working", "idle"),
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
                until=("working", "idle"),
                sleep=lambda _s: None,
            )

    def test_a_non_stall_failure_is_raised_rather_than_retried(self):
        class Broken(FakeHerdr):
            def __call__(self, *argv, **kwargs):
                if argv[:2] == ("agent", "prompt"):
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
            token = "run-build-a1"
            name = lch.agent_name_for(token)

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
                                    "status": self.agent_status,
                                }
                            }
                        }
                    return super().__call__(*argv, **kwargs)

            handle = lch.LaunchHandle(token, "w1:p1", name, root, environment={})
            herdr = BoundHerdr()
            runtime = self._runtime(herdr, handle)
            self.assertIs(runtime.resubmit(handle, prompt), handle)
            self.assertEqual(herdr.count("agent", "prompt"), 1)

            wrong = lch.LaunchHandle(
                token, "w1:p1", name, root / "other", environment={}
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
