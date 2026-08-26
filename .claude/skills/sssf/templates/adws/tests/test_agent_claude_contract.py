"""Behavioral contracts for the owned direct-Claude process route."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_cc, agent_pi, agents
from adw_modules.data_types import (
    AgentCall,
    AgentConfig,
    GenericOutput,
    Phase,
    PhaseParams,
    PiRequest,
    PromptEngineering,
    SSSFConfig,
)


FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time

record = os.environ["FAKE_CLAUDE_ARGV"]
with open(record, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
prompt_record = os.environ.get("FAKE_CLAUDE_PROMPT")
stderr_bytes = int(os.environ.get("FAKE_CLAUDE_STDERR_BYTES", "0"))
if stderr_bytes:
    sys.stderr.write("x" * stderr_bytes)
    sys.stderr.flush()
child_record = os.environ.get("FAKE_CLAUDE_CHILD_PID")
if child_record:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(child_record, "w") as handle:
        handle.write(str(child.pid))
    while not os.path.exists(os.environ.get("FAKE_CLAUDE_CHILD_READY", "")):
        time.sleep(0.01)
prompt = sys.stdin.read()
if prompt_record:
    with open(prompt_record, "a") as handle:
        handle.write(prompt)
default = [
    {"type": "system", "subtype": "init", "session_id": "claude-session",
     "model": "claude-opus-5"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "done",
     "total_cost_usd": 0.02, "usage": {"input_tokens": 7, "output_tokens": 3}},
]
events = json.loads(os.environ.get("FAKE_CLAUDE_EVENTS", json.dumps(default)))
for event in events:
    print(json.dumps(event), flush=True)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


class DirectClaudeRouteContract(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.binary = self.root / "claude"
        self.binary.write_text(FAKE_CLAUDE)
        self.binary.chmod(0o755)
        self._before = dict(os.environ)
        os.environ["CLAUDE_PATH"] = str(self.binary)
        os.environ["FAKE_CLAUDE_ARGV"] = str(self.root / "argv.jsonl")
        os.environ["FAKE_CLAUDE_PROMPT"] = str(self.root / "prompt.txt")
        for key in (
            "FAKE_CLAUDE_EVENTS",
            "FAKE_CLAUDE_EXIT",
            "FAKE_CLAUDE_STDERR_BYTES",
            "FAKE_CLAUDE_CHILD_PID",
            "FAKE_CLAUDE_CHILD_READY",
        ):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._before)
        self._tmp.cleanup()

    def request(
        self,
        *,
        session_id: str = "logical-one",
        model: str = "opus",
        prompt: str = "work",
        tools=None,
        extensions=None,
    ) -> PiRequest:
        return PiRequest(
            prompt=prompt,
            system_prompt="system",
            model=model,
            thinking="high",
            session_id=session_id,
            session_dir=str(self.root / "sessions"),
            raw_output_path=str(self.root / "raw.jsonl"),
            tools=["Read"] if tools is None else tools,
            extensions=[] if extensions is None else extensions,
            cwd=str(self.root),
        )

    def argv_records(self) -> list[list]:
        return [
            json.loads(line)
            for line in (self.root / "argv.jsonl").read_text().splitlines()
        ]

    def argv(self) -> list:
        return self.argv_records()[-1]

    def events(self, rows: list[dict]) -> None:
        os.environ["FAKE_CLAUDE_EVENTS"] = json.dumps(rows)

    def success_events(
        self, *, model: str = "claude-opus-5", result: str = "done"
    ) -> list[dict]:
        return [
            {"type": "system", "session_id": "claude-session", "model": model},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": result}]},
            },
            {
                "type": "result",
                "is_error": False,
                "result": result,
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        ]

    def test_admits_the_signed_opus_alias_and_records_the_reported_canonical_identity(
        self,
    ):
        result = agent_cc.run(self.request())

        argv = self.argv()
        self.assertEqual(result.session_id, "claude-session")
        self.assertEqual(result.model_ran, "claude/claude-opus-5")
        self.assertEqual(
            (result.usage.input_tokens, result.usage.output_tokens), (7, 3)
        )
        self.assertIn("--tools", argv)
        tools = argv.index("--tools")
        self.assertEqual(argv[tools + 1 : tools + 2], ["Read"])
        self.assertIn("--allowedTools", argv)
        allowed = argv.index("--allowedTools")
        self.assertEqual(argv[allowed + 1 : allowed + 2], ["Read"])
        self.assertIn("--disallowedTools", argv)
        disallowed = argv.index("--disallowedTools")
        self.assertEqual(argv[disallowed + 1 : disallowed + 2], ["mcp__*"])
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("work", argv)
        self.assertEqual(
            (self.root / "prompt.txt").read_text(),
            agent_cc.prepare_route_prompt_text("claude", "work"),
        )

    def test_refuses_a_real_model_substitution_even_when_the_text_is_successful(self):
        self.events(self.success_events(model="claude-sonnet-4"))

        with self.assertRaises(agent_pi.ModelBindingError) as caught:
            agent_cc.run(self.request())

        self.assertIn("opus", str(caught.exception))
        self.assertIn("claude-sonnet-4", str(caught.exception))

    def test_refuses_success_without_a_reported_model_identity(self):
        rows = self.success_events()
        del rows[0]["model"]
        self.events(rows)

        with self.assertRaises(agent_pi.ModelBindingError):
            agent_cc.run(self.request())

    def test_model_and_logical_session_rollovers_start_fresh_scoped_artifacts(self):
        request = self.request(session_id="logical-one")
        agent_cc.run(request)
        agent_cc.run(request)
        agent_cc.run(self.request(session_id="logical-one", model="claude-opus-5"))
        agent_cc.run(self.request(session_id="logical-two", model="opus"))
        calls = self.argv_records()

        self.assertNotIn("--resume", calls[0])
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], "claude-session")
        self.assertNotIn("--resume", calls[2])
        self.assertNotIn("--resume", calls[3])
        raw_files = sorted((self.root / "sessions").rglob("raw.jsonl"))
        self.assertEqual(len(raw_files), 3)
        self.assertTrue(all(path.read_text() for path in raw_files))

    def test_concurrent_logical_sessions_do_not_share_raw_streams_or_markers(self):
        errors = []

        def invoke(session_id: str) -> None:
            try:
                agent_cc.run(self.request(session_id=session_id, prompt=session_id))
            except BaseException as error:  # assertion belongs in the parent
                errors.append(error)

        first = threading.Thread(target=invoke, args=("logical-a",))
        second = threading.Thread(target=invoke, args=("logical-b",))
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(errors, [])
        calls = self.argv_records()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("-p" in call for call in calls))
        raw_files = sorted((self.root / "sessions").rglob("raw.jsonl"))
        markers = sorted((self.root / "sessions").rglob("marker.json"))
        self.assertEqual(len(raw_files), 2)
        self.assertEqual(len(markers), 2)
        self.assertTrue(
            all(
                [json.loads(line) for line in path.read_text().splitlines()]
                for path in raw_files
            )
        )

    def test_drains_large_stderr_before_accepting_a_success(self):
        os.environ["FAKE_CLAUDE_STDERR_BYTES"] = str(64 * 1024 + 1)

        result = agent_cc.run(self.request())

        self.assertEqual(result.text, "done")

    def test_descendants_holding_the_pipes_are_terminated_before_on_exit(self):
        child_pid = self.root / "child.pid"
        ready = self.root / "ready"
        os.environ["FAKE_CLAUDE_CHILD_PID"] = str(child_pid)
        os.environ["FAKE_CLAUDE_CHILD_READY"] = str(ready)
        observed = []

        def release_after_spawn(_pid: int) -> None:
            deadline = time.monotonic() + 2
            while not child_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            ready.touch()

        def exited(_pid: int) -> None:
            pid = int(child_pid.read_text())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    observed.append(pid)
                    return
                time.sleep(0.01)
            self.fail("owned descendant remained after on_exit")

        agent_cc.run(self.request(), on_spawn=release_after_spawn, on_exit=exited)
        self.assertEqual(len(observed), 1)

    def test_nonzero_exit_refuses_parseable_success_result(self):
        os.environ["FAKE_CLAUDE_EXIT"] = "9"

        with self.assertRaises(RuntimeError) as caught:
            agent_cc.run(self.request())

        self.assertIn("9", str(caught.exception))

    def test_error_result_refuses_parseable_success_text(self):
        rows = self.success_events()
        rows[-1]["is_error"] = True
        self.events(rows)

        with self.assertRaises(RuntimeError):
            agent_cc.run(self.request())

    def test_failed_stream_identity_is_never_persisted_for_resume(self):
        failed = self.success_events()
        failed[-1]["is_error"] = True
        self.events(failed)
        with self.assertRaises(RuntimeError):
            agent_cc.run(self.request())

        self.events(self.success_events())
        agent_cc.run(self.request())

        call = self.argv()
        self.assertNotIn("--resume", call)

    def test_missing_terminal_result_is_refused(self):
        self.events(self.success_events()[:-1])

        with self.assertRaises(RuntimeError) as caught:
            agent_cc.run(self.request())

        self.assertIn("terminal result", str(caught.exception))

    def test_result_event_must_remain_terminal(self):
        rows = self.success_events()
        rows.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "late"},
                    ]
                },
            }
        )
        self.events(rows)

        with self.assertRaises(RuntimeError) as caught:
            agent_cc.run(self.request())

        self.assertIn("terminal result", str(caught.exception))

    def test_primary_callback_failure_is_not_masked_by_a_failing_exit_callback(self):
        class EventFailure(RuntimeError):
            pass

        class ExitFailure(RuntimeError):
            pass

        def fail_event(_event: dict) -> None:
            raise EventFailure("event")

        def fail_exit(_pid: int) -> None:
            raise ExitFailure("exit")

        with self.assertRaises(EventFailure):
            agent_cc.run(self.request(), on_event=fail_event, on_exit=fail_exit)

    def test_nonzero_primary_failure_is_not_masked_by_on_exit_failure(self):
        class ExitFailure(RuntimeError):
            pass

        def fail_exit(_pid: int) -> None:
            raise ExitFailure("exit")

        os.environ["FAKE_CLAUDE_EXIT"] = "7"
        with self.assertRaises(RuntimeError) as caught:
            agent_cc.run(self.request(), on_exit=fail_exit)
        self.assertIn("7", str(caught.exception))

    def test_adjacent_text_blocks_are_concatenated_exactly(self):
        self.events(
            [
                {
                    "type": "system",
                    "session_id": "claude-session",
                    "model": "claude-opus-5",
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "first"},
                            {"type": "text", "text": "second"},
                        ]
                    },
                },
                {
                    "type": "result",
                    "is_error": False,
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            ]
        )

        result = agent_cc.run(self.request())

        self.assertEqual(result.text, "firstsecond")

    def test_capability_argv_limits_builtins_preserves_patterns_and_blocks_mcp(self):
        request = self.request(
            tools=[
                "Read",
                "Bash(git status)",
                "Bash(git diff:*)",
                "Bash(git status)",
            ]
        )
        agent_cc.run(request)
        argv = self.argv()

        tools = argv.index("--tools") + 1
        self.assertEqual(argv[tools : tools + 2], ["Read", "Bash"])
        allowed = argv.index("--allowedTools") + 1
        self.assertEqual(argv[allowed : allowed + 4], request.tools)
        disallowed = argv.index("--disallowedTools") + 1
        self.assertEqual(argv[disallowed : disallowed + 1], ["mcp__*"])
        self.assertNotIn("--dangerously-skip-permissions", argv)

        agent_cc.run(self.request(tools=["Read"], session_id="read-only"))
        argv = self.argv()
        tools = argv.index("--tools") + 1
        allowed = argv.index("--allowedTools") + 1
        self.assertEqual(argv[tools : tools + 1], ["Read"])
        self.assertEqual(argv[allowed : allowed + 1], ["Read"])
        self.assertNotIn("Bash", argv)
        self.assertNotIn("mcp__server__tool", argv)

        agent_cc.run(self.request(tools=[], session_id="no-tools"))
        argv = self.argv()
        tools_end = argv.index("--disallowedTools")
        self.assertEqual(argv[argv.index("--tools") + 1 : tools_end], [""])
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(
            argv[argv.index("--disallowedTools") + 1 :],
            ["mcp__*", "--session-id", agent_cc._claude_session_id("no-tools")],
        )

        (self.root / "argv.jsonl").unlink()
        with self.assertRaises(ValueError):
            agent_cc.run(self.request(extensions=["unmapped-extension"]))
        self.assertFalse((self.root / "argv.jsonl").exists())

        unrestricted = self.request()
        unrestricted.tools = None
        agent_cc.run(unrestricted)
        argv = self.argv()
        self.assertNotIn("--tools", argv)
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--disallowedTools", argv)
        (self.root / "argv.jsonl").unlink()

        with self.assertRaises(ValueError):
            agent_cc.run(self.request(tools=["mcp__server__tool"]))
        self.assertFalse((self.root / "argv.jsonl").exists())


class ClaudeToolTraceContract(unittest.TestCase):
    def test_raw_claude_tool_events_retain_the_existing_completed_call_trace(self):
        observed = []

        class Tracer:
            def event(self, record):
                observed.append(record)

        class Run:
            adw_id = "adw-1"
            tracer = Tracer()

        phase = Phase(
            phase_id="phase-1",
            adw_id="adw-1",
            seq=1,
            params=PhaseParams(
                name="direct",
                kind="agent",
                owner="direct",
                description="Trace a direct tool call.",
            ),
        )
        forward = agents._event_forwarder(Run(), phase, "direct")
        forward(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {"file_path": "safe.txt"},
                        },
                    ]
                },
            }
        )
        forward(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "contents",
                        },
                    ]
                },
            }
        )

        self.assertEqual(len(observed), 1)
        record = observed[0]
        self.assertEqual((record.type, record.name), ("tool_call", "Read: safe.txt"))
        self.assertEqual(record.payload["tool"], "Read")
        self.assertEqual(record.payload["args"], {"file_path": "safe.txt"})
        self.assertEqual(record.payload["result_snippet"], "contents")
        self.assertEqual(record.payload["agent"], "direct")


class _Tracer:
    def event(self, *_args, **_kwargs):
        pass

    def process_start(self, *_args, **_kwargs):
        pass

    def process_end(self, *_args, **_kwargs):
        pass

    def gate_row(self, *_args, **_kwargs):
        pass

    def agent_session_row(self, *_args, **_kwargs):
        pass

    def envelope_row(self, *_args, **_kwargs):
        pass


class _Console:
    def agent_started(self, *_args):
        pass

    def agent_finished(self, *_args):
        pass

    def gate_result(self, *_args):
        pass

    def retry(self, *_args):
        pass

    def envelope_summary(self, *_args):
        pass


class DirectClaudeDispatchContract(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.binary = self.root / "claude"
        self.binary.write_text(FAKE_CLAUDE)
        self.binary.chmod(0o755)
        self._before = dict(os.environ)
        os.environ["CLAUDE_PATH"] = str(self.binary)
        os.environ["FAKE_CLAUDE_ARGV"] = str(self.root / "argv.jsonl")
        os.environ["FAKE_CLAUDE_PROMPT"] = str(self.root / "prompt.txt")
        os.environ["FAKE_CLAUDE_EVENTS"] = json.dumps(
            [
                {
                    "type": "system",
                    "session_id": "claude-session",
                    "model": "claude-opus-5",
                },
                {
                    "type": "result",
                    "is_error": False,
                    "result": json.dumps({"status": "success", "summary": "direct"}),
                },
            ]
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._before)
        self._tmp.cleanup()

    def test_execute_dispatches_claude_code_to_the_real_direct_process_route(self):
        system = self.root / "system.md"
        user = self.root / "user.md"
        system.write_text("system")
        user.write_text("{{prompt}}")
        config = SSSFConfig(
            agents=[
                AgentConfig(
                    name="direct",
                    coding_agent="claude_code",
                    model="opus",
                    tools=["Read"],
                    prompt_engineering=PromptEngineering(
                        system=str(system), user=str(user)
                    ),
                )
            ]
        )

        agents.validate(config, ["direct"])

        class Run:
            cfg = config
            session_dir = self.root / "runtime"
            context_handoff_dir = self.root / "handoff"
            repo_root = self.root
            agent_map = {}
            adw_id = "adw-1"
            tracer = _Tracer()
            console = _Console()

            def agent_map_entry(self, name):
                return self.agent_map.get(name)

            def add_usage(self, *_args):
                pass

            def save_agent_map(self, *_args):
                pass

        phase = Phase(
            phase_id="phase-1",
            adw_id="adw-1",
            seq=1,
            params=PhaseParams(
                name="direct",
                kind="agent",
                owner="direct",
                description="Request a typed direct report.",
            ),
        )
        call = AgentCall(output_type=GenericOutput, prompt="direct prompt")
        with (
            patch.object(agents.permissions, "snapshot", return_value={}),
            patch.object(agents.permissions, "enforce", return_value=[]),
        ):
            envelope = agents.execute(Run(), phase, call)

        self.assertEqual(envelope.summary, "direct")
        argv = json.loads((self.root / "argv.jsonl").read_text().splitlines()[-1])
        self.assertEqual(argv[0], "-p")
        self.assertEqual(
            (self.root / "prompt.txt").read_text(),
            agent_cc.prepare_route_prompt_text("claude", "direct prompt"),
        )


if __name__ == "__main__":
    unittest.main()
