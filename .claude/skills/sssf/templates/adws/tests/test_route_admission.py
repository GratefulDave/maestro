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
from unittest import mock
from dataclasses import replace
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _path in (str(ADWS), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import contextlib
import io

import maestro
from adw_modules import launcher
from adw_modules import receipt_crypto
from adw_modules import route_admission as ra
from adw_modules.route_receipts import load_admitted_routes, load_route_receipt


FAKE_HERDR = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
root = os.environ["FAKE_ADMIT_ROOT"]
cwd = os.environ.get("FAKE_HERDR_CWD", "")
def write(name, payload):
    with open(os.path.join(root, name), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
write("argv.jsonl", {"argv": argv})

# The monotonic per-pane counter real herdr publishes on `pane get` (observed
# 12169 on a live pane). `launcher.submit_agent_prompt` accepts nothing else as
# proof that a composer took a prompt: a pane holding an unsubmitted `@<path>`
# reports `idle` exactly like one that consumed it, and only the revision tells
# them apart. A fake without the key models a pane that can never accept
# anything, which is how this suite went blind to the launcher path that
# blocked two production runs at 0 turns on 2026-08-18.
def rev_path(pane):
    return os.path.join(root, "rev_" + pane.replace(":", "_"))

def revision(pane):
    path = rev_path(pane)
    return int(open(path).read()) if os.path.exists(path) else 12169

def bump(pane):
    nxt = revision(pane) + 1
    open(rev_path(pane), "w").write(str(nxt))

def pane_of(name):
    starts_path = os.path.join(root, "starts.jsonl")
    if os.path.exists(starts_path):
        for line in reversed(open(starts_path, encoding="utf-8").read().splitlines()):
            start = json.loads(line)["argv"]
            if start[2] == name and "--pane" in start:
                return start[start.index("--pane") + 1]
    return "w1:p2"

def name_of(pane):
    starts_path = os.path.join(root, "starts.jsonl")
    if os.path.exists(starts_path):
        for line in reversed(open(starts_path, encoding="utf-8").read().splitlines()):
            start = json.loads(line)["argv"]
            if "--pane" in start and start[start.index("--pane") + 1] == pane:
                return start[2]
    return ""

def record_turn(name):
    # The durable half of submission proof: a submitted prompt appends a
    # record to the session JSONL. A composer that swallowed the text appends
    # nothing, which is the only difference between the two panes.
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

def submit(name):
    # One accepted Enter: the turn starts, and its record lands now or trails.
    open(os.path.join(root, name + ".entered"), "w").close()
    bump(pane_of(name))
    if os.environ.get("FAKE_OMIT_MARKER"):
        return
    delay = int(os.environ.get("FAKE_DELAY_MARKER", "0"))
    if delay:
        # The transcript is written at TURN granularity, so it can trail the
        # Enter that started the turn. Count the observations it trails by.
        open(os.path.join(root, name + ".pending"), "w").write(str(delay))
        return
    record_turn(name)

def tick_pending(pane):
    name = name_of(pane)
    path = os.path.join(root, name + ".pending") if name else ""
    if not path or not os.path.exists(path):
        return
    left = int(open(path).read()) - 1
    if left > 0:
        open(path, "w").write(str(left))
        return
    os.remove(path)
    record_turn(name)
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
        # Real herdr refuses with a typed error envelope, and `_pane_gone`
        # accepts only the typed code as proof of absence. A fake that wrote
        # bare prose here could not express the one field production reads.
        sys.stderr.write(json.dumps({"error": {"code": "pane_not_found"}}))
        sys.exit(1)
    tick_pending(argv[2])
    claimed = os.environ.get("FAKE_PANE_CWD", cwd)
    pane = {"pane_id": "w1:p2", "cwd": claimed}
    if not os.environ.get("FAKE_PANE_WITHOUT_REVISION"):
        pane["revision"] = revision(argv[2])
    print(json.dumps({"result": {"pane": pane}}))
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
    # An agent that was never released still owns its name. Herdr keeps the
    # record when a run ends without closing its panes, which is the leftover
    # a fresh capture then collides with.
    taken_path = os.path.join(root, "taken_names")
    taken = [name for name in os.environ.get("FAKE_TAKEN_NAMES", "").split(",")
             if name]
    if os.path.exists(taken_path):
        taken += open(taken_path, encoding="utf-8").read().split()
    if argv[2] in taken:
        sys.stdout.write(json.dumps({"error": {
            "code": "agent_name_taken",
            "message": "agent name %s already in use" % argv[2]}}))
        sys.exit(1)
    open(taken_path, "a", encoding="utf-8").write(argv[2] + "\n")
    write("starts.jsonl", {"argv": argv})
    print(json.dumps({"result": {"agent": {
        "name": argv[2], "status": "idle",
        "transcript_path": os.path.join(root, argv[2] + ".jsonl")}}}))
elif argv[:2] == ["agent", "wait"]:
    print(json.dumps({"result": {"ok": True, "status": "idle"}}))
elif argv[:2] == ["agent", "send-keys"]:
    # The recovery scope. `agent prompt` is deliberately absent from this fake:
    # it is absent from the runtime, because `@` opens the composer's
    # file-completion popup and that popup eats the Enter `agent prompt` sends
    # atomically with the text.
    submit(argv[2])
    print(json.dumps({"result": {"ok": True}}))
elif argv[:2] == ["pane", "send-text"]:
    # The text lands in the composer. It is NOT submitted by this call, which
    # is the whole reason a separate Enter exists.
    name = name_of(argv[2])
    if name:
        open(os.path.join(root, name + ".prompt"), "w", encoding="utf-8").write(
            argv[3] if len(argv) > 3 else "")
        stall_path = os.path.join(root, "stalled_prompts")
        stalled = int(open(stall_path).read()) if os.path.exists(stall_path) else 0
        if stalled < int(os.environ.get("FAKE_STALL_PROMPTS", "0")):
            # The composer never took the text: nothing on screen to press
            # Enter at, and no lifecycle change observed.
            open(stall_path, "w").write(str(stalled + 1))
            os.remove(os.path.join(root, name + ".prompt"))
            sys.stderr.write(json.dumps({"error": {"code": "agent_prompt_stalled"}}))
            sys.exit(1)
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
    # `esc` dismisses the completion popup and submits nothing. Only Enter
    # submits, and only at a pane whose composer is holding text.
    key = argv[3] if len(argv) > 3 else ""
    name = name_of(argv[2])
    if key == "enter" and name and os.path.exists(
            os.path.join(root, name + ".prompt")):
        submit(name)
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
        # Herdr reports which pane an agent occupies; a name that resolves to
        # somebody else's pane is what sends prompt text to the wrong shell.
        reported = os.environ.get("FAKE_AGENT_PANE") or pane_id
        print(json.dumps({"result": {"agent": {
            "interactive_ready": True, "pane_id": reported,
            "agent_status": "idle", "status": None}}}))
else:
    print(json.dumps({"result": {}}))
"""


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
        os.environ.pop("FAKE_TAKEN_NAMES", None)
        os.environ.pop("FAKE_AGENT_PANE", None)

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
            "#!/bin/sh\nprintf '%s\\n' 'MAESTRO_CLAUDE_RECEIPT_OK'\n", encoding="utf-8"
        )
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
            "  esc to interrupt\n"
        )
        self.assertEqual(ra._first_text([{"text": wrapped}], marker, prompt), "")
        answered = wrapped + "\n MAESTRO_CLAUDE_RECEIPT_OK\n"
        self.assertEqual(ra._first_text([{"text": answered}], marker, prompt), marker)

    def test_agent_start_waits_for_a_settled_shell(self) -> None:
        # One ready snapshot can land in the gap before login hooks spawn their
        # own foreground processes, which makes Herdr report agent_pane_busy.
        polls = []

        def call(*args, timeout=None):
            polls.append(args)
            return {
                "result": {
                    "process_info": {
                        "shell_pid": 1,
                        "foreground_process_group_id": 1,
                        "foreground_processes": [{"name": "zsh", "pid": 1}],
                    }
                }
            }

        ra._wait_for_available_shell(call, "w1:p2", timeout_s=5.0)
        self.assertGreaterEqual(len(polls), 5)

    def test_a_flickering_shell_restarts_the_settle_count(self) -> None:
        ready = {
            "result": {
                "process_info": {
                    "shell_pid": 1,
                    "foreground_process_group_id": 1,
                    "foreground_processes": [{"name": "zsh", "pid": 1}],
                }
            }
        }
        busy = {
            "result": {
                "process_info": {
                    "shell_pid": 1,
                    "foreground_process_group_id": 2,
                    "foreground_processes": [
                        {"name": "security", "pid": 2},
                        {"name": "zsh", "pid": 1},
                    ],
                }
            }
        }
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
            # its argv (the profile selects it) but the receipt still records
            # which model the caller asked for. `--model` omission for omp is
            # covered by the argv contract tests in test_step7_launcher.
            model="test-model",
            effort="high" if route == "claude" else "",
            profile="test" if route == "omp" else None,
            session_dir=self.root / "session" / route,
            timeout_s=5.0,
            startup_settle_s=0.0,
        )

    def test_missing_omp_profile_is_refused_before_herdr(self):
        with self.assertRaisesRegex(ra.AdmissionError, "OMP_PROFILE_REQUIRED"):
            ra.capture_route(replace(self.spec("omp"), profile=None))
        self.assertFalse((self.root / "argv.jsonl").exists())

    def test_capture_proves_cwd_continuity_and_cancel_for_both_routes(self):
        for route in ("omp", "claude"):
            with self.subTest(route=route):
                os.environ.pop("closed", None)
                for name in (
                    "closed",
                    "argv.jsonl",
                    "starts.jsonl",
                    "pane_seq",
                    "launched",
                ):
                    leftover = self.root / name
                    if leftover.exists():
                        leftover.unlink()
                receipt = ra.capture_route(self.spec(route))
                self.assertEqual(receipt["route"], route)
                self.assertTrue(receipt["continuity_proven"])
                self.assertTrue(receipt["visible_pane_cwd_verified"])
                self.assertTrue(receipt["cancellation_clean"])
                self.assertEqual(
                    receipt["first_turn"]["text"], receipt["continuation_turn"]["text"]
                )
                self.assertEqual(receipt["first_turn"]["exit_code"], 0)
                if route == "omp":
                    self.assertEqual(
                        receipt["continuation_turn"]["continued_with"], "-c"
                    )
                else:
                    self.assertEqual(
                        receipt["continuation_turn"]["continued_with"], "--resume"
                    )
                    self.assertEqual(
                        receipt["session_id"], "11111111-1111-4111-8111-111111111111"
                    )
                argv = [
                    entry["argv"]
                    for entry in (
                        json.loads(line)
                        for line in (self.root / "argv.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    )
                ]
                starts = [
                    (index, command)
                    for index, command in enumerate(argv)
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
                        wait_index
                        for wait_index, wait in enumerate(argv[:index])
                        if wait[:4] == ["pane", "process-info", "--pane", pane_id]
                        and wait_index > last_start
                    ]
                    self.assertTrue(wait_indexes)
                    self.assertLess(wait_indexes[-1], index)
                    start_panes.append(pane_id)
                    last_start = index
                self.assertEqual(len(set(start_panes)), len(starts))
                started_names = [command[2] for _, command in starts]
                for name, role in zip(started_names, ("first", "cont")):
                    prefix = "admit-{}-{}-".format(route, role)
                    self.assertTrue(
                        name.startswith(prefix),
                        "{!r} does not start with {!r}".format(name, prefix),
                    )
                    self.assertTrue(name[len(prefix) :])
                # Paste-then-Enter is the offer. Any `agent wait` is a
                # recovery round, not a second prompt — and it is not
                # guaranteed: this fake's Enter submits on the first press, so
                # the loop is never entered. Assert the shape of the waits that
                # DO happen rather than that any happens; the earlier
                # `assertGreaterEqual(len(waits), 1)` was asserting that
                # recovery is always needed, which is the opposite of what a
                # working first Enter looks like.
                for name in started_names:
                    waits = [
                        command
                        for command in argv
                        if command[:3] == ["agent", "wait", name]
                    ]
                    for wait in waits:
                        # Admission's turn is bounded by construction — one
                        # sentence in, one marker out — so its wait keeps a
                        # deadline. The lane path's does not; do not unify.
                        self.assertIn("--timeout", wait)
                submitted = [
                    command[-1] for command in argv if command[:2] == ["pane", "send-text"]
                ]
                self.assertEqual(len(submitted), 2)
                for prompt in submitted:
                    self.assertFalse(
                        prompt.startswith(launcher.CLAUDE_TEAM_PROMPT_PREFIX),
                    )
                    self.assertNotIn("/team", prompt)
                # Paste then Enter is the submission path. A second prompt is not.
                self.assertFalse(any(command[:2] == ["pane", "run"] for command in argv))
                self.assertFalse(any(command[:2] == ["agent", "prompt"] for command in argv))
                self.assertTrue(
                    any(command[:2] == ["pane", "send-keys"] and command[-1] == "enter"
                        for command in argv)
                )

    def test_a_stalled_prompt_is_submitted_with_enter_not_prompted_again(self):
        # Herdr reports `agent_prompt_stalled` when the composer took the text
        # but never submitted it. Prompting again would append to the line still
        # sitting there and send both halves as one garbled turn.
        os.environ["FAKE_STALL_PROMPTS"] = "1"
        receipt = ra.capture_route(self.spec("omp"))
        self.assertEqual(receipt["first_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        argv = [
            entry["argv"]
            for entry in (
                json.loads(line)
                for line in (self.root / "argv.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        ]
        first = next(
            c[2]
            for c in argv
            if c[:2] == ["agent", "start"] and c[2].startswith("admit-omp-first-")
        )
        marker_prompt = ra.FIRST_PROMPT.format(marker="MAESTRO_OMP_RECEIPT_OK")
        offers = [c for c in argv if c[:2] == ["pane", "send-text"]
                  and c[-1] == marker_prompt]
        self.assertEqual(len(offers), 1)
        self.assertFalse(any(c[:3] == ["agent", "prompt", first] for c in argv))
        keys = [c for c in argv if c[:2] == ["pane", "send-keys"] and c[-1] == "enter"]
        self.assertGreaterEqual(len(keys), 1)
        self.assertLess(argv.index(offers[0]), argv.index(keys[0]))

    def _argv_log(self) -> list:
        text = (self.root / "argv.jsonl").read_text(encoding="utf-8")
        entries = [json.loads(line) for line in text.splitlines()]
        return [entry["argv"] for entry in entries]

    def _turn_text(self, receipt, key: str = "first_turn") -> str:
        turn = receipt[key]
        assert isinstance(turn, dict)
        return str(turn["text"])

    def test_a_leftover_agent_does_not_refuse_the_next_capture(self):
        # D7: a blocked run left its panes behind, so its agents stayed
        # registered, and the next bootstrap was refused before it could do
        # anything -- `agent start` answered `agent_name_taken` for a name the
        # previous capture had already burned. The fake keeps every accepted
        # name registered, which is exactly that leftover.
        first = ra.capture_route(self.spec("omp"))
        self.assertEqual(self._turn_text(first), "MAESTRO_OMP_RECEIPT_OK")
        for leftover in (
            "closed",
            "argv.jsonl",
            "starts.jsonl",
            "pane_seq",
            "launched",
        ):
            path = self.root / leftover
            if path.exists():
                path.unlink()
        second = ra.capture_route(self.spec("omp"))
        self.assertEqual(self._turn_text(second), "MAESTRO_OMP_RECEIPT_OK")

    def test_a_capture_never_reuses_a_name_another_run_could_hold(self):
        # Every start name must be unique across runs by construction, not by
        # the leftover happening to be gone. Two captures back to back must not
        # request a single name twice.
        names = []
        for _ in range(2):
            for leftover in (
                "closed",
                "argv.jsonl",
                "starts.jsonl",
                "pane_seq",
                "launched",
            ):
                path = self.root / leftover
                if path.exists():
                    path.unlink()
            ra.capture_route(self.spec("omp"))
            names.extend(
                command[2]
                for command in self._argv_log()
                if command[:2] == ["agent", "start"]
            )
        self.assertEqual(len(names), 4)
        self.assertEqual(len(set(names)), 4)

    def test_a_taken_name_is_stepped_around_never_taken_back(self):
        # The leftover belongs to a run this one knows nothing about, and Herdr
        # reports a healthy agent between turns as `idle` exactly like an
        # abandoned one. So the collision is answered with a different name and
        # never with a rename, a close, or any other removal of the record.
        names = iter(
            [
                "admit-omp-first-" + "aa" * 4,
                "admit-omp-first-" + "bb" * 4,
                "admit-omp-cont-" + "cc" * 4,
                "admit-omp-cont-" + "dd" * 4,
            ]
        )
        taken = "admit-omp-first-" + "aa" * 4
        os.environ["FAKE_TAKEN_NAMES"] = taken
        with mock.patch.object(
            ra, "_admission_agent_name", lambda route, *, continuing: next(names)
        ):
            receipt = ra.capture_route(self.spec("omp"))
        self.assertEqual(self._turn_text(receipt), "MAESTRO_OMP_RECEIPT_OK")
        argv = self._argv_log()
        starts = [c[2] for c in argv if c[:2] == ["agent", "start"]]
        # The taken name was asked for once, refused, and stepped around.
        self.assertEqual(starts[:2], [taken, "admit-omp-first-" + "bb" * 4])
        self.assertFalse([c for c in argv if c[:2] == ["agent", "rename"]])
        self.assertFalse([c for c in argv if c[:2] == ["agent", "stop"]])
        self.assertFalse(
            [c for c in argv if c[:2] == ["agent", "prompt"] and c[2] == taken]
        )

    def test_the_taken_name_refusal_is_read_as_a_code_not_as_prose(self):
        # §1.2: the retry keys on Herdr's typed `error.code`. A refusal whose
        # message happens to mention the name is not a taken name.
        script = self.root / "coded-herdr"
        script.write_text(
            "#!/bin/sh\n"
            "printf '%s' "
            '\'{"error":{"code":"agent_name_taken","message":"x"}}\'\n'
            "exit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        with self.assertRaises(ra.AdmissionError) as caught:
            ra._herdr(script, "agent", "start", "n")
        self.assertEqual(caught.exception.code, "agent_name_taken")

        prose = self.root / "prose-herdr"
        prose.write_text(
            "#!/bin/sh\nprintf '%s' 'agent_name_taken'\nexit 1\n", encoding="utf-8"
        )
        prose.chmod(0o755)
        with self.assertRaises(ra.AdmissionError) as caught:
            ra._herdr(prose, "agent", "start", "n")
        self.assertEqual(caught.exception.code, "")

    def test_herdr_error_code_reads_the_typed_field_only(self):
        self.assertEqual(
            ra.herdr_error_code(
                '{"error":{"code":"agent_name_taken","message":"x"},'
                '"id":"cli:agent:start"}'
            ),
            "agent_name_taken",
        )
        for text in (
            "agent_name_taken",
            "",
            "[]",
            '{"error":"agent_name_taken"}',
            '{"error":{"message":"agent_name_taken"}}',
        ):
            self.assertEqual(ra.herdr_error_code(text), "")

    def test_the_node_launcher_name_is_left_deterministic(self):
        # Measured premise: a node agent name is `maestro-` + sha256 of
        # `{run_id}-{node_id}-{attempt_no}`, and `run_id` is a fresh uuid4 per
        # run, so it cannot collide across runs. Adding a discriminator there
        # would churn a name post-mortems key on for no defect. Route admission
        # is the only surface whose name is run-independent.
        first = launcher._agent_name("run1-node_a-1")
        self.assertEqual(first, launcher._agent_name("run1-node_a-1"))
        self.assertTrue(first.startswith("maestro-"))
        self.assertNotEqual(first, launcher._agent_name("run2-node_a-1"))

    def test_the_prompt_is_refused_when_the_name_resolves_to_another_pane(self):
        # `herdr agent prompt <TARGET> <TEXT>` types the text wherever Herdr
        # resolves TARGET. A name bound to a pane this capture did not create
        # sends `Reply with exactly MAESTRO_OMP_RECEIPT_OK...` into whatever
        # shell sits there -- the operator's own command line. Refuse before
        # submitting anything.
        os.environ["FAKE_AGENT_PANE"] = "w9:p9"
        with self.assertRaises(ra.AdmissionError) as caught:
            ra.capture_route(self.spec("omp"))
        self.assertIn("ROUTE_AGENT_TARGET_MISMATCH", str(caught.exception))
        argv = self._argv_log()
        self.assertFalse(
            [c for c in argv if c[:2] == ["agent", "prompt"]],
            "prompt text was submitted to an unproven target",
        )

    def test_busy_agent_pane_retries_after_shell_recheck(self):
        os.environ["FAKE_BUSY_STARTS"] = "1"
        receipt = ra.capture_route(self.spec("omp"))
        self.assertEqual(receipt["first_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        self.assertEqual((self.root / "busy_starts").read_text(), "1")

    def test_delayed_transcript_uses_official_prompt_wait(self):
        """A transcript that flushes late is not an unsubmitted prompt.

        The premise is unchanged and is the whole point: the record proving
        submission is written at TURN granularity, so it trails the Enter that
        started the turn, and reading its momentary absence as "the composer
        refused the text" refuses a prompt that landed perfectly.

        What changed is the sequence being asserted. This test used to require
        two `agent prompt --wait --until idle` calls, and `agent prompt` no
        longer exists anywhere in the runtime — it was replaced deliberately,
        because it submits its encoded Enter atomically with the text and `@`
        opens the composer's file-completion popup, which eats exactly that
        Enter. The measured replacement is three separate calls: `pane
        send-text` puts the text in the composer, `pane send-keys esc` closes
        the popup, and `pane send-keys enter` submits. DO NOT REVIVE
        `agent prompt` to make an argv assertion shorter.
        """
        os.environ["FAKE_DELAY_MARKER"] = "3"
        receipt = ra.capture_route(self.spec("omp"))
        self.assertEqual(receipt["first_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        self.assertEqual(receipt["continuation_turn"]["text"], "MAESTRO_OMP_RECEIPT_OK")
        argv = [
            entry["argv"]
            for entry in (
                json.loads(line)
                for line in (self.root / "argv.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        ]
        self.assertEqual(
            [call for call in argv if call[:2] == ["agent", "prompt"]],
            [],
            "`agent prompt` submits its Enter into the completion popup",
        )
        keys = [
            (call[1], call[3] if len(call) > 3 else "")
            for call in argv
            if call[:2] in (["pane", "send-text"], ["pane", "send-keys"])
        ]
        # Two turns, each offered as text, then popup dismissal, then Enter.
        self.assertEqual(
            keys,
            [
                ("send-text", ra.FIRST_PROMPT.format(
                    marker="MAESTRO_OMP_RECEIPT_OK")),
                ("send-keys", "esc"),
                ("send-keys", "enter"),
                ("send-text", ra.CONTINUATION_PROMPT),
                ("send-keys", "esc"),
                ("send-keys", "enter"),
            ],
        )

    def test_signed_capture_admits_and_is_not_a_copied_fixture(self):
        seed = receipt_crypto.generate_seed()
        destinations = {
            "omp": self.root / "state" / "omp.json",
            "claude": self.root / "state" / "claude.json",
        }
        written = ra.admit_routes(
            (self.spec("omp"), self.spec("claude")), destinations, route_seed=seed
        )
        self.assertEqual([item.route for item in written], ["omp", "claude"])
        self.assertFalse(any(item.reused for item in written))
        fixtures = Path(__file__).parent / "fixtures" / "step8"
        key = receipt_crypto.seed_to_public_key(seed)
        admitted = load_admitted_routes(destinations, verify_keys=(key,))
        self.assertTrue(admitted.admits("omp"))
        self.assertTrue(admitted.admits("claude"))
        for route, path in destinations.items():
            self.assertNotEqual(
                path.read_bytes(), (fixtures / (route + ".json")).read_bytes()
            )
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
            (self.root / "starts.jsonl").read_text(encoding="utf-8"), starts
        )

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
                (self.spec("omp"),),
                {"omp": path},
                route_seed=receipt_crypto.generate_seed(),
            )
        self.assertFalse(path.exists())
        self.assertFalse(Path(str(path) + ".sig").exists())

    def test_composer_text_is_not_a_receipt(self):
        prompt = ra.FIRST_PROMPT.format(marker="MAESTRO_CLAUDE_RECEIPT_OK")
        self.assertEqual(
            ra._first_text([{"text": prompt}], "MAESTRO_CLAUDE_RECEIPT_OK", prompt), ""
        )
        self.assertEqual(
            ra._first_text(
                [{"text": prompt + "\nMAESTRO_CLAUDE_RECEIPT_OK"}],
                "MAESTRO_CLAUDE_RECEIPT_OK",
                prompt,
            ),
            "MAESTRO_CLAUDE_RECEIPT_OK",
        )

    def test_missing_marker_writes_no_receipt(self):
        os.environ["FAKE_OMIT_MARKER"] = "1"
        path = self.root / "state" / "omp.json"
        with self.assertRaises(ra.AdmissionError):
            ra.admit_routes(
                (self.spec("omp"),),
                {"omp": path},
                route_seed=receipt_crypto.generate_seed(),
            )
        self.assertFalse(path.exists())

    def test_failed_cancel_writes_no_receipt(self):
        os.environ["FAKE_CLOSE_FAIL"] = "1"
        path = self.root / "state" / "omp.json"
        with self.assertRaisesRegex(
            ra.AdmissionError, "LAUNCH_REFUSED|ROUTE_CANCELLATION"
        ):
            ra.admit_routes(
                (self.spec("omp"),),
                {"omp": path},
                route_seed=receipt_crypto.generate_seed(),
            )
        self.assertFalse(path.exists())

    def test_provision_keys_are_0600_and_reusable(self):
        keys_dir = self.root / "keys"
        first = ra.provision_keys(keys_dir)
        mode = stat.S_IMODE(os.stat(keys_dir / "signing.seed").st_mode)
        self.assertEqual(mode, 0o600)
        env = ra.write_env_file(
            first,
            verify_key_env="MAESTRO_VERIFY_KEY",
            signing_seed_env="MAESTRO_SIGNING_SEED",
            route_verify_key_env="MAESTRO_ROUTE_VERIFY_KEY",
        )
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
            (Path(tmp) / "claude").write_text(
                "#!/bin/sh\necho 2.1.232\n", encoding="utf-8"
            )
            (Path(tmp) / "omp").chmod(0o755)
            (Path(tmp) / "claude").chmod(0o755)
            output = io.StringIO()
            with (
                helper._repository_cwd(fixture["repo"]),
                mock_env(
                    {"FAKE_ADMIT_ROOT": tmp, "FAKE_HERDR_CWD": str(fixture["repo"])}
                ),
                contextlib.redirect_stdout(output),
            ):
                code = maestro.main(["bootstrap"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"], "ROUTES_ADMITTED")
            keys = ra.provision_keys(fixture["state"] / "keys")
            admitted = load_admitted_routes(
                {route: Path(path) for route, path in payload["receipts"].items()},
                verify_keys=(keys.route_public,),
            )
            self.assertTrue(admitted.admits("omp"))
            self.assertTrue(admitted.admits("claude"))
            # The env-file split, end to end through the real verb rather than
            # through `write_env_file` alone: what an operator sources is what
            # bootstrap actually wrote, and `plan gate` refuses on the reviewer
            # variable being set, so the author file carrying it is the whole
            # defect. Both paths are reported so neither has to be guessed at.
            author_env = fixture["state"] / "keys" / "maestro.env"
            reviewer_env = fixture["state"] / "keys" / "reviewer-hmac.env"
            self.assertTrue(author_env.is_file())
            self.assertTrue(reviewer_env.is_file())
            self.assertEqual(payload["env_file"], str(author_env))
            self.assertEqual(payload["reviewer_env_file"], str(reviewer_env))
            author_body = author_env.read_text(encoding="ascii")
            self.assertNotIn("PLANCTL_REVIEWER_HMAC_KEY", author_body)
            self.assertNotIn(keys.reviewer_hmac.hex(), author_body)
            self.assertIn(
                "PLANCTL_REVIEWER_HMAC_KEY=" + keys.reviewer_hmac.hex(),
                reviewer_env.read_text(encoding="ascii"),
            )

            output = io.StringIO()
            with (
                helper._repository_cwd(fixture["repo"]),
                contextlib.redirect_stdout(output),
            ):
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
            (Path(tmp) / "omp").write_text("#!/bin/sh\necho 17.3.4\n", encoding="utf-8")
            (Path(tmp) / "claude").write_text(
                "#!/bin/sh\necho 2.1.232\n", encoding="utf-8"
            )
            (Path(tmp) / "omp").chmod(0o755)
            (Path(tmp) / "claude").chmod(0o755)
            output = io.StringIO()
            with (
                helper._repository_cwd(fixture["repo"]),
                mock_env(
                    {"FAKE_ADMIT_ROOT": tmp, "FAKE_HERDR_CWD": str(fixture["repo"])}
                ),
                contextlib.redirect_stdout(output),
            ):
                code = maestro.main(["bootstrap"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["outcome"], "ROUTES_ADMITTED")
            self.assertEqual(sorted(payload["routes"]), ["claude", "omp"])
            keys = ra.provision_keys(fixture["state"] / "keys")
            admitted = load_admitted_routes(
                {route: Path(path) for route, path in payload["receipts"].items()},
                verify_keys=(keys.route_public,),
            )
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
            with (
                helper._repository_cwd(fixture["repo"]),
                contextlib.redirect_stdout(output),
            ):
                code = maestro.main(["bootstrap"])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(payload["outcome"], "ROUTE_ADMISSION_FAILED")
            self.assertEqual(payload["detail"], "ROUTE_MODEL_UNCONFIGURED:claude")


class CancellationEvidenceTest(unittest.TestCase):
    """A failed absence probe is a missing observation, never absence.

    `_pane_gone` used to read ANY failed `pane get` as "gone", so herdr being
    unreachable during the probe counted as a proven cancellation. The failure
    on a path whose purpose is to produce evidence was caught and converted
    into the very evidence it failed to produce.
    """

    def test_unreadable_pane_is_not_proof_of_cancellation(self):
        def call(*argv, timeout=None):
            if argv[:2] == ("pane", "close"):
                return {"result": {"ok": True}}
            if argv[:2] == ("pane", "get"):
                raise ra.AdmissionError("LAUNCH_REFUSED:herdr timed out")
            return {"result": {}}

        with self.assertRaisesRegex(
            ra.AdmissionError, "ROUTE_CANCELLATION_UNPROVEN"
        ) as caught:
            ra._stop_agent(call, {"pane_id": "w1:p2", "name": "admit-omp"})
        # The refusal names the observation that failed, not just the
        # conclusion it blocked.
        self.assertIn("pane_unreadable", str(caught.exception))

    def test_typed_absence_still_proves_cancellation(self):
        def call(*argv, timeout=None):
            if argv[:2] == ("pane", "close"):
                return {"result": {"ok": True}}
            if argv[:2] == ("pane", "get"):
                raise ra.AdmissionError(
                    "LAUNCH_REFUSED:pane_not_found", "pane_not_found")
            return {"result": {}}

        ra._stop_agent(call, {"pane_id": "w1:p2", "name": "admit-omp"})

    def test_unreadable_agent_is_not_proof_of_absence(self):
        def call(*argv, timeout=None):
            raise ra.AdmissionError("LAUNCH_REFUSED:socket closed")

        gone, denial = ra._agent_gone(call, "admit-omp")
        self.assertFalse(gone)
        self.assertIn("agent_unreadable", denial)
        gone, denial = ra._pane_gone(call, "w1:p2")
        self.assertFalse(gone)

    def test_typed_agent_absence_is_still_absence(self):
        def call(*argv, timeout=None):
            raise ra.AdmissionError(
                "LAUNCH_REFUSED:agent_not_found", "agent_not_found")

        gone, denial = ra._agent_gone(call, "admit-omp")
        self.assertTrue(gone)
        self.assertEqual(denial, "")


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
