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

    def _actor(self, cap: int):
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.role_session_max_tokens = cap
        actor.launcher = _Launcher()
        return actor

    def _turns(self, cwd: Path, count: int, prompt_bytes: int) -> None:
        lch.role_result_path(cwd, 1).parent.mkdir(parents=True, exist_ok=True)
        for turn in range(1, count + 1):
            lch.role_prompt_path(cwd, turn).write_text("p" * prompt_bytes)
            lch.role_result_path(cwd, turn).write_text("r" * 30)

    def test_turns_before_the_last_replacement_do_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, session = Path(tmp) / "checkout", Path(tmp) / "session"
            cwd.mkdir()
            session.mkdir()
            self._turns(cwd, 5, 3_000)
            actor = self._actor(1_000)
            before, _ = actor._session_tokens(cwd, session, 5)
            actor._start_session_epoch(session, 5)
            after, _ = actor._session_tokens(cwd, session, 5)
            self.assertGreater(before, 4_000)
            self.assertLess(after, 1_100)
            self.assertEqual(actor._session_tokens(cwd, session, 4)[0], 1)

    def test_a_replaced_session_is_reused_on_the_next_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, session = Path(tmp) / "checkout", Path(tmp) / "session"
            cwd.mkdir()
            session.mkdir()
            self._turns(cwd, 4, 3_000)
            actor = self._actor(1_000)
            ctx = SimpleNamespace(lane=SimpleNamespace(lane_id=LANE))
            actor._say = lambda *args: None
            actor._roles = {}
            key = ("run", LANE, "tester")
            actor._session_handoff = lambda *args: {"reason": "ROLE_SESSION_OVER_CAP"}
            first = actor._cap_reused_session(ctx, "tester", key, None, cwd, session, 5)
            self.assertIsNotNone(first)
            self._turns(cwd, 5, 3_000)
            second = actor._cap_reused_session(ctx, "tester", key, None, cwd, session, 6)
            self.assertIsNone(second)
            self._turns(cwd, 9, 3_000)
            third = actor._cap_reused_session(ctx, "tester", key, None, cwd, session, 10)
            self.assertIsNotNone(third)
            self.assertEqual(actor._session_epoch(session), 10)

    def test_a_prompt_larger_than_the_cap_does_not_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, session = Path(tmp) / "checkout", Path(tmp) / "session"
            cwd.mkdir()
            session.mkdir()
            self._turns(cwd, 1, 30_000)
            actor = self._actor(1_000)
            actor._say = lambda *args: None
            ctx = SimpleNamespace(lane=SimpleNamespace(lane_id=LANE))
            self.assertIsNone(
                actor._cap_reused_session(ctx, "tester", ("r", LANE, "tester"), None, cwd, session, 2)
            )

    def test_the_transcript_counts_only_the_live_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd, session = Path(tmp) / "checkout", Path(tmp) / "session"
            cwd.mkdir()
            (session / "superseded-1").mkdir(parents=True)
            (session / "superseded-1" / "old.jsonl").write_text("x" * 900_000)
            (session / "live.jsonl").write_text("x" * 3_000)
            self.assertLess(self._actor(1_000)._session_tokens(cwd, session, 0)[0], 1_100)

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
