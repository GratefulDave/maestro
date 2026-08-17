"""Step 7 launcher contract: typed transport, routes, and quiescence."""

from __future__ import annotations

from dataclasses import replace
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import launcher
from adw_modules.launcher import (
    ErrorClass,
    FakeLauncher,
    HarnessQuiescenceError,
    HerdrLauncher,
    LaunchSpec,
    PollState,
    TranscriptTailer,
    build_claude_argv,
    build_omp_argv,
    quiesce_process_group,
    run_harness_process,
)
from adw_modules.route_receipts import load_admitted_routes, load_public_key


FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
record = os.environ.get("FAKE_HERDR_ARGV")
if record:
    with open(record, "a") as handle:
        handle.write(json.dumps({
            "argv": argv,
            "environment": os.environ.get("FAKE_LAUNCH_ENV"),
        }) + "\n")
if argv[:2] == ["pane", "split"]:
    print(json.dumps({"result": {"pane": {"pane_id": "w1:p2", "cwd": os.environ["FAKE_HERDR_CWD"]}}}))
elif argv[:2] == ["pane", "get"]:
    print(json.dumps({"result": {"pane": {"pane_id": "w1:p2", "cwd": os.environ["FAKE_HERDR_CWD"], "foreground_cwd": "/wrong"}}}))
elif argv[:2] == ["pane", "process-info"]:
    root = os.path.dirname(record) if record else "/tmp"
    launched = os.path.join(root, "launched")
    process = "omp" if os.path.exists(launched) and argv[-1] in open(launched).read().split() else "zsh"
    print(json.dumps({"result": {"process_info": {
        "pane_id": "w1:p2",
        "foreground_processes": [{"name": process, "argv0": process, "argv": [process]}]}}}))
elif argv[:2] == ["agent", "start"]:
    if os.environ.get("FAKE_HERDR_REFUSE"):
        sys.stderr.write(json.dumps({"error": {"code": "launch_refused"}}))
        sys.exit(1)
    print(json.dumps({"result": {"agent": {"name": argv[2], "status": "idle", "transcript_path": os.environ["FAKE_TRANSCRIPT"]}}}))
elif argv[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"ok": True, "status": "idle"}}))
elif argv[:2] == ["agent", "prompt"]:
    print(json.dumps({"result": {"ok": True}}))
elif argv[:2] == ["agent", "read"]:
    name = argv[2]
    record = os.environ.get("FAKE_HERDR_ARGV")
    root = os.path.dirname(record) if record else "/tmp"
    path = os.path.join(root, name + ".prompt")
    if os.path.exists(path):
        print(json.dumps({"result": {"text": open(path, encoding="utf-8").read()}}))
    else:
        print(json.dumps({"result": {}}))
elif argv[:2] == ["agent", "get"]:
    if os.environ.get("FAKE_HERDR_GET_FAILURE"):
        sys.stderr.write(json.dumps({"error": {"code": "transport_failure"}}))
        sys.exit(1)
    marker = os.environ.get("FAKE_HERDR_CLOSE_MARKER")
    if marker and os.path.exists(marker):
        print(json.dumps({"result": {}}))
    else:
        status = os.environ.get("FAKE_AGENT_STATUS", "working")
        print(json.dumps({"result": {"agent": {"name": argv[2], "status": status, "interactive_ready": True, "transcript_path": os.environ["FAKE_TRANSCRIPT"]}}}))
elif argv[:2] == ["pane", "close"]:
    if os.environ.get("FAKE_HERDR_CLOSE_FAILURE"):
        sys.stderr.write(json.dumps({"error": {"code": "close_failure"}}))
        sys.exit(1)
    marker = os.environ.get("FAKE_HERDR_CLOSE_MARKER")
    if marker and not os.environ.get("FAKE_HERDR_CLOSE_DOES_NOT_STOP_AGENT"):
        open(marker, "w").close()
    print(json.dumps({"result": {"closed": True}}))
elif argv[:2] == ["pane", "send-keys"]:
    root = os.path.dirname(record) if record else "/tmp"
    open(os.path.join(root, "launched"), "a").write(argv[2] + "\n")
    print(json.dumps({"result": {"ok": True}}))
else:
    print(json.dumps({"result": {}}))
'''


class LauncherContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("do the work")
        self.envelope = self.root / "envelope.json"
        self.transcript = self.root / "session.jsonl"
        self.herdr = self.root / "herdr"
        self.herdr.write_text(FAKE_HERDR)
        self.herdr.chmod(0o755)
        self._before = dict(os.environ)
        os.environ.update({
            "FAKE_HERDR_ARGV": str(self.root / "argv.jsonl"),
            "FAKE_HERDR_CLOSE_MARKER": str(self.root / "pane-closed"),
            "FAKE_HERDR_CWD": str(self.worktree),
            "FAKE_TRANSCRIPT": str(self.transcript),
        })
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = load_public_key(fixtures / "route_receipts.pub")
        self.admitted_routes = load_admitted_routes(
            {"omp": fixtures / "omp.json", "claude": fixtures / "claude.json"},
            verify_keys=(key,))

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._before)
        self._tmp.cleanup()

    def spec(self, route: str = "omp") -> LaunchSpec:
        return LaunchSpec(
            correlation_token="run1-node_a-1",
            worktree=self.worktree,
            prompt_path=self.prompt,
            envelope_path=self.envelope,
            route=route,
            model="openai-codex/gpt-5.6-sol" if route == "omp" else "opus",
            effort="high",
            profile="openai-performance" if route == "omp" else None,
            session_dir=self.root / "session",
        )

    def test_launch_verifies_pane_cwd_not_foreground_cwd(self):
        launcher = HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=self.admitted_routes)
        handle = launcher.launch(self.spec())
        self.assertEqual(handle.launched_cwd, self.worktree.resolve())
        self.assertEqual(handle.pane_id, "w1:p2")

    def test_read_commands_accept_raw_text_output(self):
        # `herdr agent read` / `pane read` print the snapshot as raw text; they
        # have no JSON output mode. Rejecting that as PROTOCOL_INVALID_JSON
        # blinds the composer-visibility wait and the receipt scan.
        script = self.root / "text-herdr"
        script.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'MAESTRO_CLAUDE_RECEIPT_OK'\n")
        script.chmod(0o755)
        harness = HerdrLauncher(herdr_path=script, omp_path=Path("/opt/omp"),
                                claude_path=Path("/opt/claude"),
                                admitted_routes=self.admitted_routes)
        payload = harness._herdr("agent", "read", "n", "--source", "visible")
        self.assertIn("MAESTRO_CLAUDE_RECEIPT_OK",
                      launcher._payload_text(payload))
        payload = harness._herdr("pane", "read", "w1:p2", "--source", "visible")
        self.assertIn("MAESTRO_CLAUDE_RECEIPT_OK",
                      launcher._payload_text(payload))
        # Every other command still has to speak JSON.
        with self.assertRaisesRegex(RuntimeError, "PROTOCOL_INVALID_JSON"):
            harness._herdr("pane", "list")

    def test_launch_refuses_unadmitted_route_before_creating_pane(self):
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        claude_only = load_admitted_routes(
            {"claude": fixtures / "claude.json"},
            verify_keys=(load_public_key(fixtures / "route_receipts.pub"),))
        launcher = HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=claude_only)
        with self.assertRaisesRegex(RuntimeError, "ROUTE_NOT_ADMITTED:omp"):
            launcher.launch(self.spec("omp"))
        self.assertFalse((self.root / "argv.jsonl").exists())

    def test_launch_refuses_wrong_pane_cwd_before_starting_agent(self):
        os.environ["FAKE_HERDR_CWD"] = str(self.root / "wrong")
        launcher = HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=self.admitted_routes)
        with self.assertRaisesRegex(RuntimeError, "BINDING_MISMATCH"):
            launcher.launch(self.spec())
        calls = [json.loads(line)["argv"] for line in (self.root / "argv.jsonl").read_text().splitlines()]
        self.assertNotIn(["agent", "start"], [call[:2] for call in calls])
        self.assertIn(["pane", "close", "w1:p2"], calls)

    def test_launch_waits_for_shell_then_starts_agent_once(self):
        launcher = HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=self.admitted_routes)
        handle = launcher.launch(self.spec())
        self.assertEqual(handle.pane_id, "w1:p2")
        calls = [json.loads(line)["argv"]
                 for line in (self.root / "argv.jsonl").read_text().splitlines()]
        wait_indexes = [
            index for index, call in enumerate(calls)
            if call[:2] == ["pane", "process-info"]
        ]
        start_indexes = [
            index for index, call in enumerate(calls)
            if call[:2] == ["agent", "start"]
        ]
        # The shell must look ready on several consecutive snapshots before we
        # start: one ready snapshot can land in the gap before login hooks
        # spawn, which makes Herdr report agent_pane_busy.
        self.assertGreaterEqual(len(wait_indexes), 5)
        self.assertEqual(len(start_indexes), 1)
        self.assertLess(wait_indexes[4], start_indexes[0])
        for index in wait_indexes:
            self.assertEqual(
                calls[index],
                ["pane", "process-info", "--pane", "w1:p2"])
        start = calls[start_indexes[0]]
        self.assertEqual(
            start[:9],
            ["agent", "start", start[2], "--kind", "omp",
             "--pane", "w1:p2", "--timeout", "180000"])
        self.assertEqual(start[9], "--")
        self.assertEqual(calls[start_indexes[0] + 1][:2], ["pane", "get"])
        # The coder must be waited for with the documented readiness gate, and
        # the prompt must be handed to the agent composer -- not typed into the
        # pane's shell -- so text plus Enter are submitted atomically.
        wait_indexes = [
            index for index, call in enumerate(calls)
            if call[:2] == ["agent", "wait"]
        ]
        self.assertEqual(len(wait_indexes), 1)
        wait = calls[wait_indexes[0]]
        self.assertEqual(wait[:5], ["agent", "wait", start[2], "--until", "idle"])
        self.assertIn("--timeout", wait)
        prompt_indexes = [
            index for index, call in enumerate(calls)
            if call[:2] == ["agent", "prompt"] and call[3].startswith("@")
        ]
        self.assertEqual(len(prompt_indexes), 1)
        prompt = calls[prompt_indexes[0]]
        self.assertEqual(
            prompt[:5],
            ["agent", "prompt", start[2],
             "@{0}".format(self.prompt.resolve()), "--wait"])
        # The harness turn runs as long as the task does, so the launch settles
        # on either working or idle rather than holding open until the run ends.
        self.assertEqual(
            [prompt[i + 1] for i, a in enumerate(prompt) if a == "--until"],
            ["working", "idle"])
        self.assertGreater(int(prompt[prompt.index("--timeout") + 1]), 5000)
        self.assertLess(wait_indexes[0], prompt_indexes[0])
        self.assertFalse(any(
            call[:2] in (["pane", "run"], ["pane", "send-keys"],
                         ["agent", "send-keys"])
            for call in calls))

    def test_launch_refusal_closes_the_allocated_pane(self):
        os.environ["FAKE_HERDR_REFUSE"] = "1"
        launcher = HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=self.admitted_routes)
        with self.assertRaisesRegex(RuntimeError, "LAUNCH_REFUSED"):
            launcher.launch(self.spec())
        calls = [json.loads(line)["argv"] for line in (self.root / "argv.jsonl").read_text().splitlines()]
        self.assertIn(["pane", "close", "w1:p2"], calls)

    def test_launch_refuses_os_failure_as_typed_refusal(self):
        launcher = HerdrLauncher(herdr_path=self.root / "missing-herdr",
                                 omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=self.admitted_routes)
        with self.assertRaisesRegex(RuntimeError, "LAUNCH_REFUSED"):
            launcher.launch(self.spec())

    def test_cancel_never_raises(self):
        launcher = FakeLauncher()
        handle = launcher.launch(self.spec())
        launcher.cancel(handle, time.monotonic() - 1.0)
        self.assertEqual(launcher.poll(handle).state, PollState.GONE)

    def test_lifecycle_uses_immutable_launch_environment(self):
        environment = {"FAKE_LAUNCH_ENV": "bound-context"}
        launcher = HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=self.admitted_routes)
        handle = launcher.launch(replace(self.spec(), environment=environment))
        environment["FAKE_LAUNCH_ENV"] = "mutated-context"
        with self.assertRaises(TypeError):
            handle.environment["FAKE_LAUNCH_ENV"] = "other-context"
        launcher.poll(handle)
        launcher.cancel(handle, time.monotonic() + 1.0)
        calls = [json.loads(line) for line in (self.root / "argv.jsonl").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call["environment"] == "bound-context" for call in calls))

    def test_cancel_removes_handle_only_after_proving_pane_gone(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr, omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes)
        handle = runtime.launch(self.spec())

        runtime.cancel(handle, time.monotonic() + 1.0)
        runtime.cancel(handle, time.monotonic() + 1.0)

        calls = [json.loads(line)["argv"]
                 for line in (self.root / "argv.jsonl").read_text().splitlines()]
        self.assertEqual(
            [call for call in calls if call[:2] == ["pane", "close"]],
            [["pane", "close", handle.pane_id]])
        self.assertEqual(runtime.reclaim(handle.correlation_token), ())

    def test_cancel_preserves_handle_when_pane_close_fails(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr, omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes)
        handle = runtime.launch(self.spec())
        os.environ["FAKE_HERDR_CLOSE_FAILURE"] = "1"

        with self.assertRaisesRegex(
                HarnessQuiescenceError, "HERDR_QUIESCENCE_UNPROVEN"):
            runtime.cancel(handle, time.monotonic() + 1.0)

        self.assertEqual(runtime.reclaim(handle.correlation_token), (handle,))

    def test_cancel_preserves_handle_when_close_does_not_stop_agent(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr, omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes)
        handle = runtime.launch(self.spec())
        os.environ["FAKE_HERDR_CLOSE_DOES_NOT_STOP_AGENT"] = "1"

        with self.assertRaisesRegex(
                HarnessQuiescenceError, "HERDR_QUIESCENCE_UNPROVEN"):
            runtime.cancel(handle, time.monotonic() + 1.0)

        self.assertEqual(runtime.reclaim(handle.correlation_token), (handle,))

    def test_cancel_maps_poll_transport_failure_to_unproven_quiescence(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr, omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes)
        handle = runtime.launch(self.spec())
        os.environ["FAKE_HERDR_GET_FAILURE"] = "1"

        with self.assertRaisesRegex(
                HarnessQuiescenceError, "HERDR_QUIESCENCE_UNPROVEN"):
            runtime.cancel(handle, time.monotonic() + 1.0)

        self.assertEqual(runtime.reclaim(handle.correlation_token), (handle,))

    def test_launch_sends_bounded_prompt_bootstrap_not_prompt_bytes(self):
        prompt_bytes = "sensitive prompt bytes " * 2048
        self.prompt.write_text(prompt_bytes, encoding="utf-8")
        launcher = HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                                 claude_path=Path("/opt/claude"),
                                 admitted_routes=self.admitted_routes)

        launcher.launch(self.spec("claude"))

        calls = [json.loads(line)["argv"]
                 for line in (self.root / "argv.jsonl").read_text().splitlines()]
        prompt_calls = [call for call in calls if call[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompt_calls), 1)
        bootstrap = prompt_calls[0][-1]
        self.assertNotIn(prompt_bytes, json.dumps(calls))
        self.assertLess(len(bootstrap), len(str(self.prompt.resolve())) + 2)

    def test_reclaim_matches_exact_token_only(self):
        launcher = FakeLauncher()
        handle = launcher.launch(self.spec())
        self.assertEqual(launcher.reclaim("run1-node_a-1"), (handle,))
        self.assertEqual(launcher.reclaim("run1-node_a"), ())

    def test_classification_is_closed(self):
        launcher = FakeLauncher()
        self.assertEqual(launcher.classify(FileNotFoundError()), ErrorClass.CONFIGURATION)
        self.assertEqual(launcher.classify(TimeoutError()), ErrorClass.TRANSIENT)
        self.assertEqual(launcher.classify(PermissionError()), ErrorClass.AUTHENTICATION)
        self.assertEqual(launcher.classify(ValueError()), ErrorClass.PROTOCOL)
        self.assertEqual(launcher.classify(RuntimeError()), ErrorClass.EXECUTION)

    def _launcher(self) -> HerdrLauncher:
        return HerdrLauncher(herdr_path=self.herdr, omp_path=Path("/opt/omp"),
                             claude_path=Path("/opt/claude"),
                             admitted_routes=self.admitted_routes)

    def test_fresh_idle_blocked_done_stay_nonterminal(self):
        route = self._launcher()
        handle = route.launch(self.spec())
        for status in ("idle", "blocked", "done"):
            os.environ["FAKE_AGENT_STATUS"] = status
            state = route.poll(handle)
            self.assertEqual(state.state, PollState.RUNNING, status)
            self.assertIsNone(state.exit_code)

    def test_working_stays_running(self):
        route = self._launcher()
        handle = route.launch(self.spec())
        os.environ["FAKE_AGENT_STATUS"] = "working"
        self.assertEqual(route.poll(handle).state, PollState.RUNNING)

    def test_completed_turn_ignores_stale_working_status(self):
        route = self._launcher()
        handle = route.launch(self.spec())
        self.transcript.write_text(
            json.dumps({"type": "maestro_envelope", "success": True}) + "\n")
        os.environ["FAKE_AGENT_STATUS"] = "working"
        state = route.poll(handle)
        self.assertEqual(state.state, PollState.EXITED)
        self.assertEqual(state.exit_code, 0)

    def test_unknown_and_starting_stay_starting(self):
        route = self._launcher()
        handle = route.launch(self.spec())
        for status in ("unknown", "starting"):
            os.environ["FAKE_AGENT_STATUS"] = status
            self.assertEqual(route.poll(handle).state, PollState.STARTING, status)

    def test_idle_with_completed_turn_synthesizes_exit(self):
        route = self._launcher()
        handle = route.launch(self.spec())
        self.transcript.write_text(
            json.dumps({"type": "maestro_envelope", "success": True}) + "\n")
        os.environ["FAKE_AGENT_STATUS"] = "idle"
        state = route.poll(handle)
        self.assertEqual(state.state, PollState.EXITED)
        self.assertEqual(state.exit_code, 0)
        self.assertEqual(state.detail, "ENVELOPE_SUCCESS")

    def test_explicit_absence_is_gone(self):
        route = self._launcher()
        handle = route.launch(self.spec())
        Path(os.environ["FAKE_HERDR_CLOSE_MARKER"]).write_text("closed")
        state = route.poll(handle)
        self.assertEqual(state.state, PollState.GONE)
        self.assertEqual(state.detail, "AGENT_GONE")


class TranscriptAndRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tailer_ignores_incomplete_last_record(self):
        transcript = self.root / "session.jsonl"
        transcript.write_bytes(
            b'{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}\n'
            b'{"type":"message_end"')
        tailer = TranscriptTailer(transcript)
        records = tailer.read_new()
        self.assertEqual(len(records), 1)
        self.assertEqual(tailer.read_new(), ())

    def test_tailer_synthesizes_success_only_from_success_envelope(self):
        transcript = self.root / "session.jsonl"
        transcript.write_text(json.dumps({"type": "maestro_envelope", "success": True}) + "\n")
        tailer = TranscriptTailer(transcript)
        tailer.read_new()
        self.assertEqual(tailer.synthesized_exit(), (0, "ENVELOPE_SUCCESS"))
        transcript.write_text(json.dumps({"type": "message_end"}) + "\n")
        empty = TranscriptTailer(transcript)
        empty.read_new()
        self.assertEqual(empty.synthesized_exit(), (1, "NO_ENVELOPE"))

    def test_omp_argv_uses_pm_profile_and_session_only(self):
        spec = LaunchSpec("t", self.root, self.root / "p", self.root / "e", "omp", "provider/model", "high", "latest-profile", self.root / "s")
        argv = build_omp_argv(Path("/bin/omp"), spec)
        self.assertEqual(argv[0], "/bin/omp")
        self.assertIn("--pm-profile", argv)
        self.assertEqual(argv[argv.index("--pm-profile") + 1], "latest-profile")
        self.assertIn("--session-dir", argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

    def test_claude_argv_is_direct_and_unattended(self):
        spec = LaunchSpec("t", self.root, self.root / "p", self.root / "e", "claude", "opus", "high", None, self.root / "s")
        argv = build_claude_argv(Path("/bin/claude"), spec)
        self.assertEqual(argv[0], "/bin/claude")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--remote-control", argv)
        self.assertIn("--model", argv)
        self.assertIn("--effort", argv)


class ProcessGroupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_quiesce_kills_the_whole_group(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"],
            start_new_session=True,
        )
        try:
            quiesce_process_group(process.pid, time.monotonic() + 1.0)
            self.assertIsNotNone(process.wait(timeout=2))
        finally:
            if process.poll() is None:
                process.kill()

    def test_clean_exit_does_not_signal_an_absent_group(self):
        signals = []
        original_killpg = launcher.os.killpg

        def record_signal(process_group, sig):
            signals.append(sig)
            return original_killpg(process_group, sig)

        with mock.patch.object(launcher.os, "killpg",
                               side_effect=record_signal):
            result = run_harness_process(
                [sys.executable, "-c", "pass"], cwd=self.root)

        self.assertEqual(result.returncode, 0)
        self.assertNotIn(signal.SIGTERM, signals)
        self.assertNotIn(signal.SIGKILL, signals)

    def test_completed_leader_quiesces_lingering_descendants(self):
        marker = self.root / "process-group"
        program = (
            "import os, subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'])\n"
            "open(sys.argv[1], 'w').write(str(os.getpgrp()))\n"
        )
        process_group = None
        try:
            result = run_harness_process(
                [sys.executable, "-c", program, str(marker)], cwd=self.root)
            process_group = int(marker.read_text())
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process_group, 0)
        finally:
            if process_group is not None:
                quiesce_process_group(process_group, time.monotonic() + 1.0)


if __name__ == "__main__":
    unittest.main()
