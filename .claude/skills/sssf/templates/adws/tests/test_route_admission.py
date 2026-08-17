"""Route admission captures visible Herdr evidence and signs it.

Fixtures from step 8 are verification material only. This suite never copies
them into a destination a production command would read.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import contextlib
import io

import maestro
from adw_modules import receipt_crypto
from adw_modules import route_admission as ra
from adw_modules.route_receipts import load_admitted_routes, load_route_receipt


FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
root = os.environ["FAKE_ADMIT_ROOT"]
cwd = os.environ.get("FAKE_HERDR_CWD", "")
def write(name, payload):
    with open(os.path.join(root, name), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
write("argv.jsonl", {"argv": argv})
if argv[:2] == ["pane", "current"]:
    print(json.dumps({"result": {"pane": {"pane_id": "w1:p1", "cwd": cwd}}}))
elif argv[:2] == ["pane", "split"]:
    claimed = cwd
    if "--cwd" in argv:
        claimed = argv[argv.index("--cwd") + 1]
    counter_path = os.path.join(root, "pane_seq")
    seq = 2
    if os.path.exists(counter_path):
        seq = int(open(counter_path).read() or "1") + 1
    open(counter_path, "w").write(str(seq))
    pane_id = "w1:p%d" % seq
    print(json.dumps({"result": {"pane": {"pane_id": pane_id, "cwd": claimed}}}))
elif argv[:2] == ["pane", "get"]:
    closed_path = os.path.join(root, "closed")
    if os.path.exists(closed_path) and argv[2] in open(closed_path, encoding="utf-8").read().split():
        sys.stderr.write("pane_not_found\n")
        sys.exit(1)
    claimed = os.environ.get("FAKE_PANE_CWD", cwd)
    print(json.dumps({"result": {"pane": {"pane_id": "w1:p2", "cwd": claimed}}}))
elif argv[:2] == ["pane", "process-info"]:
    process = "zsh"
    launched_path = os.path.join(root, "launched")
    if os.path.exists(launched_path) and argv[-1] in open(launched_path).read().split():
        starts = open(os.path.join(root, "starts.jsonl"), encoding="utf-8").read()
        process = "claude" if "claude" in starts else "omp"
    print(json.dumps({"result": {"process_info": {
        "pane_id": argv[-1],
        "foreground_processes": [{"name": process, "argv0": process, "argv": [process]}]}}}))
elif argv[:2] == ["agent", "start"]:
    busy_path = os.path.join(root, "busy_starts")
    consumed = int(open(busy_path).read()) if os.path.exists(busy_path) else 0
    if consumed < int(os.environ.get("FAKE_BUSY_STARTS", "0")):
        open(busy_path, "w").write(str(consumed + 1))
        sys.stderr.write(json.dumps({"error": {"code": "agent_pane_busy"}}))
        sys.exit(1)
    write("starts.jsonl", {"argv": argv})
    print(json.dumps({"result": {"agent": {
        "name": argv[2], "status": "idle",
        "transcript_path": os.path.join(root, argv[2] + ".jsonl")}}}))
elif argv[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"ok": True, "status": "idle"}}))
elif argv[:2] in (["agent", "prompt"], ["agent", "send-keys"]):
    name = argv[2]
    submitting = argv[:2] == ["agent", "send-keys"]
    if not submitting:
        prompt_text = argv[3] if len(argv) > 3 else ""
        open(os.path.join(root, name + ".prompt"), "w", encoding="utf-8").write(prompt_text)
        stall_path = os.path.join(root, "stalled_prompts")
        stalled = int(open(stall_path).read()) if os.path.exists(stall_path) else 0
        if stalled < int(os.environ.get("FAKE_STALL_PROMPTS", "0")):
            # The composer accepted the text but never submitted it: the prompt
            # is left sitting on screen and no lifecycle change is observed.
            open(stall_path, "w").write(str(stalled + 1))
            sys.stderr.write(json.dumps({"error": {"code": "agent_prompt_stalled"}}))
            sys.exit(1)
    open(os.path.join(root, name + ".entered"), "w").close()
    if os.environ.get("FAKE_OMIT_MARKER") or os.environ.get("FAKE_DELAY_MARKER"):
        print(json.dumps({"result": {"ok": True}}))
        raise SystemExit(0)
    route = "claude" if "claude" in name else "omp"
    marker = "MAESTRO_CLAUDE_RECEIPT_OK" if route == "claude" else "MAESTRO_OMP_RECEIPT_OK"
    record = {
        "event_type": "result" if route == "claude" else "message_end",
        "subtype": "success",
        "role": "assistant",
        "stop_reason": "stop",
        "is_error": False,
        "text": marker,
        "exit_code": 0,
        "model": os.environ.get("FAKE_REPORTED_MODEL", "reported-model"),
        "session_id": "11111111-1111-4111-8111-111111111111",
    }
    with open(os.path.join(root, name + ".jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    print(json.dumps({"result": {"ok": True}}))
elif argv[:2] == ["pane", "read"]:
    pane_id = argv[2]
    starts_path = os.path.join(root, "starts.jsonl")
    starts = [json.loads(line)["argv"] for line in open(starts_path, encoding="utf-8")]
    name = next(
        start[2] for start in reversed(starts)
        if start[start.index("--pane") + 1] == pane_id)
    prompt_path = os.path.join(root, name + ".prompt")
    entered = os.path.join(root, name + ".entered")
    if os.path.exists(prompt_path) and not os.path.exists(entered):
        print(json.dumps({"result": {"text": open(prompt_path, encoding="utf-8").read()}}))
    elif os.environ.get("FAKE_DELAY_MARKER") and os.path.exists(entered):
        route = "claude" if "claude" in name else "omp"
        marker = "MAESTRO_CLAUDE_RECEIPT_OK" if route == "claude" else "MAESTRO_OMP_RECEIPT_OK"
        print(json.dumps({"result": {"text": marker}}))
    else:
        print(json.dumps({"result": {}}))
elif argv[:2] == ["pane", "send-keys"]:
    open(os.path.join(root, "launched"), "a").write(argv[2] + "\n")
    print(json.dumps({"result": {"ok": True}}))
elif argv[:2] == ["pane", "close"]:
    if os.environ.get("FAKE_CLOSE_FAIL"):
        sys.stderr.write("close_failed\n")
        sys.exit(1)
    with open(os.path.join(root, "closed"), "a", encoding="utf-8") as handle:
        handle.write(argv[2] + "\n")
    print(json.dumps({"result": {"ok": True}}))
elif argv[:2] == ["agent", "get"]:
    starts_path = os.path.join(root, "starts.jsonl")
    starts = [json.loads(line)["argv"] for line in open(starts_path, encoding="utf-8")]
    start = next(start for start in reversed(starts) if start[2] == argv[2])
    pane_id = start[start.index("--pane") + 1]
    closed_path = os.path.join(root, "closed")
    if os.path.exists(closed_path) and pane_id in open(closed_path, encoding="utf-8").read().split():
        print(json.dumps({"result": {}}))
    else:
        print(json.dumps({"result": {"agent": {"interactive_ready": True}}}))
else:
    print(json.dumps({"result": {}}))
'''


class RouteAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cwd = self.root / "repo"
        self.cwd.mkdir()
        self.herdr = self.root / "herdr"
        self.herdr.write_text(FAKE_HERDR, encoding="utf-8")
        self.herdr.chmod(0o755)
        self.omp = self.root / "omp"
        self.claude = self.root / "claude"
        self.omp.write_text("#!/bin/sh\necho 17.3.4\n", encoding="utf-8")
        self.claude.write_text("#!/bin/sh\necho 2.1.232\n", encoding="utf-8")
        self.omp.chmod(0o755)
        self.claude.chmod(0o755)
        self._before = dict(os.environ)
        os.environ["FAKE_ADMIT_ROOT"] = str(self.root)
        os.environ["FAKE_HERDR_CWD"] = str(self.cwd)
        os.environ.pop("FAKE_PANE_CWD", None)
        os.environ.pop("FAKE_OMIT_MARKER", None)
        os.environ.pop("FAKE_DELAY_MARKER", None)
        os.environ.pop("FAKE_CLOSE_FAIL", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._before)
        self._tmp.cleanup()

    def test_read_commands_accept_raw_text_output(self) -> None:
        # `herdr agent read` / `pane read` print the snapshot as raw text; they
        # have no JSON output mode. Rejecting that as PROTOCOL_INVALID_JSON
        # blinds the composer-visibility wait and the receipt scan.
        script = self.root / "text-herdr"
        script.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'MAESTRO_CLAUDE_RECEIPT_OK'\n",
            encoding="utf-8")
        script.chmod(0o755)
        payload = ra._herdr(script, "agent", "read", "n", "--source", "visible")
        self.assertIn("MAESTRO_CLAUDE_RECEIPT_OK", ra._pane_text(payload))
        payload = ra._herdr(script, "pane", "read", "w1:p2")
        self.assertIn("MAESTRO_CLAUDE_RECEIPT_OK", ra._pane_text(payload))
        # Every other command still has to speak JSON.
        with self.assertRaisesRegex(ra.AdmissionError, "PROTOCOL_INVALID_JSON"):
            ra._herdr(script, "pane", "list")

    def test_a_wrapped_prompt_echo_is_not_a_reply(self) -> None:
        # The composer echoes the prompt, and the admission prompt contains the
        # marker. The terminal wraps and decorates that echo, so an exact-match
        # strip leaves it in place and the echo reads as a finished turn --
        # which closes the pane while the agent is still answering.
        marker = "MAESTRO_CLAUDE_RECEIPT_OK"
        prompt = ra.FIRST_PROMPT.format(marker=marker)
        wrapped = (
            "> Reply with exactly MAESTRO_CLAUDE_RECEIPT_OK\n"
            "  and nothing else.\n"
            "\n"
            "  esc to interrupt\n")
        self.assertEqual(ra._first_text([{"text": wrapped}], marker, prompt), "")
        answered = wrapped + "\n MAESTRO_CLAUDE_RECEIPT_OK\n"
        self.assertEqual(
            ra._first_text([{"text": answered}], marker, prompt), marker)

    def test_agent_start_waits_for_a_settled_shell(self) -> None:
        # One ready snapshot can land in the gap before login hooks spawn their
        # own foreground processes, which makes Herdr report agent_pane_busy.
        polls = []

        def call(*args, timeout=None):
            polls.append(args)
            return {"result": {"process_info": {
                "shell_pid": 1, "foreground_process_group_id": 1,
                "foreground_processes": [{"name": "zsh", "pid": 1}]}}}

        ra._wait_for_available_shell(call, "w1:p2", timeout_s=5.0)
        self.assertGreaterEqual(len(polls), 5)

    def test_a_flickering_shell_restarts_the_settle_count(self) -> None:
        ready = {"result": {"process_info": {
            "shell_pid": 1, "foreground_process_group_id": 1,
            "foreground_processes": [{"name": "zsh", "pid": 1}]}}}
        busy = {"result": {"process_info": {
            "shell_pid": 1, "foreground_process_group_id": 2,
            "foreground_processes": [
                {"name": "security", "pid": 2}, {"name": "zsh", "pid": 1}]}}}
        # Ready, ready, then a login hook appears: the count must restart.
        replies = [ready, ready, busy] + [ready] * 8
        seen = []

        def call(*args, timeout=None):
            seen.append(args)
            return replies[len(seen) - 1]

        ra._wait_for_available_shell(call, "w1:p2", timeout_s=5.0)
        self.assertEqual(len(seen), 8)

    def spec(self, route: str) -> ra.RouteCaptureSpec:
        return ra.RouteCaptureSpec(
            route=route,
            cwd=self.cwd,
            herdr=self.herdr,
            binary=self.omp if route == "omp" else self.claude,
            # Both routes carry a configured model: omp keeps the model out of
            # its argv (the pm-profile selects it) but the receipt still records
            # which model the caller asked for. `--model` omission for omp is
            # covered by the argv contract tests in test_step7_launcher.
            model="test-model",
            effort="high" if route == "claude" else "",
            profile="test" if route == "omp" else None,
            session_dir=self.root / "session" / route,
            timeout_s=5.0,
            startup_settle_s=0.0,
        )

    def test_capture_proves_cwd_continuity_and_cancel_for_both_routes(self):
        for route in ("omp", "claude"):
            with self.subTest(route=route):
                os.environ.pop("closed", None)
                for name in ("closed", "argv.jsonl", "starts.jsonl", "pane_seq", "launched"):
                    leftover = self.root / name
                    if leftover.exists():
                        leftover.unlink()
                receipt = ra.capture_route(self.spec(route))
                self.assertEqual(receipt["route"], route)
                self.assertTrue(receipt["continuity_proven"])
                self.assertTrue(receipt["visible_pane_cwd_verified"])
                self.assertTrue(receipt["cancellation_clean"])
                self.assertEqual(
                    receipt["first_turn"]["text"],
                    receipt["continuation_turn"]["text"])
                self.assertEqual(receipt["first_turn"]["exit_code"], 0)
                if route == "omp":
                    self.assertEqual(receipt["continuation_turn"]["continued_with"], "-c")
                else:
                    self.assertEqual(
                        receipt["continuation_turn"]["continued_with"], "--resume")
                    self.assertEqual(
                        receipt["session_id"],
                        "11111111-1111-4111-8111-111111111111")
                argv = [
                    entry["argv"] for entry in (
                        json.loads(line) for line in
                        (self.root / "argv.jsonl").read_text(
                            encoding="utf-8").splitlines())
                ]
                starts = [
                    (index, command) for index, command in enumerate(argv)
                    if command[:2] == ["agent", "start"]
                ]
                self.assertEqual(len(starts), 2)
                start_panes = []
                last_start = -1
                for index, command in starts:
                    self.assertIn("--pane", command)
                    pane_id = command[command.index("--pane") + 1]
                    self.assertEqual(command[command.index("--timeout") + 1], "180000")
                    wait_indexes = [
                        wait_index for wait_index, wait in enumerate(argv[:index])
                        if wait[:4] == ["pane", "process-info", "--pane", pane_id]
                        and wait_index > last_start
                    ]
                    self.assertTrue(wait_indexes)
                    self.assertLess(wait_indexes[-1], index)
                    start_panes.append(pane_id)
                    last_start = index
                self.assertEqual(len(set(start_panes)), len(starts))
                self.assertEqual(
                    [command[2] for _, command in starts],
                    ["admit-{}-first".format(route), "admit-{}-cont".format(route)])
                # Each start must be followed by the documented readiness gate
                # before its prompt is submitted to the agent composer.
                for name in ("admit-{}-first".format(route),
                             "admit-{}-cont".format(route)):
                    waits = [
                        index for index, command in enumerate(argv)
                        if command[:3] == ["agent", "wait", name]
                    ]
                    self.assertEqual(len(waits), 1)
                    self.assertEqual(
                        argv[waits[0]][:5],
                        ["agent", "wait", name, "--until", "idle"])
                    self.assertIn("--timeout", argv[waits[0]])
                    prompts = [
                        index for index, command in enumerate(argv)
                        if command[:3] == ["agent", "prompt", name]
                    ]
                    self.assertEqual(len(prompts), 1)
                    self.assertLess(waits[0], prompts[0])
                # A prompt that submits cleanly needs no key recovery.
                self.assertFalse(any(
                    command[:2] in (
                        ["pane", "run"], ["pane", "send-keys"],
                        ["agent", "send-keys"])
                    for command in argv))

    def test_a_stalled_prompt_is_submitted_with_enter_not_prompted_again(self):
        # Herdr reports `agent_prompt_stalled` when the composer took the text
        # but never submitted it. Prompting again would append to the line still
        # sitting there and send both halves as one garbled turn.
        os.environ["FAKE_STALL_PROMPTS"] = "1"
        receipt = ra.capture_route(self.spec("omp"))
        self.assertEqual(receipt["first_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        argv = [
            entry["argv"] for entry in (
                json.loads(line) for line in
                (self.root / "argv.jsonl").read_text(encoding="utf-8").splitlines())
        ]
        first = "admit-omp-first"
        prompts = [c for c in argv if c[:3] == ["agent", "prompt", first]]
        self.assertEqual(len(prompts), 1)
        keys = [c for c in argv if c[:3] == ["agent", "send-keys", first]]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0], ["agent", "send-keys", first, "enter"])
        self.assertLess(argv.index(prompts[0]), argv.index(keys[0]))

    def test_busy_agent_pane_retries_after_shell_recheck(self):
        os.environ["FAKE_BUSY_STARTS"] = "1"
        receipt = ra.capture_route(self.spec("omp"))
        self.assertEqual(receipt["first_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        self.assertEqual((self.root / "busy_starts").read_text(), "1")

    def test_delayed_transcript_uses_official_prompt_wait(self):
        os.environ["FAKE_DELAY_MARKER"] = "1"
        receipt = ra.capture_route(self.spec("omp"))
        self.assertEqual(receipt["first_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        self.assertEqual(
            receipt["continuation_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        prompt_argvs = [
            entry["argv"] for entry in (
                json.loads(line) for line in
                (self.root / "argv.jsonl").read_text(encoding="utf-8").splitlines())
            if entry["argv"][:2] == ["agent", "prompt"]
        ]
        self.assertEqual(len(prompt_argvs), 2)
        for argv in prompt_argvs:
            # `--wait` is what proves the composer accepted the prompt: without
            # it Herdr reports success even when the text is left unsubmitted.
            self.assertIn("--wait", argv)
            self.assertEqual(argv[argv.index("--until") + 1], "idle")
            self.assertGreater(int(argv[argv.index("--timeout") + 1]), 5000)

    def test_signed_capture_admits_and_is_not_a_copied_fixture(self):
        seed = receipt_crypto.generate_seed()
        destinations = {
            "omp": self.root / "state" / "omp.json",
            "claude": self.root / "state" / "claude.json",
        }
        written = ra.admit_routes(
            (self.spec("omp"), self.spec("claude")),
            destinations, route_seed=seed)
        self.assertEqual([item.route for item in written], ["omp", "claude"])
        self.assertFalse(any(item.reused for item in written))
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = receipt_crypto.seed_to_public_key(seed)
        admitted = load_admitted_routes(destinations, verify_keys=(key,))
        self.assertTrue(admitted.admits("omp"))
        self.assertTrue(admitted.admits("claude"))
        for route, path in destinations.items():
            self.assertNotEqual(path.read_bytes(), (fixtures / (route + ".json")).read_bytes())
            load_route_receipt(path, verify_keys=(key,))

    def test_second_admit_reuses_verified_bytes_and_does_not_recapture(self):
        seed = receipt_crypto.generate_seed()
        destinations = {"omp": self.root / "state" / "omp.json"}
        first = ra.admit_routes((self.spec("omp"),), destinations, route_seed=seed)
        starts = (self.root / "starts.jsonl").read_text(encoding="utf-8")
        second = ra.admit_routes((self.spec("omp"),), destinations, route_seed=seed)
        self.assertFalse(first[0].reused)
        self.assertTrue(second[0].reused)
        self.assertEqual(
            (self.root / "starts.jsonl").read_text(encoding="utf-8"), starts)

    def test_invalid_existing_receipt_is_not_overwritten(self):
        seed = receipt_crypto.generate_seed()
        path = self.root / "state" / "omp.json"
        path.parent.mkdir()
        path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ra.AdmissionError, "ROUTE_RECEIPT_EXISTS"):
            ra.admit_routes((self.spec("omp"),), {"omp": path}, route_seed=seed)
        self.assertEqual(path.read_text(encoding="utf-8"), "{}\n")

    def test_cwd_mismatch_writes_no_receipt(self):
        os.environ["FAKE_PANE_CWD"] = str(self.root / "other")
        path = self.root / "state" / "omp.json"
        with self.assertRaisesRegex(ra.AdmissionError, "ROUTE_CWD_UNPROVEN"):
            ra.admit_routes(
                (self.spec("omp"),), {"omp": path},
                route_seed=receipt_crypto.generate_seed())
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + ".sig").exists())

    def test_composer_text_is_not_a_receipt(self):
        prompt = ra.FIRST_PROMPT.format(marker="MAESTRO_CLAUDE_RECEIPT_OK")
        self.assertEqual(ra._first_text(
            [{"text": prompt}], "MAESTRO_CLAUDE_RECEIPT_OK", prompt), "")
        self.assertEqual(ra._first_text(
            [{"text": prompt + "\nMAESTRO_CLAUDE_RECEIPT_OK"}],
            "MAESTRO_CLAUDE_RECEIPT_OK", prompt),
            "MAESTRO_CLAUDE_RECEIPT_OK")

    def test_missing_marker_writes_no_receipt(self):
        os.environ["FAKE_OMIT_MARKER"] = "1"
        path = self.root / "state" / "omp.json"
        with self.assertRaises(ra.AdmissionError):
            ra.admit_routes(
                (self.spec("omp"),), {"omp": path},
                route_seed=receipt_crypto.generate_seed())
        self.assertFalse(path.exists())

    def test_failed_cancel_writes_no_receipt(self):
        os.environ["FAKE_CLOSE_FAIL"] = "1"
        path = self.root / "state" / "omp.json"
        with self.assertRaisesRegex(ra.AdmissionError, "LAUNCH_REFUSED|ROUTE_CANCELLATION"):
            ra.admit_routes(
                (self.spec("omp"),), {"omp": path},
                route_seed=receipt_crypto.generate_seed())
        self.assertFalse(path.exists())

    def test_provision_keys_are_0600_and_reusable(self):
        keys_dir = self.root / "keys"
        first = ra.provision_keys(keys_dir)
        mode = stat.S_IMODE(os.stat(keys_dir / "signing.seed").st_mode)
        self.assertEqual(mode, 0o600)
        env = ra.write_env_file(
            first, verify_key_env="MAESTRO_VERIFY_KEY",
            signing_seed_env="MAESTRO_SIGNING_SEED",
            route_verify_key_env="MAESTRO_ROUTE_VERIFY_KEY")
        self.assertTrue(env.is_file())
        second = ra.provision_keys(keys_dir)
        self.assertEqual(first.signing_seed, second.signing_seed)
        self.assertEqual(first.route_public, second.route_public)
        self.assertEqual(second.created, ())


class BootstrapCliTest(unittest.TestCase):
    def test_configured_bootstrap_writes_state_receipts_and_keys(self):
        from test_step10_cli import OperatorCliTest

        with tempfile.TemporaryDirectory() as tmp:
            helper = OperatorCliTest()
            fixture = helper._named_plan_configuration(Path(tmp))
            for route in ("omp", "claude"):
                path = fixture["state"] / "route-receipts" / (route + ".json")
                path.unlink()
            (Path(tmp) / "herdr").write_text(FAKE_HERDR, encoding="utf-8")
            (Path(tmp) / "herdr").chmod(0o755)
            (Path(tmp) / "omp").write_text("#!/bin/sh\necho 17.3.4\n", encoding="utf-8")
            (Path(tmp) / "claude").write_text("#!/bin/sh\necho 2.1.232\n", encoding="utf-8")
            (Path(tmp) / "omp").chmod(0o755)
            (Path(tmp) / "claude").chmod(0o755)
            output = io.StringIO()
            with helper._repository_cwd(fixture["repo"]), mock_env({
                    "FAKE_ADMIT_ROOT": tmp,
                    "FAKE_HERDR_CWD": str(fixture["repo"])}), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(["bootstrap"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"], "ROUTES_ADMITTED")
            keys = ra.provision_keys(fixture["state"] / "keys")
            admitted = load_admitted_routes(
                {route: Path(path) for route, path in payload["receipts"].items()},
                verify_keys=(keys.route_public,))
            self.assertTrue(admitted.admits("omp"))
            self.assertTrue(admitted.admits("claude"))
            self.assertTrue((fixture["state"] / "keys" / "maestro.env").is_file())

            output = io.StringIO()
            with helper._repository_cwd(fixture["repo"]), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(["plan", "validate", "named"])
            self.assertEqual(code, 2)
            self.assertIn("AUTHORING_BLOCKED", output.getvalue())

    def test_the_authoring_lane_route_is_admitted_not_refused(self):
        """A route only the `author:` block names still gets a receipt.

        `_load_maestro_layout` requires every configured lane's route to have a
        receipt path, but bootstrap's capture specs were built from the
        execution and reviewer lanes alone. A deployment whose reviewer and
        execution share one route while the authoring lane rides another —
        which is the shape `maestro deliver` asks for — therefore refused
        `ROUTE_MODEL_UNCONFIGURED` for a route its own configuration demanded,
        and could never mint the receipt `run start` then insists on loading.
        """
        from test_step10_cli import OperatorCliTest

        with tempfile.TemporaryDirectory() as tmp:
            helper = OperatorCliTest()
            fixture = helper._named_plan_configuration(Path(tmp))
            config_path = fixture["repo"] / "adws" / "maestro.config.yaml"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["reviewer"]["route"] = "omp"
            config["reviewer"]["profile"] = "reviewer-profile"
            config["author"] = {
                "route": "claude",
                "model": "author-model",
                "effort": "high",
                "author_timeout_s": 600,
                "turn_timeout_s": 25,
                "poll_interval_s": 1,
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            for route in ("omp", "claude"):
                (fixture["state"] / "route-receipts" / (route + ".json")).unlink()
            (Path(tmp) / "herdr").write_text(FAKE_HERDR, encoding="utf-8")
            (Path(tmp) / "herdr").chmod(0o755)
            (Path(tmp) / "omp").write_text(
                "#!/bin/sh\necho 17.3.4\n", encoding="utf-8")
            (Path(tmp) / "claude").write_text(
                "#!/bin/sh\necho 2.1.232\n", encoding="utf-8")
            (Path(tmp) / "omp").chmod(0o755)
            (Path(tmp) / "claude").chmod(0o755)
            output = io.StringIO()
            with helper._repository_cwd(fixture["repo"]), mock_env({
                    "FAKE_ADMIT_ROOT": tmp,
                    "FAKE_HERDR_CWD": str(fixture["repo"])}), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(["bootstrap"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"], "ROUTES_ADMITTED")
            self.assertEqual(sorted(payload["routes"]), ["claude", "omp"])
            keys = ra.provision_keys(fixture["state"] / "keys")
            admitted = load_admitted_routes(
                {route: Path(path)
                 for route, path in payload["receipts"].items()},
                verify_keys=(keys.route_public,))
            self.assertTrue(admitted.admits("claude"))
            self.assertTrue(admitted.admits("omp"))

    def test_a_route_no_lane_names_is_still_refused(self):
        """The refusal survives; only the authoring lane stops triggering it."""
        from test_step10_cli import OperatorCliTest

        with tempfile.TemporaryDirectory() as tmp:
            helper = OperatorCliTest()
            fixture = helper._named_plan_configuration(Path(tmp))
            config_path = fixture["repo"] / "adws" / "maestro.config.yaml"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["reviewer"]["route"] = "omp"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            for route in ("omp", "claude"):
                (fixture["state"] / "route-receipts" / (route + ".json")).unlink()
            output = io.StringIO()
            with helper._repository_cwd(fixture["repo"]), \
                    contextlib.redirect_stdout(output):
                code = maestro.main(["bootstrap"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(payload["outcome"], "ROUTE_ADMISSION_FAILED")
            self.assertEqual(payload["detail"], "ROUTE_MODEL_UNCONFIGURED:claude")


@contextlib.contextmanager
def mock_env(values):
    before = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
