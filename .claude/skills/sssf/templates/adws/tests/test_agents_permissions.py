"""permissions.enforce must run even when execute's send/gate span fails."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adw_modules import agents
from adw_modules.data_types import (
    AgentCall,
    AgentConfig,
    GenericOutput,
    Phase,
    PhaseParams,
    PiResult,
    PromptEngineering,
    SSSFConfig,
    UsageBreakdown,
)


def _agent() -> AgentConfig:
    return AgentConfig(
        name="builder",
        prompt_engineering=PromptEngineering(system="system.md", user="user.md"),
        writes=[],
    )


def _phase() -> Phase:
    return Phase(
        phase_id="p1", adw_id="adw1", seq=1,
        params=PhaseParams(
            name="build", kind="agent", owner="builder",
            description="Build the requested change in the worktree"),
    )

def _run(root: Path, agent: AgentConfig):
    cfg = SSSFConfig(agents=[agent])
    tracer = mock.Mock()
    console = mock.Mock()
    agent_map = {}
    return SimpleNamespace(
        cfg=cfg,
        adw_id="adw1",
        repo_root=root,
        session_dir=root / "session",
        context_handoff_dir=root / "handoff",
        agent_map=agent_map,
        agent_map_entry=agent_map.get,
        tracer=tracer,
        console=console,
        add_usage=mock.Mock(),
        save_agent_map=mock.Mock(),
    )


class ExecutePermissionEnforcementTests(unittest.TestCase):

    def test_enforce_breach_does_not_mask_send_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "session").mkdir()
            agent = _agent()
            run = _run(root, agent)
            call = AgentCall(output_type=GenericOutput, prompt="do work")
            boom = RuntimeError("agent exploded")
            breach = agents.permissions.PermissionBreach("out of scope")

            with mock.patch.object(agents, "resolve", return_value=agent), \
                    mock.patch.object(agents.prompts, "render", return_value="prompt"), \
                    mock.patch.object(agents.prompts, "save"), \
                    mock.patch.object(agents.permissions, "snapshot",
                                      return_value={}), \
                    mock.patch.object(agents.permissions, "enforce",
                                      side_effect=breach) as enforce, \
                    mock.patch.object(agents.agent_pi, "run", side_effect=boom):
                with self.assertRaises(RuntimeError) as raised:
                    agents.execute(run, _phase(), call)

            self.assertIs(raised.exception, boom)
            enforce.assert_called_once()

    def test_permission_breach_on_success_path_still_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "session").mkdir()
            agent = _agent()
            run = _run(root, agent)
            call = AgentCall(output_type=GenericOutput, prompt="do work")
            result = PiResult(
                text='{"status": "success", "summary": "ok"}',
                tokens=0, cost=0.0, usage=UsageBreakdown(),
                context_tokens=0, context_window=0)
            breach = agents.permissions.PermissionBreach("out of scope")

            with mock.patch.object(agents, "resolve", return_value=agent), \
                    mock.patch.object(agents.prompts, "render", return_value="prompt"), \
                    mock.patch.object(agents.prompts, "save"), \
                    mock.patch.object(agents.permissions, "snapshot",
                                      return_value={}), \
                    mock.patch.object(agents.permissions, "enforce",
                                      side_effect=breach), \
                    mock.patch.object(agents.agent_pi, "run", return_value=result), \
                    mock.patch.object(agents, "_parse_with_retries",
                                      return_value=(
                                          GenericOutput(status="success",
                                                        summary="ok"),
                                          1)):
                with self.assertRaises(agents.permissions.PermissionBreach):
                    agents.execute(run, _phase(), call)
