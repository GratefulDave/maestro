"""A reused role session that grows past `role_session_max_tokens` is replaced.

Reuse under the cap is unchanged (#332). Past it the next dispatch closes the
old session, moves its transcript aside, and starts a fresh one whose prompt
carries a compact handoff, so a long lane does not pile context onto one
window. These cases drive `HerdrStageActor._launch`, where every role's session
is created or continued.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro
from adw_modules import launcher as lch

LANE = "lane-tests"
DIGEST = "a" * 64


class _Launcher:
    def __init__(self) -> None:
        self.cancelled: list = []
        self.specs: list = []

    def launch(self, spec):
        self.specs.append(spec)
        spec.envelope_path.parent.mkdir(parents=True, exist_ok=True)
        spec.envelope_path.write_text('{"declared": "tests"}', encoding="utf-8")
        return SimpleNamespace(launched_cwd=spec.worktree)

    def wait_for_idle(self, handle, timeout_s=60.0) -> None:
        pass

    def retain(self, handle) -> None:
        pass

    def cancel(self, handle, deadline) -> None:
        self.cancelled.append(handle)


class SessionCapTest(unittest.TestCase):
    def _dispatch(self, cap: int, transcript_bytes: int):
        launcher = _Launcher()
        said: list = []
        prompts: list = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "tester" / "checkout"
            cwd.mkdir(parents=True)
            session = root / "tester" / "session"
            session.mkdir()
            (session / "t.jsonl").write_text("x" * transcript_bytes)
            lch.role_result_path(cwd, 1).parent.mkdir(parents=True, exist_ok=True)
            lch.role_result_path(cwd, 1).write_text(
                json.dumps({"test_files": {"tests/a.py": "..."}, "note": "n" * 400})
            )
            lch.role_prompt_path(cwd, 1).write_text("{}")
            actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
            actor.launcher = launcher
            actor.role_session_max_tokens = cap
            actor.worktrees = root / "worktrees"
            actor.step = lambda lane, message, detail="": said.append((lane, message, detail))
            handle = object()
            actor._roles = {}
            ctx = SimpleNamespace(
                run_id="run-1",
                lane=SimpleNamespace(
                    lane_id=LANE, lane_kind=None, spec_digest=DIGEST,
                    public_acceptance=("alpha returns one",),
                ),
                plan_revision=1,
                input_digest="digest",
                stage=SimpleNamespace(value="testing"),
                artifacts={},
            )
            key = actor._role_key(ctx, "tester")
            stored = maestro._RoleSession(handle, cwd, root / "tester", None, "run-1")
            stored.turns = 1
            actor._roles[key] = stored
            actor.role_routes = {
                "tester": {"route": "omp", "model": "m", "effort": "low", "profile": "p"}
            }
            actor.target = SimpleNamespace(
                target_repository_fingerprint="target", target_repository_root=str(root)
            )
            actor._release_superseded = lambda key: False
            actor._prepared_cwd = lambda path, prepare, provision=True: prepare(path)
            actor._materialize_role_instructions = lambda *args: root / "system.md"
            actor._prompt = lambda ctx, role, envelope, cwd, extra: prompts.append(dict(extra)) or {}
            actor._launch_environment = lambda path: {}
            actor._workspace_label = lambda ctx: "workspace"
            actor._role_cwds = lambda ctx: {}
            actor._lane_child_anchor = lambda ctx, path: None
            actor._launch(ctx, "tester", cwd, {}, prepare_cwd=lambda path: None)
            moved = sorted(p.name for p in session.glob("superseded-*/*.jsonl"))
            kept = sorted(p.name for p in session.glob("*.jsonl"))
        return launcher, said, prompts, handle, moved, kept

    def test_under_the_cap_the_session_is_reused(self) -> None:
        launcher, said, prompts, _, moved, kept = self._dispatch(10_000, 300)
        self.assertEqual(launcher.cancelled, [])
        self.assertEqual((moved, kept), ([], ["t.jsonl"]))
        self.assertNotIn("session_handoff", prompts[-1])
        self.assertFalse([s for s in said if "over cap" in s[1]])

    def test_over_the_cap_starts_a_fresh_session_with_a_handoff(self) -> None:
        launcher, said, prompts, handle, moved, kept = self._dispatch(100, 6_000)
        self.assertEqual(launcher.cancelled, [handle])
        self.assertEqual((moved, kept), (["t.jsonl"], []))
        self.assertTrue([s for s in said if s[1] == "tester session over cap: fresh session"])
        handoff = prompts[-1]["session_handoff"]
        self.assertEqual(handoff["reason"], "ROLE_SESSION_OVER_CAP")
        self.assertEqual(handoff["prior_turns"], 1)
        self.assertEqual(handoff["open_obligations"], ["alpha returns one"])
        self.assertEqual(handoff["last_answer"]["test_files"], ["tests/a.py"])

    def test_a_zero_cap_turns_it_off(self) -> None:
        launcher, _, prompts, _, moved, _ = self._dispatch(0, 6_000_000)
        self.assertEqual((launcher.cancelled, moved), ([], []))
        self.assertNotIn("session_handoff", prompts[-1])

    def test_config_key_default_and_validation(self) -> None:
        self.assertEqual(
            maestro._config_role_session_max_tokens(None, "k"),
            maestro.DEFAULT_ROLE_SESSION_MAX_TOKENS,
        )
        self.assertEqual(maestro._config_role_session_max_tokens(0, "k"), 0)
        for bad in (-1, True, "5", 1.5):
            with self.assertRaises(maestro._MaestroConfigurationError):
                maestro._config_role_session_max_tokens(bad, "k")


if __name__ == "__main__":
    unittest.main()
