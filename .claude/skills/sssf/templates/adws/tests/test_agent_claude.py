"""Direct Claude route uses the same typed request/result seam as omp."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agent_cc, agent_pi
from adw_modules.data_types import PiRequest


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_CLAUDE_ARGV"], "w") as handle:
    json.dump(sys.argv[1:], handle)
_ = sys.stdin.read()
stderr_bytes = int(os.environ.get("FAKE_CLAUDE_STDERR_BYTES", "0"))
if stderr_bytes:
    sys.stderr.write("x" * stderr_bytes)
    sys.stderr.flush()
model = os.environ.get("FAKE_CLAUDE_MODEL", "claude-opus-5")
init = {"type":"system","subtype":"init","session_id":"claude-session"}
if model:
    init["model"] = model
print(json.dumps(init))
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"done"}],"usage":{"input_tokens":7,"output_tokens":3}}}))
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"done","total_cost_usd":0.02,"usage":{"input_tokens":7,"output_tokens":3}}))
'''


class ClaudeRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.binary = self.root / "claude"
        self.binary.write_text(FAKE_CLAUDE)
        self.binary.chmod(0o755)
        self._before = dict(os.environ)
        os.environ["CLAUDE_PATH"] = str(self.binary)
        os.environ["FAKE_CLAUDE_ARGV"] = str(self.root / "argv.json")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._before)
        self._tmp.cleanup()

    def request(self) -> PiRequest:
        return PiRequest(
            prompt="work", system_prompt="system", model="opus", thinking="high",
            session_id="maestro-claude-1", session_dir=str(self.root / "session"),
            raw_output_path=str(self.root / "raw.jsonl"), tools=["Read"],
            cwd=str(self.root),
        )

    def test_run_builds_direct_capability_limited_claude_argv_and_records_matching_model(self):
        result = agent_cc.run(self.request())
        argv = json.loads((self.root / "argv.json").read_text())
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--tools") + 1:argv.index("--allowedTools")],
                         ["Read"])
        self.assertEqual(argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")],
                         ["Read"])
        self.assertEqual(argv[argv.index("--disallowedTools") + 1:
                             argv.index("--session-id")], ["mcp__*"])
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--model", argv)
        session_id = argv[argv.index("--session-id") + 1]
        self.assertEqual(str(uuid.UUID(session_id)), session_id)
        self.assertEqual(result.text, "done")
        self.assertEqual(result.model_ran, "claude/claude-opus-5")
        self.assertEqual(result.cost, 0.02)

    def test_permission_bypass_is_explicitly_opt_in(self):
        request = self.request()
        request.dangerously_skip_permissions = True
        agent_cc.run(request)
        argv = json.loads((self.root / "argv.json").read_text())
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_rejects_a_substituted_stream_reported_model(self):
        os.environ["FAKE_CLAUDE_MODEL"] = "sonnet"
        spawned = []
        exited = []

        with self.assertRaises(agent_pi.ModelBindingError) as caught:
            agent_cc.run(self.request(), on_spawn=spawned.append,
                         on_exit=exited.append)

        self.assertIn("opus", str(caught.exception))
        self.assertIn("sonnet", str(caught.exception))
        self.assertEqual(exited, spawned)

    def test_refuses_a_run_without_a_stream_model_identity(self):
        os.environ["FAKE_CLAUDE_MODEL"] = ""

        with self.assertRaises(agent_pi.ModelBindingError):
            agent_cc.run(self.request())

    def test_drains_stderr_while_streaming_stdout(self):
        os.environ["FAKE_CLAUDE_STDERR_BYTES"] = str(64 * 1024 + 1)

        result = agent_cc.run(self.request())

        self.assertEqual(result.text, "done")
        self.assertEqual(result.returncode, 0)
        raw_path = next((self.root / "session").rglob("raw.jsonl"))
        raw_events = [json.loads(line) for line in raw_path.read_text().splitlines()]
        self.assertEqual([event["type"] for event in raw_events],
                         ["system", "assistant", "result"])

    def test_callbacks_bracket_the_real_child(self):
        spawned = []
        exited = []
        agent_cc.run(self.request(), on_spawn=spawned.append, on_exit=exited.append)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(exited, spawned)


if __name__ == "__main__":
    unittest.main()
