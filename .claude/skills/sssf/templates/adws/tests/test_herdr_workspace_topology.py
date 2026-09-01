"""Parent Space + linked lane children, lazy panes, adopt, COMPLETE cleanup.

The Herdr backend is `tests/herdr_fake.py`, shaped after `herdr 0.8.2`
(`herdr api schema --json`, protocol 20). It models real payloads: optional
fields absent until set, `agent_status` only on agents, `already_open` on a
second `worktree open`, and typed refusal codes.
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Callable, Iterator
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch
from tests.herdr_fake import (
    FakeHerdr,
    FakeHerdrStopped,
    env_from_args as _env_from_args,
    flag as _flag,
    same_path as _same_path,
)


PROJECT = "FDAdb"
RUN_HASH = "e892fe8df79046ca8ea6504934e912c6"
RUN_PREFIXED = "run-9f20c17fabcdef0123456789"
REPO = "repo-fdadb"
TESTS_LANE = "lane-wp6-tests"
BUILD_LANE = "lane-wp6-build"


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
    launcher._split_parent_id = None
    launcher._parent_workspace_id = ""
    launcher._workspace_id = ""
    launcher._run_id = run_id
    launcher._repository_fingerprint = fingerprint
    launcher._repository_root = Path()
    launcher._tabs = {}
    launcher._seed_tab_id = ""
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
    env = lch.role_pane_environment(worktree, {})
    root = Path(repository_root) if repository_root is not None else worktree.parent
    return lch.LaunchSpec(
        correlation_token=lch.role_session_token(run_id, lane, role),
        worktree=worktree,
        prompt_path=worktree / "prompt.json",
        envelope_path=worktree / "envelope.json",
        route="omp",
        model="",
        effort="",
        profile="grok-maestro",
        session_dir=worktree / "session",
        environment=env,
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
    handle = lch.LaunchHandle(
        spec.correlation_token,
        pane_id,
        name,
        spec.worktree.resolve(),
        envelope_path=spec.envelope_path,
        environment=env,
        workspace_id=layout.child_workspace_id,
        tab_id=layout.tab_id,
        lane_key=spec.lane_key,
        parent_workspace_id=layout.parent_workspace_id or launcher._parent_workspace_id,
        child_workspace_id=layout.child_workspace_id,
        pane_role=str(spec.pane_role or ""),
        lane_label=str(spec.lane_label or spec.lane_key or ""),
    )
    with launcher._handles_lock:
        launcher._handles[spec.correlation_token] = handle
    launcher._register_role_handle(spec, handle)
    return handle, layout, reused


def _verbs(herdr: FakeHerdr) -> list[tuple[str, ...]]:
    return [call[:2] for call in herdr.calls]


class NamingTest(unittest.TestCase):
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

    def test_full_run_id_stays_in_identity_tokens_not_the_caption(self) -> None:
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        tokens = launcher._parent_identity_tokens()
        self.assertEqual(tokens[lch.METADATA_TOKEN_RUN], RUN_HASH)
        self.assertEqual(tokens[lch.METADATA_TOKEN_REPO], REPO)
        self.assertEqual(tokens[lch.METADATA_TOKEN_KIND], lch.METADATA_KIND_RUN)
        self.assertNotIn(RUN_HASH, launcher.workspace_label)
        self.assertTrue(launcher.workspace_label.endswith("-e892"))

    def test_session_and_pane_labels_match_approved_captions(self) -> None:
        self.assertEqual(lch.pane_label_for("tester"), "tester")
        self.assertEqual(lch.pane_label_for("test-reviewer"), "tester-reviewer")
        self.assertEqual(
            lch.session_name_for(PROJECT, RUN_HASH, TESTS_LANE, "tester"),
            "FDAdb-e892-lane-wp6-tests-tester",
        )
        self.assertEqual(
            lch.session_name_for(
                PROJECT, RUN_HASH, BUILD_LANE, "integration-reviewer"
            ),
            "FDAdb-e892-lane-wp6-build-integration-reviewer",
        )


class LazyParentChildTest(unittest.TestCase):
    def test_first_tester_creates_one_parent_and_one_linked_child(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            tester = _checkout(Path(tmp), "tester")
            (tester / "secret.txt").write_text("private-tests", encoding="utf-8")
            spec = _spec(tester, lane=TESTS_LANE, role="tester")
            handle, layout, reused = _place(launcher, herdr, spec)
        self.assertFalse(reused)
        creates = [call for call in herdr.calls if call[:2] == ("workspace", "create")]
        opens = [call for call in herdr.calls if call[:2] == ("worktree", "open")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(_flag(creates[0], "--label"), "FDAdb-e892")
        self.assertTrue(_same_path(_flag(creates[0], "--cwd"), tester.parent))
        self.assertIn("--no-focus", creates[0])
        self.assertEqual(len(opens), 1)
        self.assertEqual(_flag(opens[0], "--workspace"), handle.parent_workspace_id)
        self.assertEqual(_flag(opens[0], "--label"), TESTS_LANE)
        self.assertTrue(_same_path(_flag(opens[0], "--path"), tester))
        self.assertIn("--no-focus", opens[0])
        splits = [call for call in herdr.calls if call[:2] == ("pane", "split")]
        self.assertEqual(len(splits), 1)
        self.assertTrue(_same_path(_flag(splits[0], "--cwd"), tester))
        split_env = _env_from_args(splits[0])
        for key in lch.PANE_ENV_KEYS:
            self.assertEqual(split_env[key], spec.environment[key])
        self.assertIn(splits[0][2], herdr.closed_panes)
        parent = herdr.workspaces[handle.parent_workspace_id]
        child = herdr.workspaces[handle.child_workspace_id]
        self.assertEqual(parent["tokens"][lch.METADATA_TOKEN_RUN], RUN_HASH)
        self.assertEqual(parent["tokens"][lch.METADATA_TOKEN_KIND], lch.METADATA_KIND_RUN)
        self.assertFalse(parent["worktree"]["is_linked_worktree"])
        self.assertTrue(child["worktree"]["is_linked_worktree"])
        self.assertEqual(child["tokens"][lch.METADATA_TOKEN_LANE], TESTS_LANE)
        self.assertEqual(
            child["tokens"][lch.METADATA_TOKEN_PARENT], handle.parent_workspace_id
        )
        self.assertEqual(layout.role_panes, {"tester": handle.pane_id})
        self.assertNotIn("test-reviewer", layout.role_panes)
        self.assertNotIn("builder", layout.role_panes)
        self.assertEqual(list(launcher._tabs), [TESTS_LANE])

    def test_sibling_lane_is_absent_until_it_dispatches(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            _place(launcher, herdr, _spec(tester, lane=TESTS_LANE, role="tester"))
            self.assertNotIn(BUILD_LANE, launcher._tabs)
            opens_before = sum(1 for call in herdr.calls if call[:2] == ("worktree", "open"))
            builder = _checkout(root, "builder")
            handle, layout, _ = _place(
                launcher, herdr, _spec(builder, lane=BUILD_LANE, role="builder")
            )
        self.assertEqual(
            sum(1 for call in herdr.calls if call[:2] == ("worktree", "open")),
            opens_before + 1,
        )
        self.assertEqual(
            sum(1 for call in herdr.calls if call[:2] == ("workspace", "create")),
            1,
        )
        self.assertEqual(handle.lane_key, BUILD_LANE)
        self.assertEqual(layout.lane_key, BUILD_LANE)
        self.assertNotEqual(
            launcher._tabs[TESTS_LANE].child_workspace_id,
            layout.child_workspace_id,
        )
        self.assertEqual(
            launcher._tabs[TESTS_LANE].parent_workspace_id,
            layout.parent_workspace_id,
        )


class SameLanePlacementTest(unittest.TestCase):
    def test_every_role_uses_a_scratch_bound_split_inside_child(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            (tester / "secret.txt").write_text("private-tests", encoding="utf-8")
            reviewer = _checkout(root, "reviewer")
            builder = _checkout(root, "builder")
            tester_spec = _spec(tester, lane=TESTS_LANE, role="tester")
            first, layout, _ = _place(launcher, herdr, tester_spec)
            splits_before = [
                call for call in herdr.calls if call[:2] == ("pane", "split")
            ]
            self.assertEqual(len(splits_before), 1)
            self.assertIn(splits_before[0][2], herdr.closed_panes)
            self.assertEqual(first.pane_id, layout.panes[0])
            first_env = _env_from_args(splits_before[0])
            for key in lch.PANE_ENV_KEYS:
                self.assertEqual(first_env[key], tester_spec.environment[key])
            self.assertEqual(
                herdr.panes[first.pane_id]["tokens"][lch.METADATA_TOKEN_SCRATCH],
                lch.METADATA_SCRATCH_REDIRECT,
            )
            reviewer_spec = _spec(
                reviewer, lane=TESTS_LANE, role="test-reviewer"
            )
            second, same, _ = _place(launcher, herdr, reviewer_spec)
            self.assertIs(same, layout)
            splits = [call for call in herdr.calls if call[:2] == ("pane", "split")]
            self.assertEqual(len(splits), 2)
            self.assertEqual(splits[1][2], first.pane_id)
            self.assertIn("--no-focus", splits[1])
            self.assertNotIn("--current", splits[1])
            self.assertTrue(_same_path(_flag(splits[1], "--cwd"), reviewer))
            self.assertEqual(lch.workspace_of(second.pane_id), first.child_workspace_id)
            self.assertEqual(herdr.panes[second.pane_id]["label"], "tester-reviewer")
            second_env = _env_from_args(splits[1])
            for key in lch.PANE_ENV_KEYS:
                self.assertEqual(second_env[key], reviewer_spec.environment[key])
            other, other_layout, _ = _place(
                launcher, herdr, _spec(builder, lane=BUILD_LANE, role="builder")
            )
        self.assertEqual(other.parent_workspace_id, first.parent_workspace_id)
        self.assertNotEqual(other.child_workspace_id, first.child_workspace_id)
        self.assertEqual(other_layout.role_panes, {"builder": other.pane_id})
        self.assertFalse(any("--current" in call for call in herdr.calls))
        for call in herdr.calls:
            if call[:2] in (
                ("workspace", "create"),
                ("worktree", "open"),
                ("pane", "split"),
            ):
                self.assertIn("--no-focus", call)

    def test_each_factory_role_gets_its_own_scratch_contract(self) -> None:
        for index, role in enumerate(lch.LANE_PANE_ROLES):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                herdr = FakeHerdr()
                launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
                launcher._herdr = herdr  # type: ignore[method-assign]
                checkout = _checkout(Path(tmp), role)
                spec = _spec(
                    checkout,
                    lane="lane-role-{}".format(index),
                    role=role,
                )
                handle, layout, reused = _place(launcher, herdr, spec)
                splits = [
                    call
                    for call in herdr.calls
                    if call[:2] == ("pane", "split")
                ]
                self.assertFalse(reused)
                self.assertEqual(len(splits), 1)
                self.assertEqual(layout.role_panes, {role: handle.pane_id})
                split_env = _env_from_args(splits[0])
                for key in lch.PANE_ENV_KEYS:
                    self.assertEqual(split_env[key], spec.environment[key])
                self.assertEqual(
                    herdr.panes[handle.pane_id]["tokens"][
                        lch.METADATA_TOKEN_SCRATCH
                    ],
                    lch.METADATA_SCRATCH_REDIRECT,
                )


class ParallelFirstLaunchTest(unittest.TestCase):
    def test_concurrent_lanes_share_one_parent_and_get_one_child_each(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        errors: list[BaseException] = []
        handles: dict[str, lch.LaunchHandle] = {}

        def worker(lane: str, role: str, worktree: Path) -> None:
            try:
                handle, _, _ = _place(
                    launcher, herdr, _spec(worktree, lane=lane, role=role)
                )
                handles[lane] = handle
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            builder = _checkout(root, "builder")
            threads = [
                threading.Thread(target=worker, args=(TESTS_LANE, "tester", tester)),
                threading.Thread(target=worker, args=(BUILD_LANE, "builder", builder)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(set(handles), {TESTS_LANE, BUILD_LANE})
        creates = [call for call in herdr.calls if call[:2] == ("workspace", "create")]
        opens = [call for call in herdr.calls if call[:2] == ("worktree", "open")]
        tabs = [call for call in herdr.calls if call[:2] == ("tab", "create")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(opens), 2)
        self.assertEqual(tabs, [])
        self.assertEqual(
            handles[TESTS_LANE].parent_workspace_id,
            handles[BUILD_LANE].parent_workspace_id,
        )
        self.assertNotEqual(
            handles[TESTS_LANE].child_workspace_id,
            handles[BUILD_LANE].child_workspace_id,
        )
        self.assertEqual(len(launcher._tabs), 2)


class RetainResubmitTest(unittest.TestCase):
    def test_revise_keeps_panes_and_rebinds_fresh_envelope(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            (tester / "secret.txt").write_text("private-tests", encoding="utf-8")
            reviewer = _checkout(root, "reviewer")
            builder = _checkout(root, "builder")
            tester_spec = _spec(tester, lane=TESTS_LANE, role="tester")
            reviewer_spec = _spec(reviewer, lane=TESTS_LANE, role="test-reviewer")
            tester_handle, _, _ = _place(launcher, herdr, tester_spec)
            reviewer_handle, _, _ = _place(launcher, herdr, reviewer_spec)
            old_envelope = tester / "envelope-old.json"
            old_envelope.write_text(
                json.dumps({"success": True, "turn": "old"}), encoding="utf-8"
            )
            object.__setattr__(tester_handle, "envelope_path", old_envelope)
            stale = launcher.poll(tester_handle)
            self.assertEqual(stale.state, lch.PollState.EXITED)
            self.assertEqual(stale.detail, "ENVELOPE_SUCCESS")
            launcher.retain(tester_handle)
            launcher.retain(reviewer_handle)
            self.assertIn(tester_handle.pane_id, herdr.panes)
            self.assertNotIn(tester_handle.pane_id, herdr.closed_panes)
            self.assertNotIn(reviewer_handle.pane_id, herdr.closed_panes)
            self.assertTrue(tester.is_dir())
            self.assertTrue(reviewer.is_dir())
            fresh_prompt = tester / "prompt-2.json"
            fresh_prompt.write_text('{"turn":"repair"}', encoding="utf-8")
            fresh_envelope = tester / "envelope-new.json"
            pane_before = tester_handle.pane_id
            agent_before = tester_handle.agent_name
            cwd_before = tester_handle.launched_cwd
            with (
                mock.patch.object(lch, "wait_for_interactive_agent"),
                mock.patch.object(lch, "submit_agent_prompt"),
            ):
                rebound = launcher.resubmit(
                    tester_handle,
                    fresh_prompt,
                    envelope_path=fresh_envelope,
                )
            self.assertIs(rebound, tester_handle)
            self.assertEqual(rebound.pane_id, pane_before)
            self.assertEqual(rebound.agent_name, agent_before)
            self.assertEqual(rebound.launched_cwd, cwd_before)
            self.assertEqual(rebound.envelope_path, fresh_envelope)
            self.assertTrue(old_envelope.is_file())
            self.assertFalse(fresh_envelope.exists())
            current = launcher.poll(tester_handle)
            self.assertNotEqual(current.detail, "ENVELOPE_SUCCESS")
            self.assertNotEqual(current.state, lch.PollState.EXITED)
            _place(launcher, herdr, _spec(builder, lane=BUILD_LANE, role="builder"))
            builder_opens = [
                call
                for call in herdr.calls
                if call[:2] == ("worktree", "open")
                and _flag(call, "--label") == BUILD_LANE
            ]
            self.assertTrue(builder_opens)
            self.assertTrue(_same_path(_flag(builder_opens[-1], "--path"), builder))
            self.assertFalse(
                any(_same_path(_flag(call, "--path"), tester) for call in builder_opens)
            )


class RediscoveryTest(unittest.TestCase):
    def test_reconstructed_launcher_adopts_exact_match_and_creates_nothing(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        first = _launcher(label)
        first._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            tester = _checkout(Path(tmp), "tester")
            spec = _spec(tester, lane=TESTS_LANE, role="tester")
            handle, _, _ = _place(first, herdr, spec)
            before = list(herdr.calls)
            second = _launcher(label)
            second._herdr = herdr  # type: ignore[method-assign]
            env = dict(spec.environment)
            layout = second._tab_for(spec, tester, (), env)
            self.assertEqual(layout.child_workspace_id, handle.child_workspace_id)
            self.assertEqual(layout.parent_workspace_id, handle.parent_workspace_id)
            self.assertEqual(second._parent_workspace_id, handle.parent_workspace_id)
            extra = herdr.calls[len(before) :]
            self.assertFalse(any(call[:2] == ("workspace", "create") for call in extra))
            self.assertFalse(any(call[:2] == ("worktree", "open") for call in extra))
            self.assertFalse(any(call[:2] == ("pane", "split") for call in extra))

    def test_reconstructed_launcher_replaces_unredirected_role_pane(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        first = _launcher(label)
        first._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            tester = _checkout(Path(tmp), "tester")
            spec = _spec(tester, lane=TESTS_LANE, role="tester")
            old, _, _ = _place(first, herdr, spec)
            herdr.panes[old.pane_id]["tokens"].pop(
                lch.METADATA_TOKEN_SCRATCH, None
            )
            second = _launcher(label)
            second._herdr = herdr  # type: ignore[method-assign]
            second._bind_run_identity(spec)
            env = dict(spec.environment)
            self.assertIsNone(second._reconnect_live_agent(spec, env))
            before = len(herdr.calls)
            replacement, layout, reused = _place(second, herdr, spec)
            extra = herdr.calls[before:]
        self.assertFalse(reused)
        self.assertNotEqual(replacement.pane_id, old.pane_id)
        self.assertEqual(replacement.child_workspace_id, old.child_workspace_id)
        self.assertEqual(layout.role_panes, {"tester": replacement.pane_id})
        self.assertIn(old.pane_id, herdr.closed_panes)
        splits = [call for call in extra if call[:2] == ("pane", "split")]
        self.assertEqual(len(splits), 1)
        split_env = _env_from_args(splits[0])
        for key in lch.PANE_ENV_KEYS:
            self.assertEqual(split_env[key], spec.environment[key])
        self.assertEqual(
            herdr.panes[replacement.pane_id]["tokens"][
                lch.METADATA_TOKEN_SCRATCH
            ],
            lch.METADATA_SCRATCH_REDIRECT,
        )

    def test_missing_child_recreates_only_that_object(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        first = _launcher(label)
        first._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            builder = _checkout(root, "builder")
            handle, _, _ = _place(
                first, herdr, _spec(tester, lane=TESTS_LANE, role="tester")
            )
            parent_id = handle.parent_workspace_id
            herdr.worktrees[parent_id] = []
            second = _launcher(label)
            second._herdr = herdr  # type: ignore[method-assign]
            before = len(herdr.calls)
            rebuilt, layout, _ = _place(
                second, herdr, _spec(builder, lane=BUILD_LANE, role="builder")
            )
            extra = herdr.calls[before:]
            self.assertFalse(any(call[:2] == ("workspace", "create") for call in extra))
            self.assertEqual(
                sum(1 for call in extra if call[:2] == ("worktree", "open")),
                1,
            )
            self.assertEqual(rebuilt.parent_workspace_id, parent_id)
            self.assertEqual(layout.parent_workspace_id, parent_id)

    def test_untagged_space_on_the_repo_is_adopted_without_tagging(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        with tempfile.TemporaryDirectory() as tmp:
            launcher._repository_root = Path(tmp)
            operator = herdr.add_workspace("FDAdb", tmp)
            launcher._herdr = herdr  # type: ignore[method-assign]
            self.assertEqual(launcher._run_workspace({}), operator)
        self.assertFalse(any(call[:2] == ("workspace", "create") for call in herdr.calls))
        self.assertFalse(
            any(call[:2] == ("workspace", "report-metadata") for call in herdr.calls)
        )
        self.assertNotIn("tokens", herdr.workspaces[operator])

    def test_second_space_on_the_repo_is_not_the_parent(self) -> None:
        """Two Spaces on one repository is not a tie to break.

        Herdr binds a repository to one source Space -- the first opened on
        its primary checkout -- and a later Space on the same checkout is
        neither the source nor bound at all. The parent is therefore the
        operator's own Space even when a Maestro-tagged leftover from an
        earlier run is still open beside it, and nothing is created.
        """
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        with tempfile.TemporaryDirectory() as tmp:
            launcher._repository_root = Path(tmp)
            first = herdr.add_workspace("FDAdb", tmp)
            second = herdr.add_workspace(label, tmp, tokens=launcher._parent_identity_tokens())
            launcher._herdr = herdr  # type: ignore[method-assign]
            before = herdr.snapshot()
            self.assertEqual(launcher._run_workspace({}), first)
        self.assertFalse(any(call[:2] == ("workspace", "create") for call in herdr.calls))
        # The operator's Space is not Maestro's to tag, and the stale
        # tagged Space is left exactly as it was for them to close.
        self.assertNotIn("tokens", herdr.workspaces[first])
        self.assertTrue(herdr.records_unchanged(before, {second}))

    def test_wrong_parent_child_is_not_adopted(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        launcher._herdr = herdr  # type: ignore[method-assign]
        parent_tokens = launcher._parent_identity_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_id = herdr.add_workspace(label, root, tokens=parent_tokens)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            other = herdr.add_workspace("elsewhere", elsewhere)
            wrong = _checkout(root, "wrong")
            wrong_id = herdr.open_child(
                parent_id,
                wrong,
                TESTS_LANE,
                tokens=launcher._lane_identity_tokens(TESTS_LANE, other),
            )
            tester = _checkout(root, "tester")
            spec = _spec(tester, lane=TESTS_LANE, role="tester")
            # Ours by run/repo/lane but pinned to a different live Space: a
            # typed refusal, never a second child beside it.
            with self.assertRaises(lch.LaunchRefused) as raised:
                _place(launcher, herdr, spec)
        self.assertIn("LANE_WORKSPACE_TOKEN_MISMATCH:{}".format(wrong_id), raised.exception.detail)
        self.assertFalse(any(call[:2] in (("worktree", "open"), ("pane", "split")) for call in herdr.calls))
        self.assertEqual(
            herdr.workspaces[wrong_id]["tokens"][lch.METADATA_TOKEN_PARENT], other
        )

    def test_wrong_cwd_and_unknown_agent_refuse_reconnect(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            other = _checkout(root, "other")
            spec = _spec(tester, lane=TESTS_LANE, role="tester")
            handle, _, _ = _place(launcher, herdr, spec)
            herdr.panes[handle.pane_id]["cwd"] = str(other.resolve())
            fresh = _launcher(label)
            fresh._herdr = herdr  # type: ignore[method-assign]
            with self.assertRaises(lch.LaunchRefused) as raised:
                fresh._reconnect_live_agent(spec, dict(spec.environment))
            self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
            herdr.panes[handle.pane_id]["cwd"] = str(tester.resolve())
            herdr.set_agent_status(handle.agent_name, "blocked")
            blocked = _launcher(label)
            blocked._herdr = herdr  # type: ignore[method-assign]
            with self.assertRaises(lch.LaunchRefused) as blocked_raised:
                blocked._reconnect_live_agent(spec, dict(spec.environment))
            self.assertEqual(
                blocked_raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertIn("AGENT_STATUS_UNOBSERVABLE", blocked_raised.exception.detail)

    def test_wrong_agent_name_refuses_reconnect(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            tester = _checkout(Path(tmp), "tester")
            spec = _spec(tester, lane=TESTS_LANE, role="tester")
            handle, _, _ = _place(launcher, herdr, spec)
            herdr.agents[handle.agent_name]["name"] = "not-the-stable-agent"
            fresh = _launcher(label)
            fresh._herdr = herdr  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError) as raised:
                fresh._reconnect_live_agent(spec, dict(spec.environment))
            self.assertIn("HERDR_AGENT_NAME_MISMATCH", str(raised.exception))


class FinalReviewPlacementTest(unittest.TestCase):
    def test_integration_reviewer_is_lazy_in_last_lane_child(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            builder = _checkout(root, "builder")
            integrator = _checkout(root, "integration-reviewer")
            _place(launcher, herdr, _spec(tester, lane=TESTS_LANE, role="tester"))
            build_handle, build_layout, _ = _place(
                launcher, herdr, _spec(builder, lane=BUILD_LANE, role="builder")
            )
            self.assertNotIn("integration-reviewer", build_layout.role_panes)
            self.assertNotIn(
                "integration-reviewer", launcher._tabs[TESTS_LANE].role_panes
            )
            review, layout, _ = _place(
                launcher,
                herdr,
                _spec(integrator, lane=BUILD_LANE, role="integration-reviewer"),
            )
            launcher.retain(review)
            launcher.retain(build_handle)
        self.assertEqual(layout.child_workspace_id, build_handle.child_workspace_id)
        self.assertEqual(review.child_workspace_id, build_handle.child_workspace_id)
        self.assertNotEqual(
            review.child_workspace_id,
            launcher._tabs[TESTS_LANE].child_workspace_id,
        )
        self.assertEqual(herdr.panes[review.pane_id]["label"], "integration-reviewer")
        self.assertNotIn(review.pane_id, herdr.closed_panes)
        self.assertNotIn(build_handle.child_workspace_id, herdr.closed_workspaces)
        self.assertFalse(any(call[:2] == ("workspace", "close") for call in herdr.calls))


class RenameCloseTest(unittest.TestCase):
    def _two_roles(self, herdr: FakeHerdr, launcher: lch.HerdrLauncher, root: Path):
        tester = _checkout(root, "tester")
        reviewer = _checkout(root, "reviewer")
        tester_handle, _, _ = _place(
            launcher, herdr, _spec(tester, lane=TESTS_LANE, role="tester")
        )
        reviewer_handle, _, _ = _place(
            launcher, herdr, _spec(reviewer, lane=TESTS_LANE, role="test-reviewer")
        )
        return tester, reviewer, tester_handle, reviewer_handle

    def test_rename_then_close_children_only(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            tester, reviewer, tester_handle, reviewer_handle = self._two_roles(
                herdr, launcher, Path(tmp)
            )
            before = len(herdr.calls)
            launcher.complete_run(
                [tester_handle, reviewer_handle],
                project_identity=PROJECT,
                timeout_s=1.0,
            )
            extra = herdr.calls[before:]
            texts = [call for call in extra if call[:2] == ("pane", "send-text")]
            keys = [call for call in extra if call[:2] == ("pane", "send-keys")]
            closes = [call for call in extra if call[:2] == ("workspace", "close")]
            self.assertEqual(len(texts), 2)
            self.assertTrue(all(call[3].startswith("/rename ") for call in texts))
            self.assertEqual(
                {call[3][len("/rename ") :] for call in texts},
                {
                    lch.session_name_for(PROJECT, RUN_HASH, TESTS_LANE, "tester"),
                    lch.session_name_for(
                        PROJECT, RUN_HASH, TESTS_LANE, "test-reviewer"
                    ),
                },
            )
            self.assertTrue(all(call[3] == "enter" for call in keys))
            self.assertEqual(
                [call[2] for call in closes],
                [tester_handle.child_workspace_id],
            )
            self.assertIn(tester_handle.child_workspace_id, herdr.closed_workspaces)
            # The parent Space is never closed by Maestro.
            self.assertNotIn(tester_handle.parent_workspace_id, herdr.closed_workspaces)
            self.assertTrue(tester.is_dir())
            self.assertTrue(reviewer.is_dir())
            again = len(herdr.calls)
            launcher.complete_run(
                [tester_handle, reviewer_handle],
                project_identity=PROJECT,
                timeout_s=1.0,
            )
            self.assertFalse(
                any(call[:2] == ("workspace", "create") for call in herdr.calls[again:])
            )
            self.assertFalse(
                any(call[:2] == ("worktree", "open") for call in herdr.calls[again:])
            )
            self.assertFalse(
                any(call[:2] == ("workspace", "close") for call in herdr.calls[again:])
            )

    def test_rename_failure_leaves_workspaces_and_cwds(self) -> None:
        herdr = FakeHerdr()
        herdr.wait_output_error = "wait_output_timeout"
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            tester, reviewer, tester_handle, reviewer_handle = self._two_roles(
                herdr, launcher, Path(tmp)
            )
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher.complete_run(
                    [tester_handle, reviewer_handle],
                    project_identity=PROJECT,
                    timeout_s=1.0,
                )
            self.assertEqual(
                raised.exception.refusal, lch.LaunchRefusal.SESSION_RENAME_UNCONFIRMED
            )
            self.assertFalse(
                any(call[:2] == ("workspace", "close") for call in herdr.calls)
            )
            self.assertNotIn(tester_handle.child_workspace_id, herdr.closed_workspaces)
            self.assertTrue(tester.is_dir())
            self.assertTrue(reviewer.is_dir())

    def test_close_failure_leaves_cwd(self) -> None:
        herdr = FakeHerdr()
        herdr.close_workspace_error = "busy"
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            tester, reviewer, tester_handle, reviewer_handle = self._two_roles(
                herdr, launcher, Path(tmp)
            )
            with self.assertRaises(lch.HerdrCallError):
                launcher.complete_run(
                    [tester_handle, reviewer_handle],
                    project_identity=PROJECT,
                    timeout_s=1.0,
                )
            self.assertNotIn(tester_handle.child_workspace_id, herdr.closed_workspaces)
            self.assertTrue(tester.is_dir())
            self.assertTrue(reviewer.is_dir())


# ---- closure-matrix helpers ---------------------------------------------------

FIVE_ROLES = (
    "tester",
    "test-reviewer",
    "builder",
    "code-reviewer",
    "integration-reviewer",
)


@contextlib.contextmanager
def _launch_patches() -> Iterator[None]:
    """Patch only the composer steps that need a live model.

    Prompt preparation, readiness wait, prompt submission and liveness pid are
    stubbed; placement, labelling, tagging, agent start and adoption all run
    for real. Entered once per test: `mock.patch` is not thread-safe, so
    threads racing `launch` share one entry (see `_race`).
    """
    with (
        mock.patch.object(lch, "prepare_route_prompt"),
        mock.patch.object(lch, "preflight_launch_prompt"),
        mock.patch.object(lch, "build_omp_argv", return_value=("omp", "--pm-profile", "grok")),
        mock.patch.object(lch, "_wait_for_available_shell"),
        mock.patch.object(lch, "wait_for_interactive_agent"),
        mock.patch.object(lch, "submit_agent_prompt"),
        mock.patch.object(lch, "pane_liveness_pid", return_value=None),
    ):
        yield


def _drive(launcher: lch.HerdrLauncher, spec: lch.LaunchSpec) -> lch.LaunchHandle:
    """`launch` under an already-entered `_launch_patches`."""
    if not spec.prompt_path.exists():
        spec.prompt_path.parent.mkdir(parents=True, exist_ok=True)
        spec.prompt_path.write_text("{}", encoding="utf-8")
    return launcher.launch(spec)


def _launch(
    launcher: lch.HerdrLauncher, herdr: FakeHerdr, spec: lch.LaunchSpec
) -> lch.LaunchHandle:
    """Drive the real `HerdrLauncher.launch` against the fake."""
    del herdr
    with _launch_patches():
        return _drive(launcher, spec)


def _live_workspaces(herdr: FakeHerdr) -> dict[str, dict]:
    return {
        wid: rec
        for wid, rec in herdr.workspaces.items()
        if wid not in herdr.closed_workspaces
    }


def _is_linked(record: dict) -> bool:
    info = record.get("worktree")
    return isinstance(info, dict) and info.get("is_linked_worktree") is True


def _run_parents(herdr: FakeHerdr, launcher: lch.HerdrLauncher) -> list[str]:
    """Non-linked live Spaces bound to the launcher's primary checkout.

    Under Shape A that is the parent's whole identity: the operator's own
    Space when one is open on the repository, else the one Maestro created.
    """
    root = Path(launcher._repository_root).resolve()
    return [
        wid
        for wid, rec in _live_workspaces(herdr).items()
        if isinstance(rec.get("worktree"), dict)
        and rec["worktree"].get("is_linked_worktree") is False
        and Path(str(rec["worktree"].get("repo_root") or "")).resolve() == root
    ]


def _lane_children(
    herdr: FakeHerdr, launcher: lch.HerdrLauncher, parent_id: str, lane: str
) -> list[str]:
    expected = launcher._lane_identity_tokens(lane, parent_id)
    # Open worktrees are a repository-level fact (listed under whichever
    # Space opened them); the parent relation is the child's `parent` token.
    listed = {
        item.get("open_workspace_id")
        for items in herdr.worktrees.values()
        for item in items
        if item.get("open_workspace_id")
    }
    return [
        wid
        for wid, rec in _live_workspaces(herdr).items()
        if wid in listed
        and _is_linked(rec)
        and lch._tokens_match(lch._herdr_tokens(rec), expected)
    ]


def _live_panes(herdr: FakeHerdr, child_id: str) -> dict[str, dict]:
    return {
        pid: pane
        for pid, pane in herdr.panes.items()
        if pane["workspace_id"] == child_id and pid not in herdr.closed_panes
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
) -> str:
    """Exactly 1 parent, 1 direct child per lane, 1 pane + 1 agent per role.

    Returns the parent id. Also proves there is no orphan: every live pane in
    a lane child is a role pane, and no other live workspace carries this
    run's tokens (no nested or duplicate Space).
    """
    parents = _run_parents(herdr, launcher)
    case.assertEqual(len(parents), 1, "run parents: {}".format(parents))
    parent_id = parents[0]
    children_seen: set[str] = set()
    for lane, roles in lanes.items():
        children = _lane_children(herdr, launcher, parent_id, lane)
        case.assertEqual(len(children), 1, "children of {}: {}".format(lane, children))
        child_id = children[0]
        children_seen.add(child_id)
        panes = _live_panes(herdr, child_id)
        for role, spec in roles.items():
            label = lch.pane_label_for(role)
            role_panes = [
                pid
                for pid, pane in panes.items()
                if pane.get("label") == label
            ]
            case.assertEqual(
                len(role_panes), 1, "{} panes for {}: {}".format(lane, role, role_panes)
            )
            pane = panes[role_panes[0]]
            tokens = lch._herdr_tokens(pane)
            case.assertEqual(tokens.get(lch.METADATA_TOKEN_SCRATCH), lch.METADATA_SCRATCH_REDIRECT)
            case.assertEqual(tokens.get(lch.METADATA_TOKEN_ROLE), role)
            case.assertEqual(tokens.get(lch.METADATA_TOKEN_LANE), lane)
            case.assertEqual(tokens.get(lch.METADATA_TOKEN_PARENT), parent_id)
            case.assertTrue(_same_path(pane.get("cwd"), spec.worktree))
            agents = _live_agents_in(herdr, role_panes[0])
            case.assertEqual(agents, [lch.agent_name_for(spec.correlation_token)])
        for pid, pane in panes.items():
            case.assertTrue(
                pane.get("label") and lch.pane_role_for_label(str(pane["label"])),
                "orphan pane {} in {}".format(pid, lane),
            )
    for wid, rec in _live_workspaces(herdr).items():
        tokens = lch._herdr_tokens(rec)
        if tokens.get(lch.METADATA_TOKEN_RUN) == launcher._run_id and tokens.get(
            lch.METADATA_TOKEN_REPO
        ) == launcher._repository_fingerprint:
            case.assertIn(wid, {parent_id, *children_seen}, "stray run object {}".format(wid))
    return parent_id


def _ids_of(herdr: FakeHerdr, *workspace_ids: str) -> set[str]:
    ids = set(workspace_ids)
    ids.update(pid for pid, pane in herdr.panes.items() if pane["workspace_id"] in workspace_ids)
    ids.update(tid for tid, tab in herdr.tabs.items() if tab["workspace_id"] in workspace_ids)
    ids.update(
        name for name, agent in herdr.agents.items()
        if herdr.panes.get(str(agent.get("pane_id") or ""), {}).get("workspace_id") in workspace_ids
    )
    return ids


def _plant_foreign(herdr: FakeHerdr, root: Path) -> set[str]:
    """Herdr objects that are not this run's: another repository's Space with
    a linked child and a live agent, and a stale Space tagged for another run
    on a third repository. Returns every id so a test can prove they were
    untouched."""
    other_repo = root / "other-repo"
    other_repo.mkdir(parents=True, exist_ok=True)
    other = herdr.add_workspace("other-repo", other_repo)
    feature = root / "other-repo-feature"
    feature.mkdir(parents=True, exist_ok=True)
    child = herdr.open_child(other, feature, "feature")
    pane_id = next(iter(_live_panes(herdr, child)))
    herdr.start_agent("other-repo-claude", pane_id, status="working")
    stale_repo = root / "stale-run-repo"
    stale_repo.mkdir(parents=True, exist_ok=True)
    stale = herdr.add_workspace(
        lch.workspace_label_for(PROJECT, "deadbeef" * 4),
        stale_repo,
        tokens={
            lch.METADATA_TOKEN_KIND: lch.METADATA_KIND_RUN,
            lch.METADATA_TOKEN_RUN: "deadbeef" * 4,
            lch.METADATA_TOKEN_REPO: "repo-stale",
        },
    )
    return _ids_of(herdr, other, child, stale)


def _plant_operator_space(herdr: FakeHerdr, root: Path) -> str:
    """The operator's own Space on the repository (Shape A's parent): open at
    the primary checkout, untagged, with a second tab and a pane where the
    operator's own agent is working. Everything in it must stay untouched."""
    operator = herdr.add_workspace(PROJECT, root)
    tab = herdr._new_tab(operator, "notes")
    pane = herdr._new_pane(operator, tab["tab_id"], str(root))
    herdr.start_agent("operator-claude", pane["pane_id"], status="working")
    return operator


def _restore_listing(
    herdr: FakeHerdr, parent_id: str, listing: list[dict]
) -> Callable[[tuple[str, ...]], None]:
    """A `worktree open` hook: the parent's listing appears only now, as if
    another process had opened the child between our listing and our open."""

    def hook(_args: tuple[str, ...]) -> None:
        herdr.worktrees.setdefault(parent_id, listing)

    return hook


def _calls_after(herdr: FakeHerdr, mark: int, *verbs: tuple[str, str]) -> list[tuple[str, ...]]:
    return [call for call in herdr.calls[mark:] if call[:2] in verbs]


CREATE = ("workspace", "create")
OPEN = ("worktree", "open")
SPLIT = ("pane", "split")
START = ("agent", "start")


class _RunFixture:
    """One run: a repository root with role checkouts, one fake, launchers.

    By default the operator's own Space is open on the repository (Shape A);
    `operator=False` models no Space open, where Maestro creates the parent.
    """

    def __init__(self, tmp: str, *, operator: bool = True) -> None:
        self.root = Path(tmp)
        self.herdr = FakeHerdr()
        self.label = lch.workspace_label_for(PROJECT, RUN_HASH)
        self.foreign = _plant_foreign(self.herdr, self.root)
        self.operator = _plant_operator_space(self.herdr, self.root) if operator else ""
        if self.operator:
            self.foreign |= _ids_of(self.herdr, self.operator)
        self.before = self.herdr.snapshot()

    def launcher(self) -> lch.HerdrLauncher:
        launcher = _launcher(self.label)
        launcher._herdr = self.herdr  # type: ignore[method-assign]
        launcher._repository_root = self.root.resolve()
        return launcher

    def spec(self, lane: str, role: str, checkout: str = "") -> lch.LaunchSpec:
        path = self.root / (checkout or "{}-{}".format(lane, role))
        if not path.exists():
            _checkout(self.root, path.name)
        return _spec(path, lane=lane, role=role, repository_root=self.root)

    def foreign_untouched(self) -> bool:
        return self.herdr.records_unchanged(self.before, self.foreign)


class HerdrTopologySpecTest(unittest.TestCase):
    """The spec's 'Herdr topology and resume' items, under Shape A: lanes
    are linked children of the operator's own Space on the repository."""

    def test_00_operator_space_reporting_no_binding_is_still_the_parent(self) -> None:
        """The Space an operator has open reports no `worktree` of its own.

        Herdr fills `WorkspaceInfo.worktree` in only for a Space it bound
        when it created it, and never backfills, so the Space the operator
        opened -- the very Space Shape A hangs lanes under -- reports none
        while `worktree list` names it the repository's source. Reading the
        record's field instead of asking Herdr made every operator Space
        look unbound: the run then created a second Space, which Herdr hands
        no binding either because the repository already has a source, and
        refused it as `RUN_WORKSPACE_UNBOUND:...:NO_WORKTREE_BINDING`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            listed = run.herdr("workspace", "list")["result"]["workspaces"]
            operator = next(
                item for item in listed if item["workspace_id"] == run.operator
            )
            self.assertNotIn("worktree", operator)
            fetched = run.herdr("workspace", "get", run.operator)
            self.assertNotIn("worktree", fetched["result"]["workspace"])
            # Herdr still knows the binding, and answers when asked.
            source = run.herdr("worktree", "list", "--cwd", str(run.root))
            self.assertEqual(
                source["result"]["source"]["source_workspace_id"], run.operator
            )
            spec = run.spec(TESTS_LANE, "tester")
            handle = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(handle.parent_workspace_id, run.operator)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())

    def test_01_fresh_first_lane_hangs_under_the_operators_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            handle = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(handle.parent_workspace_id, run.operator)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            opens = _calls_after(run.herdr, 0, OPEN)
            self.assertEqual(len(opens), 1)
            self.assertEqual(_flag(opens[0], "--workspace"), run.operator)
            self.assertEqual(_flag(opens[0], "--label"), TESTS_LANE)
            child = run.herdr.workspaces[handle.child_workspace_id]
            self.assertEqual(child["tokens"][lch.METADATA_TOKEN_PARENT], run.operator)
            self.assertEqual(child["tokens"][lch.METADATA_TOKEN_RUN], RUN_HASH)
            self.assertNotIn("tokens", run.herdr.workspaces[run.operator])
            self.assertFalse(
                any(call[:2] == ("workspace", "report-metadata") and call[2] == run.operator
                    for call in run.herdr.calls)
            )
            self.assertTrue(run.foreign_untouched())
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})

    def test_02_no_space_open_creates_one_parent_at_the_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            spec = run.spec(TESTS_LANE, "tester")
            handle = _launch(run.launcher(), run.herdr, spec)
            creates = _calls_after(run.herdr, 0, CREATE)
            self.assertEqual(len(creates), 1)
            self.assertTrue(_same_path(_flag(creates[0], "--cwd"), run.root))
            opens = _calls_after(run.herdr, 0, OPEN)
            self.assertEqual(len(opens), 1)
            self.assertEqual(_flag(opens[0], "--workspace"), handle.parent_workspace_id)
            parent = run.herdr.workspaces[handle.parent_workspace_id]
            self.assertFalse(_is_linked(parent))
            self.assertEqual(parent["tokens"][lch.METADATA_TOKEN_KIND], lch.METADATA_KIND_RUN)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())

    def test_a_second_space_on_the_repo_is_ignored(self) -> None:
        """A second Space open on the repository does not divert the lanes.

        Herdr's source Space for a repository is the first opened on its
        primary checkout, so a second one is not a candidate parent: lanes
        stay under the operator's Space and the second Space is untouched.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            second = run.herdr.add_workspace("FDAdb (2)", run.root)
            before = run.herdr.snapshot()
            handle = _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))
            self.assertEqual(handle.parent_workspace_id, run.operator)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            self.assertTrue(run.herdr.records_unchanged(before, run.foreign | _ids_of(run.herdr, second)))

    def test_11b_two_lanes_are_siblings_under_the_operators_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            builder = run.spec(BUILD_LANE, "builder")
            first = _launch(run.launcher(), run.herdr, tester)
            second = _launch(run.launcher(), run.herdr, builder)
            self.assertEqual(first.parent_workspace_id, run.operator)
            self.assertEqual(second.parent_workspace_id, run.operator)
            self.assertNotEqual(first.child_workspace_id, second.child_workspace_id)
            listed = [
                item["open_workspace_id"] for item in run.herdr.worktrees[run.operator]
            ]
            self.assertEqual(sorted(listed), sorted([first.child_workspace_id, second.child_workspace_id]))
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            _assert_converged(
                self, run.herdr, run.launcher(),
                {TESTS_LANE: {"tester": tester}, BUILD_LANE: {"builder": builder}},
            )
            self.assertTrue(run.foreign_untouched())

    def test_completion_closes_lane_children_and_never_the_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            builder = run.spec(BUILD_LANE, "builder")
            handles = [_launch(launcher, run.herdr, tester), _launch(launcher, run.herdr, builder)]
            mark = len(run.herdr.calls)
            launcher.complete_run(handles, project_identity=PROJECT, timeout_s=1.0)
            closes = [call[2] for call in _calls_after(run.herdr, mark, ("workspace", "close"))]
            self.assertEqual(sorted(closes), sorted(h.child_workspace_id for h in handles))
            self.assertNotIn(run.operator, run.herdr.closed_workspaces)
            self.assertTrue(run.foreign_untouched())
            self.assertFalse(
                any(call[:2] in (("tab", "close"), ("pane", "close"), ("pane", "rename"))
                    and (call[2] in run.foreign or run.herdr.panes.get(call[2], {}).get("workspace_id") == run.operator)
                    for call in run.herdr.calls)
            )

    def test_parent_vanishing_mid_run_refuses_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            _launch(launcher, run.herdr, tester)
            # The operator closes their Space; its linked children go too.
            run.herdr.closed_workspaces.add(run.operator)
            for pid, pane in run.herdr.panes.items():
                if pane["workspace_id"] == run.operator:
                    run.herdr.closed_panes.add(pid)
            for wid in [item["open_workspace_id"] for item in run.herdr.worktrees.get(run.operator, ())]:
                run.herdr.closed_workspaces.add(wid)
                for pid, pane in run.herdr.panes.items():
                    if pane["workspace_id"] == wid:
                        run.herdr.closed_panes.add(pid)
            builder = run.spec(BUILD_LANE, "builder")
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(launcher, run.herdr, builder)
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED)
            self.assertIn("PARENT_WORKSPACE_GONE", raised.exception.detail)
            self.assertIn(run.operator, raised.exception.detail)
            self.assertFalse(raised.exception.pane_created)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            # The dead id is released; the next launch resolves the parent by
            # the normal rule again (no Space open on the repo -> create one).
            mark = len(run.herdr.calls)
            handle = _launch(launcher, run.herdr, builder)
            self.assertNotEqual(handle.parent_workspace_id, run.operator)
            self.assertEqual(len(_calls_after(run.herdr, mark, CREATE)), 1)
            _assert_converged(self, run.herdr, launcher, {BUILD_LANE: {"builder": builder}})

    def test_04_reconstructed_launcher_with_everything_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, spec)
            mark = len(run.herdr.calls)
            again = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(again.pane_id, first.pane_id)
            self.assertEqual(again.agent_name, first.agent_name)
            self.assertEqual(again.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())

    def test_05_existing_parent_and_lane_missing_role_pane_creates_one_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            reviewer = run.spec(TESTS_LANE, "test-reviewer")
            mark = len(run.herdr.calls)
            second = _launch(run.launcher(), run.herdr, reviewer)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 1)
            self.assertEqual(len(_calls_after(run.herdr, mark, START)), 1)
            self.assertEqual(second.child_workspace_id, first.child_workspace_id)
            _assert_converged(
                self,
                run.herdr,
                run.launcher(),
                {TESTS_LANE: {"tester": tester, "test-reviewer": reviewer}},
            )
            self.assertTrue(run.foreign_untouched())

    def test_07_already_open_tagged_lane_is_adopted_with_its_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            # The agent finished (Herdr forgets it); the listing lags: the
            # child is open but not yet reported under the parent, so the
            # launcher's own `worktree open` is what finds it open.
            run.herdr.agents.pop(first.agent_name)
            run.herdr.panes[first.pane_id]["agent_status"] = "unknown"
            listing = run.herdr.worktrees.pop(first.parent_workspace_id)
            run.herdr.hooks_before.setdefault(OPEN, []).append(
                _restore_listing(run.herdr, first.parent_workspace_id, listing)
            )
            mark = len(run.herdr.calls)
            second = _launch(run.launcher(), run.herdr, tester)
            opens = _calls_after(run.herdr, mark, OPEN)
            self.assertEqual(len(opens), 1)
            self.assertEqual(_flag(opens[0], "--workspace"), first.parent_workspace_id)
            self.assertEqual(second.child_workspace_id, first.child_workspace_id)
            self.assertEqual(second.tab_id, first.tab_id)
            # The loaded layout held the stale tester pane; it is replaced
            # in place, not duplicated beside a second child.
            self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 1)
            self.assertIn(first.pane_id, run.herdr.closed_panes)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})

    def test_07b_already_open_untagged_lane_is_adopted_by_path_and_tagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            child = run.herdr.workspaces[first.child_workspace_id]
            child.pop("tokens")
            run.herdr.agents.pop(first.agent_name)
            run.herdr.panes[first.pane_id]["agent_status"] = "unknown"
            listing = run.herdr.worktrees.pop(first.parent_workspace_id)
            run.herdr.hooks_before.setdefault(OPEN, []).append(
                _restore_listing(run.herdr, first.parent_workspace_id, listing)
            )
            mark = len(run.herdr.calls)
            again = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(again.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, OPEN)), 1)
            self.assertEqual(
                child["tokens"],
                run.launcher()._lane_identity_tokens(TESTS_LANE, first.parent_workspace_id),
            )
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})

    def test_07c_already_open_untagged_child_at_another_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            run.herdr.workspaces[first.child_workspace_id].pop("tokens")
            run.herdr.agents.pop(first.agent_name)
            listing = run.herdr.worktrees.pop(first.parent_workspace_id)
            run.herdr.hooks_before.setdefault(OPEN, []).append(
                _restore_listing(run.herdr, first.parent_workspace_id, listing)
            )
            # The open child holds our exact path but Herdr lists it under
            # another source workspace, not this run's parent: the parent
            # relation fails, so it is not ours to tag.
            elsewhere = run.herdr.add_workspace("elsewhere", run.root / "other-repo")
            run.herdr.worktrees[elsewhere] = listing
            run.herdr.hooks_before[OPEN].clear()
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, tester)
            self.assertIn("LABEL_ONLY_LANE_WORKSPACE", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, SPLIT, START), [])
            self.assertNotIn("tokens", run.herdr.workspaces[first.child_workspace_id])

    def test_08_linked_workspace_with_run_label_is_never_the_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            nested = _checkout(run.root, "nested")
            linked_tagged = run.herdr.add_workspace(
                run.label, nested, linked=True, repo_root=run.root,
                tokens=launcher._parent_identity_tokens(),
            )
            nested_two = _checkout(run.root, "nested-two")
            linked_label_only = run.herdr.add_workspace(
                run.label, nested_two, linked=True, repo_root=run.root
            )
            tester = run.spec(TESTS_LANE, "tester")
            handle = _launch(launcher, run.herdr, tester)
            self.assertNotIn(handle.parent_workspace_id, (linked_tagged, linked_label_only))
            self.assertEqual(handle.parent_workspace_id, run.operator)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            opens = _calls_after(run.herdr, 0, OPEN)
            self.assertEqual(_flag(opens[0], "--workspace"), handle.parent_workspace_id)
            for stray in (linked_tagged, linked_label_only):
                self.assertNotIn(stray, run.herdr.closed_workspaces)
                self.assertEqual(run.herdr.workspaces[stray], run.before["workspaces"].get(stray, run.herdr.workspaces[stray]))
            self.assertTrue(run.foreign_untouched())

    def test_09_duplicate_exact_lane_refuses_without_creating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            parent_id = run.operator
            tokens = launcher._lane_identity_tokens(TESTS_LANE, parent_id)
            for name in ("dup-a", "dup-b"):
                run.herdr.open_child(parent_id, _checkout(run.root, name), TESTS_LANE, tokens=tokens)
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(launcher, run.herdr, run.spec(TESTS_LANE, "tester"))
            self.assertIn("DUPLICATE_LANE_WORKSPACE", raised.exception.detail)
            self.assertFalse(raised.exception.pane_created)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            self.assertTrue(run.foreign_untouched())

    def test_10_two_concurrent_first_dispatches_same_lane_share_one_child(self) -> None:
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
                gate.wait()
                try:
                    outcomes[role] = _drive(launcher, specs[role])
                except BaseException as exc:  # noqa: BLE001
                    outcomes[role] = exc

            threads = [threading.Thread(target=worker, args=(role,)) for role in specs]
            with _launch_patches():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            for role, outcome in outcomes.items():
                self.assertIsInstance(outcome, lch.LaunchHandle, "{}: {!r}".format(role, outcome))
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, 0, OPEN)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: specs})
            self.assertTrue(run.foreign_untouched())

    def test_12_revision_reuses_pane_and_agent_for_every_role(self) -> None:
        for role in FIVE_ROLES:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                run = _RunFixture(tmp)
                launcher = run.launcher()
                spec = run.spec(BUILD_LANE, role)
                handle = _launch(launcher, run.herdr, spec)
                revised = run.root / "prompt-revise.json"
                revised.write_text('{"turn": "revise"}', encoding="utf-8")
                envelope = run.root / "envelope-revise.json"
                mark = len(run.herdr.calls)
                with (
                    mock.patch.object(lch, "wait_for_interactive_agent"),
                    mock.patch.object(lch, "submit_agent_prompt") as submit,
                ):
                    rebound = launcher.resubmit(handle, revised, envelope_path=envelope)
                self.assertIs(rebound, handle)
                self.assertEqual(rebound.envelope_path, envelope)
                self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
                self.assertEqual(submit.call_args.args[1], handle.pane_id)
                self.assertEqual(submit.call_args.args[3], handle.agent_name)
                self.assertIn(str(revised.resolve()), submit.call_args.args[2])
                _assert_converged(self, run.herdr, launcher, {BUILD_LANE: {role: spec}})


class OwnershipMatrixTest(unittest.TestCase):
    """Parent / lane child / role pane / stable agent, one row at a time."""

    # -- run parent ----------------------------------------------------------

    def test_parent_created_then_stopped_before_tagging_converges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            run.herdr.crash_after(CREATE)
            spec = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, spec)
            untagged = [
                wid for wid, rec in _live_workspaces(run.herdr).items()
                if rec["label"] == run.label and "tokens" not in rec
            ]
            self.assertEqual(len(untagged), 1)
            mark = len(run.herdr.calls)
            handle = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(handle.parent_workspace_id, untagged[0])
            self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())

    def test_parent_tagged_then_stopped_before_child_opens_one_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            run.herdr.crash_before(OPEN)
            spec = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(len(_run_parents(run.herdr, run.launcher())), 1)
            mark = len(run.herdr.calls)
            _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, OPEN)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})

    def test_space_bound_to_another_repo_is_not_the_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            elsewhere = run.root / "elsewhere"
            elsewhere.mkdir()
            other = run.herdr.add_workspace(run.label, elsewhere)
            before = run.herdr.snapshot()
            spec = run.spec(TESTS_LANE, "tester")
            handle = _launch(run.launcher(), run.herdr, spec)
            self.assertNotEqual(handle.parent_workspace_id, other)
            self.assertEqual(len(_calls_after(run.herdr, 0, CREATE)), 1)
            self.assertTrue(run.herdr.records_unchanged(before, _ids_of(run.herdr, other)))
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})

    def test_another_runs_space_on_the_repo_is_the_shared_parent(self) -> None:
        """A Maestro-created parent from an earlier run on the same
        repository is the parent for this run too: identity is the repo
        binding, and its tokens are left as they are."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            launcher = run.launcher()
            earlier = launcher._parent_identity_tokens()
            earlier[lch.METADATA_TOKEN_RUN] = "0" * 32
            shared = run.herdr.add_workspace(run.label, run.root, tokens=dict(earlier))
            spec = run.spec(TESTS_LANE, "tester")
            handle = _launch(launcher, run.herdr, spec)
            self.assertEqual(handle.parent_workspace_id, shared)
            self.assertEqual(run.herdr.workspaces[shared]["tokens"], earlier)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            _assert_converged(self, run.herdr, launcher, {TESTS_LANE: {"tester": spec}})

    def test_operators_space_not_a_valid_source_creates_maestros_own(self) -> None:
        """The operator's Space is open on a linked checkout, so Herdr names
        no source for the repository. Maestro creates its own parent once
        rather than hanging lanes off a Space Herdr does not group them
        under."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            run.herdr.workspaces[run.operator]["worktree"]["is_linked_worktree"] = True
            spec = run.spec(TESTS_LANE, "tester")
            handle = _launch(launcher, run.herdr, spec)
            self.assertNotEqual(handle.parent_workspace_id, run.operator)
            self.assertEqual(len(_calls_after(run.herdr, 0, CREATE)), 1)
            _assert_converged(self, run.herdr, launcher, {TESTS_LANE: {"tester": spec}})

    def test_parent_disappearing_after_resolution_refuses_then_recovers(self) -> None:
        """The Space closes between being named the source and being used.

        The launch refuses once naming the dead parent, releases every
        placement under it, and the next launch resolves the parent afresh
        instead of inheriting the dead id.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            parent_id = run.operator

            def close(args: tuple[str, ...]) -> None:
                if parent_id in args:
                    run.herdr.closed_workspaces.add(parent_id)

            run.herdr.hooks_before.setdefault(("worktree", "list"), []).append(close)
            spec = run.spec(TESTS_LANE, "tester")
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(launcher, run.herdr, spec)
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED)
            self.assertIn("PARENT_WORKSPACE_GONE", raised.exception.detail)
            self.assertFalse(raised.exception.pane_created)
            handle = _launch(launcher, run.herdr, spec)
            self.assertNotEqual(handle.parent_workspace_id, parent_id)
            self.assertEqual(len(_calls_after(run.herdr, 0, CREATE)), 1)
            _assert_converged(self, run.herdr, launcher, {TESTS_LANE: {"tester": spec}})

    def test_repository_herdr_calls_off_worktree_refuses_before_creating(self) -> None:
        """Nothing is created when Herdr cannot resolve the checkout at all.

        The binding is read before a Space exists, so an unbindable parent is
        never built and there is nothing to close.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            run.herdr.non_repo_cwds.add(str(run.root.resolve()))
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))
            self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED)
            self.assertIn("REPO_NOT_A_WORKTREE", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE, OPEN), [])
            live = [w for w in _live_workspaces(run.herdr).values() if w["label"] == run.label]
            self.assertEqual(live, [])

    def test_repository_herdr_resolves_elsewhere_refuses_before_creating(self) -> None:
        """Herdr's source checkout disagreeing with the run's primary is fatal."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            run.herdr.linked_checkouts[str(run.root.resolve())] = str((run.root / "..").resolve())
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
            self.assertIn("REPO_ROOT_MISMATCH", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE, OPEN), [])
            live = [w for w in _live_workspaces(run.herdr).values() if w["label"] == run.label]
            self.assertEqual(live, [])

    def test_over_long_identity_token_refuses_before_creating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            long_lane = "lane-" + "x" * lch.METADATA_TOKEN_VALUE_MAX
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(long_lane, "tester", checkout="long"))
            self.assertIn("METADATA_TOKEN_TOO_LONG", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE, OPEN, SPLIT, START), [])
            self.assertEqual(_run_parents(run.herdr, run.launcher()), [run.operator])

    # -- lane child ------------------------------------------------------------

    def test_lane_opened_then_stopped_before_tagging_converges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.crash_after(OPEN)
            spec = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, spec)
            parent_id = _run_parents(run.herdr, run.launcher())[0]
            untagged = [
                item["open_workspace_id"] for item in run.herdr.worktrees[parent_id]
            ]
            self.assertEqual(len(untagged), 1)
            self.assertNotIn("tokens", run.herdr.workspaces[untagged[0]])
            mark = len(run.herdr.calls)
            handle = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(handle.child_workspace_id, untagged[0])
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())

    def test_lane_opened_untagged_by_another_role_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.crash_after(OPEN)
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "test-reviewer"))
            self.assertIn("LABEL_ONLY_LANE_WORKSPACE", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])

    def test_lane_tagged_then_stopped_before_pane_creates_one_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.crash_before(SPLIT)
            spec = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, spec)
            mark = len(run.herdr.calls)
            _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})

    def test_lane_with_stale_tokens_on_already_open_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            child = run.herdr.workspaces[first.child_workspace_id]
            child["tokens"][lch.METADATA_TOKEN_RUN] = "2" * 32
            mark = len(run.herdr.calls)
            # With the stable agent live, reconnection proves the child too.
            with self.assertRaises(lch.LaunchRefused) as live:
                _launch(run.launcher(), run.herdr, tester)
            self.assertIn("LANE_TOKEN_MISMATCH", live.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            # With the agent gone, the child is found open by `worktree open`.
            run.herdr.agents.pop(first.agent_name)
            listing = run.herdr.worktrees.pop(first.parent_workspace_id)
            run.herdr.hooks_before.setdefault(OPEN, []).append(
                _restore_listing(run.herdr, first.parent_workspace_id, listing)
            )
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, tester)
            self.assertIn("LANE_WORKSPACE_TOKEN_MISMATCH", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, SPLIT, START), [])

    def test_lane_child_of_another_lane_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            builder = run.spec(BUILD_LANE, "builder")
            first = _launch(run.launcher(), run.herdr, builder)
            tester = run.spec(TESTS_LANE, "tester")
            second = _launch(run.launcher(), run.herdr, tester)
            self.assertNotEqual(second.child_workspace_id, first.child_workspace_id)
            self.assertEqual(second.parent_workspace_id, first.parent_workspace_id)
            _assert_converged(
                self,
                run.herdr,
                run.launcher(),
                {BUILD_LANE: {"builder": builder}, TESTS_LANE: {"tester": tester}},
            )

    def test_lane_disappearing_between_list_and_get_opens_a_fresh_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            run.herdr.agents.pop(first.agent_name)

            def close(args: tuple[str, ...]) -> None:
                if args[2] == first.child_workspace_id:
                    run.herdr.closed_workspaces.add(first.child_workspace_id)
                    for pid, pane in run.herdr.panes.items():
                        if pane["workspace_id"] == first.child_workspace_id:
                            run.herdr.closed_panes.add(pid)

            run.herdr.hooks_before.setdefault(("workspace", "get"), []).append(close)
            mark = len(run.herdr.calls)
            again = _launch(run.launcher(), run.herdr, tester)
            self.assertNotEqual(again.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, OPEN)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})

    def test_lane_role_panes_in_second_tab_are_found_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            # An operator opened another tab in the lane child, listed first.
            extra = run.herdr._new_tab(first.child_workspace_id, "scratch")
            run.herdr._new_pane(first.child_workspace_id, extra["tab_id"], str(run.root))
            ordered = {extra["tab_id"]: run.herdr.tabs.pop(extra["tab_id"])}
            ordered.update(run.herdr.tabs)
            run.herdr.tabs = ordered
            mark = len(run.herdr.calls)
            again = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(again.pane_id, first.pane_id)
            self.assertEqual(again.tab_id, first.tab_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])

    def test_lane_role_panes_spanning_tabs_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            extra = run.herdr._new_tab(first.child_workspace_id, "scratch")
            stray = run.herdr._new_pane(first.child_workspace_id, extra["tab_id"], str(run.root))
            stray["label"] = "builder"
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "builder"))
            self.assertIn("ROLE_PANES_SPAN_TABS", raised.exception.detail)

    # -- role pane -------------------------------------------------------------

    def test_pane_labelled_then_stopped_before_tagging_is_replaced_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.crash_after(("pane", "rename"))
            spec = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, spec)
            parent_id = _run_parents(run.herdr, run.launcher())[0]
            child_id = _lane_children(run.herdr, run.launcher(), parent_id, TESTS_LANE)[0]
            stale = [pid for pid, pane in _live_panes(run.herdr, child_id).items() if pane.get("label") == "tester"]
            self.assertEqual(len(stale), 1)
            mark = len(run.herdr.calls)
            handle = _launch(run.launcher(), run.herdr, spec)
            self.assertNotEqual(handle.pane_id, stale[0])
            self.assertIn(stale[0], run.herdr.closed_panes)
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})

    def test_pane_tagged_then_stopped_before_agent_starts_one_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.crash_before(START)
            spec = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(run.herdr.agents.get(lch.agent_name_for(spec.correlation_token)), None)
            mark = len(run.herdr.calls)
            _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, START)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})

    def test_duplicate_role_pane_refuses_without_creating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            twin = run.herdr._new_pane(first.child_workspace_id, first.tab_id, str(tester.worktree))
            twin["label"] = "tester"
            twin["tokens"] = dict(run.herdr.panes[first.pane_id]["tokens"])
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "test-reviewer"))
            self.assertIn("DUPLICATE_ROLE_PANE", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])

    def test_pane_with_stale_tokens_refuses_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            run.herdr.panes[first.pane_id]["tokens"][lch.METADATA_TOKEN_LANE] = "lane-other"
            fresh = run.launcher()
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(fresh, run.herdr, tester)
            self.assertIn("PANE_TOKEN_MISMATCH", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])

    def test_pane_disappearing_between_discovery_and_split_recovers_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            reviewer = run.spec(TESTS_LANE, "test-reviewer")
            second = _launch(run.launcher(), run.herdr, reviewer)
            builder = run.spec(TESTS_LANE, "builder")
            fresh = run.launcher()
            closed: list[str] = []

            def vanish(args: tuple[str, ...]) -> None:
                # The pane the split would hang off closes just before it.
                if not closed:
                    closed.append(args[2])
                    run.herdr.closed_panes.add(args[2])
                    for name, agent in list(run.herdr.agents.items()):
                        if agent["pane_id"] == args[2]:
                            run.herdr.agents.pop(name)

            run.herdr.hooks_before.setdefault(SPLIT, []).append(vanish)
            handle = _launch(fresh, run.herdr, builder)
            self.assertEqual(handle.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, 0, OPEN)), 1)
            self.assertIn(closed[0], (first.pane_id, second.pane_id))
            splits = _calls_after(run.herdr, 0, SPLIT)
            self.assertEqual(len(splits), 4)
            self.assertNotEqual(splits[-1][2], closed[0])
            self.assertNotIn(handle.pane_id, run.herdr.closed_panes)
            survivors = {first.pane_id, second.pane_id} - {closed[0]}
            live = _live_panes(run.herdr, first.child_workspace_id)
            self.assertEqual(set(live), survivors | {handle.pane_id})

    def test_dead_role_pane_without_agent_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            # The agent exited: Herdr forgets the agent, the pane is a shell.
            run.herdr.agents.pop(first.agent_name)
            run.herdr.panes[first.pane_id]["agent_status"] = "unknown"
            mark = len(run.herdr.calls)
            again = _launch(run.launcher(), run.herdr, tester)
            self.assertNotEqual(again.pane_id, first.pane_id)
            self.assertIn(first.pane_id, run.herdr.closed_panes)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})

    def test_pane_split_then_stopped_before_label_is_never_adopted_by_another_role(self) -> None:
        """The process died right after the tester's first `pane split`: the
        lane child holds the root pane and an unlabelled, untagged pane at the
        tester's checkout. On reconstruction the tester splits its own pane
        and every other role must too -- an orphan is never handed to a role
        by grid position, and it is closed rather than left behind."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.crash_after(SPLIT)
            tester = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, tester)
            parent_id = _run_parents(run.herdr, run.launcher())[0]
            child_id = _lane_children(run.herdr, run.launcher(), parent_id, TESTS_LANE)[0]
            orphans = [
                pid for pid, pane in _live_panes(run.herdr, child_id).items()
                if "label" not in pane
            ]
            self.assertEqual(len(orphans), 2)  # root + the unlabelled split
            rebuilt = run.launcher()
            mark = len(run.herdr.calls)
            first = _launch(rebuilt, run.herdr, tester)
            reviewer = run.spec(TESTS_LANE, "test-reviewer")
            second = _launch(rebuilt, run.herdr, reviewer)
            self.assertNotIn(second.pane_id, orphans)
            self.assertNotIn(first.pane_id, orphans)
            self.assertTrue(_same_path(run.herdr.panes[second.pane_id]["cwd"], reviewer.worktree))
            self.assertTrue(_same_path(run.herdr.panes[first.pane_id]["cwd"], tester.worktree))
            for orphan in orphans:
                self.assertIn(orphan, run.herdr.closed_panes)
                self.assertNotIn("label", run.herdr.panes[orphan])
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 2)
            _assert_converged(
                self, run.herdr, rebuilt,
                {TESTS_LANE: {"tester": tester, "test-reviewer": reviewer}},
            )
            self.assertTrue(run.foreign_untouched())

    def test_orphan_pane_beside_role_panes_is_closed_not_adopted(self) -> None:
        """Same window, later: two roles are placed, the builder's split
        landed and the process stopped before labelling it. The next role to
        dispatch is not the builder; it must not inherit the builder's
        unlabelled pane, and the builder itself gets a fresh split later."""
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(BUILD_LANE, "tester")
            reviewer = run.spec(BUILD_LANE, "test-reviewer")
            _launch(run.launcher(), run.herdr, tester)
            _launch(run.launcher(), run.herdr, reviewer)
            run.herdr.crash_after(SPLIT, nth=3)
            builder = run.spec(BUILD_LANE, "builder")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, builder)
            parent_id = _run_parents(run.herdr, run.launcher())[0]
            child_id = _lane_children(run.herdr, run.launcher(), parent_id, BUILD_LANE)[0]
            orphans = [
                pid for pid, pane in _live_panes(run.herdr, child_id).items()
                if "label" not in pane
            ]
            self.assertEqual(len(orphans), 1)
            rebuilt = run.launcher()
            code_reviewer = run.spec(BUILD_LANE, "code-reviewer")
            mark = len(run.herdr.calls)
            third = _launch(rebuilt, run.herdr, code_reviewer)
            self.assertNotEqual(third.pane_id, orphans[0])
            self.assertTrue(_same_path(run.herdr.panes[third.pane_id]["cwd"], code_reviewer.worktree))
            # The orphan sits at the builder's cwd: it is the builder's to
            # close, not the code-reviewer's (it could be a builder split in
            # flight in another process). Left alone, never adopted.
            self.assertNotIn(orphans[0], run.herdr.closed_panes)
            self.assertNotIn("label", run.herdr.panes[orphans[0]])
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 1)
            fourth = _launch(rebuilt, run.herdr, builder)
            self.assertTrue(_same_path(run.herdr.panes[fourth.pane_id]["cwd"], builder.worktree))
            self.assertNotEqual(fourth.pane_id, orphans[0])
            self.assertIn(orphans[0], run.herdr.closed_panes)
            _assert_converged(
                self, run.herdr, rebuilt,
                {BUILD_LANE: {
                    "tester": tester, "test-reviewer": reviewer,
                    "code-reviewer": code_reviewer, "builder": builder,
                }},
            )

    # -- stable agent ------------------------------------------------------------

    def test_idle_and_working_agents_are_adopted_blocked_and_unknown_refuse(self) -> None:
        for status, adopted in (("idle", True), ("working", True), ("done", True), ("blocked", False), ("unknown", False)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                run = _RunFixture(tmp)
                tester = run.spec(TESTS_LANE, "tester")
                first = _launch(run.launcher(), run.herdr, tester)
                run.herdr.set_agent_status(first.agent_name, status)
                mark = len(run.herdr.calls)
                if adopted:
                    again = _launch(run.launcher(), run.herdr, tester)
                    self.assertEqual(again.pane_id, first.pane_id)
                    self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
                else:
                    with self.assertRaises(lch.LaunchRefused) as raised:
                        _launch(run.launcher(), run.herdr, tester)
                    self.assertIn("AGENT_STATUS_UNOBSERVABLE", raised.exception.detail)
                    self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
                    self.assertNotIn(first.pane_id, run.herdr.closed_panes)

    def test_agent_not_ready_keeps_pane_and_agent_then_reconnects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.agent_start_refusal = lch.AGENT_NOT_READY
            tester = run.spec(TESTS_LANE, "tester")
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.AGENT_START_REFUSED)
            self.assertIn(lch.AGENT_NOT_READY, raised.exception.detail)
            self.assertTrue(raised.exception.pane_created)
            name = lch.agent_name_for(tester.correlation_token)
            self.assertEqual(run.herdr.agents[name]["agent_status"], "blocked")
            self.assertNotIn(run.herdr.agents[name]["pane_id"], run.herdr.closed_panes)
            # The operator answered the startup prompt; the agent is idle.
            run.herdr.agent_start_refusal = ""
            run.herdr.set_agent_status(name, "idle")
            mark = len(run.herdr.calls)
            handle = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(handle.agent_name, name)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})

    def test_agent_started_then_stopped_before_registering_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            # Dies on the `pane get` that follows a successful `agent start`.
            run.herdr.hooks_after.setdefault(START, []).append(
                lambda args: run.herdr.crash_before(("pane", "get"), nth=run.herdr._counts.get(("pane", "get"), 0) + 1)
            )
            tester = run.spec(TESTS_LANE, "tester")
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, tester)
            name = lch.agent_name_for(tester.correlation_token)
            self.assertIn(name, run.herdr.agents)
            mark = len(run.herdr.calls)
            handle = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(handle.agent_name, name)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})


class ConcurrencyMatrixTest(unittest.TestCase):
    """Deterministic races through the fake's hooks; no sleeps anywhere."""

    def _race(self, run: _RunFixture, jobs: dict[str, tuple[lch.HerdrLauncher, lch.LaunchSpec]]) -> dict[str, object]:
        gate = threading.Barrier(len(jobs))
        outcomes: dict[str, object] = {}

        def worker(key: str) -> None:
            launcher, spec = jobs[key]
            gate.wait()
            try:
                outcomes[key] = _drive(launcher, spec)
            except BaseException as exc:  # noqa: BLE001
                outcomes[key] = exc

        threads = [threading.Thread(target=worker, args=(key,)) for key in jobs]
        with _launch_patches():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        return outcomes

    def test_same_lane_same_role_converges_to_one_pane_and_one_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            spec = run.spec(TESTS_LANE, "tester")
            # Both dispatches reach `agent start` with no agent registered.
            both_at_start = threading.Barrier(2)

            def hold_at_start(_args: tuple[str, ...]) -> None:
                both_at_start.wait()

            run.herdr.hooks_before.setdefault(START, []).append(hold_at_start)
            outcomes = self._race(run, {"a": (launcher, spec), "b": (launcher, spec)})
            handles = [o for o in outcomes.values() if isinstance(o, lch.LaunchHandle)]
            refusals = [o for o in outcomes.values() if isinstance(o, lch.LaunchRefused)]
            self.assertEqual(len(handles), 1, outcomes)
            self.assertEqual(len(refusals), 1, outcomes)
            self.assertIn("STABLE_AGENT_ALREADY_PRESENT", refusals[0].detail)
            self.assertFalse(refusals[0].pane_created)
            self.assertEqual(len(_calls_after(run.herdr, 0, START)), 2)
            self.assertEqual(len(_calls_after(run.herdr, 0, SPLIT)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())

    def test_same_lane_different_roles_get_one_pane_each_in_one_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            specs = {role: run.spec(TESTS_LANE, role) for role in ("tester", "test-reviewer")}
            outcomes = self._race(run, {role: (launcher, spec) for role, spec in specs.items()})
            for role, outcome in outcomes.items():
                self.assertIsInstance(outcome, lch.LaunchHandle, "{}: {!r}".format(role, outcome))
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, 0, OPEN)), 1)
            self.assertEqual(len(_calls_after(run.herdr, 0, SPLIT)), 2)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: specs})

    def test_different_lanes_share_one_parent_as_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            builder = run.spec(BUILD_LANE, "builder")
            outcomes = self._race(run, {"t": (launcher, tester), "b": (launcher, builder)})
            for key, outcome in outcomes.items():
                self.assertIsInstance(outcome, lch.LaunchHandle, "{}: {!r}".format(key, outcome))
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, 0, OPEN)), 2)
            parent_id = _assert_converged(
                self, run.herdr, run.launcher(),
                {TESTS_LANE: {"tester": tester}, BUILD_LANE: {"builder": builder}},
            )
            for call in _calls_after(run.herdr, 0, OPEN):
                self.assertEqual(_flag(call, "--workspace"), parent_id)

    def test_reconnect_racing_creation_converges_on_one_pane_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            creator, reconnector = run.launcher(), run.launcher()
            spec = run.spec(TESTS_LANE, "tester")
            created = threading.Event()
            looked = threading.Event()

            def after_create(args: tuple[str, ...]) -> None:
                # The creator has the parent but has not tagged it; the
                # reconnecting process lists workspaces exactly now.
                created.set()
                looked.wait(5.0)

            run.herdr.hooks_after.setdefault(CREATE, []).append(after_create)
            outcomes: dict[str, object] = {}

            def create() -> None:
                try:
                    outcomes["creator"] = _drive(creator, spec)
                except BaseException as exc:  # noqa: BLE001
                    outcomes["creator"] = exc

            def reconnect() -> None:
                created.wait(5.0)
                try:
                    outcomes["reconnector"] = _drive(reconnector, spec)
                except BaseException as exc:  # noqa: BLE001
                    outcomes["reconnector"] = exc
                finally:
                    looked.set()

            threads = [threading.Thread(target=create), threading.Thread(target=reconnect)]
            with _launch_patches():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            # The reconnector ran to completion over the creator's untagged
            # parent: adopted it by repo binding, tagged it, opened the lane,
            # started the agent. The creator then resumed onto that state.
            reconnected, created_handle = outcomes["reconnector"], outcomes["creator"]
            self.assertIsInstance(reconnected, lch.LaunchHandle, outcomes)
            self.assertIsInstance(created_handle, lch.LaunchHandle, outcomes)
            assert isinstance(reconnected, lch.LaunchHandle)
            assert isinstance(created_handle, lch.LaunchHandle)
            self.assertEqual(created_handle.pane_id, reconnected.pane_id)
            self.assertEqual(created_handle.agent_name, reconnected.agent_name)
            self.assertEqual(len(_calls_after(run.herdr, 0, CREATE)), 1)
            # The creator resumed onto the reconnector's tagged child and
            # adopted it from the listing; nothing was opened twice.
            self.assertEqual(len(_calls_after(run.herdr, 0, OPEN)), 1)
            self.assertEqual(len(_calls_after(run.herdr, 0, SPLIT)), 1)
            self.assertEqual(len(_calls_after(run.herdr, 0, START)), 1)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())

    def test_one_creator_failing_while_the_other_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            spec = run.spec(TESTS_LANE, "tester")
            # Creator A dies right after opening the lane child.
            run.herdr.crash_after(OPEN)
            with self.assertRaises((lch.LaunchRefused, FakeHerdrStopped)):
                _launch(run.launcher(), run.herdr, spec)
            # Creator B (another process) completes over A's partial state.
            mark = len(run.herdr.calls)
            handle = _launch(run.launcher(), run.herdr, spec)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            self.assertEqual(len(_calls_after(run.herdr, mark, SPLIT)), 1)
            self.assertEqual(len(_calls_after(run.herdr, mark, START)), 1)
            self.assertIsInstance(handle, lch.LaunchHandle)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": spec}})
            self.assertTrue(run.foreign_untouched())


class EveryRoleLifecycleTest(unittest.TestCase):
    """Creation, collection, reconnect, revise, invalidation and cleanup for
    each of the five roles; a fix proven for the tester alone is incomplete."""

    def test_role_lifecycle(self) -> None:
        for role in FIVE_ROLES:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                run = _RunFixture(tmp)
                launcher = run.launcher()
                spec = run.spec(BUILD_LANE, role)
                # creation
                handle = _launch(launcher, run.herdr, spec)
                self.assertEqual(run.herdr.panes[handle.pane_id]["label"], lch.pane_label_for(role))
                self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
                self.assertEqual(handle.parent_workspace_id, run.operator)
                self.assertEqual(len(_calls_after(run.herdr, 0, OPEN)), 1)
                self.assertEqual(len(_calls_after(run.herdr, 0, SPLIT)), 1)
                self.assertEqual(len(_calls_after(run.herdr, 0, START)), 1)
                _assert_converged(self, run.herdr, launcher, {BUILD_LANE: {role: spec}})
                # collection: the declared envelope outranks pane status
                self.assertEqual(launcher.poll(handle).state, lch.PollState.RUNNING)
                spec.envelope_path.write_text(json.dumps({"success": True}), encoding="utf-8")
                polled = launcher.poll(handle)
                self.assertEqual((polled.state, polled.detail), (lch.PollState.EXITED, "ENVELOPE_SUCCESS"))
                launcher.retain(handle)
                self.assertNotIn(handle.pane_id, run.herdr.closed_panes)
                # persistence + reconnect from a reconstructed launcher
                mark = len(run.herdr.calls)
                rebuilt = run.launcher()
                again = _launch(rebuilt, run.herdr, spec)
                self.assertEqual((again.pane_id, again.agent_name), (handle.pane_id, handle.agent_name))
                self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
                # revise: same pane and agent, only prompt/envelope rebound
                revised = run.root / "prompt-revise.json"
                revised.write_text("{}", encoding="utf-8")
                envelope = run.root / "envelope-revise.json"
                mark = len(run.herdr.calls)
                with (
                    mock.patch.object(lch, "wait_for_interactive_agent"),
                    mock.patch.object(lch, "submit_agent_prompt"),
                ):
                    rebound = rebuilt.resubmit(again, revised, envelope_path=envelope)
                self.assertIs(rebound, again)
                self.assertEqual(rebound.envelope_path, envelope)
                self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
                _assert_converged(self, run.herdr, rebuilt, {BUILD_LANE: {role: spec}})
                # invalidation: the child vanished under a live launcher
                run.herdr.closed_workspaces.add(again.child_workspace_id)
                run.herdr.closed_panes.add(again.pane_id)
                mark = len(run.herdr.calls)
                replaced = _launch(rebuilt, run.herdr, spec)
                self.assertNotEqual(replaced.child_workspace_id, again.child_workspace_id)
                self.assertEqual(replaced.parent_workspace_id, again.parent_workspace_id)
                self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
                self.assertEqual(len(_calls_after(run.herdr, mark, OPEN)), 1)
                _assert_converged(self, run.herdr, rebuilt, {BUILD_LANE: {role: spec}})
                # cleanup: the lane child closes; the operator's Space, its
                # tabs, panes and agent survive untouched
                rebuilt.complete_run([replaced], project_identity=PROJECT, timeout_s=1.0)
                closes = _calls_after(run.herdr, mark, ("workspace", "close"))
                self.assertEqual([call[2] for call in closes], [replaced.child_workspace_id])
                self.assertTrue(run.foreign_untouched())
                self.assertEqual(_run_parents(run.herdr, rebuilt), [run.operator])
                self.assertNotIn(run.operator, run.herdr.closed_workspaces)


class FakeShapeTest(unittest.TestCase):
    """The fake answers in the installed CLI's shape, optional fields absent."""

    def test_every_reply_carries_result_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            replies: list[tuple[tuple[str, ...], dict]] = []
            real = run.herdr._dispatch

            def record(verb: tuple[str, str], args: tuple[str, ...]) -> dict:
                reply = real(verb, args)
                replies.append((args, reply))
                return reply

            run.herdr._dispatch = record  # type: ignore[method-assign]
            tester = run.spec(TESTS_LANE, "tester")
            owner = run.launcher()
            handle = _launch(owner, run.herdr, tester)
            _launch(run.launcher(), run.herdr, tester)
            owner.complete_run([handle], project_identity=PROJECT, timeout_s=1.0)
            self.assertTrue(replies)
            for args, reply in replies:
                if args[:2] in (("pane", "send-text"), ("pane", "send-keys")):
                    self.assertEqual(reply, {}, args)
                    continue
                self.assertIn("result", reply, args)
                self.assertIsInstance(reply["result"].get("type"), str, args)

    def test_records_omit_optional_fields_until_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = FakeHerdr()
            root = Path(tmp)
            created = herdr("workspace", "create", "--cwd", str(root), "--label", "x", "--no-focus")
            workspace = created["result"]["workspace"]
            for key in ("workspace_id", "number", "label", "focused", "pane_count", "tab_count", "active_tab_id", "agent_status", "worktree"):
                self.assertIn(key, workspace)
            self.assertNotIn("tokens", workspace)
            self.assertNotIn("source", workspace)
            self.assertEqual(set(workspace["worktree"]), {"repo_key", "repo_name", "repo_root", "checkout_path", "is_linked_worktree"})
            root_pane = created["result"]["root_pane"]
            self.assertNotIn("label", root_pane)
            self.assertNotIn("tokens", root_pane)
            self.assertEqual(root_pane["agent_status"], "unknown")
            elsewhere = root / "nowhere"
            elsewhere.mkdir()
            herdr.non_repo_cwds.add(str(elsewhere.resolve()))
            bare = herdr("workspace", "create", "--cwd", str(elsewhere), "--label", "y", "--no-focus")
            # Off-repo: Herdr emits no binding at all, not a null one.
            self.assertNotIn("worktree", bare["result"]["workspace"])
            # A second Space on an already-sourced repository is handed no
            # binding either, and does not displace the source.
            again = herdr("workspace", "create", "--cwd", str(root), "--label", "z", "--no-focus")
            self.assertNotIn("worktree", again["result"]["workspace"])
            listed = herdr("worktree", "list", "--cwd", str(root))
            self.assertEqual(
                listed["result"]["source"]["source_workspace_id"],
                workspace["workspace_id"],
            )
            herdr.start_agent("a1", root_pane["pane_id"])
            agent = herdr("agent", "get", "a1")["result"]["agent"]
            self.assertNotIn("status", agent)
            self.assertIn(agent["agent_status"], ("idle", "working", "blocked", "done", "unknown"))
            with self.assertRaises(lch.HerdrCallError) as raised:
                herdr("agent", "get", "nobody")
            self.assertEqual(raised.exception.code, lch.AGENT_NOT_FOUND)
            child = root / "child"
            child.mkdir()
            first = herdr("worktree", "open", "--workspace", workspace["workspace_id"], "--path", str(child), "--label", "lane", "--no-focus")
            self.assertFalse(first["result"]["already_open"])
            second = herdr("worktree", "open", "--workspace", workspace["workspace_id"], "--path", str(child), "--label", "lane", "--no-focus")
            self.assertTrue(second["result"]["already_open"])
            self.assertEqual(second["result"]["workspace"]["workspace_id"], first["result"]["workspace"]["workspace_id"])
            listed = herdr("worktree", "list", "--workspace", workspace["workspace_id"])["result"]
            self.assertEqual(listed["source"]["source_workspace_id"], workspace["workspace_id"])
            self.assertEqual(set(listed["worktrees"][0]) >= {"path", "is_bare", "is_detached", "is_prunable", "is_linked_worktree", "label", "open_workspace_id"}, True)


class HerdrUnavailableTest(unittest.TestCase):
    """An unreachable `herdr` binary is a typed refusal, not a traceback."""

    def test_missing_binary_is_a_typed_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
            launcher.herdr_path = root / "no-such-herdr"
            spec = _spec(_checkout(root, "tester"), lane=TESTS_LANE, role="tester", repository_root=root)
            with _launch_patches(), self.assertRaises(lch.LaunchRefused) as raised:
                _drive(launcher, spec)
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.HERDR_UNAVAILABLE)
            self.assertTrue(str(raised.exception).startswith("LAUNCH_REFUSED:HERDR_UNAVAILABLE:"))
            self.assertIn("no-such-herdr", raised.exception.detail)
            self.assertTrue(raised.exception.refusal.deterministic)
            self.assertNotIsInstance(raised.exception, lch.HerdrCallError)


class ParentRelationTest(unittest.TestCase):
    """Where Herdr groups a fresh child is read back, never assumed (F1).

    With the operator's own Space open at the primary checkout, two
    non-linked Spaces bind the same repo_root and which one Herdr treats as
    the source is undocumented (see `source_space_rule` in herdr_fake.py).
    """

    def test_fresh_open_grouped_under_another_space_is_closed_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.source_space_rule = "last-open"
            planted: list[str] = []

            def operator_opens_a_space(_args: tuple[str, ...]) -> None:
                # Between our listing and our open a second Space opens at
                # the primary checkout; Herdr then groups the child there.
                if not planted:
                    planted.append(run.herdr.add_workspace("FDAdb (2)", run.root))

            run.herdr.hooks_before.setdefault(OPEN, []).append(operator_opens_a_space)
            tester = run.spec(TESTS_LANE, "tester")
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, tester)
            second = planted[0]
            before = run.herdr.snapshot()
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_DRIFT)
            self.assertIn("LANE_CHILD_NOT_UNDER_PARENT", raised.exception.detail)
            self.assertFalse(raised.exception.pane_created)
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            opens = _calls_after(run.herdr, 0, OPEN)
            self.assertEqual(len(opens), 1)
            self.assertEqual(_flag(opens[0], "--workspace"), run.operator)
            # The regrouped child was closed before it could be tagged.
            children = [
                wid for wid, rec in run.herdr.workspaces.items()
                if _is_linked(rec) and rec["label"] == TESTS_LANE
            ]
            self.assertEqual(len(children), 1)
            self.assertIn(children[0], run.herdr.closed_workspaces)
            self.assertNotIn("tokens", run.herdr.workspaces[children[0]])
            self.assertEqual(_calls_after(run.herdr, 0, SPLIT, START), [])
            self.assertTrue(run.herdr.records_unchanged(before, _ids_of(run.herdr, second)))
            self.assertTrue(run.foreign_untouched())

    def test_restart_after_regrouped_open_follows_herdr_deterministically(self) -> None:
        """Herdr's answer is the parent, and every restart gets the same one.

        Under the observed rule a second Space cannot take a repository's
        source, so this drives the contrary rule: when Herdr does regroup,
        the run follows it rather than guessing, and does so identically on
        every restart. Nothing is created -- the Space Herdr named already
        exists.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.source_space_rule = "last-open"
            planted: list[str] = []
            run.herdr.hooks_before.setdefault(OPEN, []).append(
                lambda _args: planted.append(run.herdr.add_workspace("FDAdb (2)", run.root))
                if not planted
                else None
            )
            tester = run.spec(TESTS_LANE, "tester")
            with self.assertRaises(lch.LaunchRefused) as first:
                _launch(run.launcher(), run.herdr, tester)
            self.assertIn("LANE_CHILD_NOT_UNDER_PARENT", first.exception.detail)
            second = planted[0]
            parents = set()
            for _ in range(2):
                mark = len(run.herdr.calls)
                handle = _launch(run.launcher(), run.herdr, tester)
                parents.add(handle.parent_workspace_id)
                self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
            self.assertEqual(parents, {second})
            self.assertNotIn(run.operator, parents)

    def test_child_missing_from_parent_listing_is_closed_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")

            def vanish_listing(_args: tuple[str, ...]) -> None:
                for key in list(run.herdr.worktrees):
                    run.herdr.worktrees[key] = []

            run.herdr.hooks_after.setdefault(OPEN, []).append(vanish_listing)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, tester)
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_DRIFT)
            self.assertIn("LANE_CHILD_NOT_UNDER_PARENT", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, 0, SPLIT, START), [])
            for wid, rec in run.herdr.workspaces.items():
                if _is_linked(rec) and rec["label"] == TESTS_LANE:
                    self.assertIn(wid, run.herdr.closed_workspaces)
                    self.assertNotIn("tokens", rec)

    def test_nullable_source_id_falls_back_to_listing_membership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.source_workspace_nullable = True
            tester = run.spec(TESTS_LANE, "tester")
            handle = _launch(run.launcher(), run.herdr, tester)
            again = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(again.child_workspace_id, handle.child_workspace_id)
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})

    def test_adopt_existing_lane_refuses_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            run.herdr.agents.pop(first.agent_name)
            # Herdr reports another Space as the repository's source at the
            # moment the lane is listed (a Space that opens and closes around
            # that listing), while the parent lookup saw only the operator's.
            run.herdr.source_space_rule = "last-open"
            planted: list[str] = []

            def open_elsewhere(_args: tuple[str, ...]) -> None:
                if not planted:
                    planted.append(run.herdr.add_workspace("FDAdb (2)", run.root))

            def close_elsewhere(_args: tuple[str, ...]) -> None:
                if planted:
                    run.herdr.closed_workspaces.add(planted[0])

            run.herdr.hooks_before.setdefault(("worktree", "list"), []).append(open_elsewhere)
            run.herdr.hooks_after.setdefault(("worktree", "list"), []).append(close_elsewhere)
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, tester)
            # The Space Herdr named as the source is gone by the time the
            # lane is looked up under it. The run refuses by name and places
            # nothing, rather than adopting a lane grouped somewhere else.
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED)
            self.assertIn("PARENT_WORKSPACE_GONE", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])


class StrayPaneTest(unittest.TestCase):
    """Unlabelled panes are closed only inside the role's own cwd scope (F5)."""

    def _foreign_pane(self, run: _RunFixture, child_id: str, tab_id: str, cwd: str) -> str:
        pane = run.herdr._new_pane(child_id, tab_id, cwd)
        if not cwd:
            pane["cwd"] = None
        return str(pane["pane_id"])

    def test_operator_pane_with_foreign_or_null_cwd_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            elsewhere = run.root / "elsewhere"
            elsewhere.mkdir()
            foreign = self._foreign_pane(run, first.child_workspace_id, first.tab_id, str(elsewhere))
            # The tester's agent finished; its pane is replaced on restart.
            run.herdr.agents.pop(first.agent_name)
            run.herdr.panes[first.pane_id]["agent_status"] = "unknown"
            again = _launch(run.launcher(), run.herdr, tester)
            self.assertNotEqual(again.pane_id, first.pane_id)
            self.assertIn(first.pane_id, run.herdr.closed_panes)
            self.assertNotIn(foreign, run.herdr.closed_panes)
            self.assertNotIn("label", run.herdr.panes[foreign])
            reviewer = run.spec(TESTS_LANE, "test-reviewer")
            _launch(run.launcher(), run.herdr, reviewer)
            self.assertNotIn(foreign, run.herdr.closed_panes)
            nothing = self._foreign_pane(run, first.child_workspace_id, first.tab_id, "")
            run.herdr.closed_panes.add(foreign)
            builder = run.spec(TESTS_LANE, "builder")
            _launch(run.launcher(), run.herdr, builder)
            self.assertNotIn(nothing, run.herdr.closed_panes)
            self.assertIsNone(run.herdr.panes[nothing]["cwd"])

    def test_two_foreign_panes_beside_role_panes_refuse_unlabelled_lane_panes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            elsewhere = run.root / "elsewhere"
            elsewhere.mkdir()
            strays = [
                self._foreign_pane(run, first.child_workspace_id, first.tab_id, str(elsewhere))
                for _ in range(2)
            ]
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "test-reviewer"))
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
            self.assertIn("UNLABELLED_LANE_PANES", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            for stray in strays:
                self.assertNotIn(stray, run.herdr.closed_panes)
            self.assertNotIn(first.pane_id, run.herdr.closed_panes)

    def test_concurrent_split_racing_label_is_not_reaped_by_a_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester_launcher, reviewer_launcher = run.launcher(), run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            reviewer = run.spec(TESTS_LANE, "test-reviewer")
            tester_split = threading.Event()
            reviewer_done = threading.Event()
            first_split: list[str] = []

            def hold_after_split(_args: tuple[str, ...]) -> None:
                # The tester's pane exists, unlabelled, while the reviewer
                # process runs its whole placement over it.
                if not first_split:
                    first_split.append("tester")
                    tester_split.set()
                    reviewer_done.wait(5.0)

            run.herdr.hooks_after.setdefault(SPLIT, []).append(hold_after_split)
            outcomes: dict[str, object] = {}

            def run_tester() -> None:
                try:
                    outcomes["tester"] = _drive(tester_launcher, tester)
                except BaseException as exc:  # noqa: BLE001
                    outcomes["tester"] = exc

            def run_reviewer() -> None:
                tester_split.wait(5.0)
                try:
                    outcomes["reviewer"] = _drive(reviewer_launcher, reviewer)
                except BaseException as exc:  # noqa: BLE001
                    outcomes["reviewer"] = exc
                finally:
                    reviewer_done.set()

            threads = [threading.Thread(target=run_tester), threading.Thread(target=run_reviewer)]
            with _launch_patches():
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            for key, outcome in outcomes.items():
                self.assertIsInstance(outcome, lch.LaunchHandle, "{}: {!r}".format(key, outcome))
            self.assertEqual(_calls_after(run.herdr, 0, CREATE), [])
            self.assertEqual(len(_calls_after(run.herdr, 0, OPEN)), 1)
            self.assertEqual(len(_calls_after(run.herdr, 0, SPLIT)), 2)
            _assert_converged(
                self, run.herdr, run.launcher(),
                {TESTS_LANE: {"tester": tester, "test-reviewer": reviewer}},
            )


class UntestedExitsTest(unittest.TestCase):
    """One behavioural test per refusal exit the review found untested (F7)."""

    def test_ambiguous_lane_tab_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            parent_id = run.operator
            tester = run.spec(TESTS_LANE, "tester")
            child_id = run.herdr.open_child(
                parent_id, tester.worktree, TESTS_LANE,
                tokens=launcher._lane_identity_tokens(TESTS_LANE, parent_id),
            )
            extra = run.herdr._new_tab(child_id, "scratch")
            run.herdr._new_pane(child_id, extra["tab_id"], str(run.root))
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(launcher, run.herdr, tester)
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.TAB_UNRESOLVED)
            self.assertIn("AMBIGUOUS_LANE_TAB", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])

    def test_stale_role_pane_missing_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            handle = _launch(launcher, run.herdr, tester)
            _launch(launcher, run.herdr, run.spec(TESTS_LANE, "test-reviewer"))
            layout = launcher._tabs[TESTS_LANE]
            # The tester's pane was marked stale and then closed by a reap
            # elsewhere: forgotten from the grid, the role still flagged.
            layout.refresh_roles.add("tester")
            layout.forget(handle.pane_id)
            run.herdr.agents.pop(handle.agent_name)
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(launcher, run.herdr, tester)
            self.assertIn("STALE_ROLE_PANE_MISSING", raised.exception.detail)
            self.assertFalse(raised.exception.pane_created)
            self.assertEqual(_calls_after(run.herdr, mark, SPLIT, START), [])

    def test_stale_role_pane_unbound_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            launcher = run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            handle = _launch(launcher, run.herdr, tester)
            other = _launch(launcher, run.herdr, run.spec(TESTS_LANE, "test-reviewer"))
            layout = launcher._tabs[TESTS_LANE]
            # The role map still names the stale pane, the grid does not.
            layout.refresh_roles.add("tester")
            layout.panes = [other.pane_id]
            run.herdr.agents.pop(handle.agent_name)
            mark = len(run.herdr.calls)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(launcher, run.herdr, tester)
            self.assertIn("STALE_ROLE_PANE_UNBOUND", raised.exception.detail)
            self.assertTrue(raised.exception.pane_created)
            self.assertEqual(_calls_after(run.herdr, mark, SPLIT, START), [])

    def test_unconfigured_role_pane_not_closed_refuses_and_reports_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.close_pane_error = "pane_busy"
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
            self.assertIn("UNCONFIGURED_ROLE_PANE_NOT_CLOSED", raised.exception.detail)
            self.assertTrue(raised.exception.pane_created)
            self.assertEqual(len(_calls_after(run.herdr, 0, SPLIT)), 1)
            self.assertEqual(_calls_after(run.herdr, 0, START), [])
            self.assertEqual(len(_calls_after(run.herdr, 0, ("pane", "close"))), 2)

    def test_unbound_parent_close_refusal_is_reported(self) -> None:
        """A created parent that lost the source race, and will not close.

        With no Space open on the repository Maestro creates its own, then
        proves the binding by asking Herdr again. A Space opened at the
        primary checkout in between takes the source, so the parent Maestro
        built is not it -- and when closing that parent is refused too, both
        the reason and the refused close are reported.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp, operator=False)
            run.herdr.source_space_rule = "last-open"
            run.herdr.close_workspace_error = "workspace_busy"
            planted: list[str] = []

            def operator_opens_a_space(_args: tuple[str, ...]) -> None:
                if not planted:
                    planted.append(run.herdr.add_workspace("FDAdb (2)", run.root))

            run.herdr.hooks_after.setdefault(CREATE, []).append(operator_opens_a_space)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(run.launcher(), run.herdr, run.spec(TESTS_LANE, "tester"))
            self.assertIs(raised.exception.refusal, lch.LaunchRefusal.WORKSPACE_UNRESOLVED)
            self.assertIn("RUN_WORKSPACE_UNBOUND", raised.exception.detail)
            self.assertIn("NOT_SOURCE_SPACE:{}".format(planted[0]), raised.exception.detail)
            self.assertIn("close refused", raised.exception.detail)
            self.assertEqual(_calls_after(run.herdr, 0, OPEN), [])

class ReopenedOperatorSpaceTest(unittest.TestCase):
    """The operator closes and reopens their Space (S1). A lane child pinned
    to the old Space id by its `parent` token must self-heal when Herdr left
    it open, and be re-opened once when Herdr closed it with the Space."""

    def _reopen(self, run: _RunFixture) -> str:
        run.herdr("workspace", "close", run.operator)
        return _plant_operator_space(run.herdr, run.root)

    def test_child_surviving_the_space_is_retagged_and_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            # Herdr 0.8.2 cascades (the observed default); this is the other
            # branch, which the launcher must still self-heal from.
            run.herdr.cascade_close_children = False
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            old_parent = first.parent_workspace_id
            run.herdr.agents.pop(first.agent_name)
            run.herdr.panes[first.pane_id]["agent_status"] = "unknown"
            new_parent = self._reopen(run)
            new_ids = _ids_of(run.herdr, new_parent)
            before = run.herdr.snapshot()
            self.assertNotIn(first.child_workspace_id, run.herdr.closed_workspaces)
            mark = len(run.herdr.calls)
            again = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(again.parent_workspace_id, new_parent)
            self.assertEqual(again.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            child = run.herdr.workspaces[first.child_workspace_id]
            self.assertEqual(child["tokens"][lch.METADATA_TOKEN_PARENT], new_parent)
            self.assertNotEqual(old_parent, new_parent)
            self.assertTrue(run.herdr.records_unchanged(before, new_ids))
            self.assertNotIn("tokens", run.herdr.workspaces[new_parent])
            launcher = run.launcher()
            self.assertEqual(_assert_converged(self, run.herdr, launcher, {TESTS_LANE: {"tester": tester}}), new_parent)
            # Restart after the retag converges without touching anything.
            mark = len(run.herdr.calls)
            third = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(third.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])

    def test_same_launcher_keeps_its_surviving_child_then_a_restart_retags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.cascade_close_children = False
            launcher = run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(launcher, run.herdr, tester)
            run.herdr.agents.pop(first.agent_name)
            run.herdr.panes[first.pane_id]["agent_status"] = "unknown"
            new_parent = self._reopen(run)
            # The running launcher's lane child and pane are still there:
            # it keeps using them (nothing created or opened).
            mark = len(run.herdr.calls)
            again = _launch(launcher, run.herdr, tester)
            self.assertEqual(again.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN), [])
            # A restart resolves the new Space and re-pins the child to it.
            mark = len(run.herdr.calls)
            third = _launch(run.launcher(), run.herdr, tester)
            self.assertEqual(third.parent_workspace_id, new_parent)
            self.assertEqual(third.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            _assert_converged(self, run.herdr, run.launcher(), {TESTS_LANE: {"tester": tester}})

    def test_child_closed_with_the_space_is_reopened_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            run.herdr.cascade_close_children = True
            launcher = run.launcher()
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(launcher, run.herdr, tester)
            new_parent = self._reopen(run)
            self.assertIn(first.child_workspace_id, run.herdr.closed_workspaces)
            with self.assertRaises(lch.LaunchRefused) as raised:
                _launch(launcher, run.herdr, tester)
            self.assertIn("PARENT_WORKSPACE_GONE", raised.exception.detail)
            mark = len(run.herdr.calls)
            again = _launch(launcher, run.herdr, tester)
            self.assertEqual(again.parent_workspace_id, new_parent)
            self.assertNotEqual(again.child_workspace_id, first.child_workspace_id)
            self.assertEqual(_calls_after(run.herdr, mark, CREATE), [])
            opens = _calls_after(run.herdr, mark, OPEN)
            self.assertEqual(len(opens), 1)
            self.assertEqual(_flag(opens[0], "--workspace"), new_parent)
            _assert_converged(self, run.herdr, launcher, {TESTS_LANE: {"tester": tester}})

    def test_child_pinned_to_a_live_space_still_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = _RunFixture(tmp)
            tester = run.spec(TESTS_LANE, "tester")
            first = _launch(run.launcher(), run.herdr, tester)
            run.herdr.agents.pop(first.agent_name)
            # The child names a live Space on another repository as its parent.
            elsewhere = run.root / "elsewhere"
            elsewhere.mkdir()
            other = run.herdr.add_workspace("elsewhere", elsewhere)
            run.herdr.workspaces[first.child_workspace_id]["tokens"][lch.METADATA_TOKEN_PARENT] = other
            before = run.herdr.snapshot()
            for _ in range(2):
                mark = len(run.herdr.calls)
                with self.assertRaises(lch.LaunchRefused) as raised:
                    _launch(run.launcher(), run.herdr, tester)
                self.assertIn("LANE_WORKSPACE_TOKEN_MISMATCH", raised.exception.detail)
                self.assertEqual(_calls_after(run.herdr, mark, CREATE, OPEN, SPLIT, START), [])
            self.assertEqual(
                run.herdr.workspaces[first.child_workspace_id]["tokens"][lch.METADATA_TOKEN_PARENT],
                other,
            )
            self.assertTrue(run.herdr.records_unchanged(before, _ids_of(run.herdr, other)))


if __name__ == "__main__":
    unittest.main()
