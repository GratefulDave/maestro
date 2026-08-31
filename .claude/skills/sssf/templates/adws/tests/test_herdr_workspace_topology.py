"""Parent Space + linked lane children, lazy panes, adopt, COMPLETE cleanup."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch


PROJECT = "FDAdb"
RUN_HASH = "e892fe8df79046ca8ea6504934e912c6"
RUN_PREFIXED = "run-9f20c17fabcdef0123456789"
REPO = "repo-fdadb"
TESTS_LANE = "lane-wp6-tests"
BUILD_LANE = "lane-wp6-build"


def _flag(args: tuple[str, ...], name: str) -> str | None:
    if name in args:
        return args[args.index(name) + 1]
    return None


def _tokens_from_args(args: tuple[str, ...]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    index = 0
    while index < len(args):
        if args[index] == "--token" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            tokens[key] = value
            index += 2
            continue
        index += 1
    return tokens


def _env_from_args(args: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, item in enumerate(args):
        if item == "--env" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            values[key] = value
    return values


def _same_path(left: str | Path | None, right: str | Path) -> bool:
    if left is None:
        return False
    return Path(left).resolve() == Path(right).resolve()


class FakeHerdr:
    """In-memory herdr: records argv and holds live workspace/pane/agent state."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, ...]] = []
        self.workspaces: dict[str, dict] = {}
        self.worktrees: dict[str, list[dict]] = {}
        self.tabs: dict[str, dict] = {}
        self.panes: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.closed_workspaces: set[str] = set()
        self.closed_panes: set[str] = set()
        self.rename_confirms = True
        self.wait_output_error = ""
        self.close_workspace_error = ""
        self._seq = 0

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return "{}{}".format(prefix, self._seq)

    def _raise(self, code: str) -> None:
        raise lch.HerdrCallError("LAUNCH_REFUSED:{}".format(code), code)

    def _workspace_payload(self, workspace_id: str) -> dict:
        record = self.workspaces[workspace_id]
        return {"result": {"workspace": dict(record)}}

    def _pane_payload(self, pane_id: str) -> dict:
        record = self.panes[pane_id]
        return {"result": {"pane": dict(record)}}

    def start_agent(self, name: str, pane_id: str, status: str = "idle") -> None:
        with self.lock:
            pane = self.panes[pane_id]
            pane["agent_status"] = status
            self.agents[name] = {
                "name": name,
                "pane_id": pane_id,
                "status": status,
                "agent_status": status,
            }

    def set_agent_status(self, name: str, status: str) -> None:
        with self.lock:
            agent = self.agents[name]
            agent["status"] = status
            agent["agent_status"] = status
            pane_id = str(agent.get("pane_id") or "")
            if pane_id in self.panes:
                self.panes[pane_id]["agent_status"] = status

    def __call__(self, *args: str, **kwargs: object) -> dict:
        del kwargs
        with self.lock:
            self.calls.append(args)
            verb = args[:2] if len(args) >= 2 else args
            if verb == ("workspace", "create"):
                return self._workspace_create(args)
            if verb == ("workspace", "list"):
                return {
                    "result": {
                        "workspaces": [
                            dict(item)
                            for item in self.workspaces.values()
                            if item["workspace_id"] not in self.closed_workspaces
                        ]
                    }
                }
            if verb == ("workspace", "get"):
                workspace_id = args[2]
                if (
                    workspace_id not in self.workspaces
                    or workspace_id in self.closed_workspaces
                ):
                    self._raise(lch.WORKSPACE_NOT_FOUND)
                return self._workspace_payload(workspace_id)
            if verb == ("workspace", "close"):
                return self._workspace_close(args[2])
            if verb == ("workspace", "report-metadata"):
                return self._tag(self.workspaces, args[2], args)
            if verb == ("worktree", "open"):
                return self._worktree_open(args)
            if verb == ("worktree", "list"):
                parent_id = _flag(args, "--workspace") or ""
                return {
                    "result": {
                        "source": {"source_workspace_id": parent_id},
                        "worktrees": list(self.worktrees.get(parent_id, ())),
                    }
                }
            if verb == ("tab", "list"):
                workspace_id = _flag(args, "--workspace") or ""
                tabs = [
                    dict(tab)
                    for tab in self.tabs.values()
                    if tab.get("workspace_id") == workspace_id
                ]
                return {"result": {"tabs": tabs}}
            if verb == ("tab", "close"):
                self.tabs.pop(args[2], None)
                return {"result": {"type": "ok"}}
            if verb == ("pane", "split"):
                return self._pane_split(args)
            if verb == ("pane", "rename"):
                pane_id, label = args[2], args[3]
                if pane_id not in self.panes or pane_id in self.closed_panes:
                    self._raise(lch.PANE_NOT_FOUND)
                self.panes[pane_id]["label"] = label
                return {}
            if verb == ("pane", "get"):
                pane_id = args[2]
                if pane_id not in self.panes or pane_id in self.closed_panes:
                    self._raise(lch.PANE_NOT_FOUND)
                return self._pane_payload(pane_id)
            if verb == ("pane", "list"):
                workspace_id = _flag(args, "--workspace") or ""
                panes = [
                    dict(pane)
                    for pane in self.panes.values()
                    if pane.get("workspace_id") == workspace_id
                    and pane["pane_id"] not in self.closed_panes
                ]
                return {"result": {"panes": panes}}
            if verb == ("pane", "close"):
                pane_id = args[2]
                if pane_id not in self.panes or pane_id in self.closed_panes:
                    self._raise(lch.PANE_NOT_FOUND)
                self.closed_panes.add(pane_id)
                return {"result": {"type": "ok", "closed": True}}
            if verb == ("pane", "report-metadata"):
                return self._tag(self.panes, args[2], args)
            if verb == ("pane", "read"):
                pane_id = args[2]
                if pane_id not in self.panes or pane_id in self.closed_panes:
                    self._raise(lch.PANE_NOT_FOUND)
                return {"result": {"text": str(self.panes[pane_id].get("output") or "")}}
            if verb == ("pane", "send-text"):
                pane_id, text = args[2], args[3]
                pane = self.panes[pane_id]
                pane["output"] = str(pane.get("output") or "") + text
                pane["last_text"] = text
                pane["revision"] = int(pane.get("revision") or 0) + 1
                return {}
            if verb == ("pane", "send-keys"):
                pane_id, key = args[2], args[3]
                pane = self.panes[pane_id]
                pane["keys"] = list(pane.get("keys") or []) + [key]
                if key.lower() == "enter" and self.rename_confirms:
                    last = str(pane.get("last_text") or "")
                    if last.startswith("/rename "):
                        name = last[len("/rename ") :]
                        pane["output"] = lch.session_rename_confirmation(name)
                return {}
            if verb == ("pane", "wait-output"):
                if self.wait_output_error:
                    self._raise(self.wait_output_error)
                pane_id = args[2]
                needle = _flag(args, "--match") or ""
                text = str(self.panes[pane_id].get("output") or "")
                if needle and needle not in text:
                    self._raise("wait_output_timeout")
                return {"result": {"text": text}}
            if verb == ("pane", "process-info"):
                pane_id = _flag(args, "--pane") or ""
                return {
                    "result": {
                        "process_info": {
                            "shell_pid": 11,
                            "foreground_process_group_id": 11,
                            "foreground_processes": [{"pid": 11, "name": "zsh"}],
                        }
                    }
                }
            if verb == ("agent", "start"):
                name = args[2]
                pane_id = _flag(args, "--pane") or ""
                self.agents[name] = {
                    "name": name,
                    "pane_id": pane_id,
                    "status": "idle",
                    "agent_status": "idle",
                }
                if pane_id in self.panes:
                    self.panes[pane_id]["agent_status"] = "idle"
                return {"result": {"agent": dict(self.agents[name])}}
            if verb == ("agent", "get"):
                name = args[2]
                agent = self.agents.get(name)
                if agent is None:
                    self._raise(lch.AGENT_NOT_FOUND)
                pane_id = str(agent.get("pane_id") or "")
                if pane_id in self.closed_panes:
                    self._raise(lch.AGENT_NOT_FOUND)
                return {"result": {"agent": dict(agent)}}
            if verb == ("agent", "wait"):
                name = args[2]
                agent = self.agents.get(name)
                if agent is None:
                    self._raise(lch.AGENT_NOT_FOUND)
                return {"result": {"agent": dict(agent)}}
            raise AssertionError(args)

    def _tag(self, store: dict[str, dict], item_id: str, args: tuple[str, ...]) -> dict:
        if item_id not in store:
            self._raise(lch.PANE_NOT_FOUND)
        tokens = dict(store[item_id].get("tokens") or {})
        tokens.update(_tokens_from_args(args))
        store[item_id]["tokens"] = tokens
        return {}

    def _workspace_create(self, args: tuple[str, ...]) -> dict:
        workspace_id = self._next("w")
        tab_id = "{}:t1".format(workspace_id)
        pane_id = "{}:p1".format(workspace_id)
        label = _flag(args, "--label") or ""
        workspace = {
            "workspace_id": workspace_id,
            "label": label,
            "tokens": {},
            "worktree": {"is_linked_worktree": False},
        }
        self.workspaces[workspace_id] = workspace
        self.tabs[tab_id] = {
            "tab_id": tab_id,
            "workspace_id": workspace_id,
            "label": label,
        }
        self.panes[pane_id] = {
            "pane_id": pane_id,
            "tab_id": tab_id,
            "workspace_id": workspace_id,
            "cwd": "",
            "label": "",
            "tokens": {},
            "revision": 0,
            "output": "",
            "agent_status": "unknown",
        }
        return {
            "result": {
                "workspace": dict(workspace),
                "tab": dict(self.tabs[tab_id]),
                "root_pane": dict(self.panes[pane_id]),
            }
        }

    def _worktree_open(self, args: tuple[str, ...]) -> dict:
        parent_id = _flag(args, "--workspace") or ""
        path = _flag(args, "--path") or ""
        label = _flag(args, "--label") or ""
        if parent_id not in self.workspaces or parent_id in self.closed_workspaces:
            self._raise(lch.WORKSPACE_NOT_FOUND)
        child_id = self._next("w")
        tab_id = "{}:t1".format(child_id)
        pane_id = "{}:p1".format(child_id)
        workspace = {
            "workspace_id": child_id,
            "label": label,
            "tokens": {},
            "worktree": {"is_linked_worktree": True, "path": path},
        }
        self.workspaces[child_id] = workspace
        self.worktrees.setdefault(parent_id, []).append(
            {
                "open_workspace_id": child_id,
                "is_linked_worktree": True,
                "path": path,
                "label": label,
            }
        )
        self.tabs[tab_id] = {
            "tab_id": tab_id,
            "workspace_id": child_id,
            "label": label,
        }
        self.panes[pane_id] = {
            "pane_id": pane_id,
            "tab_id": tab_id,
            "workspace_id": child_id,
            "cwd": str(Path(path).resolve()) if path else "",
            "label": "",
            "tokens": {},
            "revision": 0,
            "output": "",
            "agent_status": "unknown",
        }
        return {
            "result": {
                "workspace": dict(workspace),
                "tab": dict(self.tabs[tab_id]),
                "root_pane": dict(self.panes[pane_id]),
                "worktree": {"path": path, "is_linked_worktree": True},
                "already_open": False,
            }
        }

    def _pane_split(self, args: tuple[str, ...]) -> dict:
        parent_id = args[2]
        if parent_id not in self.panes or parent_id in self.closed_panes:
            self._raise(lch.PANE_NOT_FOUND)
        parent = self.panes[parent_id]
        workspace_id = str(parent["workspace_id"])
        existing = [
            pane_id
            for pane_id, pane in self.panes.items()
            if pane.get("workspace_id") == workspace_id
        ]
        pane_id = "{}:p{}".format(workspace_id, len(existing) + 1)
        cwd = _flag(args, "--cwd") or ""
        self.panes[pane_id] = {
            "pane_id": pane_id,
            "tab_id": parent["tab_id"],
            "workspace_id": workspace_id,
            "cwd": str(Path(cwd).resolve()) if cwd else "",
            "label": "",
            "tokens": {},
            "revision": 0,
            "output": "",
            "agent_status": "unknown",
        }
        return {"result": {"pane": dict(self.panes[pane_id])}}

    def _workspace_close(self, workspace_id: str) -> dict:
        if self.close_workspace_error:
            self._raise(self.close_workspace_error)
        if workspace_id not in self.workspaces or workspace_id in self.closed_workspaces:
            self._raise(lch.WORKSPACE_NOT_FOUND)
        self.closed_workspaces.add(workspace_id)
        for pane_id, pane in list(self.panes.items()):
            if pane.get("workspace_id") == workspace_id:
                self.closed_panes.add(pane_id)
        return {"result": {"type": "ok"}}


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
) -> lch.LaunchSpec:
    label = workspace_label or lch.workspace_label_for(PROJECT, run_id)
    env = lch.role_pane_environment(worktree, {})
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
        self.assertIn("--no-focus", creates[0])
        self.assertEqual(len(opens), 1)
        self.assertEqual(_flag(opens[0], "--workspace"), handle.parent_workspace_id)
        self.assertEqual(_flag(opens[0], "--label"), TESTS_LANE)
        self.assertTrue(_same_path(_flag(opens[0], "--path"), tester))
        self.assertIn("--no-focus", opens[0])
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
    def test_first_role_uses_child_root_later_roles_split_inside_child(self) -> None:
        herdr = FakeHerdr()
        launcher = _launcher(lch.workspace_label_for(PROJECT, RUN_HASH))
        launcher._herdr = herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tester = _checkout(root, "tester")
            (tester / "secret.txt").write_text("private-tests", encoding="utf-8")
            reviewer = _checkout(root, "reviewer")
            builder = _checkout(root, "builder")
            first, layout, _ = _place(
                launcher, herdr, _spec(tester, lane=TESTS_LANE, role="tester")
            )
            splits_before = [call for call in herdr.calls if call[:2] == ("pane", "split")]
            self.assertEqual(splits_before, [])
            self.assertEqual(first.pane_id, layout.panes[0])
            second, same, _ = _place(
                launcher,
                herdr,
                _spec(reviewer, lane=TESTS_LANE, role="test-reviewer"),
            )
            self.assertIs(same, layout)
            splits = [call for call in herdr.calls if call[:2] == ("pane", "split")]
            self.assertEqual(len(splits), 1)
            self.assertEqual(splits[0][2], first.pane_id)
            self.assertIn("--no-focus", splits[0])
            self.assertNotIn("--current", splits[0])
            self.assertTrue(_same_path(_flag(splits[0], "--cwd"), reviewer))
            self.assertEqual(lch.workspace_of(second.pane_id), first.child_workspace_id)
            self.assertEqual(herdr.panes[second.pane_id]["label"], "tester-reviewer")
            self.assertNotIn(
                "private-tests",
                _env_from_args(splits[0]).get("TMPDIR", ""),
            )
            self.assertTrue(
                _env_from_args(splits[0])["TMPDIR"].startswith(str(reviewer.resolve()))
            )
            other, other_layout, _ = _place(
                launcher, herdr, _spec(builder, lane=BUILD_LANE, role="builder")
            )
        self.assertEqual(other.parent_workspace_id, first.parent_workspace_id)
        self.assertNotEqual(other.child_workspace_id, first.child_workspace_id)
        self.assertEqual(other_layout.role_panes, {"builder": other.pane_id})
        self.assertFalse(any("--current" in call for call in herdr.calls))
        for call in herdr.calls:
            if call[:2] in (("workspace", "create"), ("worktree", "open"), ("pane", "split")):
                self.assertIn("--no-focus", call)


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

    def test_label_only_run_workspace_is_refused(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        herdr.workspaces["w9"] = {
            "workspace_id": "w9",
            "label": label,
            "tokens": {},
            "worktree": {"is_linked_worktree": False},
        }
        launcher._herdr = herdr  # type: ignore[method-assign]
        with self.assertRaises(lch.LaunchRefused) as raised:
            launcher._run_workspace({})
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertIn("LABEL_ONLY_RUN_WORKSPACE", raised.exception.detail)
        self.assertFalse(any(call[:2] == ("workspace", "create") for call in herdr.calls))

    def test_duplicate_run_workspace_is_refused(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        tokens = launcher._parent_identity_tokens()
        for workspace_id in ("w1", "w2"):
            herdr.workspaces[workspace_id] = {
                "workspace_id": workspace_id,
                "label": label,
                "tokens": dict(tokens),
                "worktree": {"is_linked_worktree": False},
            }
        launcher._herdr = herdr  # type: ignore[method-assign]
        with self.assertRaises(lch.LaunchRefused) as raised:
            launcher._run_workspace({})
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertIn("DUPLICATE_RUN_WORKSPACE", raised.exception.detail)

    def test_wrong_parent_child_is_not_adopted(self) -> None:
        herdr = FakeHerdr()
        label = lch.workspace_label_for(PROJECT, RUN_HASH)
        launcher = _launcher(label)
        launcher._herdr = herdr  # type: ignore[method-assign]
        parent_tokens = launcher._parent_identity_tokens()
        herdr.workspaces["wP"] = {
            "workspace_id": "wP",
            "label": label,
            "tokens": dict(parent_tokens),
            "worktree": {"is_linked_worktree": False},
        }
        herdr.workspaces["wWrong"] = {
            "workspace_id": "wWrong",
            "label": TESTS_LANE,
            "tokens": {
                **launcher._lane_identity_tokens(TESTS_LANE, "wOTHER"),
            },
            "worktree": {"is_linked_worktree": True, "path": "/tmp/wrong"},
        }
        herdr.worktrees["wP"] = [
            {
                "open_workspace_id": "wWrong",
                "is_linked_worktree": True,
                "label": TESTS_LANE,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tester = _checkout(Path(tmp), "tester")
            spec = _spec(tester, lane=TESTS_LANE, role="tester")
            handle, layout, _ = _place(launcher, herdr, spec)
        self.assertNotEqual(handle.child_workspace_id, "wWrong")
        self.assertNotEqual(layout.child_workspace_id, "wWrong")
        self.assertEqual(handle.parent_workspace_id, "wP")

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

    def test_rename_then_close_children_then_parent(self) -> None:
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
                [tester_handle.child_workspace_id, tester_handle.parent_workspace_id],
            )
            self.assertIn(tester_handle.child_workspace_id, herdr.closed_workspaces)
            self.assertIn(tester_handle.parent_workspace_id, herdr.closed_workspaces)
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


if __name__ == "__main__":
    unittest.main()
