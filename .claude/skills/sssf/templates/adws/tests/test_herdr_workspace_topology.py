"""One repository workspace with one lane tab and role panes per lane."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch
from tests.herdr_fake import (
    FakeHerdr,
    FakeHerdrStopped,
    env_from_args as _env_from_args,
    same_path as _same_path,
)


PROJECT = "FDAdb"
RUN_HASH = "e892fe8df79046ca8ea6504934e912c6"
RUN_PREFIXED = "run-9f20c17fabcdef0123456789"
REPO = "repo-fdadb"
TESTS_LANE = "lane-wp6-tests"
BUILD_LANE = "lane-wp6-build"
FIVE_ROLES = (
    "tester",
    "test-reviewer",
    "builder",
    "code-reviewer",
    "integration-reviewer",
)

CREATE = ("workspace", "create")
OPEN = ("worktree", "open")
SPLIT = ("pane", "split")
START = ("agent", "start")


def _launcher(
    label: str,
    *,
    run_id: str = RUN_HASH,
    fingerprint: str = REPO,
) -> lch.HerdrLauncher:
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher.herdr_path = Path("herdr")
    launcher.omp_path = Path("omp")
    launcher.claude_path = Path("claude")
    launcher.admitted_routes = type(
        "Routes", (), {"admits": lambda self, route: True}
    )()
    launcher.provision_argv = ()
    launcher.workspace_label = label
    launcher.agent_start_busy_window_s = 0.0
    launcher.quiescence_confirm_s = 0.0
    launcher._handles_lock = threading.RLock()
    launcher._handles = {}
    launcher._tailers = {}
    launcher._quiescent_since = {}
    launcher._proven_absent = {}
    launcher._parent_workspace_id = ""
    launcher._run_id = run_id
    launcher._repository_fingerprint = fingerprint
    launcher._repository_root = Path()
    launcher._tabs = {}
    launcher._role_handles = {}
    launcher._cleaned_absent = set()
    return launcher


def _checkout(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    lch.scratch_environment(path)
    return path


def _spec(
    worktree: Path,
    *,
    lane: str,
    role: str,
    run_id: str = RUN_HASH,
    workspace_label: str = "",
    fingerprint: str = REPO,
    repository_root: Path | None = None,
) -> lch.LaunchSpec:
    label = workspace_label or lch.workspace_label_for(PROJECT, run_id)
    root = Path(repository_root) if repository_root is not None else worktree.parent
    token = lch.role_session_token(run_id, lane, role)
    return lch.LaunchSpec(
        correlation_token=token,
        worktree=worktree,
        prompt_path=worktree / "prompt.json",
        envelope_path=worktree / "envelope.json",
        route="omp",
        model="",
        effort="",
        profile="grok-maestro",
        session_dir=worktree / "session",
        environment=lch.role_pane_environment(worktree, {}),
        workspace_label=label,
        lane_key=lane,
        lane_label=lane,
        pane_role=role,
        run_id=run_id,
        repository_fingerprint=fingerprint,
        repository_root=root,
    )


def _place(
    launcher: lch.HerdrLauncher,
    herdr: FakeHerdr,
    spec: lch.LaunchSpec,
    *,
    start_agent: bool = True,
    status: str = "idle",
) -> tuple[lch.LaunchHandle, lch._TabLayout, bool]:
    env = dict(spec.environment)
    pane_id, layout, reused = launcher._acquire_pane(spec, spec.worktree, env)
    launcher._label_pane(pane_id, spec, env)
    name = lch.agent_name_for(spec.correlation_token)
    if start_agent:
        herdr.start_agent(name, pane_id, status=status)
    workspace_id = layout.parent_workspace_id
    handle = lch.LaunchHandle(
        spec.correlation_token,
        pane_id,
        name,
        spec.worktree.resolve(),
        envelope_path=spec.envelope_path,
        environment=env,
        workspace_id=workspace_id,
        tab_id=layout.tab_id,
        lane_key=spec.lane_key,
        pane_role=str(spec.pane_role or ""),
        lane_label=str(spec.lane_label or spec.lane_key or ""),
    )
    with launcher._handles_lock:
        launcher._handles[spec.correlation_token] = handle
    launcher._register_role_handle(spec, handle)
    return handle, layout, reused


@contextlib.contextmanager
def _launch_patches() -> Iterator[None]:
    """Stub model interaction while retaining real placement and adoption."""
    with (
        mock.patch.object(lch, "prepare_route_prompt"),
        mock.patch.object(lch, "preflight_launch_prompt"),
        mock.patch.object(
            lch,
            "build_omp_argv",
            return_value=("omp", "--pm-profile", "grok"),
        ),
        mock.patch.object(lch, "_wait_for_available_shell"),
        mock.patch.object(lch, "wait_for_interactive_agent"),
        mock.patch.object(lch, "submit_agent_prompt"),
        mock.patch.object(lch, "pane_liveness_pid", return_value=None),
    ):
        yield


def _drive(launcher: lch.HerdrLauncher, spec: lch.LaunchSpec) -> lch.LaunchHandle:
    if not spec.prompt_path.exists():
        spec.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        spec.prompt_path.write_text("{}", encoding="utf-8")
    return launcher.launch(spec)


def _launch(
    launcher: lch.HerdrLauncher, herdr: FakeHerdr, spec: lch.LaunchSpec
) -> lch.LaunchHandle:
    del herdr
    with _launch_patches():
        return _drive(launcher, spec)


def _calls_after(
    herdr: FakeHerdr, mark: int, *verbs: tuple[str, str]
) -> list[tuple[str, ...]]:
    return [call for call in herdr.calls[mark:] if call[:2] in verbs]


def _flag(call: tuple[str, ...], name: str) -> str | None:
    if name not in call:
        return None
    return call[call.index(name) + 1]


def _live_panes(
    herdr: FakeHerdr, workspace_id: str, tab_id: str = ""
) -> dict[str, dict]:
    return {
        pane_id: pane
        for pane_id, pane in herdr.panes.items()
        if pane_id not in herdr.closed_panes
        and pane.get("workspace_id") == workspace_id
        and (not tab_id or pane.get("tab_id") == tab_id)
    }


def _live_agents_in(herdr: FakeHerdr, pane_id: str) -> list[str]:
    return [
        name
        for name, agent in herdr.agents.items()
        if agent.get("pane_id") == pane_id and pane_id not in herdr.closed_panes
    ]

def _assert_converged(
    case: unittest.TestCase,
    herdr: FakeHerdr,
    launcher: lch.HerdrLauncher,
    lanes: dict[str, dict[str, lch.LaunchSpec]],
    *,
    allowed_unowned: dict[str, set[str]] | None = None,
) -> str:
    """Prove one direct tab per lane and one pane per role."""
    parent_id = launcher._parent_workspace_id or launcher._run_workspace({})
    case.assertTrue(parent_id)
    case.assertNotIn(parent_id, herdr.closed_workspaces)
    case.assertFalse(_calls_after(herdr, 0, CREATE))
    case.assertFalse(_calls_after(herdr, 0, OPEN))
    tabs_seen: set[str] = set()
    for lane, roles in lanes.items():
        expected_lane = launcher._lane_identity_tokens(lane, parent_id)
        lane_panes = {
            pane_id: pane
            for pane_id, pane in _live_panes(herdr, parent_id).items()
            if lch._tokens_match(lch._herdr_tokens(pane), expected_lane)
        }
        tabs = {
            str(pane.get("tab_id") or "")
            for pane in lane_panes.values()
            if pane.get("tab_id")
        }
        case.assertEqual(len(tabs), 1, "tabs for {}: {}".format(lane, tabs))
        tab_id = next(iter(tabs))
        tabs_seen.add(tab_id)
        case.assertEqual(herdr.tabs[tab_id]["workspace_id"], parent_id)
        case.assertEqual(herdr.tabs[tab_id]["label"], lane)
        panes = _live_panes(herdr, parent_id, tab_id)
        for role, spec in roles.items():
            role_panes = [
                pane_id
                for pane_id, pane in panes.items()
                if pane.get("label") == lch.pane_label_for(role)
            ]
            case.assertEqual(
                len(role_panes),
                1,
                "{} panes for {}: {}".format(role, lane, role_panes),
            )
            pane = panes[role_panes[0]]
            tokens = lch._herdr_tokens(pane)
            case.assertTrue(
                lch._tokens_match(
                    tokens,
                    launcher._pane_identity_tokens(
                        lane,
                        role,
                        parent_id,
                    ),
                )
            )
            case.assertEqual(
                tokens.get(lch.METADATA_TOKEN_SCRATCH),
                lch.METADATA_SCRATCH_REDIRECT,
            )
            case.assertTrue(_same_path(pane.get("cwd"), spec.worktree))
            case.assertEqual(
                _live_agents_in(herdr, role_panes[0]),
                [lch.agent_name_for(spec.correlation_token)],
            )
        case.assertEqual(
            {
                pane_id
                for pane_id, pane in panes.items()
                if not pane.get("label") and not lch._herdr_tokens(pane)
            },
            (allowed_unowned or {}).get(lane, set()),
            "unlabelled pane left in lane tab",
        )
    case.assertEqual(len(tabs_seen), len(lanes))
    return parent_id

class _RunFixture:
    """One repository with an existing operator workspace and role checkouts."""

    def __init__(self, tmp: str) -> None:
        self.root = Path(tmp)
        self.herdr = FakeHerdr()
        self.label = lch.workspace_label_for(PROJECT, RUN_HASH)
        self.operator = self.herdr.add_workspace(PROJECT, self.root)
        tab_id = str(self.herdr.workspaces[self.operator]["active_tab_id"])
        self.herdr.tabs[tab_id]["label"] = "notes"
        pane = next(
            pane
            for pane in self.herdr.panes.values()
            if pane["workspace_id"] == self.operator and pane["tab_id"] == tab_id
        )
        self.herdr.start_agent("operator-claude", pane["pane_id"], status="working")

    def launcher(self) -> lch.HerdrLauncher:
        launcher = _launcher(self.label)
        launcher._herdr = self.herdr  # type: ignore[method-assign]
        launcher._repository_root = self.root.resolve()
        return launcher

    def spec(
        self, lane: str, role: str, checkout: str = ""
    ) -> lch.LaunchSpec:
        path = self.root / (checkout or "{}-{}".format(lane, role))
        if not path.exists():
            _checkout(self.root, path.name)
        return _spec(
            path,
            lane=lane,
            role=role,
            repository_root=self.root,
        )


class NamingTest(unittest.TestCase):
    def test_labels_keep_short_run_caption(self) -> None:
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        self.assertEqual(launcher.workspace_label, "FDAdb-e892")
        self.assertEqual(lch.run_hash_prefix(RUN_PREFIXED), "9f20")
        self.assertEqual(lch.pane_label_for("test-reviewer"), "tester-reviewer")

    def test_parent_label_uses_basename_and_four_hash_chars(self) -> None:
        self.assertEqual(
            lch.workspace_label_for(PROJECT, RUN_HASH),
            "FDAdb-e892",
        )
        self.assertEqual(
            lch.workspace_label_for(PROJECT, RUN_PREFIXED),
            "FDAdb-9f20",
        )
        self.assertEqual(lch.run_hash_prefix(RUN_HASH), "e892")
        self.assertEqual(lch.run_hash_prefix(RUN_PREFIXED), "9f20")


    def test_session_and_pane_labels_match_approved_captions(self) -> None:
        self.assertEqual(lch.pane_label_for("tester"), "tester")
        self.assertEqual(lch.pane_label_for("test-reviewer"), "tester-reviewer")
        self.assertEqual(
            lch.session_name_for(PROJECT, RUN_HASH, TESTS_LANE, "tester"),
            "FDAdb-e892-lane-wp6-tests-tester",
        )
        self.assertEqual(
            lch.session_name_for(PROJECT, RUN_HASH, BUILD_LANE, "integration-reviewer"),
            "FDAdb-e892-lane-wp6-build-integration-reviewer",
        )


class RepositoryLaneTabTest(unittest.TestCase):
    def test_first_role_creates_lane_tab_in_repository_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            spec = run.spec(TESTS_LANE, "tester")
            handle, _layout, reused = _place(launcher, run.herdr, spec)
            self.assertFalse(reused)
            self.assertEqual(handle.workspace_id, run.operator)
            self.assertEqual(
                run.herdr.tabs[handle.tab_id]["workspace_id"], run.operator
            )
            self.assertEqual(run.herdr.tabs[handle.tab_id]["label"], TESTS_LANE)
            self.assertEqual(
                run.herdr.panes[handle.pane_id]["workspace_id"], run.operator
            )
            self.assertEqual(
                run.herdr.panes[handle.pane_id]["tab_id"], handle.tab_id
            )
            self.assertTrue(
                _same_path(run.herdr.panes[handle.pane_id]["cwd"], spec.worktree)
            )
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            self.assertEqual(_calls_after(run.herdr, 0, OPEN), [])
            self.assertEqual(
                _calls_after(run.herdr, 0, ("worktree", "list")), []
            )
            self.assertEqual(
                len(_calls_after(run.herdr, 0, ("tab", "create"))), 1
            )

    def test_multiple_exact_root_panes_in_one_workspace_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            duplicate_tab = run.herdr._new_tab(run.operator, "duplicate")
            run.herdr._new_pane(
                run.operator, duplicate_tab["tab_id"], str(run.root)
            )
            mark = len(run.herdr.calls)

            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(
                    run.launcher(),
                    run.herdr,
                    run.spec(TESTS_LANE, "tester"),
                )

            self.assertIs(
                raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertIn("AMBIGUOUS_REPOSITORY_PANE:", raised.exception.detail)
            self.assertFalse(
                _calls_after(
                    run.herdr,
                    mark,
                    ("tab", "create"),
                    ("pane", "split"),
                    ("agent", "start"),
                )
            )

    def test_repository_pane_in_space_bound_elsewhere_does_not_compete(self) -> None:
        # Run be064e58: an operator session opened on FDAdb from the maestro
        # Space refused every launch AMBIGUOUS_REPOSITORY_PANE. Worktree shapes
        # are the real binary's `herdr workspace list` records.
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            elsewhere = _checkout(Path(tmp), "maestro")
            other = run.herdr.add_workspace("maestro", elsewhere)
            other_tab = str(run.herdr.workspaces[other]["active_tab_id"])
            session = run.herdr._new_pane(other, other_tab, str(run.root))
            run.herdr.start_agent("operator-session", session["pane_id"], status="working")
            run.herdr.workspaces[other]["worktree"] = {
                "checkout_path": str(elsewhere),
                "is_linked_worktree": False,
                "repo_root": str(elsewhere),
            }
            run.herdr.workspaces[run.operator]["worktree"] = {
                "checkout_path": str(run.root),
                "is_linked_worktree": False,
                "repo_root": str(run.root),
            }

            handle = _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))

            self.assertEqual(handle.workspace_id, run.operator)
            self.assertEqual(run.herdr.tabs[handle.tab_id]["workspace_id"], run.operator)

    def test_unbound_space_pane_does_not_compete_with_bound_repository_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            stray = run.herdr.add_workspace("scratch", run.root)
            run.herdr.workspaces[run.operator]["worktree"] = {
                "checkout_path": str(run.root),
                "is_linked_worktree": False,
                "repo_root": str(run.root),
            }

            handle = _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))

            self.assertEqual(handle.workspace_id, run.operator)
            self.assertNotEqual(handle.workspace_id, stray)

    def test_two_spaces_bound_to_repository_root_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            second = run.herdr.add_workspace("FDAdb-2", run.root)
            for workspace_id in (run.operator, second):
                run.herdr.workspaces[workspace_id]["worktree"] = {
                    "checkout_path": str(run.root),
                    "is_linked_worktree": False,
                    "repo_root": str(run.root),
                }
            mark = len(run.herdr.calls)

            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))

            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
            self.assertIn("AMBIGUOUS_REPOSITORY_WORKSPACE:", raised.exception.detail)
            self.assertFalse(
                _calls_after(run.herdr, mark, ("tab", "create"), ("agent", "start"))
            )

    def test_roles_in_one_lane_share_tab_and_keep_distinct_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester_spec = run.spec(TESTS_LANE, "tester")
            reviewer_spec = run.spec(TESTS_LANE, "test-reviewer")
            tester, _, _ = _place(launcher, run.herdr, tester_spec)
            reviewer, _, _ = _place(launcher, run.herdr, reviewer_spec)
            self.assertEqual(tester.tab_id, reviewer.tab_id)
            self.assertNotEqual(tester.pane_id, reviewer.pane_id)
            self.assertEqual(tester.workspace_id, reviewer.workspace_id)
            self.assertEqual(tester.workspace_id, run.operator)
            self.assertEqual(_calls_after(run.herdr, 0, OPEN), [])
            self.assertEqual(
                len(_calls_after(run.herdr, 0, ("tab", "create"))), 1
            )
            _assert_converged(
                self,
                run.herdr,
                launcher,
                {
                    TESTS_LANE: {
                        "tester": tester_spec,
                        "test-reviewer": reviewer_spec,
                    }
                },
            )

    def test_lanes_use_distinct_tabs_in_repository_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tests_spec = run.spec(TESTS_LANE, "tester")
            build_spec = run.spec(BUILD_LANE, "builder")
            tester, _, _ = _place(launcher, run.herdr, tests_spec)
            builder, _, _ = _place(launcher, run.herdr, build_spec)
            self.assertNotEqual(tester.tab_id, builder.tab_id)
            self.assertEqual(
                {tester.workspace_id, builder.workspace_id}, {run.operator}
            )
            self.assertEqual(_calls_after(run.herdr, 0, OPEN), [])
            self.assertEqual(
                len(_calls_after(run.herdr, 0, ("tab", "create"))), 2
            )
            _assert_converged(
                self,
                run.herdr,
                launcher,
                {
                    TESTS_LANE: {"tester": tests_spec},
                    BUILD_LANE: {"builder": build_spec},
                },
            )

    def test_target_repository_workspace_owns_lane_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoking_repo = _checkout(root, "invoking")
            target_repo = _checkout(root, "target")
            herdr = FakeHerdr()
            herdr.add_workspace("invoking", invoking_repo)
            target = herdr.add_workspace("target", target_repo)
            launcher = _launcher(
                lch.workspace_label_for("target", RUN_HASH),
            )
            launcher._herdr = herdr  # type: ignore[method-assign]
            launcher._repository_root = target_repo.resolve()
            role = _checkout(target_repo, "tester")
            spec = _spec(
                role,
                lane=TESTS_LANE,
                role="tester",
                repository_root=target_repo,
                workspace_label=launcher.workspace_label,
            )
            handle = _launch(launcher, herdr, spec)
            self.assertEqual(handle.workspace_id, target)
            self.assertEqual(
                herdr.tabs[handle.tab_id]["workspace_id"], target
            )
            self.assertEqual(
                herdr.panes[handle.pane_id]["workspace_id"], target
            )
            self.assertEqual(_calls_after(herdr, 0, OPEN), [])
            self.assertEqual(
                _calls_after(herdr, 0, ("worktree", "list")), []
            )
            self.assertEqual(
                len(_calls_after(herdr, 0, ("tab", "create"))), 1
            )

    def test_completion_renames_then_closes_every_role_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester = _launch(
                launcher, run.herdr, run.spec(TESTS_LANE, "tester")
            )
            reviewer = _launch(
                launcher, run.herdr, run.spec(TESTS_LANE, "test-reviewer")
            )
            for handle in (tester, reviewer):
                launcher.wait_for_idle(handle, timeout_s=1.0)
            mark = len(run.herdr.calls)

            result = launcher.complete_run(
                [tester, reviewer], project_identity=PROJECT, timeout_s=1.0
            )
            completion_calls = run.herdr.calls[mark:]
            rename_positions = [
                index
                for index, call in enumerate(completion_calls)
                if call[:2] == ("pane", "send-text")
            ]
            close_positions = [
                index
                for index, call in enumerate(completion_calls)
                if call[:2] == ("pane", "close")
            ]

            self.assertIsNone(result)
            self.assertEqual(tester.tab_id, reviewer.tab_id)
            self.assertTrue(
                {tester.pane_id, reviewer.pane_id}.issubset(run.herdr.closed_panes)
            )
            self.assertEqual(len(rename_positions), 2)
            self.assertEqual(len(close_positions), 2)
            self.assertLess(max(rename_positions), min(close_positions))
            self.assertNotIn(run.operator, run.herdr.closed_workspaces)
            self.assertEqual(_calls_after(run.herdr, 0, ("workspace", "close")), [])
            self.assertIn(tester.tab_id, run.herdr.tabs)
            self.assertFalse(
                any(
                    pane.get("tab_id") == tester.tab_id
                    and pane_id not in run.herdr.closed_panes
                    for pane_id, pane in run.herdr.panes.items()
                )
            )

    def test_completion_closes_restored_pane_after_agent_record_expires(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            planted, _, _ = _place(run.launcher(), run.herdr, spec)
            run.herdr.agents.pop(planted.agent_name)

            fresh = run.launcher()
            self.assertEqual(fresh.restore_layout(spec), planted.pane_id)
            result = fresh.complete_run([], project_identity=PROJECT, timeout_s=1.0)

            self.assertIsNone(result)
            self.assertIn(planted.pane_id, run.herdr.closed_panes)
            self.assertTrue(spec.worktree.is_dir())

    def test_completion_closes_pane_when_agent_expires_after_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            handle = _launch(
                launcher, run.herdr, run.spec(TESTS_LANE, "tester")
            )
            run.herdr.agents.pop(handle.agent_name)

            result = launcher.complete_run(
                [handle], project_identity=PROJECT, timeout_s=1.0
            )

            self.assertIsNone(result)
            self.assertIn(handle.pane_id, run.herdr.closed_panes)

    def test_completion_accepts_pane_disappearing_before_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            handle = _launch(
                launcher, run.herdr, run.spec(TESTS_LANE, "tester")
            )

            def disappear(
                waiting: lch.LaunchHandle, timeout_s: float = 60.0
            ) -> None:
                del timeout_s
                run.herdr.closed_panes.add(waiting.pane_id)

            with mock.patch.object(launcher, "wait_for_idle", side_effect=disappear):
                result = launcher.complete_run(
                    [handle], project_identity=PROJECT, timeout_s=1.0
                )

            self.assertIsNone(result)
            self.assertIn(handle.pane_id, run.herdr.closed_panes)

    def test_completion_closes_pane_when_agent_expires_before_rename(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            handle = _launch(
                launcher, run.herdr, run.spec(TESTS_LANE, "tester")
            )

            def expire(
                waiting: lch.LaunchHandle, timeout_s: float = 60.0
            ) -> None:
                del timeout_s
                run.herdr.agents.pop(waiting.agent_name)

            with mock.patch.object(launcher, "wait_for_idle", side_effect=expire):
                result = launcher.complete_run(
                    [handle], project_identity=PROJECT, timeout_s=0.1
                )

            self.assertIsNone(result)
            self.assertIn(handle.pane_id, run.herdr.closed_panes)

    def test_restart_recovers_tab_when_first_seed_tag_was_interrupted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            run.herdr.crash_before(("pane", "report-metadata"))
            with self.assertRaises(FakeHerdrStopped):
                _place(run.launcher(), run.herdr, spec)

            tabs = [
                tab
                for tab in run.herdr.tabs.values()
                if tab["workspace_id"] == run.operator
                and str(tab.get("label") or "").startswith("maestro-pending-")
            ]
            self.assertEqual(len(tabs), 1)
            tab_id = str(tabs[0]["tab_id"])
            live = _live_panes(run.herdr, run.operator, tab_id)
            self.assertEqual(len(live), 1)
            seed = next(iter(live))
            self.assertFalse(lch._herdr_tokens(live[seed]))

            recovered = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(recovered.workspace_id, run.operator)
            self.assertEqual(recovered.tab_id, tab_id)
            self.assertIn(seed, run.herdr.closed_panes)
            self.assertEqual(
                set(_live_panes(run.herdr, run.operator, recovered.tab_id)),
                {recovered.pane_id},
            )

    def test_rename_failure_reuses_authenticated_role_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            run.herdr.crash_after(("pane", "rename"))
            with self.assertRaises(FakeHerdrStopped):
                _place(run.launcher(), run.herdr, spec)
            tabs = [
                tab
                for tab in run.herdr.tabs.values()
                if tab["workspace_id"] == run.operator
                and tab.get("label") == TESTS_LANE
            ]
            self.assertEqual(len(tabs), 1)
            tab_id = str(tabs[0]["tab_id"])
            live = _live_panes(run.herdr, run.operator, tab_id)
            self.assertEqual(len(live), 2)
            identified = [
                pane_id
                for pane_id, pane in live.items()
                if lch._herdr_tokens(pane).get(lch.METADATA_TOKEN_ROLE)
                == "tester"
            ]
            self.assertEqual(len(identified), 1)
            self.assertTrue(
                _same_path(live[identified[0]].get("cwd", ""), spec.worktree)
            )

            recovered = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(recovered.workspace_id, run.operator)
            self.assertEqual(recovered.tab_id, tab_id)
            self.assertEqual(recovered.pane_id, identified[0])
            self.assertNotIn(identified[0], run.herdr.closed_panes)
            self.assertEqual(
                set(_live_panes(run.herdr, run.operator, recovered.tab_id)),
                {recovered.pane_id},
            )

    def test_restart_reuses_existing_role_without_creating_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, spec)
            mark = len(run.herdr.calls)

            same = _launch(run.launcher(), run.herdr, spec)

            self.assertEqual(
                (same.pane_id, same.tab_id, same.agent_name),
                (first.pane_id, first.tab_id, first.agent_name),
            )
            self.assertFalse(
                _calls_after(
                    run.herdr,
                    mark,
                    ("tab", "create"),
                    ("pane", "split"),
                    ("agent", "start"),
                    ("workspace", "create"),
                    ("worktree", "open"),
                )
            )

    def test_role_metadata_mismatch_refuses_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, spec)
            launcher = run.launcher()
            wrong = launcher._pane_identity_tokens(
                TESTS_LANE, "builder", run.operator
            )
            wrong[lch.METADATA_TOKEN_SCRATCH] = lch.METADATA_SCRATCH_REDIRECT
            launcher._tag_pane(first.pane_id, wrong, spec.environment)
            mark = len(run.herdr.calls)

            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, spec)

            self.assertIs(
                raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertFalse(
                _calls_after(
                    run.herdr,
                    mark,
                    ("tab", "create"),
                    ("pane", "split"),
                    ("agent", "start"),
                )
            )

    def test_user_renamed_role_label_is_repaired_from_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, spec)
            run.herdr.panes[first.pane_id]["label"] = "my-debug-session"

            same = _launch(run.launcher(), run.herdr, spec)

            self.assertEqual(same.pane_id, first.pane_id)
            self.assertEqual(run.herdr.panes[first.pane_id]["label"], "tester")

    def test_unlabelled_operator_pane_inside_lane_tab_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester_spec = run.spec(TESTS_LANE, "tester")
            tester = _launch(launcher, run.herdr, tester_spec)
            created = run.herdr(
                "pane",
                "split",
                tester.pane_id,
                "--cwd",
                str(run.root),
                "--no-focus",
            )
            operator_pane = str(created["result"]["pane"]["pane_id"])

            reviewer = _launch(
                launcher,
                run.herdr,
                run.spec(TESTS_LANE, "test-reviewer"),
            )

            self.assertEqual(reviewer.tab_id, tester.tab_id)
            self.assertNotIn(operator_pane, run.herdr.closed_panes)
            self.assertFalse(lch._herdr_tokens(run.herdr.panes[operator_pane]))

    def test_duplicate_authored_lane_tabs_refuse_without_creating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr._new_tab(run.operator, TESTS_LANE)
            run.herdr._new_tab(run.operator, TESTS_LANE)
            mark = len(run.herdr.calls)

            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(
                    run.launcher(),
                    run.herdr,
                    run.spec(TESTS_LANE, "tester"),
                )

            self.assertIs(
                raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertFalse(
                _calls_after(
                    run.herdr,
                    mark,
                    ("tab", "create"),
                    ("pane", "split"),
                    ("agent", "start"),
                )
            )

    def test_repository_workspace_disappearing_during_tab_confirmation_refuses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)

            def close_workspace(_args: tuple[str, ...]) -> None:
                if run.herdr._counts.get(("tab", "list"), 0) == 1:
                    run.herdr._close_workspace_state(run.operator)

            run.herdr.hooks_before.setdefault(("tab", "list"), []).append(
                close_workspace
            )
            launcher = run.launcher()
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(
                    launcher,
                    run.herdr,
                    run.spec(TESTS_LANE, "tester"),
                )

            self.assertIs(
                raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED
            )
            self.assertEqual(
                raised.exception.detail,
                "PARENT_WORKSPACE_GONE:{}".format(run.operator),
            )
            self.assertEqual(launcher._parent_workspace_id, "")
            self.assertEqual(launcher._tabs, {})



class AdoptionTest(unittest.TestCase):
    def test_restart_adopts_existing_role_pane_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, spec)
            mark = len(run.herdr.calls)
            fresh = run.launcher()
            same = _launch(fresh, run.herdr, spec)
            self.assertEqual(
                (same.pane_id, same.tab_id, same.agent_name),
                (first.pane_id, first.tab_id, first.agent_name),
            )
            self.assertFalse(
                _calls_after(
                    run.herdr,
                    mark,
                    ("tab", "create"),
                    SPLIT,
                    START,
                    OPEN,
                )
            )

    def test_restart_repairs_known_role_label_from_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            planted, _, _ = _place(run.launcher(), run.herdr, spec)
            run.herdr.panes[planted.pane_id]["label"] = "builder"

            restored = run.launcher().restore_layout(spec)

            self.assertEqual(restored, planted.pane_id)
            self.assertEqual(run.herdr.panes[planted.pane_id]["label"], "tester")
            self.assertEqual(
                lch._herdr_tokens(run.herdr.panes[planted.pane_id]).get(
                    lch.METADATA_TOKEN_ROLE
                ),
                "tester",
            )

    def test_restart_repairs_reviewer_alias_to_canonical_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "test-reviewer")
            planted, _, _ = _place(run.launcher(), run.herdr, spec)
            run.herdr.panes[planted.pane_id]["label"] = "test-reviewer"
            mark = len(run.herdr.calls)

            pane_id, _, reused = run.launcher()._acquire_pane(
                spec, spec.worktree.resolve(), spec.environment
            )

            self.assertEqual(pane_id, planted.pane_id)
            self.assertTrue(reused)
            self.assertEqual(
                run.herdr.panes[planted.pane_id]["label"], "tester-reviewer"
            )
            self.assertEqual(_calls_after(run.herdr, mark, SPLIT, START, OPEN), [])


    def test_user_labeled_operator_pane_is_not_adopted_or_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            first = _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))
            reviewer = run.spec(TESTS_LANE, "test-reviewer")
            operator_cwd = run.root / "operator-notes"
            operator_cwd.mkdir()
            user = run.herdr._new_pane(
                first.workspace_id, first.tab_id, str(operator_cwd)
            )
            user_id = str(user["pane_id"])
            run.herdr.panes[user_id]["label"] = "builder"

            launched = _launch(run.launcher(), run.herdr, reviewer)

            self.assertNotEqual(launched.pane_id, user_id)
            self.assertNotIn(user_id, run.herdr.closed_panes)
            self.assertEqual(lch._herdr_tokens(run.herdr.panes[user_id]), {})
            self.assertEqual(run.herdr.panes[user_id]["label"], "builder")

    def test_token_owned_role_is_relabelled_after_user_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, spec)
            run.herdr.agents.pop(first.agent_name)
            run.herdr.panes[first.pane_id]["agent_status"] = "unknown"
            run.herdr.panes[first.pane_id]["label"] = "builder"

            recovered = _launch(run.launcher(), run.herdr, spec)

            self.assertEqual(recovered.pane_id, first.pane_id)
            self.assertEqual(run.herdr.panes[first.pane_id]["label"], "tester")
            self.assertEqual(
                lch._herdr_tokens(run.herdr.panes[first.pane_id]).get(
                    lch.METADATA_TOKEN_ROLE
                ),
                "tester",
            )


class ConcurrencyTest(unittest.TestCase):
    def test_same_lane_roles_racing_create_one_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            specs = {
                "tester": run.spec(TESTS_LANE, "tester"),
                "test-reviewer": run.spec(TESTS_LANE, "test-reviewer"),
            }
            gate = threading.Barrier(2)
            outcomes: dict[str, object] = {}

            def worker(role: str) -> None:
                gate.wait(30)
                try:
                    outcomes[role] = _drive(launcher, specs[role])
                except BaseException as exc:  # noqa: BLE001
                    outcomes[role] = exc

            threads = [threading.Thread(target=worker, args=(role,)) for role in specs]
            with _launch_patches():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
            for role, outcome in outcomes.items():
                self.assertIsInstance(
                    outcome, lch.LaunchHandle, "{}: {!r}".format(role, outcome)
                )
            self.assertEqual(
                len(_calls_after(run.herdr, 0, ("tab", "create"))), 1
            )
            _assert_converged(
                self,
                run.herdr,
                launcher,
                {TESTS_LANE: specs},
            )

    def test_separate_controllers_racing_same_lane_create_one_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launchers = {
                "tester": run.launcher(),
                "test-reviewer": run.launcher(),
            }
            specs = {
                role: run.spec(TESTS_LANE, role) for role in ("tester", "test-reviewer")
            }
            gate = threading.Barrier(2)
            outcomes: dict[str, object] = {}

            def worker(role: str) -> None:
                gate.wait(30)
                try:
                    outcomes[role] = _drive(launchers[role], specs[role])
                except BaseException as exc:  # noqa: BLE001
                    outcomes[role] = exc

            threads = [threading.Thread(target=worker, args=(role,)) for role in specs]
            with _launch_patches():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
            for role, outcome in outcomes.items():
                self.assertIsInstance(
                    outcome, lch.LaunchHandle, "{}: {!r}".format(role, outcome)
                )
            self.assertEqual(
                len(_calls_after(run.herdr, 0, ("tab", "create"))), 1
            )
            handles = [cast(lch.LaunchHandle, value) for value in outcomes.values()]
            self.assertEqual({handle.tab_id for handle in handles}, {handles[0].tab_id})

    def test_cached_controller_refreshes_before_role_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            current = run.launcher()
            stale = run.launcher()
            tester_spec = run.spec(TESTS_LANE, "tester")
            reviewer_spec = run.spec(TESTS_LANE, "test-reviewer")
            tester = _launch(current, run.herdr, tester_spec)
            self.assertEqual(stale.restore_layout(tester_spec), tester.pane_id)
            reviewer = _launch(current, run.herdr, reviewer_spec)
            mark = len(run.herdr.calls)

            pane_id, layout, reused = stale._acquire_pane(
                reviewer_spec,
                reviewer_spec.worktree.resolve(),
                reviewer_spec.environment,
            )

            self.assertTrue(reused)
            self.assertEqual(pane_id, reviewer.pane_id)
            self.assertEqual(layout.tab_id, reviewer.tab_id)
            self.assertFalse(_calls_after(run.herdr, mark, SPLIT))

    def test_different_lanes_racing_create_sibling_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            specs = {
                TESTS_LANE: run.spec(TESTS_LANE, "tester"),
                BUILD_LANE: run.spec(BUILD_LANE, "builder"),
            }
            gate = threading.Barrier(2)
            handles: dict[str, lch.LaunchHandle] = {}

            def worker(lane: str) -> None:
                gate.wait(30)
                handles[lane] = _drive(launcher, specs[lane])

            threads = [threading.Thread(target=worker, args=(lane,)) for lane in specs]
            with _launch_patches():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(30)
            self.assertEqual(set(handles), set(specs))
            self.assertEqual(
                {handle.workspace_id for handle in handles.values()},
                {run.operator},
            )
            self.assertEqual(len({handle.tab_id for handle in handles.values()}), 2)
            self.assertEqual(
                len(_calls_after(run.herdr, 0, ("tab", "create"))), 2
            )
            _assert_converged(
                self,
                run.herdr,
                launcher,
                {
                    TESTS_LANE: {"tester": specs[TESTS_LANE]},
                    BUILD_LANE: {"builder": specs[BUILD_LANE]},
                },
            )


class RepositoryWorkspaceTest(unittest.TestCase):
    def test_missing_repository_workspace_refuses_without_creating_one(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher("run")
        launcher._herdr = herdr
        with self.assertRaises(lch.LaunchRefused) as caught:
            launcher._run_workspace({})
        self.assertEqual(
            caught.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED
        )
        self.assertEqual(herdr.workspaces, {})

    def test_cross_repo_target_tab_reuses_role_and_preserves_workspaces(self) -> None:
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoking_repo = _checkout(root, "invoking")
            target_repo = _checkout(root, "target")
            invoking = herdr.add_workspace("invoking", invoking_repo)
            target = herdr.add_workspace("target", target_repo)
            other_pane = next(
                pid
                for pid, pane in herdr.panes.items()
                if pane["workspace_id"] == target
            )
            herdr._focus_pane(other_pane)
            before = herdr.snapshot()
            launcher = _launcher(
                lch.workspace_label_for("target", RUN_HASH),
            )
            launcher._herdr = herdr
            tester = _checkout(target_repo, "tester")
            spec = _spec(
                tester,
                lane=TESTS_LANE,
                role="tester",
                repository_root=target_repo,
                workspace_label=launcher.workspace_label,
            )
            first = _launch(launcher, herdr, spec)
            self.assertEqual(first.workspace_id, target)
            self.assertEqual(herdr.panes[first.pane_id]["cwd"], str(tester.resolve()))
            self.assertEqual(
                herdr.tabs[first.tab_id]["workspace_id"], target
            )
            herdr._focus_pane(other_pane)
            with _launch_patches():
                launcher.resubmit(first, spec.prompt_path)
            herdr._focus_pane(other_pane)
            _launch(launcher, herdr, spec)
            launcher.wait_for_idle(first, timeout_s=1.0)
            launcher.retain(first)
            self.assertNotIn(first.pane_id, herdr.closed_panes)
            mark = len(herdr.calls)
            fresh = _launcher(
                lch.workspace_label_for("target", RUN_HASH),
            )
            fresh._herdr = herdr
            herdr._focus_pane(other_pane)
            same = _launch(fresh, herdr, spec)
            self.assertEqual(
                (same.pane_id, same.agent_name), (first.pane_id, first.agent_name)
            )
            self.assertEqual(same.tab_id, first.tab_id)
            self.assertEqual(same.launched_cwd, tester.resolve())
            herdr._focus_pane(other_pane)
            fresh.wait_for_idle(same, timeout_s=1.0)
            fresh.poll(same)
            self.assertTrue(herdr.panes[other_pane]["focused"])
            self.assertFalse(
                set(call[:2] for call in herdr.calls[mark:])
                & {("pane", "split"), ("agent", "start"), ("worktree", "open")}
            )
            fresh.wait_for_idle(same, timeout_s=1.0)
            fresh.complete_run([same], timeout_s=1.0)
            self.assertIn(first.pane_id, herdr.closed_panes)
            self.assertNotIn(target, herdr.closed_workspaces)
            for workspace_id in (invoking, target):
                for key in ("tokens", "label"):
                    self.assertEqual(
                        herdr.workspaces[workspace_id].get(key),
                        before["workspaces"][workspace_id].get(key),
                    )

    def test_sibling_role_selects_its_split_and_resubmit_reveals_original(self) -> None:
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            herdr.add_workspace("invoking", root)
            launcher = _launcher(
                lch.workspace_label_for(PROJECT, RUN_HASH),
            )
            launcher._herdr = herdr
            tester_spec = _spec(
                _checkout(root, "tester"), lane=TESTS_LANE, role="tester"
            )
            reviewer_spec = _spec(
                _checkout(root, "reviewer"), lane=TESTS_LANE, role="test-reviewer"
            )
            tester = _launch(launcher, herdr, tester_spec)
            reviewer = _launch(launcher, herdr, reviewer_spec)
            self.assertEqual(tester.tab_id, reviewer.tab_id)
            self.assertNotEqual(tester.pane_id, reviewer.pane_id)
            with _launch_patches():
                launcher.resubmit(tester, tester_spec.prompt_path)



    def test_resume_from_another_workspace_keeps_target_repository_workspace(
        self,
    ) -> None:
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_repo = _checkout(root, "target")
            target = herdr.add_workspace("target", target_repo)
            invoking = herdr.add_workspace("invoking", _checkout(root, "invoking"))
            launcher = _launcher(lch.workspace_label_for("target", RUN_HASH))
            launcher._herdr = herdr
            worktree = _checkout(target_repo, "tester")
            spec = _spec(
                worktree,
                lane=TESTS_LANE,
                role="tester",
                repository_root=target_repo,
                workspace_label=launcher.workspace_label,
            )
            handle = _launch(launcher, herdr, spec)
            terminal = herdr.panes[handle.pane_id]["terminal_id"]
            fresh = _launcher(
                lch.workspace_label_for("target", RUN_HASH),
            )
            fresh._herdr = herdr
            resumed = _launch(fresh, herdr, spec)
            pane = herdr.panes[resumed.pane_id]
            self.assertNotEqual(resumed.workspace_id, invoking)
            self.assertEqual(resumed.workspace_id, target)
            self.assertEqual(pane["terminal_id"], terminal)
            self.assertEqual(pane["cwd"], str(worktree.resolve()))
            self.assertEqual(resumed.pane_id, handle.pane_id)
            self.assertEqual(
                herdr.agents[handle.agent_name]["pane_id"], resumed.pane_id
            )
            fresh.wait_for_idle(resumed, timeout_s=1.0)
            fresh.retain(resumed)
            self.assertNotIn(handle.pane_id, herdr.closed_panes)
            self.assertNotIn(target, herdr.closed_workspaces)


if __name__ == "__main__":
    unittest.main()
