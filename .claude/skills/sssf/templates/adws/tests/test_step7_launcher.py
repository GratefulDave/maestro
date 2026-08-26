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

# Aliased as well, because several tests below bind a local named `launcher`
# to a `HerdrLauncher` instance and would otherwise shadow the module.
from adw_modules import launcher as launcher_module
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
from adw_modules import worktree as worktree_module


FAKE_HERDR = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
record = os.environ.get("FAKE_HERDR_ARGV")
if record:
    with open(record, "a") as handle:
        handle.write(json.dumps({
            "argv": argv,
            "environment": os.environ.get("FAKE_LAUNCH_ENV"),
        }) + "\n")
ROOT = os.path.dirname(record) if record else "/tmp"
# The monotonic per-pane counter real herdr publishes on `pane get`. It is the
# only signal `launcher.submit_agent_prompt` accepts as proof that a composer
# actually took a prompt, so a fake that omits it models a pane that can never
# accept anything -- which is what silently blinded this suite to the launcher
# path that killed two production runs on 2026-08-18.
REV = os.path.join(ROOT, "pane_revision")

def revision():
    return int(open(REV).read()) if os.path.exists(REV) else 4096

def bump():
    nxt = revision() + 1
    open(REV, "w").write(str(nxt))

if argv[:2] == ["pane", "current"]:
    # A real herdr session always answers this: `route_admission` refuses
    # admission outright with HERDR_SESSION_REQUIRED when it cannot, so no
    # launch can be reached from a session that has no current pane. The fake
    # used to fall through to `{}` and the launcher quietly used the
    # `--current` selector instead; that fallback is gone (it reinstated the
    # focus race and scattered panes across workspaces), so the fake now
    # models the session it was always standing in for. `p0` in the same
    # workspace as the split child below, because a split lands beside its
    # parent.
    if os.environ.get("FAKE_NO_CURRENT_PANE"):
        print(json.dumps({"result": {}}))
    else:
        print(json.dumps({"result": {"pane": {"pane_id": "w1:p0"}}}))
elif argv[:2] == ["pane", "split"]:
    print(json.dumps({"result": {"pane": {"pane_id": "w1:p2", "cwd": os.environ["FAKE_HERDR_CWD"]}}}))
elif argv[:2] == ["pane", "get"]:
    if os.environ.get("FAKE_PANE_GET_FAILURE"):
        sys.stderr.write(json.dumps({"error": {"code": "transport_failure"}}))
        sys.exit(1)
    pane = {"pane_id": "w1:p2", "cwd": os.environ["FAKE_HERDR_CWD"],
            "foreground_cwd": "/wrong"}
    if not os.environ.get("FAKE_PANE_WITHOUT_REVISION"):
        pane["revision"] = revision()
    print(json.dumps({"result": {"pane": pane}}))
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
    agent = {"name": argv[2], "status": "idle"}
    if not os.environ.get("FAKE_START_WITHOUT_SESSION"):
        agent["transcript_path"] = os.environ["FAKE_TRANSCRIPT"]
    print(json.dumps({"result": {"agent": agent}}))
elif argv[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"ok": True, "status": "idle"}}))
elif argv[:2] == ["agent", "prompt"]:
    stalls = os.path.join(ROOT, "stalls")
    seen = int(open(stalls).read()) if os.path.exists(stalls) else 0
    if seen < int(os.environ.get("FAKE_PROMPT_STALLS", "0")):
        # The composer took the text and never submitted it: no lifecycle
        # change, and -- the part that matters -- no revision movement.
        open(stalls, "w").write(str(seen + 1))
        sys.stderr.write(json.dumps({"error": {"code": "agent_prompt_stalled"}}))
        sys.exit(1)
    bump()
    print(json.dumps({"result": {"ok": True}}))
elif argv[:2] == ["agent", "send-keys"]:
    bump()
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
    if os.environ.get("FAKE_AGENT_SESSION_EXITED"):
        # What real Herdr answers once a finished agent's session has exited:
        # a refusal with a typed code, not an agent record with empty fields.
        sys.stdout.write(json.dumps({
            "error": {"code": "agent_not_found",
                      "message": "agent target %s not found" % argv[2]},
            "id": "cli:agent:get"}))
        sys.exit(1)
    marker = os.environ.get("FAKE_HERDR_CLOSE_MARKER")
    if marker and os.path.exists(marker):
        print(json.dumps({"result": {}}))
    else:
        status = os.environ.get("FAKE_AGENT_STATUS", "working")
        agent = {"name": argv[2], "status": status, "interactive_ready": True}
        if os.environ.get("FAKE_SESSION_NEVER"):
            pass
        elif os.environ.get("FAKE_SESSION_TAGGED"):
            # What real herdr sends for a route that writes a JSONL
            # transcript: a tagged value, never a flat `transcript_path`.
            agent["agent_session"] = {"agent": "omp", "kind": "path",
                                      "source": "herdr:omp",
                                      "value": os.environ["FAKE_TRANSCRIPT"]}
        else:
            agent["transcript_path"] = os.environ["FAKE_TRANSCRIPT"]
        print(json.dumps({"result": {"agent": agent}}))
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
"""


class LauncherContractTest(unittest.TestCase):
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
        self.herdr = self.root / "herdr"
        self.herdr.write_text(FAKE_HERDR)
        self.herdr.chmod(0o755)
        self._before = dict(os.environ)
        os.environ.update(
            {
                "FAKE_HERDR_ARGV": str(self.root / "argv.jsonl"),
                "FAKE_HERDR_CLOSE_MARKER": str(self.root / "pane-closed"),
                "FAKE_HERDR_CWD": str(self.worktree),
                "FAKE_TRANSCRIPT": str(self.transcript),
            }
        )
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = load_public_key(fixtures / "route_receipts.pub")
        self.admitted_routes = load_admitted_routes(
            {"omp": fixtures / "omp.json", "claude": fixtures / "claude.json"},
            verify_keys=(key,),
        )

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
            # B13: `omp` publishes a catalog, so every spec dispatched on it
            # must carry the window `preflight_launch_prompt` measures against.
            # `claude` publishes none, so `None` is the honest value there.
            context_window_tokens=400_000 if route == "omp" else None,
            environment=worktree_module.launch_env(self.scratch),
        )

    @staticmethod
    def split_call(calls):
        """The one `pane split` argv, which is the pane's whole launch surface."""
        splits = [call for call in calls if call[:2] == ["pane", "split"]]
        if len(splits) != 1:
            raise AssertionError(
                "expected exactly one pane split, got {}".format(len(splits))
            )
        return splits[0]

    @staticmethod
    def pane_environment(split):
        """What the pane's shell will actually be forked with.

        Only `--env KEY=VALUE` counts. The environment this process hands the
        `herdr` CLI reaches the client and stops there, because the server
        forks the pane; asserting on the CLI subprocess's own environment is
        the assertion that let the 2026-08-17 defect through.
        """
        pane_env = {}
        for index, token in enumerate(split):
            if token == "--env":
                key, _, value = split[index + 1].partition("=")
                pane_env[key] = value
        return pane_env

    def recorded_calls(self):
        return [
            json.loads(line)["argv"]
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]

    def test_launch_verifies_pane_cwd_not_foreground_cwd(self):
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = launcher.launch(self.spec())
        self.assertEqual(handle.launched_cwd, self.worktree.resolve())
        self.assertEqual(handle.pane_id, "w1:p2")

    def test_claude_prompt_has_the_required_team_prefix(self):
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        launcher.launch(self.spec("claude"))
        self.assertEqual(
            self.prompt.read_text(),
            launcher_module.CLAUDE_TEAM_PROMPT_PREFIX + "do the work",
        )

    def test_claude_prompt_prefix_is_idempotent(self):
        spec = self.spec("claude")
        launcher_module.prepare_route_prompt(spec)
        launcher_module.prepare_route_prompt(spec)
        self.assertEqual(
            self.prompt.read_text().count(launcher_module.CLAUDE_TEAM_PROMPT_PREFIX), 1
        )

    def test_claude_prompt_prefix_carries_the_idle_evidence_rule(self):
        """The operating rule the prefix must state: a teammate is idle only
        on a real SendMessage result or ListAgents no longer reporting it
        running. Silence, flat mtime, and idle_notification are never
        evidence -- the same law LIVE_WORKING_STATUSES enforces runtime-side.
        """
        self.assertIn("idle only when", launcher_module.CLAUDE_TEAM_PROMPT_PREFIX)
        self.assertIn("SendMessage result", launcher_module.CLAUDE_TEAM_PROMPT_PREFIX)
        self.assertIn("herdr agent list", launcher_module.CLAUDE_TEAM_PROMPT_PREFIX)
        for non_evidence in ("Silence", "flat transcript mtime", "idle_notification"):
            self.assertIn(non_evidence, launcher_module.CLAUDE_TEAM_PROMPT_PREFIX)
        self.assertIn(
            "never start its work yourself", launcher_module.CLAUDE_TEAM_PROMPT_PREFIX
        )

    def test_omp_prompt_does_not_get_the_claude_team_prefix(self):
        launcher_module.prepare_route_prompt(self.spec("omp"))
        self.assertEqual(self.prompt.read_text(), "do the work")

    def test_read_commands_accept_raw_text_output(self):
        # `herdr agent read` / `pane read` print the snapshot as raw text; they
        # have no JSON output mode. Rejecting that as PROTOCOL_INVALID_JSON
        # blinds the composer-visibility wait and the receipt scan.
        script = self.root / "text-herdr"
        script.write_text("#!/bin/sh\nprintf '%s\\n' 'MAESTRO_CLAUDE_RECEIPT_OK'\n")
        script.chmod(0o755)
        harness = HerdrLauncher(
            herdr_path=script,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        payload = harness._herdr("agent", "read", "n", "--source", "visible")
        self.assertIn("MAESTRO_CLAUDE_RECEIPT_OK", payload["result"]["text"])
        payload = harness._herdr("pane", "read", "w1:p2", "--source", "visible")
        self.assertIn("MAESTRO_CLAUDE_RECEIPT_OK", payload["result"]["text"])
        # Every other command still has to speak JSON.
        with self.assertRaisesRegex(RuntimeError, "PROTOCOL_INVALID_JSON"):
            harness._herdr("pane", "list")

    def test_launch_refuses_unadmitted_route_before_creating_pane(self):
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        claude_only = load_admitted_routes(
            {"claude": fixtures / "claude.json"},
            verify_keys=(load_public_key(fixtures / "route_receipts.pub"),),
        )
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=claude_only,
        )
        with self.assertRaisesRegex(RuntimeError, "ROUTE_NOT_ADMITTED:omp"):
            launcher.launch(self.spec("omp"))
        self.assertFalse((self.root / "argv.jsonl").exists())

    def test_launch_carries_scratch_redirection_into_the_pane_itself(self):
        # §8.3: byproducts are redirected out of the worktree, and the pane
        # environment at allocation is one of the three contexts that must
        # carry the redirection. The pane's shell is forked by the herdr
        # server, so `--env` on `pane split` is the only thing that reaches it
        # -- the CLI subprocess's own environment does not. On 2026-08-17 an
        # agent whose pane never received these wrote 226 `.pyc` files and a
        # `.pytest_cache` into its worktree running its own tests, and was
        # convicted under the permission check for the harness's omission.
        harness = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        harness.launch(self.spec())
        split = self.split_call(self.recorded_calls())
        pane_env = self.pane_environment(split)
        self.assertEqual(set(pane_env), set(launcher.SCRATCH_ENV_KEYS))
        expected = worktree_module.scratch_env(self.scratch)
        self.assertEqual(pane_env, expected)
        # Every redirected path must land in the attempt's scratch, outside the
        # worktree, or the redirect is decorative.
        self.assertEqual(pane_env["PYTHONPYCACHEPREFIX"], str(self.scratch / "pycache"))
        self.assertIn(
            "cache_dir={}".format(self.scratch / "pytest_cache"),
            pane_env["PYTEST_ADDOPTS"],
        )
        for key, value in pane_env.items():
            path = (
                value.split("cache_dir=", 1)[-1] if key == "PYTEST_ADDOPTS" else value
            )
            self.assertTrue(Path(path).is_relative_to(self.scratch), key)
            self.assertFalse(Path(path).is_relative_to(self.worktree), key)

    def test_pane_env_flags_survive_values_carrying_equals_and_spaces(self):
        # `PYTEST_ADDOPTS="-n 1 -o cache_dir=<path>"` carries both a space and a
        # second `=`. Measured against herdr 0.8.0 on 2026-08-17: `--env`
        # splits on the first `=` only, so the value arrives whole.
        flags = launcher.pane_env_flags(worktree_module.scratch_env(self.scratch))
        pane_env = self.pane_environment(("pane", "split") + flags)
        self.assertEqual(
            pane_env["PYTEST_ADDOPTS"],
            "-o cache_dir={}".format(self.scratch / "pytest_cache"),
        )

    def test_launch_refuses_when_herdr_cannot_name_the_current_pane(self):
        """No pane to split from is a refusal, not a fall back to `--current`.

        The selector used to be the answer here. It reinstated the focus race
        the resolved-once parent exists to remove, and because focus can sit in
        any workspace it put panes wherever focus happened to be — one run's
        agents scattered across w13F, w13G, w13H, w13J and w13K while its first
        pane sat in w13A. §1.2 forbids keying a decision on ambient mutable
        state, so this refuses and splits nothing.

        Reachable only in this fake: `route_admission._require_herdr_session`
        refuses admission with HERDR_SESSION_REQUIRED when `pane current` has
        no answer, so no production launch runs from a session in this state.
        The refusal is the fail-closed floor under that check, not a path the
        runtime is expected to take.
        """
        harness = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        os.environ["FAKE_NO_CURRENT_PANE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_NO_CURRENT_PANE", None)
        with self.assertRaises(launcher_module.LaunchRefused) as caught:
            harness.launch(self.spec())

        self.assertIs(
            caught.exception.refusal,
            launcher_module.LaunchRefusal.SPLIT_PARENT_UNRESOLVED,
        )
        # Nothing was split, so nothing was left behind and the refusal says so
        # — and it stays retryable, because herdr may answer the next ask.
        self.assertEqual(
            [call for call in self.recorded_calls() if call[:2] == ["pane", "split"]],
            [],
        )
        self.assertFalse(caught.exception.pane_created)
        self.assertFalse(caught.exception.deterministic)

    def test_launch_splits_the_resolved_parent_and_stays_in_its_workspace(self):
        """Every pane of a run belongs to one workspace, and it is measured."""
        harness = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = harness.launch(self.spec())

        split = self.split_call(self.recorded_calls())
        self.assertEqual(split[2], "w1:p0")
        self.assertNotIn("--current", split)
        self.assertEqual(
            launcher_module.workspace_of(handle.pane_id),
            launcher_module.workspace_of("w1:p0"),
        )
        # The direction is a pure function of how many panes this launcher has
        # already split, so the first is deterministic (§ the alternation rule
        # lives in `test_pane_placement.py`).
        self.assertEqual(split[split.index("--direction") + 1], "right")

    def test_launch_refuses_before_creating_a_pane_when_redirection_is_missing(self):
        # A redirect that silently fails to arrive convicts the agent for a
        # harness defect, so an incomplete environment is refused before any
        # untrusted code runs rather than degraded into a conviction (§8.3).
        environment = worktree_module.launch_env(self.scratch)
        del environment["PYTHONPYCACHEPREFIX"]
        harness = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        with self.assertRaisesRegex(
            RuntimeError, "LAUNCH_REFUSED:SCRATCH_REDIRECT_MISSING:PYTHONPYCACHEPREFIX"
        ) as caught:
            harness.launch(replace(self.spec(), environment=environment))
        self.assertFalse((self.root / "argv.jsonl").exists())
        # The absence of `argv.jsonl` is what "before creating a pane" means
        # here, and it is a fact this test can see because it drives the whole
        # launcher. §8.3's quiesce step is downstream and cannot see it, so the
        # launcher states it as a typed field on the refusal rather than
        # leaving the scheduler to infer it from a message (§16.3 item 45).
        self.assertIs(
            caught.exception.refusal,
            launcher_module.LaunchRefusal.SCRATCH_REDIRECT_MISSING,
        )
        self.assertFalse(caught.exception.pane_created)
        self.assertTrue(caught.exception.deterministic)

    def test_launch_refuses_wrong_pane_cwd_before_starting_agent(self):
        os.environ["FAKE_HERDR_CWD"] = str(self.root / "wrong")
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        with self.assertRaisesRegex(RuntimeError, "BINDING_MISMATCH"):
            launcher.launch(self.spec())
        calls = [
            json.loads(line)["argv"]
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]
        self.assertNotIn(["agent", "start"], [call[:2] for call in calls])
        self.assertIn(["pane", "close", "w1:p2"], calls)

    def test_launch_waits_for_shell_then_starts_agent_once(self):
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = launcher.launch(self.spec())
        self.assertEqual(handle.pane_id, "w1:p2")
        calls = [
            json.loads(line)["argv"]
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]
        wait_indexes = [
            index
            for index, call in enumerate(calls)
            if call[:2] == ["pane", "process-info"]
        ]
        start_indexes = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "start"]
        ]
        # The shell must look ready on several consecutive snapshots before we
        # start: one ready snapshot can land in the gap before login hooks
        # spawn, which makes Herdr report agent_pane_busy.
        self.assertGreaterEqual(len(wait_indexes), 5)
        self.assertEqual(len(start_indexes), 1)
        self.assertLess(wait_indexes[4], start_indexes[0])
        for index in wait_indexes:
            self.assertEqual(calls[index], ["pane", "process-info", "--pane", "w1:p2"])
        start = calls[start_indexes[0]]
        self.assertEqual(
            start[:9],
            [
                "agent",
                "start",
                start[2],
                "--kind",
                "omp",
                "--pane",
                "w1:p2",
                "--timeout",
                "180000",
            ],
        )
        self.assertEqual(start[9], "--")
        self.assertEqual(calls[start_indexes[0] + 1][:2], ["pane", "get"])
        # The coder is waited to its interactive composer before the complete
        # node prompt is submitted. Startup argv must not carry `@<file>`:
        # that races OMP initialization and can execute only the prompt's
        # leading command.
        wait_indexes = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "wait"]
        ]
        self.assertEqual(len(wait_indexes), 1)
        wait = calls[wait_indexes[0]]
        self.assertEqual(wait[:5], ["agent", "wait", start[2], "--until", "idle"])
        self.assertIn("--timeout", wait)
        route_argv = start[start.index("--") + 1 :]
        self.assertFalse(any(arg.startswith("@") for arg in route_argv))
        prompts = [call for call in calls if call[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][3], "@{0}".format(self.prompt.resolve()))
        self.assertLess(wait_indexes[0], calls.index(prompts[0]))
        self.assertFalse(
            any(
                call[:2]
                in (["pane", "run"], ["pane", "send-keys"], ["agent", "send-keys"])
                for call in calls
            )
        )

    def test_launch_waits_for_a_transcript_path_that_arrives_after_start(self):
        # `agent start` returns once herdr holds the process, which for the omp
        # route is before the coder has opened its JSONL session -- so herdr
        # sends no `agent_session` key at all. Reading only the start payload
        # made SESSION_PATH_MISSING a race: on 2026-08-17 one of three attempts
        # on the same node happened to win it and ran 61 turns, and the two
        # that lost it died at turn zero.
        os.environ["FAKE_START_WITHOUT_SESSION"] = "1"
        os.environ["FAKE_SESSION_TAGGED"] = "1"
        harness = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = harness.launch(self.spec())
        self.assertEqual(handle.transcript_path, self.transcript)
        # The tailer must be registered for the resolved path, not skipped
        # because the path was unknown at the instant the handle was built.
        tailer = harness._tailers["run1-node_a-1"]
        self.assertEqual(tailer.path, self.transcript)
        # And the registry must still name the object the caller holds.
        self.assertIs(harness._handles["run1-node_a-1"], handle)
        self.assertIn(["agent", "get", handle.agent_name], self.recorded_calls())

    def test_launch_bounds_the_wait_when_no_transcript_ever_arrives(self):
        # The bounded half: when the path genuinely never appears the wait
        # terminates and the handle carries None, so the caller's
        # SESSION_PATH_MISSING is still reachable rather than traded for a hang.
        os.environ["FAKE_START_WITHOUT_SESSION"] = "1"
        os.environ["FAKE_SESSION_NEVER"] = "1"
        harness = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        with mock.patch.object(launcher, "TRANSCRIPT_PATH_TIMEOUT_S", 0.05):
            started = time.monotonic()
            handle = harness.launch(self.spec())
            elapsed = time.monotonic() - started
        self.assertIsNone(handle.transcript_path)
        self.assertNotIn("run1-node_a-1", harness._tailers)
        self.assertLess(elapsed, 30.0)

    def test_the_transcript_wait_reads_the_typed_field_and_terminates(self):
        # A typed ID without a route source or launched cwd cannot be invented
        # into a path, and the bounded wait must terminate honestly.
        calls = []

        def herdr_call(*args, **kwargs):
            calls.append(args)
            return {
                "result": {"agent": {"agent_session": {"kind": "id", "value": "abc"}}}
            }

        slept = []
        self.assertIsNone(
            launcher.wait_for_agent_transcript(
                herdr_call, "a", 0.0, poll_interval_s=0.0, sleep=slept.append
            )
        )
        self.assertEqual(calls, [("agent", "get", "a")])

    def test_claude_session_id_resolves_the_exact_cwd_transcript(self):
        config = self.root / "claude"
        project = "".join(
            character if character.isalnum() or character == "-" else "-"
            for character in str(self.worktree.resolve())
        )
        transcript = config / "projects" / project / "session-123.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        agent = {
            "agent_session": {
                "kind": "id",
                "source": "herdr:claude",
                "value": "session-123",
            },
        }

        self.assertEqual(
            launcher_module._agent_transcript_path(
                agent, self.worktree, {"CLAUDE_CONFIG_DIR": str(config)}
            ),
            transcript,
        )

    def test_launch_refusal_closes_the_allocated_pane(self):
        os.environ["FAKE_HERDR_REFUSE"] = "1"
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        with self.assertRaisesRegex(RuntimeError, "LAUNCH_REFUSED") as caught:
            launcher.launch(self.spec())
        calls = [
            json.loads(line)["argv"]
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]
        self.assertIn(["pane", "close", "w1:p2"], calls)
        # A pane was allocated and this handler closed it, so the refusal
        # reports the close herdr accepted rather than a constant. Saying
        # `True` after a successful close is what sent §8.3's quiesce step
        # after an attempt that was never registered, turning a retryable
        # launch failure into a terminal QUIESCENCE_UNPROVEN. The close-failed
        # direction is the control, in `test_launch_refusal_cleanup.py`.
        self.assertIsInstance(caught.exception, launcher_module.LaunchRefused)
        self.assertIs(
            caught.exception.refusal, launcher_module.LaunchRefusal.AGENT_START_REFUSED
        )
        self.assertFalse(caught.exception.pane_created)

    def test_launch_refuses_os_failure_as_typed_refusal(self):
        launcher = HerdrLauncher(
            herdr_path=self.root / "missing-herdr",
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        with self.assertRaisesRegex(RuntimeError, "LAUNCH_REFUSED"):
            launcher.launch(self.spec())

    def test_cancel_never_raises(self):
        launcher = FakeLauncher()
        handle = launcher.launch(self.spec())
        launcher.cancel(handle, time.monotonic() - 1.0)
        self.assertEqual(launcher.poll(handle).state, PollState.GONE)

    def test_lifecycle_uses_immutable_launch_environment(self):
        environment = dict(
            worktree_module.launch_env(self.scratch), FAKE_LAUNCH_ENV="bound-context"
        )
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = launcher.launch(replace(self.spec(), environment=environment))
        environment["FAKE_LAUNCH_ENV"] = "mutated-context"
        with self.assertRaises(TypeError):
            handle.environment["FAKE_LAUNCH_ENV"] = "other-context"
        launcher.poll(handle)
        launcher.cancel(handle, time.monotonic() + 1.0)
        calls = [
            json.loads(line)
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]
        self.assertTrue(calls)
        self.assertTrue(all(call["environment"] == "bound-context" for call in calls))

    def test_cancel_removes_handle_only_after_proving_pane_gone(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = runtime.launch(self.spec())

        runtime.cancel(handle, time.monotonic() + 1.0)
        runtime.cancel(handle, time.monotonic() + 1.0)

        calls = [
            json.loads(line)["argv"]
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [call for call in calls if call[:2] == ["pane", "close"]],
            [["pane", "close", handle.pane_id]],
        )
        self.assertEqual(runtime.reclaim(handle.correlation_token), ())

    def test_cancel_preserves_handle_when_pane_close_fails(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = runtime.launch(self.spec())
        os.environ["FAKE_HERDR_CLOSE_FAILURE"] = "1"

        with self.assertRaisesRegex(
            HarnessQuiescenceError, "HERDR_QUIESCENCE_UNPROVEN"
        ):
            runtime.cancel(handle, time.monotonic() + 1.0)

        self.assertEqual(runtime.reclaim(handle.correlation_token), (handle,))

    def test_cancel_preserves_handle_when_close_does_not_stop_agent(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = runtime.launch(self.spec())
        os.environ["FAKE_HERDR_CLOSE_DOES_NOT_STOP_AGENT"] = "1"

        with self.assertRaisesRegex(
            HarnessQuiescenceError, "HERDR_QUIESCENCE_UNPROVEN"
        ):
            runtime.cancel(handle, time.monotonic() + 1.0)

        self.assertEqual(runtime.reclaim(handle.correlation_token), (handle,))

    def test_cancel_maps_poll_transport_failure_to_unproven_quiescence(self):
        runtime = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        handle = runtime.launch(self.spec())
        os.environ["FAKE_HERDR_GET_FAILURE"] = "1"

        with self.assertRaisesRegex(
            HarnessQuiescenceError, "HERDR_QUIESCENCE_UNPROVEN"
        ):
            runtime.cancel(handle, time.monotonic() + 1.0)

        self.assertEqual(runtime.reclaim(handle.correlation_token), (handle,))

    def test_launch_sends_bounded_prompt_bootstrap_not_prompt_bytes(self):
        prompt_bytes = "sensitive prompt bytes " * 2048
        os.environ["FAKE_START_WITHOUT_SESSION"] = "1"
        self.prompt.write_text(prompt_bytes, encoding="utf-8")
        launcher = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )

        launcher.launch(self.spec("claude"))

        calls = [
            json.loads(line)["argv"]
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]
        prompt_calls = [call for call in calls if call[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompt_calls), 1)
        bootstrap = prompt_calls[0][3]
        self.assertEqual(bootstrap, "@{0}".format(self.prompt.resolve()))
        self.assertNotIn(prompt_bytes, json.dumps(calls))
        wait_index = next(
            index for index, call in enumerate(calls) if call[:2] == ["agent", "wait"]
        )
        prompt_index = next(
            index for index, call in enumerate(calls) if call[:2] == ["agent", "prompt"]
        )
        transcript_index = next(
            index
            for index, call in enumerate(calls)
            if index > prompt_index and call[:2] == ["agent", "get"]
        )
        self.assertLess(wait_index, prompt_index)
        self.assertLess(prompt_index, transcript_index)

    def test_every_claude_launch_including_retry_gets_one_ready_prompt(self):
        for attempt in (1, 2):
            runtime = HerdrLauncher(
                herdr_path=self.herdr,
                omp_path=Path("/opt/omp"),
                claude_path=Path("/opt/claude"),
                admitted_routes=self.admitted_routes,
            )
            runtime.launch(
                replace(
                    self.spec("claude"),
                    correlation_token="run1-node_a-{}".format(attempt),
                )
            )

        calls = self.recorded_calls()
        starts = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "start"]
        ]
        waits = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "wait"]
        ]
        prompts = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "prompt"]
        ]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(waits), 2)
        self.assertEqual(len(prompts), 2)
        for start, ready, prompt in zip(starts, waits, prompts):
            self.assertLess(start, ready)
            self.assertLess(ready, prompt)

    def test_every_omp_launch_including_retry_gets_one_ready_prompt(self):
        for attempt in (1, 2):
            runtime = HerdrLauncher(
                herdr_path=self.herdr,
                omp_path=Path("/opt/omp"),
                claude_path=Path("/opt/claude"),
                admitted_routes=self.admitted_routes,
            )
            runtime.launch(
                replace(
                    self.spec("omp"), correlation_token="run1-node_a-{}".format(attempt)
                )
            )

        calls = self.recorded_calls()
        starts = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "start"]
        ]
        waits = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "wait"]
        ]
        prompts = [
            index for index, call in enumerate(calls) if call[:2] == ["agent", "prompt"]
        ]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(waits), 2)
        self.assertEqual(len(prompts), 2)
        for start, ready, prompt in zip(starts, waits, prompts):
            self.assertLess(start, ready)
            self.assertLess(ready, prompt)

    def test_a_claude_composer_that_stalls_is_recovered_with_enter(self):
        """A Claude composer that swallows the prompt must still be caught.

        Direct Claude hands `@<path>` to a ready composer. A composer that
        swallows the text reports `idle` exactly like one that took it. The
        pane's monotonic `revision` is the only thing that separates them, so
        this drives the real Claude recovery loop through `launch`.
        """
        os.environ["FAKE_PROMPT_STALLS"] = "1"
        runtime = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        runtime.launch(self.spec("claude"))
        calls = self.recorded_calls()
        self.assertEqual(
            len([call for call in calls if call[:2] == ["agent", "prompt"]]), 1
        )
        # Enter was pressed on what the composer was already holding, and the
        # prompt was never re-issued -- a second `agent prompt` would append to
        # the unsubmitted line and send both as one garbled turn.
        send_keys = [call for call in calls if call[:2] == ["agent", "send-keys"]]
        self.assertEqual(len(send_keys), 1)
        self.assertEqual(send_keys[0][3], "enter")

    def test_a_pane_that_never_advances_its_revision_refuses_the_launch(self):
        """The blindness this suite carried until 2026-08-18.

        With no `revision` key at all the launcher can never prove submission,
        so the launch must refuse rather than hand back a handle for an agent
        that was never given any work.
        """
        os.environ["FAKE_PANE_WITHOUT_REVISION"] = "1"
        os.environ["FAKE_AGENT_STATUS"] = "idle"
        runtime = HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        with self.assertRaises(launcher.LaunchRefused) as caught:
            runtime.launch(self.spec("claude"))
        self.assertIs(
            caught.exception.refusal, launcher.LaunchRefusal.PROMPT_SUBMISSION_REFUSED
        )
        self.assertIsInstance(
            caught.exception.__cause__, launcher.PromptSubmissionUnobservable
        )

    def test_reclaim_matches_exact_token_only(self):
        launcher = FakeLauncher()
        handle = launcher.launch(self.spec())
        self.assertEqual(launcher.reclaim("run1-node_a-1"), (handle,))
        self.assertEqual(launcher.reclaim("run1-node_a"), ())

    def test_classification_is_closed(self):
        launcher = FakeLauncher()
        self.assertEqual(
            launcher.classify(FileNotFoundError()), ErrorClass.CONFIGURATION
        )
        self.assertEqual(launcher.classify(TimeoutError()), ErrorClass.TRANSIENT)
        self.assertEqual(
            launcher.classify(PermissionError()), ErrorClass.AUTHENTICATION
        )
        self.assertEqual(launcher.classify(ValueError()), ErrorClass.PROTOCOL)
        self.assertEqual(launcher.classify(RuntimeError()), ErrorClass.EXECUTION)

    def _launcher(self) -> HerdrLauncher:
        return HerdrLauncher(
            herdr_path=self.herdr,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )

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
            json.dumps({"type": "maestro_envelope", "success": True}) + "\n"
        )
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
            json.dumps({"type": "maestro_envelope", "success": True}) + "\n"
        )
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

    def test_a_success_envelope_outlives_the_agent_that_wrote_it(self):
        # The live failure: the agent wrote a complete `"success": true`
        # envelope and its session exited moments later, so `herdr agent get`
        # answered `agent_not_found` and the attempt was scored GONE with the
        # envelope sitting unread on disk. Three attempts of
        # `lane-wrtop-store-document-tests` were thrown away this way and the
        # node failed ENVIRONMENTAL_BUDGET_EXHAUSTED. The faster the agent, the
        # likelier it loses this race.
        route = self._launcher()
        handle = route.launch(self.spec())
        self.envelope.write_text(
            json.dumps({"success": True, "summary": "10 passed"}), encoding="utf-8"
        )
        os.environ["FAKE_AGENT_SESSION_EXITED"] = "1"
        state = route.poll(handle)
        self.assertEqual(state.state, PollState.EXITED)
        self.assertEqual(state.exit_code, 0)
        self.assertEqual(state.detail, "ENVELOPE_SUCCESS")

    def test_a_failure_envelope_also_outlives_its_agent(self):
        # The artifact wins in both directions: a declared failure must not be
        # laundered into GONE either, or the retry class is decided by a race.
        route = self._launcher()
        handle = route.launch(self.spec())
        self.envelope.write_text(json.dumps({"success": False}), encoding="utf-8")
        os.environ["FAKE_AGENT_SESSION_EXITED"] = "1"
        state = route.poll(handle)
        self.assertEqual(state.state, PollState.EXITED)
        self.assertEqual(state.exit_code, 1)
        self.assertEqual(state.detail, "ENVELOPE_FAILURE")

    def test_a_transcript_declaration_outlives_its_agent(self):
        # Routes that declare in the transcript rather than by writing the
        # envelope file must survive the same race.
        route = self._launcher()
        handle = route.launch(self.spec())
        self.transcript.write_text(
            json.dumps({"type": "maestro_envelope", "success": True}) + "\n"
        )
        os.environ["FAKE_AGENT_SESSION_EXITED"] = "1"
        state = route.poll(handle)
        self.assertEqual(state.state, PollState.EXITED)
        self.assertEqual(state.exit_code, 0)

    def test_a_vanished_agent_that_declared_nothing_is_still_gone(self):
        # GONE keeps its meaning: agent gone AND nothing declared. The failure
        # this fix removes is good work scored as failure; the failure it must
        # not introduce is no work scored as success.
        route = self._launcher()
        handle = route.launch(self.spec())
        os.environ["FAKE_AGENT_SESSION_EXITED"] = "1"
        state = route.poll(handle)
        self.assertEqual(state.state, PollState.GONE)
        self.assertEqual(state.detail, "AGENT_GONE")

    def test_a_half_written_envelope_is_never_a_success(self):
        # The incident's own log has the agent SIGHUP'd *during* the envelope
        # write -- its `write` returned "Aborted". That file happened to land
        # complete; a truncated one must not be read as a declaration. Giving
        # the artifact precedence makes this branch reachable for the first
        # time, so it is worth a test rather than an assumption.
        for content in ("", "{", '{"success": tru'):
            with self.subTest(content=content):
                route = self._launcher()
                handle = route.launch(self.spec())
                self.envelope.write_text(content, encoding="utf-8")
                os.environ["FAKE_AGENT_SESSION_EXITED"] = "1"
                state = route.poll(handle)
                self.assertEqual(state.state, PollState.EXITED)
                self.assertEqual(state.exit_code, 1)
                self.assertEqual(state.detail, "ENVELOPE_UNPARSED")

    def test_an_envelope_without_a_success_verdict_is_not_a_success(self):
        # `success` absent, or present but not the boolean `true`, is not a
        # declaration of success. Only `is True` counts.
        for payload in (
            {},
            {"summary": "did things"},
            {"success": "true"},
            {"success": 1},
        ):
            with self.subTest(payload=payload):
                route = self._launcher()
                handle = route.launch(self.spec())
                self.envelope.write_text(json.dumps(payload), encoding="utf-8")
                os.environ["FAKE_AGENT_SESSION_EXITED"] = "1"
                state = route.poll(handle)
                self.assertEqual(state.exit_code, 1)
                self.assertEqual(state.detail, "ENVELOPE_FAILURE")

    def test_resume_probe_reads_live_agent_from_herdr(self):
        route = self._launcher()
        self.assertIs(route.agent_presence("run-node-1"), True)

    def test_resume_probe_requires_typed_agent_absence(self):
        route = self._launcher()
        os.environ["FAKE_AGENT_SESSION_EXITED"] = "1"
        self.assertIs(route.agent_presence("run-node-1"), False)

    def test_resume_probe_keeps_transport_failure_unknown(self):
        route = self._launcher()
        os.environ["FAKE_HERDR_GET_FAILURE"] = "1"
        self.assertIsNone(route.agent_presence("run-node-1"))

    def test_agent_absence_is_read_from_the_code_not_the_message(self):
        # §1.2: `agent_not_found` is a typed field. A refusal whose prose
        # merely contains the word is a different refusal and must surface.
        script = self.root / "prose-herdr"
        script.write_text(
            "#!/bin/sh\nprintf '%s' 'agent_not_found'\nexit 1\n", encoding="utf-8"
        )
        script.chmod(0o755)
        route = self._launcher()
        handle = route.launch(self.spec())
        prose = HerdrLauncher(
            herdr_path=script,
            omp_path=Path("/opt/omp"),
            claude_path=Path("/opt/claude"),
            admitted_routes=self.admitted_routes,
        )
        with self.assertRaises(launcher.HerdrCallError) as caught:
            prose.poll(handle)
        self.assertEqual(caught.exception.code, "")

    def test_cancel_proves_quiescence_for_an_attempt_that_succeeded(self):
        # `cancel` runs in a `finally` after every attempt, successful ones
        # included, so by then a successful attempt's envelope is on disk. If
        # `cancel` asked `poll` whether the agent was gone it would be told
        # EXITED -- the attempt's outcome, not the pane's state -- read that as
        # PANE_STILL_LIVE, and refuse quiescence for every node that worked.
        route = self._launcher()
        handle = route.launch(self.spec())
        self.envelope.write_text(json.dumps({"success": True}), encoding="utf-8")
        route.cancel(handle, time.monotonic() + 1.0)
        self.assertEqual(route.reclaim(handle.correlation_token), ())


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
            b'{"type":"message_end"'
        )
        tailer = TranscriptTailer(transcript)
        records = tailer.read_new()
        self.assertEqual(len(records), 1)
        self.assertEqual(tailer.read_new(), ())

    def test_tailer_synthesizes_success_only_from_success_envelope(self):
        transcript = self.root / "session.jsonl"
        transcript.write_text(
            json.dumps({"type": "maestro_envelope", "success": True}) + "\n"
        )
        tailer = TranscriptTailer(transcript)
        tailer.read_new()
        self.assertEqual(tailer.synthesized_exit(), (0, "ENVELOPE_SUCCESS"))
        transcript.write_text(json.dumps({"type": "message_end"}) + "\n")
        empty = TranscriptTailer(transcript)
        empty.read_new()
        self.assertEqual(empty.synthesized_exit(), (1, "NO_ENVELOPE"))

    def test_omp_argv_uses_profile_and_session_only(self):
        spec = LaunchSpec(
            "t",
            self.root,
            self.root / "p",
            self.root / "e",
            "omp",
            "provider/model",
            "high",
            "latest-profile",
            self.root / "s",
        )
        argv = build_omp_argv(Path("/bin/omp"), spec)
        self.assertEqual(argv[0], "/bin/omp")
        self.assertIn("--profile", argv)
        self.assertEqual(argv[argv.index("--profile") + 1], "latest-profile")
        self.assertIn("--session-dir", argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)

    def test_claude_argv_is_direct_and_unattended(self):
        spec = LaunchSpec(
            "t",
            self.root,
            self.root / "p",
            self.root / "e",
            "claude",
            "opus",
            "high",
            None,
            self.root / "s",
        )
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
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)",
            ],
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

        with mock.patch.object(launcher.os, "killpg", side_effect=record_signal):
            result = run_harness_process([sys.executable, "-c", "pass"], cwd=self.root)

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
                [sys.executable, "-c", program, str(marker)], cwd=self.root
            )
            process_group = int(marker.read_text())
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process_group, 0)
        finally:
            if process_group is not None:
                quiesce_process_group(process_group, time.monotonic() + 1.0)


if __name__ == "__main__":
    unittest.main()
