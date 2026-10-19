"""A role session is reused only for its own lane, role and lane spec digest.

Run 2acd2f5f resumed after an amendment changed `lane-eval-build`'s spec, and
its code reviewer reconnected to the omp session it had carried through ten
pre-amendment rounds: the stable agent name and the `-c` session directory
named the lane and role, never the spec. These cases hold the invariant at
`HerdrLauncher`, the one path every launch and reconnect crosses.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from types import SimpleNamespace

import maestro
from adw_modules import launcher as lch
from tests.herdr_fake import FakeHerdr
from tests.test_herdr_workspace_topology import (
    BUILD_LANE,
    PROJECT,
    RUN_HASH,
    TESTS_LANE,
    _checkout,
    _launcher,
    _place,
    _spec,
)

OLD = "a" * 64
NEW = "b" * 64


def _role_spec(
    worktree: Path, *, lane: str, role: str, digest: str, replaced: list
) -> lch.LaunchSpec:
    return replace(
        _spec(worktree, lane=lane, role=role),
        spec_digest=digest,
        on_session_replaced=lambda reason, detail: replaced.append(reason),
    )


class SessionReuseRefusalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = replace(
            _spec(Path("/tmp/x"), lane=TESTS_LANE, role="tester"), spec_digest=NEW
        )

    def record(self, **changes: str) -> dict:
        return dict(lch.session_identity(self.spec), **changes)

    def test_same_lane_role_and_digest_is_reused(self) -> None:
        self.assertIsNone(lch.session_reuse_refusal(self.spec, self.record()))

    def test_each_mismatch_has_its_own_reason(self) -> None:
        cases = {
            lch.SESSION_LANE_MISMATCH: self.record(lane=BUILD_LANE),
            lch.SESSION_ROLE_MISMATCH: self.record(role="builder"),
            lch.SESSION_SPEC_DIGEST_CHANGED: self.record(spec_digest=OLD),
            lch.SESSION_UNRECORDED: {},
        }
        for reason, recorded in cases.items():
            with self.subTest(reason):
                self.assertEqual(
                    lch.session_reuse_refusal(self.spec, recorded), reason
                )

    def test_a_record_without_a_digest_does_not_match_a_digest(self) -> None:
        self.assertEqual(
            lch.session_reuse_refusal(self.spec, self.record(spec_digest="")),
            lch.SESSION_SPEC_DIGEST_CHANGED,
        )


class LiveSessionReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.herdr = FakeHerdr()
        self.herdr.add_workspace(PROJECT, self.root)
        self.label = lch.workspace_label_for(PROJECT, RUN_HASH)
        self.replaced: list = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def launcher(self) -> lch.HerdrLauncher:
        launcher = _launcher(self.label)
        launcher._herdr = self.herdr  # type: ignore[method-assign]
        return launcher

    def spec(self, worktree: Path, lane: str, role: str, digest: str) -> lch.LaunchSpec:
        return _role_spec(
            worktree, lane=lane, role=role, digest=digest, replaced=self.replaced
        )

    def test_the_pane_records_the_spec_digest(self) -> None:
        tester = _checkout(self.root, "tester")
        handle, _, _ = _place(
            self.launcher(), self.herdr, self.spec(tester, TESTS_LANE, "tester", OLD)
        )
        tokens = self.herdr.panes[handle.pane_id]["tokens"]
        self.assertEqual(tokens[lch.METADATA_TOKEN_SPEC], OLD)
        self.assertEqual(tokens[lch.METADATA_TOKEN_LANE], TESTS_LANE)
        self.assertEqual(tokens[lch.METADATA_TOKEN_ROLE], "tester")

    def test_restart_reconnect_reuses_an_unchanged_lane(self) -> None:
        tester = _checkout(self.root, "tester")
        spec = self.spec(tester, TESTS_LANE, "tester", OLD)
        old, _, _ = _place(self.launcher(), self.herdr, spec)
        second = self.launcher()
        second._bind_run_identity(spec)
        adopted = second._reconnect_live_agent(spec, dict(spec.environment))
        self.assertIsNotNone(adopted)
        assert adopted is not None
        self.assertEqual(adopted.pane_id, old.pane_id)
        self.assertNotIn(old.pane_id, self.herdr.closed_panes)
        self.assertEqual(self.replaced, [])

    def test_restart_reconnect_replaces_an_amended_lane(self) -> None:
        tester = _checkout(self.root, "tester")
        old, _, _ = _place(
            self.launcher(), self.herdr, self.spec(tester, TESTS_LANE, "tester", OLD)
        )
        amended = self.spec(tester, TESTS_LANE, "tester", NEW)
        second = self.launcher()
        second._bind_run_identity(amended)
        self.assertIsNone(
            second._reconnect_live_agent(amended, dict(amended.environment))
        )
        self.assertIn(old.pane_id, self.herdr.closed_panes)
        self.assertEqual(self.replaced, [lch.SESSION_SPEC_DIGEST_CHANGED])
        fresh, _, _ = _place(second, self.herdr, amended)
        self.assertNotEqual(fresh.pane_id, old.pane_id)
        self.assertEqual(
            self.herdr.panes[fresh.pane_id]["tokens"][lch.METADATA_TOKEN_SPEC], NEW
        )

    def test_in_process_role_handle_is_replaced_for_an_amended_lane(self) -> None:
        launcher = self.launcher()
        builder = _checkout(self.root, "builder")
        old, _, _ = _place(
            launcher, self.herdr, self.spec(builder, BUILD_LANE, "builder", OLD)
        )
        amended = self.spec(builder, BUILD_LANE, "builder", NEW)
        self.assertIsNone(
            launcher._validated_existing_role_handle(
                amended, dict(amended.environment)
            )
        )
        self.assertIn(old.pane_id, self.herdr.closed_panes)
        self.assertNotIn((BUILD_LANE, "builder"), launcher._role_handles)
        self.assertEqual(self.replaced, [lch.SESSION_SPEC_DIGEST_CHANGED])

    def test_in_process_role_handle_is_reused_for_an_unchanged_lane(self) -> None:
        launcher = self.launcher()
        builder = _checkout(self.root, "builder")
        spec = self.spec(builder, BUILD_LANE, "builder", OLD)
        old, _, _ = _place(launcher, self.herdr, spec)
        self.assertIs(
            launcher._validated_existing_role_handle(spec, dict(spec.environment)),
            old,
        )
        self.assertNotIn(old.pane_id, self.herdr.closed_panes)
        self.assertEqual(self.replaced, [])

    def test_a_session_is_never_reused_across_lanes_or_roles(self) -> None:
        # A pane recording another lane or role contradicts the agent name
        # that found it: it is refused, and left open, because nothing proves
        # it is Maestro's to close.
        cases = (
            (lch.SESSION_LANE_MISMATCH, BUILD_LANE, "tester"),
            (lch.SESSION_ROLE_MISMATCH, TESTS_LANE, "test-reviewer"),
        )
        for reason, lane, role in cases:
            with self.subTest(reason):
                launcher = self.launcher()
                tester = _checkout(self.root, "tester-" + reason.lower())
                old, _, _ = _place(
                    launcher, self.herdr, self.spec(tester, TESTS_LANE, "tester", OLD)
                )
                other = self.spec(tester, lane, role, OLD)
                pane = self.herdr.panes[old.pane_id]
                with self.assertRaises(lch.LaunchRefused) as raised:
                    launcher._refused_session_replaced(other, old, pane)
                self.assertEqual(
                    raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
                )
                self.assertTrue(raised.exception.detail.startswith(reason))
                self.assertNotIn(old.pane_id, self.herdr.closed_panes)
                self.assertEqual(self.replaced, [])

    def test_a_restarted_launcher_refuses_another_lanes_pane(self) -> None:
        tester = _checkout(self.root, "tester")
        spec = self.spec(tester, TESTS_LANE, "tester", OLD)
        old, _, _ = _place(self.launcher(), self.herdr, spec)
        self.herdr.panes[old.pane_id]["tokens"][lch.METADATA_TOKEN_ROLE] = "builder"
        second = self.launcher()
        second._bind_run_identity(spec)
        with self.assertRaises(lch.LaunchRefused):
            second._reconnect_live_agent(spec, dict(spec.environment))
        self.assertNotIn(old.pane_id, self.herdr.closed_panes)

    def test_lanes_and_roles_have_distinct_session_names(self) -> None:
        names = {
            lch.agent_name_for(lch.role_session_token(RUN_HASH, lane, role))
            for lane in (TESTS_LANE, BUILD_LANE)
            for role in lch.PERSISTENT_PANE_ROLES
        }
        self.assertEqual(len(names), 2 * len(lch.PERSISTENT_PANE_ROLES))

    def test_an_unconfirmed_close_refuses_rather_than_reuses(self) -> None:
        tester = _checkout(self.root, "tester")
        _place(
            self.launcher(), self.herdr, self.spec(tester, TESTS_LANE, "tester", OLD)
        )
        self.herdr.close_pane_error = "pane_not_found"
        amended = self.spec(tester, TESTS_LANE, "tester", NEW)
        second = self.launcher()
        second._bind_run_identity(amended)
        with self.assertRaises(lch.LaunchRefused) as raised:
            second._reconnect_live_agent(amended, dict(amended.environment))
        self.assertIn(
            "{0}:CLOSE_UNCONFIRMED".format(lch.SESSION_SPEC_DIGEST_CHANGED),
            raised.exception.detail,
        )
        self.assertEqual(self.replaced, [])


class OmpSessionDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.replaced: list = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def spec(self, digest: str, lane: str = TESTS_LANE) -> lch.LaunchSpec:
        return _role_spec(
            self.root, lane=lane, role="tester", digest=digest, replaced=self.replaced
        )

    def seed(self, digest: str) -> Path:
        spec = self.spec(digest)
        lch.record_session_dir_identity(spec)
        transcript = spec.session_dir / "2026-10-18T19-52-02-697Z_old.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        return transcript

    def test_an_unchanged_lane_continues_its_transcript(self) -> None:
        transcript = self.seed(OLD)
        spec = self.spec(OLD)
        self.assertIsNone(lch.retire_superseded_session_dir(spec))
        self.assertTrue(transcript.exists())
        self.assertIn("-c", lch.build_omp_argv(Path("omp"), spec))
        self.assertEqual(self.replaced, [])

    def test_an_amended_lane_starts_a_fresh_transcript(self) -> None:
        transcript = self.seed(OLD)
        spec = self.spec(NEW)
        self.assertEqual(
            lch.retire_superseded_session_dir(spec), lch.SESSION_SPEC_DIGEST_CHANGED
        )
        self.assertFalse(transcript.exists())
        moved = list(spec.session_dir.glob("superseded-*/" + transcript.name))
        self.assertEqual(len(moved), 1)
        self.assertNotIn("-c", lch.build_omp_argv(Path("omp"), spec))
        recorded = json.loads(
            (spec.session_dir / lch.SESSION_IDENTITY_FILE).read_text("utf-8")
        )
        self.assertEqual(recorded, lch.session_identity(spec))
        self.assertEqual(self.replaced, [lch.SESSION_SPEC_DIGEST_CHANGED])

    def test_another_lanes_record_is_never_continued(self) -> None:
        self.seed(OLD)
        spec = self.spec(OLD, lane=BUILD_LANE)
        self.assertNotIn("-c", lch.build_omp_argv(Path("omp"), spec))
        self.assertEqual(
            lch.retire_superseded_session_dir(spec), lch.SESSION_LANE_MISMATCH
        )

    def test_an_unrecorded_transcript_is_never_continued(self) -> None:
        spec = self.spec(OLD)
        spec.session_dir.mkdir(parents=True)
        (spec.session_dir / "legacy.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertNotIn("-c", lch.build_omp_argv(Path("omp"), spec))
        self.assertEqual(
            lch.retire_superseded_session_dir(spec), lch.SESSION_UNRECORDED
        )


class _ReplacingLauncher:
    """Records the spec, reports one replaced session, declares a result."""

    def __init__(self) -> None:
        self.specs: list = []

    def launch(self, spec: lch.LaunchSpec) -> SimpleNamespace:
        self.specs.append(spec)
        assert spec.on_session_replaced is not None
        spec.on_session_replaced(lch.SESSION_SPEC_DIGEST_CHANGED, "pane w1:p1")
        spec.envelope_path.parent.mkdir(parents=True, exist_ok=True)
        spec.envelope_path.write_text('{"declared": "tests"}', encoding="utf-8")
        return SimpleNamespace(launched_cwd=spec.worktree)

    def wait_for_idle(self, handle: object, timeout_s: float = 60.0) -> None:
        del handle, timeout_s

    def retain(self, handle: object) -> None:
        del handle


class ActorRecordsTheSpecDigestTest(unittest.TestCase):
    """`HerdrStageActor._launch`, the one place every role's spec is built."""

    def _launch(self, digest: str) -> tuple[_ReplacingLauncher, list]:
        launcher = _ReplacingLauncher()
        said: list = []
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "tester" / "checkout"
            cwd.mkdir(parents=True)
            actor.launcher = launcher
            actor.step = lambda lane, message, detail="": said.append(
                (lane, message, detail)
            )
            actor._roles = {}
            actor.role_routes = {
                "tester": {
                    "route": "omp",
                    "model": "unused",
                    "effort": "low",
                    "profile": "unused",
                }
            }
            actor.target = SimpleNamespace(
                target_repository_fingerprint="target",
                target_repository_root=str(root),
            )
            actor._release_superseded = lambda key: False
            actor._prepared_cwd = lambda path, prepare, provision=True: prepare(path)
            actor._materialize_role_instructions = lambda *args: root / "system.md"
            actor._prompt = lambda *args: {}
            actor._launch_environment = lambda path: {}
            actor._workspace_label = lambda ctx: "workspace"
            actor._role_cwds = lambda ctx: {}
            actor._lane_child_anchor = lambda ctx, path: None
            ctx = SimpleNamespace(
                run_id="run-1",
                lane=SimpleNamespace(
                    lane_id=TESTS_LANE, lane_kind=None, spec_digest=digest
                ),
                plan_revision=1,
                input_digest="digest",
                stage=SimpleNamespace(value="testing"),
                artifacts={},
            )
            actor._launch(ctx, "tester", cwd, {}, prepare_cwd=lambda path: None)
        return launcher, said

    def test_every_launch_carries_the_lane_spec_digest(self) -> None:
        launcher, said = self._launch(NEW)
        self.assertEqual([spec.spec_digest for spec in launcher.specs], [NEW])

    def test_a_replaced_session_writes_a_steps_line(self) -> None:
        _, said = self._launch(NEW)
        self.assertIn(
            (
                TESTS_LANE,
                "{0}: fresh tester session".format(lch.SESSION_SPEC_DIGEST_CHANGED),
                "pane w1:p1",
            ),
            said,
        )

    def test_a_lane_without_a_spec_digest_is_refused(self) -> None:
        with self.assertRaises(maestro.FactoryRefused) as raised:
            self._launch("")
        self.assertIn("ROLE_SESSION_SPEC_DIGEST_MISSING", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
