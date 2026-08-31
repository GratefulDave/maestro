"""Persistent role panes, project+run workspaces, resume reconnect."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher as lch


def _bare_launcher(label: str) -> lch.HerdrLauncher:
    launcher = lch.HerdrLauncher.__new__(lch.HerdrLauncher)
    launcher.herdr_path = Path("herdr")
    launcher.omp_path = Path("omp")
    launcher.claude_path = Path("claude")
    launcher.admitted_routes = None  # type: ignore[assignment]
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
    launcher._workspace_id = ""
    launcher._tabs = {}
    launcher._seed_tab_id = ""
    launcher._role_handles = {}
    return launcher


def _attempt_checkout(
    root: Path,
    run_id: str,
    lane: str,
    stage: str,
    digest: str,
    attempt: str,
) -> Path:
    path = root / run_id / lane / stage / digest[:16] / attempt / "checkout"
    path.mkdir(parents=True)
    return path


def _placement_lists(args: tuple[str, ...]) -> dict | None:
    if args[:2] == ("workspace", "list"):
        return {
            "result": {"workspaces": [{"workspace_id": "w9", "label": "product run-1"}]}
        }
    if args[:2] == ("tab", "list"):
        return {"result": {"tabs": [{"tab_id": "w9:t1", "label": "lane-a"}]}}
    return None


def _env_from_herdr_args(args: tuple[str, ...]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, item in enumerate(args):
        if item == "--env" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            values[key] = value
    return values


def _role_environment(root: Path) -> dict[str, str]:
    return lch.role_pane_environment(root, {})


class WorkspaceLabelTest(unittest.TestCase):
    def test_project_plus_run_not_bare_run_id(self) -> None:
        self.assertEqual(
            lch.workspace_label_for("lexgenius", "run-abc"),
            "lexgenius run-abc",
        )
        self.assertNotEqual(lch.workspace_label_for("lexgenius", "run-abc"), "run-abc")

    def test_empty_project_falls_back_without_dropping_run(self) -> None:
        self.assertEqual(lch.workspace_label_for("   ", "run-1"), "maestro run-1")


class WorkspaceAdoptTest(unittest.TestCase):
    def test_run_workspace_adopts_matching_label(self) -> None:
        launcher = _bare_launcher("product run-1")
        calls: list[tuple[str, ...]] = []

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            calls.append(args)
            if args[:2] == ("workspace", "list"):
                return {
                    "result": {
                        "workspaces": [
                            {"workspace_id": "w9", "label": "product run-1"},
                        ]
                    }
                }
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        self.assertEqual(launcher._run_workspace({}), "w9")
        self.assertTrue(any(call[:2] == ("workspace", "list") for call in calls))
        self.assertFalse(any(call[:2] == ("workspace", "create") for call in calls))

    def test_acquire_pane_reuses_role_without_split(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._workspace_id = "w9"
        panes = ["w9:p1", "w9:p2", "w9:p3", "w9:p4", "w9:p5"]
        layout = lch._TabLayout(tab_id="w9:t1", panes=list(panes), claimed=5)
        for role, pane_id in zip(lch.LANE_PANE_ROLES, panes):
            layout.role_panes[role] = pane_id
        launcher._tabs["lane-a"] = layout
        renamed: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "tester"),
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="tester",
                workspace_label="product run-1",
                pane_group_size=5,
            )

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                if args[:2] == ("pane", "rename"):
                    renamed.append(args[3])
                    return {}
                if args[:2] == ("pane", "get"):
                    role = lch.LANE_PANE_ROLES[panes.index(args[2])]
                    return {"result": {"pane": {"pane_id": args[2], "label": role}}}
                raise AssertionError(args)

            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            pane_id, found, reused = launcher._acquire_pane(
                spec, worktree, _role_environment(worktree)
            )
        self.assertEqual(pane_id, "w9:p1")
        self.assertIs(found, layout)
        self.assertTrue(reused)
        self.assertEqual(renamed, list(lch.LANE_PANE_ROLES))
        self.assertFalse(any(name == "pane split" for name in renamed))

    def test_adoption_replaces_agentless_shells_without_closing_live_role(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._workspace_id = "w9"
        old_panes = ["w9:p1", "w9:p2", "w9:p3", "w9:p4", "w9:p5"]
        payload = {
            "result": {
                "panes": [
                    {
                        "pane_id": pane_id,
                        "tab_id": "w9:t1",
                        "label": role,
                        "agent_status": "working" if role == "tester" else "unknown",
                    }
                    for role, pane_id in zip(lch.LANE_PANE_ROLES, old_panes)
                ]
            }
        }
        layout = launcher._validated_role_layout("w9:t1", payload)
        launcher._tabs["lane-a"] = layout
        closed: list[str] = []
        split_calls: list[tuple[str, ...]] = []
        labels = dict(zip(old_panes, lch.LANE_PANE_ROLES))

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("pane", "close"):
                closed.append(args[2])
                return {"result": {"closed": True}}
            if args[:2] == ("pane", "split"):
                split_calls.append(args)
                pane_id = "w9:p{}".format(6 + len(split_calls) - 1)
                return {"result": {"pane": {"pane_id": pane_id}}}
            if args[:2] == ("pane", "rename"):
                labels[args[2]] = args[3]
                return {}
            if args[:2] == ("pane", "get"):
                return {
                    "result": {
                        "pane": {
                            "pane_id": args[2],
                            "label": labels.get(args[2], ""),
                        }
                    }
                }
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_cwds = {role: root / role for role in lch.LANE_PANE_ROLES}
            for cwd in role_cwds.values():
                cwd.mkdir()
            environment = _role_environment(role_cwds["builder"])
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "builder"),
                worktree=role_cwds["builder"],
                prompt_path=root / "prompt.json",
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                environment=environment,
                lane_key="lane-a",
                lane_label="lane-a",
                pane_role="builder",
                run_id="run-1",
                workspace_label="product run-1",
                pane_group_size=5,
                role_cwds=role_cwds,
            )
            pane_id, found, reused = launcher._acquire_pane(
                spec, role_cwds["builder"], environment
            )

        self.assertIs(found, layout)
        self.assertTrue(reused)
        self.assertEqual(layout.role_panes["tester"], old_panes[0])
        self.assertEqual(set(closed), set(old_panes[1:]))
        self.assertEqual(pane_id, layout.role_panes["builder"])
        self.assertEqual(len(split_calls), 4)
        for args, role in zip(split_calls, lch.LANE_PANE_ROLES[1:]):
            self.assertTrue(
                _env_from_herdr_args(args)["TMPDIR"].startswith(
                    str(role_cwds[role].resolve())
                )
            )

    def test_reconnect_live_agent_does_not_create_workspace(self) -> None:
        launcher = _bare_launcher("product run-1")
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            token = lch.role_session_token("run-1", "lane-a", "tester")
            name = lch.agent_name_for(token)
            transcript = worktree / "session.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            calls: list[tuple[str, ...]] = []

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                calls.append(args)
                if args[:2] == ("agent", "get"):
                    self.assertEqual(args[2], name)
                    return {
                        "result": {
                            "agent": {
                                "pane_id": "w9:p1",
                                "status": "idle",
                                "agent_session": {
                                    "kind": "path",
                                    "value": str(transcript),
                                },
                            }
                        }
                    }
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p1",
                                "tab_id": "w9:t1",
                                "cwd": str(worktree),
                                "label": "tester",
                            }
                        }
                    }
                if args[:2] == ("pane", "list"):
                    return {
                        "result": {
                            "panes": [
                                {
                                    "pane_id": "w9:p1",
                                    "tab_id": "w9:t1",
                                    "label": "tester",
                                },
                                {
                                    "pane_id": "w9:p2",
                                    "tab_id": "w9:t1",
                                    "label": "test-reviewer",
                                },
                                {
                                    "pane_id": "w9:p3",
                                    "tab_id": "w9:t1",
                                    "label": "builder",
                                },
                                {
                                    "pane_id": "w9:p4",
                                    "tab_id": "w9:t1",
                                    "label": "code-reviewer",
                                },
                                {
                                    "pane_id": "w9:p5",
                                    "tab_id": "w9:t1",
                                    "label": "integration-reviewer",
                                },
                            ]
                        }
                    }
                placed = _placement_lists(args)
                if placed is not None:
                    return placed
                raise AssertionError(args)

            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="tester",
                workspace_label="product run-1",
            )
            handle = launcher._reconnect_live_agent(spec, {})
            self.assertIsNotNone(handle)
            assert handle is not None
            self.assertEqual(handle.pane_id, "w9:p1")
            self.assertEqual(handle.agent_name, name)
            self.assertEqual(handle.transcript_path, transcript)
            self.assertFalse(any(call[:2] == ("workspace", "create") for call in calls))
            self.assertFalse(any(call[:2] == ("pane", "split") for call in calls))
            self.assertFalse(any(call[:2] == ("agent", "start") for call in calls))
            self.assertIn(("lane-a", "tester"), launcher._role_handles)
            launcher._verified_handle_binding(handle)

    def test_fresh_launcher_adopts_same_live_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            token = lch.role_session_token("run-1", "lane-a", "builder")
            name = lch.agent_name_for(token)
            transcript = worktree / "omp.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                if args[:2] == ("agent", "get"):
                    self.assertEqual(args[2], name)
                    return {
                        "result": {
                            "pane_id": "w9:p4",
                            "agent_status": "idle",
                            "agent_session": {
                                "kind": "path",
                                "value": str(transcript),
                            },
                        }
                    }
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p4",
                                "tab_id": "w9:t1",
                                "cwd": str(worktree),
                                "label": "builder",
                            }
                        }
                    }
                if args[:2] == ("pane", "list"):
                    return {
                        "result": {
                            "panes": [
                                {
                                    "pane_id": "w9:p1",
                                    "tab_id": "w9:t1",
                                    "label": "tester",
                                },
                                {
                                    "pane_id": "w9:p2",
                                    "tab_id": "w9:t1",
                                    "label": "test-reviewer",
                                },
                                {
                                    "pane_id": "w9:p4",
                                    "tab_id": "w9:t1",
                                    "label": "builder",
                                },
                                {
                                    "pane_id": "w9:p5",
                                    "tab_id": "w9:t1",
                                    "label": "code-reviewer",
                                },
                                {
                                    "pane_id": "w9:p6",
                                    "tab_id": "w9:t1",
                                    "label": "integration-reviewer",
                                },
                            ]
                        }
                    }
                placed = _placement_lists(args)
                if placed is not None:
                    return placed
                raise AssertionError(args)

            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="builder",
                workspace_label="product run-1",
            )
            first = _bare_launcher("product run-1")
            first._herdr = fake_herdr  # type: ignore[method-assign]
            second = _bare_launcher("product run-1")
            second._herdr = fake_herdr  # type: ignore[method-assign]
            handle = first._reconnect_live_agent(spec, {})
            adopted = second._reconnect_live_agent(spec, {})
            self.assertIsNotNone(handle)
            self.assertIsNotNone(adopted)
            assert handle is not None and adopted is not None
            self.assertEqual(handle.pane_id, adopted.pane_id)
            self.assertEqual(adopted.agent_name, name)
            self.assertEqual(adopted.transcript_path, transcript)
            self.assertIn(("lane-a", "builder"), first._role_handles)
            self.assertIn(("lane-a", "builder"), second._role_handles)
            second._verified_handle_binding(adopted)
            self.assertIsNot(
                first._role_handles[("lane-a", "builder")],
                second._role_handles[("lane-a", "builder")],
            )

    def test_stable_reconnect_prior_digest_cwd_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            role_root = root / "run-1" / "lane-a" / "tester"
            prior = role_root / "prior" / "checkout"
            current = role_root / "checkout"
            prior.mkdir(parents=True)
            current.mkdir(parents=True)
            token = lch.role_session_token("run-1", "lane-a", "tester")
            name = lch.agent_name_for(token)
            transcript = prior / "session.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            calls: list[tuple[str, ...]] = []

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                calls.append(args)
                if args[:2] == ("agent", "get"):
                    self.assertEqual(args[2], name)
                    return {
                        "result": {
                            "agent": {
                                "pane_id": "w9:p1",
                                "status": "idle",
                                "agent_session": {
                                    "kind": "path",
                                    "value": str(transcript),
                                },
                            }
                        }
                    }
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p1",
                                "tab_id": "w9:t1",
                                "workspace_id": "w9",
                                "label": "tester",
                                "cwd": str(prior),
                            }
                        }
                    }
                if args[:2] == ("pane", "list"):
                    return {
                        "result": {
                            "panes": [
                                {
                                    "pane_id": "w9:p1",
                                    "tab_id": "w9:t1",
                                    "label": "tester",
                                }
                            ]
                        }
                    }
                placed = _placement_lists(args)
                if placed is not None:
                    return placed
                raise AssertionError(args)

            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=current,
                prompt_path=current / "prompt.json",
                envelope_path=current / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=current / "session",
                lane_key="lane-a",
                pane_role="tester",
                run_id="run-1",
                workspace_label="product run-1",
            )
            launcher = _bare_launcher("product run-1")
            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._reconnect_live_agent(spec, {})
            self.assertEqual(
                raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertIn(str(prior.resolve()), raised.exception.detail)
            self.assertIn(str(current.resolve()), raised.exception.detail)
            self.assertFalse(any(call[:2] == ("workspace", "create") for call in calls))
            self.assertFalse(any(call[:2] == ("pane", "split") for call in calls))
            self.assertFalse(any(call[:2] == ("agent", "start") for call in calls))

    def test_stable_name_out_of_scope_cwd_refuses(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as other,
        ):
            root = Path(tmp).resolve()
            digest = "aabbccddeeff00112233445566778899"
            current = _attempt_checkout(
                root, "run-1", "lane-a", "WRITING_TESTS", digest, "attempt-new"
            )
            foreign = Path(other).resolve()
            token = lch.role_session_token("run-1", "lane-a", "tester")
            name = lch.agent_name_for(token)
            calls: list[tuple[str, ...]] = []

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                calls.append(args)
                if args[:2] == ("agent", "get"):
                    self.assertEqual(args[2], name)
                    return {
                        "result": {
                            "agent": {
                                "pane_id": "w9:p1",
                                "status": "idle",
                            }
                        }
                    }
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p1",
                                "tab_id": "w9:t1",
                                "cwd": str(foreign),
                            }
                        }
                    }
                raise AssertionError(args)

            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=current,
                prompt_path=current / "prompt.json",
                envelope_path=current / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=current / "session",
                lane_key="lane-a",
                pane_role="tester",
                run_id="run-1",
                stage="WRITING_TESTS",
                input_digest=digest,
                workspace_label="product run-1",
            )
            launcher = _bare_launcher("product run-1")
            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._reconnect_live_agent(spec, {})
            self.assertEqual(
                raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertFalse(raised.exception.pane_created)
            self.assertFalse(any(call[:2] == ("workspace", "create") for call in calls))
            self.assertFalse(any(call[:2] == ("agent", "start") for call in calls))
            self.assertFalse(any(call[:2] == ("pane", "split") for call in calls))

    def test_label_pane_uses_exact_persistent_role(self) -> None:
        launcher = _bare_launcher("product run-1")
        renamed: list[str] = []

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("pane", "rename"):
                renamed.append(args[3])
                return {}
            if args[:2] == ("pane", "get"):
                return {"result": {"pane": {"pane_id": "w9:p1", "label": "tester"}}}
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "tester"),
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="tester",
                attempt_no=3,
            )
            launcher._label_pane("w9:p1", spec, {})
        self.assertEqual(renamed, ["tester"])

    def test_label_pane_reaps_when_exact_label_is_unconfirmed(self) -> None:
        launcher = _bare_launcher("product run-1")
        layout = lch._TabLayout(tab_id="w9:t1", panes=["w9:p1"], claimed=1)
        launcher._tabs["lane-a"] = layout
        calls: list[tuple[str, ...]] = []

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            calls.append(args)
            if args[:2] == ("pane", "rename"):
                return {}
            if args[:2] == ("pane", "get"):
                return {"result": {"pane": {"pane_id": "w9:p1", "label": "a3"}}}
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "tester"),
                worktree=root,
                prompt_path=root / "prompt.json",
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                lane_key="lane-a",
                pane_role="tester",
            )
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._label_pane("w9:p1", spec, {})
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertTrue(raised.exception.pane_created)
        self.assertEqual(layout.panes, ["w9:p1"])
        self.assertFalse(any(call[:2] == ("pane", "close") for call in calls))

    def test_stable_lookup_runtime_error_is_not_absence(self) -> None:
        launcher = _bare_launcher("product run-1")
        calls: list[tuple[str, ...]] = []

        def failed(*args: str, **kwargs: object) -> dict:
            del kwargs
            calls.append(args)
            raise RuntimeError("transport failed")

        launcher._herdr = failed  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            launcher._fetch_agent("maestro-stable", {})
        self.assertEqual(calls, [("agent", "get", "maestro-stable")])

    def test_stable_lookup_malformed_record_is_not_absence(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._herdr = (  # type: ignore[method-assign]
            lambda *args, **kwargs: {"result": {}}
        )
        with self.assertRaisesRegex(RuntimeError, "HERDR_AGENT_RECORD_INVALID"):
            launcher._fetch_agent("maestro-stable", {})

    def test_flat_agent_record_drives_status_poll_and_presence(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._herdr = (  # type: ignore[method-assign]
            lambda *args, **kwargs: {
                "result": {"pane_id": "w9:p1", "agent_status": "working"}
            }
        )
        token = lch.role_session_token("run-1", "lane-a", "tester")
        handle = lch.LaunchHandle(
            token,
            "w9:p1",
            lch.agent_name_for(token),
            Path("/tmp"),
        )
        self.assertEqual(launcher.agent_status(handle), "working")
        self.assertEqual(launcher.poll(handle).state, lch.PollState.RUNNING)
        self.assertFalse(launcher._agent_absent(handle))
        self.assertTrue(launcher.agent_presence(token))

    def test_cached_role_cwd_mismatch_refuses_before_resubmit(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher.admitted_routes = type(
            "Routes", (), {"admits": lambda self, route: route == "omp"}
        )()
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old"
            old.mkdir()
            fresh = root / "fresh"
            prompt = root / "prompt.json"
            prompt.write_text("{}", encoding="utf-8")
            token = lch.role_session_token("run-1", "lane-a", "builder")
            handle = lch.LaunchHandle(
                token,
                "w9:p4",
                lch.agent_name_for(token),
                old,
                workspace_id="w9",
                tab_id="w9:t1",
                lane_key="lane-a",
            )
            launcher._role_handles[("lane-a", "builder")] = handle
            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=fresh,
                prompt_path=prompt,
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                lane_key="lane-a",
                pane_role="builder",
                workspace_label="product run-1",
                prepare_adopted_cwd=lambda cwd: events.append("prepare:" + str(cwd)),
            )
            agent = {
                "pane_id": "w9:p4",
                "status": "idle",
                "name": handle.agent_name,
            }
            pane = {
                "pane_id": "w9:p4",
                "tab_id": "w9:t1",
                "workspace_id": "w9",
                "cwd": str(old),
                "label": "builder",
            }
            with (
                mock.patch.object(lch, "prepare_route_prompt"),
                mock.patch.object(lch, "preflight_launch_prompt"),
                mock.patch.object(lch, "build_omp_argv", return_value=("omp",)),
                mock.patch.object(lch, "pane_env_flags", return_value=()),
                mock.patch.object(launcher, "_fetch_agent", return_value=agent),
                mock.patch.object(launcher, "_prove_live_pane", return_value=pane),
            ):
                with self.assertRaises(lch.LaunchRefused) as raised:
                    launcher.launch(spec)
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertIn(str(old.resolve()), raised.exception.detail)
        self.assertIn(str(fresh.resolve()), raised.exception.detail)
        self.assertEqual(events, [])

    def test_reused_idle_role_pane_prepares_matching_cwd_before_start(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher.provision_argv = ("bun", "install", "--frozen-lockfile")
        launcher.admitted_routes = type(
            "Routes", (), {"admits": lambda self, route: route == "omp"}
        )()
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retained = root / "retained"
            retained.mkdir()
            prompt = root / "prompt.json"
            prompt.write_text("{}", encoding="utf-8")
            token = lch.role_session_token("run-1", "lane-a", "builder")
            layout = lch._TabLayout(tab_id="w9:t1", panes=["w9:p4"], claimed=5)
            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=retained,
                prompt_path=prompt,
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                lane_key="lane-a",
                pane_role="builder",
                prepare_adopted_cwd=lambda cwd: events.append("prepare:" + str(cwd)),
            )

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p4",
                                "tab_id": "w9:t1",
                                "cwd": str(retained),
                                "label": "builder",
                            }
                        }
                    }
                if args[:2] == ("agent", "start"):
                    events.append("start")
                    return {
                        "result": {
                            "agent": {
                                "pane_id": "w9:p4",
                                "transcript_path": str(root / "session.jsonl"),
                            }
                        }
                    }
                if args[:2] == ("agent", "get"):
                    return {
                        "result": {
                            "agent": {
                                "pane_id": "w9:p4",
                                "status": "idle",
                                "agent_session": {
                                    "kind": "path",
                                    "value": str(root / "session.jsonl"),
                                },
                            }
                        }
                    }
                raise AssertionError(args)

            def prepare(_spec: lch.LaunchSpec) -> None:
                events.append("route")

            def preflight(_spec: lch.LaunchSpec) -> None:
                events.append("preflight")

            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            with (
                mock.patch.object(lch, "prepare_route_prompt", prepare),
                mock.patch.object(lch, "preflight_launch_prompt", preflight),
                mock.patch.object(lch, "build_omp_argv", return_value=("omp",)),
                mock.patch.object(lch, "pane_env_flags", return_value=()),
                mock.patch.object(launcher, "_existing_role_handle", return_value=None),
                mock.patch.object(launcher, "_reconnect_live_agent", return_value=None),
                mock.patch.object(
                    launcher,
                    "provision",
                    side_effect=lambda worktree: events.append("provision"),
                ),
                mock.patch.object(
                    launcher,
                    "_acquire_pane",
                    return_value=("w9:p4", layout, True),
                ),
                mock.patch.object(launcher, "_label_pane"),
                mock.patch.object(lch, "_wait_for_available_shell"),
                mock.patch.object(
                    lch,
                    "_start_agent_when_free",
                    side_effect=lambda start, **kwargs: start(),
                ),
                mock.patch.object(lch, "wait_for_interactive_agent"),
                mock.patch.object(lch, "submit_agent_prompt"),
                mock.patch.object(lch, "pane_liveness_pid", return_value=None),
            ):
                handle = launcher.launch(spec)
        self.assertEqual(handle.launched_cwd, retained.resolve())
        self.assertEqual(
            events,
            [
                "route",
                "preflight",
                "provision",
                "prepare:" + str(retained.resolve()),
                "route",
                "preflight",
                "start",
            ],
        )

    def test_claude_discovers_transcript_after_first_prompt_submission(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher.admitted_routes = type(
            "Routes", (), {"admits": lambda self, route: route == "claude"}
        )()
        events: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "checkout"
            worktree.mkdir()
            config = root / "claude"
            prompt = root / "prompt.json"
            prompt.write_text("{}", encoding="utf-8")
            session_id = "claude-session"
            project = "".join(
                character if character.isalnum() or character == "-" else "-"
                for character in str(worktree.resolve())
            )
            transcript = config / "projects" / project / (session_id + ".jsonl")
            layout = lch._TabLayout(tab_id="w9:t1", panes=["w9:p4"], claimed=5)
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "tester"),
                worktree=worktree,
                prompt_path=prompt,
                envelope_path=root / "envelope.json",
                route="claude",
                model="opus",
                effort="high",
                profile=None,
                session_dir=root / "session",
                environment={"CLAUDE_CONFIG_DIR": str(config)},
                lane_key="lane-a",
                pane_role="tester",
            )

            def agent_record() -> dict:
                return {
                    "result": {
                        "agent": {
                            "pane_id": "w9:p4",
                            "agent_status": "idle",
                            "agent_session": {
                                "kind": "id",
                                "source": "herdr:claude",
                                "value": session_id,
                            },
                        }
                    }
                }

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p4",
                                "tab_id": "w9:t1",
                                "cwd": str(worktree),
                                "label": "tester",
                            }
                        }
                    }
                if args[:2] in (("agent", "start"), ("agent", "get")):
                    return agent_record()
                raise AssertionError(args)

            def submit(
                _herdr: object,
                _pane_id: str,
                _text: str,
                _name: str,
                **kwargs: object,
            ) -> None:
                events.append("submit")
                self.assertFalse(transcript.exists())
                transcript.parent.mkdir(parents=True)
                transcript.write_text(
                    "@" + str(prompt.resolve()) + "\n", encoding="utf-8"
                )
                recorded = kwargs["submission_recorded"]
                self.assertTrue(callable(recorded))
                self.assertTrue(recorded())
                events.append("discovered")

            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            with (
                mock.patch.object(lch, "prepare_route_prompt"),
                mock.patch.object(lch, "preflight_launch_prompt"),
                mock.patch.object(lch, "build_claude_argv", return_value=("claude",)),
                mock.patch.object(lch, "pane_env_flags", return_value=()),
                mock.patch.object(launcher, "_existing_role_handle", return_value=None),
                mock.patch.object(launcher, "_reconnect_live_agent", return_value=None),
                mock.patch.object(launcher, "provision"),
                mock.patch.object(
                    launcher,
                    "_acquire_pane",
                    return_value=("w9:p4", layout, True),
                ),
                mock.patch.object(launcher, "_label_pane"),
                mock.patch.object(lch, "_wait_for_available_shell"),
                mock.patch.object(
                    lch,
                    "_start_agent_when_free",
                    side_effect=lambda start, **kwargs: start(),
                ),
                mock.patch.object(lch, "wait_for_interactive_agent"),
                mock.patch.object(lch, "submit_agent_prompt", side_effect=submit),
                mock.patch.object(
                    lch,
                    "wait_for_agent_transcript",
                    side_effect=AssertionError("pre-submit transcript wait"),
                ),
                mock.patch.object(lch, "pane_liveness_pid", return_value=None),
            ):
                handle = launcher.launch(spec)

        self.assertEqual(events, ["submit", "discovered"])
        self.assertEqual(handle.transcript_path, transcript)


class BuilderFindingsRoutingTest(unittest.TestCase):
    def test_role_session_token_is_stable_across_stages(self) -> None:
        first = lch.role_session_token("run-1", "lane-a", "builder")
        second = lch.role_session_token("run-1", "lane-a", "builder")
        self.assertEqual(first, second)
        self.assertEqual(lch.agent_name_for(first), lch.agent_name_for(second))
        self.assertNotEqual(
            lch.role_session_token("run-1", "lane-a", "builder"),
            lch.role_session_token("run-1", "lane-a", "tester"),
        )


class TabAdoptTest(unittest.TestCase):
    def test_adopt_existing_tab_maps_role_labels(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._workspace_id = "w9"

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("tab", "list"):
                return {"result": {"tabs": [{"tab_id": "w9:t2", "label": "lane-a"}]}}
            if args[:2] == ("pane", "list"):
                return {
                    "result": {
                        "panes": [
                            {
                                "pane_id": "w9:p5",
                                "tab_id": "w9:t2",
                                "label": "integration-reviewer",
                            },
                            {"pane_id": "w9:p3", "tab_id": "w9:t2", "label": "tester"},
                            {"pane_id": "w9:p4", "tab_id": "w9:t2", "label": "builder"},
                            {
                                "pane_id": "w9:p2",
                                "tab_id": "w9:t2",
                                "label": "test-reviewer",
                            },
                            {
                                "pane_id": "w9:p6",
                                "tab_id": "w9:t2",
                                "label": "code-reviewer",
                            },
                        ]
                    }
                }
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        layout = launcher._adopt_existing_tab("w9", "lane-a", "lane-a", {})
        self.assertIsNotNone(layout)
        assert layout is not None
        self.assertEqual(layout.tab_id, "w9:t2")
        self.assertEqual(layout.role_panes["tester"], "w9:p3")
        self.assertEqual(layout.role_panes["test-reviewer"], "w9:p2")
        self.assertEqual(layout.role_panes["builder"], "w9:p4")
        self.assertEqual(layout.role_panes["code-reviewer"], "w9:p6")
        self.assertEqual(layout.role_panes["integration-reviewer"], "w9:p5")
        self.assertEqual(layout.panes, ["w9:p3", "w9:p2", "w9:p4", "w9:p6", "w9:p5"])

    def test_adopt_existing_tab_refuses_unknown_pane_label(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._workspace_id = "w9"

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("tab", "list"):
                return {"result": {"tabs": [{"tab_id": "w9:t2", "label": "lane-a"}]}}
            if args[:2] == ("pane", "list"):
                return {
                    "result": {
                        "panes": [
                            {
                                "pane_id": "w9:p3",
                                "tab_id": "w9:t2",
                                "label": "builder-a2",
                            }
                        ]
                    }
                }
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with self.assertRaises(lch.LaunchRefused) as raised:
            launcher._adopt_existing_tab("w9", "lane-a", "lane-a", {})
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertIn("UNMIGRATED_PANE_LABEL", raised.exception.detail)


class FivePaneTopologyTest(unittest.TestCase):
    def test_lane_tab_contains_exactly_five_role_panes(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._workspace_id = "w9"
        splits: list[str] = []
        renamed: list[str] = []
        labels: dict[str, str] = {}
        next_pane = {"n": 1}

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("tab", "list"):
                return {"result": {"tabs": []}}
            if args[:2] == ("tab", "create"):
                return {"result": {"tab": {"tab_id": "w9:t1"}}}
            if args[:2] == ("pane", "list"):
                return {"result": {"panes": [{"pane_id": "w9:p1", "tab_id": "w9:t1"}]}}
            if args[:2] == ("pane", "split"):
                splits.append(args[3])
                next_pane["n"] += 1
                return {
                    "result": {"pane": {"pane_id": "w9:p{}".format(next_pane["n"])}}
                }
            if args[:2] == ("pane", "rename"):
                renamed.append(args[3])
                labels[args[2]] = args[3]
                return {}
            if args[:2] == ("pane", "get"):
                return {
                    "result": {
                        "pane": {
                            "pane_id": args[2],
                            "label": labels.get(args[2], ""),
                        }
                    }
                }
            if args[:2] == ("pane", "close"):
                return {}
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_cwds = {role: root / role for role in lch.LANE_PANE_ROLES}
            for path in role_cwds.values():
                path.mkdir()
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "tester"),
                worktree=role_cwds["tester"],
                prompt_path=root / "prompt.json",
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                lane_key="lane-a",
                lane_label="lane-a",
                pane_role="tester",
                run_id="run-1",
                workspace_label="product run-1",
                pane_group_size=5,
                role_cwds=role_cwds,
            )
            pane_id, layout, reused = launcher._acquire_pane(
                spec,
                role_cwds["tester"],
                _role_environment(role_cwds["tester"]),
            )
            self.assertEqual(pane_id, "w9:p1")
            self.assertTrue(reused)
            self.assertEqual(list(layout.role_panes), list(lch.LANE_PANE_ROLES))
            self.assertEqual(
                [layout.role_panes[role] for role in lch.LANE_PANE_ROLES],
                ["w9:p1", "w9:p2", "w9:p3", "w9:p4", "w9:p5"],
            )
            self.assertEqual(len(splits), 4)
            self.assertEqual(renamed, list(lch.LANE_PANE_ROLES))
            reviewer = lch.LaunchSpec(
                correlation_token=lch.role_session_token(
                    "run-1", "lane-a", "integration-reviewer"
                ),
                worktree=role_cwds["integration-reviewer"],
                prompt_path=root / "prompt.json",
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                lane_key="lane-a",
                pane_role="integration-reviewer",
                run_id="run-1",
                workspace_label="product run-1",
                pane_group_size=5,
                role_cwds=role_cwds,
            )
            pane_id, found, reused = launcher._acquire_pane(
                reviewer,
                role_cwds["integration-reviewer"],
                _role_environment(role_cwds["integration-reviewer"]),
            )
            self.assertEqual(found.tab_id, layout.tab_id)
            self.assertEqual(pane_id, "w9:p5")
            self.assertTrue(reused)

    def test_non_tester_first_launch_binds_each_role_checkout_env(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher._workspace_id = "w9"
        calls: list[tuple[str, ...]] = []
        labels: dict[str, str] = {}
        next_pane = {"n": 1}

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            calls.append(args)
            if args[:2] == ("tab", "list"):
                return {"result": {"tabs": []}}
            if args[:2] == ("tab", "create"):
                return {"result": {"tab": {"tab_id": "w9:t1"}}}
            if args[:2] == ("pane", "list"):
                return {"result": {"panes": [{"pane_id": "w9:p1", "tab_id": "w9:t1"}]}}
            if args[:2] == ("pane", "split"):
                next_pane["n"] += 1
                return {
                    "result": {"pane": {"pane_id": "w9:p{}".format(next_pane["n"])}}
                }
            if args[:2] == ("pane", "rename"):
                labels[args[2]] = args[3]
                return {}
            if args[:2] == ("pane", "get"):
                return {
                    "result": {
                        "pane": {
                            "pane_id": args[2],
                            "label": labels.get(args[2], ""),
                        }
                    }
                }
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_cwds = {role: root / role / "checkout" for role in lch.LANE_PANE_ROLES}
            for path in role_cwds.values():
                path.mkdir(parents=True)
            builder_env = lch.role_pane_environment(role_cwds["builder"], {})
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "builder"),
                worktree=role_cwds["builder"],
                prompt_path=root / "prompt.json",
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                environment=builder_env,
                lane_key="lane-a",
                lane_label="lane-a",
                pane_role="builder",
                run_id="run-1",
                workspace_label="product run-1",
                pane_group_size=5,
                role_cwds=role_cwds,
            )
            pane_id, layout, reused = launcher._acquire_pane(
                spec, role_cwds["builder"], builder_env
            )
            self.assertEqual(pane_id, layout.role_panes["builder"])
            self.assertTrue(reused)
            tab_create = next(args for args in calls if args[:2] == ("tab", "create"))
            tester_root = str(role_cwds["tester"].resolve())
            self.assertEqual(tab_create[tab_create.index("--cwd") + 1], tester_root)
            self.assertTrue(
                _env_from_herdr_args(tab_create)["TMPDIR"].startswith(tester_root)
            )
            splits = [args for args in calls if args[:2] == ("pane", "split")]
            self.assertEqual(len(splits), 4)
            for args, role in zip(splits, lch.LANE_PANE_ROLES[1:]):
                expected = str(role_cwds[role].resolve())
                self.assertEqual(args[args.index("--cwd") + 1], expected)
                env = _env_from_herdr_args(args)
                self.assertTrue(env["TMPDIR"].startswith(expected))
                if role != "builder":
                    self.assertNotEqual(env["TMPDIR"], builder_env["TMPDIR"])

    def test_failed_role_label_reaps_new_shell(self) -> None:
        launcher = _bare_launcher("product run-1")
        closed: list[str] = []

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("pane", "rename"):
                raise RuntimeError("rename refused")
            if args[:2] == ("pane", "close"):
                closed.append(args[2])
                return {}
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_cwds = {role: root / role / "checkout" for role in lch.LANE_PANE_ROLES}
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "tester"),
                worktree=role_cwds["tester"],
                prompt_path=root / "prompt.json",
                envelope_path=root / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=root / "session",
                lane_key="lane-a",
                pane_role="tester",
                role_cwds=role_cwds,
            )
            layout = lch._TabLayout(tab_id="w9:t1", panes=["w9:p1"], claimed=0)
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._ensure_lane_role_panes(
                    spec, layout, _role_environment(role_cwds["tester"])
                )
        self.assertEqual(closed, ["w9:p1"])
        self.assertFalse(raised.exception.pane_created)
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)


class NoLegacyAdoptionTest(unittest.TestCase):
    def test_legacy_stage_agent_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "run-1" / "lane-a" / "tester" / "checkout"
            worktree.mkdir(parents=True)
            token = lch.role_session_token("run-1", "lane-a", "tester")
            calls: list[tuple[str, ...]] = []

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                calls.append(args)
                if args[:2] == ("agent", "get"):
                    raise lch.HerdrCallError("missing", lch.AGENT_NOT_FOUND)
                raise AssertionError(args)

            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="tester",
                run_id="run-1",
                stage="WRITING_TESTS",
                input_digest="aabbccddeeff00112233445566778899",
                workspace_label="product run-1",
            )
            launcher = _bare_launcher("product run-1")
            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            self.assertIsNone(launcher._reconnect_live_agent(spec, {}))
            self.assertEqual(calls, [("agent", "get", lch.agent_name_for(token))])
            self.assertFalse(any(call[1] == "rename" for call in calls))
            self.assertFalse(any(call[:2] == ("agent", "start") for call in calls))


class RenameFailClosedTest(unittest.TestCase):
    def test_label_pane_rename_failure_refuses(self) -> None:
        launcher = _bare_launcher("product run-1")

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("pane", "rename"):
                raise lch.HerdrCallError("busy", "pane_busy")
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            spec = lch.LaunchSpec(
                correlation_token=lch.role_session_token("run-1", "lane-a", "tester"),
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="tester",
            )
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._label_pane("w9:p1", spec, {})
        self.assertEqual(raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH)
        self.assertTrue(raised.exception.pane_created)


class RestartRediscoverTest(unittest.TestCase):
    def test_dead_stable_agent_is_absence(self) -> None:
        launcher = _bare_launcher("product run-1")
        token = lch.role_session_token("run-1", "lane-a", "tester")

        def fake_herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("agent", "get"):
                return {"result": {"agent": {"pane_id": "w9:p1", "status": "exited"}}}
            raise AssertionError(args)

        launcher._herdr = fake_herdr  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="tester",
            )
            self.assertIsNone(launcher._reconnect_live_agent(spec, {}))

    def test_stable_missing_pane_label_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            token = lch.role_session_token("run-1", "lane-a", "tester")
            name = lch.agent_name_for(token)
            calls: list[tuple[str, ...]] = []

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                calls.append(args)
                if args[:2] == ("agent", "get"):
                    self.assertEqual(args[2], name)
                    return {"result": {"agent": {"pane_id": "w9:p1", "status": "idle"}}}
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p1",
                                "tab_id": "w9:t1",
                                "cwd": str(worktree),
                            }
                        }
                    }
                raise AssertionError(args)

            spec = lch.LaunchSpec(
                correlation_token=token,
                worktree=worktree,
                prompt_path=worktree / "prompt.json",
                envelope_path=worktree / "envelope.json",
                route="omp",
                model="",
                effort="",
                profile="grok-maestro",
                session_dir=worktree / "session",
                lane_key="lane-a",
                pane_role="tester",
                workspace_label="product run-1",
            )
            launcher = _bare_launcher("product run-1")
            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            with self.assertRaises(lch.LaunchRefused) as raised:
                launcher._reconnect_live_agent(spec, {})
            self.assertEqual(
                raised.exception.refusal, lch.LaunchRefusal.BINDING_MISMATCH
            )
            self.assertFalse(raised.exception.pane_created)
            self.assertFalse(any(call[:2] == ("workspace", "create") for call in calls))
            self.assertFalse(any(call[:2] == ("agent", "start") for call in calls))


class NoTranscriptLaneOfferTest(unittest.TestCase):
    def _omp_spec(
        self, root: Path, worktree: Path, prompt: Path, *, role: str = "tester"
    ) -> lch.LaunchSpec:
        return lch.LaunchSpec(
            correlation_token=lch.role_session_token("run-1", "lane-a", role),
            worktree=worktree,
            prompt_path=prompt,
            envelope_path=root / "envelope.json",
            route="omp",
            model="",
            effort="",
            profile="grok-maestro",
            session_dir=root / "session",
            lane_key="lane-a",
            pane_role=role,
            workspace_label="product run-1",
        )

    def _launch_omp(self, launcher: lch.HerdrLauncher, spec: lch.LaunchSpec, submit):
        layout = lch._TabLayout(tab_id="w9:t1", panes=["w9:p4"], claimed=5)
        with (
            mock.patch.object(lch, "prepare_route_prompt"),
            mock.patch.object(lch, "preflight_launch_prompt"),
            mock.patch.object(lch, "build_omp_argv", return_value=("omp",)),
            mock.patch.object(lch, "pane_env_flags", return_value=()),
            mock.patch.object(launcher, "_existing_role_handle", return_value=None),
            mock.patch.object(launcher, "_reconnect_live_agent", return_value=None),
            mock.patch.object(launcher, "provision"),
            mock.patch.object(
                launcher, "_acquire_pane", return_value=("w9:p4", layout, True)
            ),
            mock.patch.object(launcher, "_label_pane"),
            mock.patch.object(lch, "_wait_for_available_shell"),
            mock.patch.object(
                lch, "_start_agent_when_free", side_effect=lambda start, **kwargs: start()
            ),
            mock.patch.object(lch, "wait_for_interactive_agent"),
            mock.patch.object(lch, "submit_agent_prompt", side_effect=submit),
            mock.patch.object(
                lch,
                "wait_for_agent_transcript",
                side_effect=AssertionError("lane must not wait for transcript"),
            ),
            mock.patch.object(lch, "pane_liveness_pid", return_value=None),
        ):
            return launcher.launch(spec)

    def test_omp_launch_without_transcript_returns_handle_then_envelope(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher.admitted_routes = type(
            "Routes", (), {"admits": lambda self, route: route == "omp"}
        )()
        offers: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "checkout"
            worktree.mkdir()
            prompt = root / "prompt.json"
            prompt.write_text("{}", encoding="utf-8")
            spec = self._omp_spec(root, worktree, prompt)

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p4",
                                "tab_id": "w9:t1",
                                "cwd": str(worktree),
                                "label": "tester",
                            }
                        }
                    }
                if args[:2] in (("agent", "start"), ("agent", "get")):
                    return {
                        "result": {
                            "agent": {
                                "pane_id": "w9:p4",
                                "agent_status": "idle",
                            }
                        }
                    }
                raise AssertionError(args)

            def submit(
                _herdr: object,
                pane_id: str,
                text: str,
                name: str,
                **kwargs: object,
            ) -> None:
                offers.append(
                    {
                        "pane_id": pane_id,
                        "text": text,
                        "name": name,
                        "refuse_unproven": kwargs.get("refuse_unproven"),
                        "working_proves": kwargs.get("working_proves"),
                    }
                )
                recorded = kwargs["submission_recorded"]
                self.assertTrue(callable(recorded))
                self.assertFalse(recorded())

            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            handle = self._launch_omp(launcher, spec, submit)
            self.assertIsNone(handle.transcript_path)
            self.assertEqual(handle.pane_id, "w9:p4")
            self.assertEqual(handle.agent_name, lch.agent_name_for(spec.correlation_token))
            self.assertEqual(handle.correlation_token, spec.correlation_token)
            self.assertEqual(len(offers), 1)
            self.assertEqual(offers[0]["refuse_unproven"], False)
            self.assertEqual(offers[0]["working_proves"], True)
            self.assertEqual(
                offers[0]["text"], "@{0} ".format(prompt.resolve())
            )
            self.assertNotIn(handle.correlation_token, launcher._tailers)
            spec.envelope_path.write_text('{"success": true}', encoding="utf-8")
            result = launcher.poll(handle)
            self.assertEqual(result.state, lch.PollState.EXITED)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.detail, "ENVELOPE_SUCCESS")

    def test_adopted_resubmit_without_transcript_offers_once(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher.admitted_routes = type(
            "Routes", (), {"admits": lambda self, route: route == "omp"}
        )()
        offers: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "checkout"
            worktree.mkdir()
            prompt = root / "prompt.json"
            prompt.write_text("{}", encoding="utf-8")
            spec = self._omp_spec(root, worktree, prompt, role="builder")
            handle = lch.LaunchHandle(
                spec.correlation_token,
                "w9:p4",
                lch.agent_name_for(spec.correlation_token),
                worktree,
                envelope_path=spec.envelope_path,
                workspace_id="w9",
                tab_id="w9:t1",
                lane_key="lane-a",
            )
            launcher._handles[spec.correlation_token] = handle
            launcher._role_handles[("lane-a", "builder")] = handle

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                if args[:2] == ("agent", "get"):
                    return {
                        "result": {
                            "agent": {
                                "name": handle.agent_name,
                                "pane_id": "w9:p4",
                                "agent_status": "idle",
                            }
                        }
                    }
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p4",
                                "tab_id": "w9:t1",
                                "workspace_id": "w9",
                                "cwd": str(worktree),
                                "label": "builder",
                            }
                        }
                    }
                placed = _placement_lists(args)
                if placed is not None:
                    return placed
                raise AssertionError(args)

            def submit(
                _herdr: object,
                pane_id: str,
                text: str,
                name: str,
                **kwargs: object,
            ) -> None:
                del _herdr, text
                offers.append(pane_id)
                self.assertEqual(name, handle.agent_name)
                self.assertFalse(kwargs.get("refuse_unproven"))
                self.assertTrue(kwargs.get("working_proves"))
                self.assertFalse(kwargs["submission_recorded"]())

            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            with (
                mock.patch.object(lch, "prepare_route_prompt"),
                mock.patch.object(lch, "preflight_launch_prompt"),
                mock.patch.object(lch, "build_omp_argv", return_value=("omp",)),
                mock.patch.object(lch, "pane_env_flags", return_value=()),
                mock.patch.object(launcher, "provision"),
                mock.patch.object(lch, "wait_for_interactive_agent"),
                mock.patch.object(lch, "submit_agent_prompt", side_effect=submit),
                mock.patch.object(
                    lch,
                    "wait_for_agent_transcript",
                    side_effect=AssertionError("resubmit must not wait for transcript"),
                ),
            ):
                adopted = launcher.launch(spec)
            self.assertIs(adopted, handle)
            self.assertEqual(adopted.pane_id, "w9:p4")
            self.assertEqual(adopted.agent_name, handle.agent_name)
            self.assertEqual(adopted.correlation_token, spec.correlation_token)
            self.assertIsNone(adopted.transcript_path)
            self.assertEqual(offers, ["w9:p4"])
            self.assertEqual(
                launcher._role_handles[("lane-a", "builder")].pane_id, "w9:p4"
            )

    def test_transcript_appearing_during_proof_attaches_tailer(self) -> None:
        launcher = _bare_launcher("product run-1")
        launcher.admitted_routes = type(
            "Routes", (), {"admits": lambda self, route: route == "omp"}
        )()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "checkout"
            worktree.mkdir()
            prompt = root / "prompt.json"
            prompt.write_text("{}", encoding="utf-8")
            transcript = root / "session.jsonl"
            revealed = {"on": False}
            spec = self._omp_spec(root, worktree, prompt)

            def fake_herdr(*args: str, **kwargs: object) -> dict:
                del kwargs
                if args[:2] == ("pane", "get"):
                    return {
                        "result": {
                            "pane": {
                                "pane_id": "w9:p4",
                                "tab_id": "w9:t1",
                                "cwd": str(worktree),
                                "label": "tester",
                            }
                        }
                    }
                if args[:2] in (("agent", "start"), ("agent", "get")):
                    agent: dict[str, object] = {
                        "pane_id": "w9:p4",
                        "agent_status": "idle",
                    }
                    if revealed["on"]:
                        agent["agent_session"] = {
                            "kind": "path",
                            "value": str(transcript),
                        }
                    return {"result": {"agent": agent}}
                raise AssertionError(args)

            def submit(
                _herdr: object,
                _pane_id: str,
                _text: str,
                _name: str,
                **kwargs: object,
            ) -> None:
                recorded = kwargs["submission_recorded"]
                self.assertFalse(recorded())
                transcript.write_text(
                    "@" + str(prompt.resolve()) + "\n", encoding="utf-8"
                )
                revealed["on"] = True
                self.assertTrue(recorded())

            launcher._herdr = fake_herdr  # type: ignore[method-assign]
            handle = self._launch_omp(launcher, spec, submit)
            self.assertEqual(handle.transcript_path, transcript)
            tailer = launcher._tailers.get(handle.correlation_token)
            self.assertIsNotNone(tailer)
            assert tailer is not None
            self.assertEqual(tailer.path, transcript)

    def test_agent_get_proof_failure_lane_offer_nonterminal(self) -> None:
        def herdr(*args: str, **kwargs: object) -> dict:
            del kwargs
            if args[:2] == ("pane", "get"):
                return {"result": {"pane": {"pane_id": "w9:p1", "revision": 1}}}
            if args[:2] in (("pane", "send-text"), ("pane", "send-keys")):
                return {}
            if args[:2] == ("agent", "send-keys"):
                return {}
            if args[:2] == ("agent", "wait"):
                raise lch.HerdrCallError("wait timeout", code="timeout")
            raise AssertionError(args)

        def recorded() -> bool:
            raise lch.HerdrCallError("lookup failed", code="transport")

        clock = [0.0]

        def mono() -> float:
            clock[0] += 100.0
            return clock[0]

        lch.submit_agent_prompt(
            herdr,
            "w9:p1",
            "@/tmp/prompt ",
            "maestro-x",
            timeout_s=5.1,
            attempts=1,
            sleep=lambda _s: None,
            monotonic=mono,
            refuse_unproven=False,
            working_proves=True,
            submission_recorded=recorded,
        )
        with self.assertRaises(lch.PromptSubmissionUnobservable) as raised:
            lch.submit_agent_prompt(
                herdr,
                "w9:p1",
                "@/tmp/prompt ",
                "maestro-x",
                timeout_s=5.1,
                attempts=1,
                sleep=lambda _s: None,
                monotonic=mono,
                refuse_unproven=True,
                working_proves=True,
                submission_recorded=recorded,
            )
        self.assertIn("AGENT_PROMPT_UNOBSERVED", str(raised.exception))
        self.assertTrue(
            any(item.phase == "proof-probe" for item in raised.exception.failures)
        )
        self.assertTrue(
            any(item.code == "transport" for item in raised.exception.failures)
        )

    def test_missing_transcript_idle_reaches_no_envelope(self) -> None:
        launcher = _bare_launcher("product run-1")
        token = lch.role_session_token("run-1", "lane-a", "tester")
        handle = lch.LaunchHandle(
            token,
            "w9:p1",
            lch.agent_name_for(token),
            Path("/tmp"),
            envelope_path=Path("/tmp/missing-envelope.json"),
        )
        launcher._herdr = (  # type: ignore[method-assign]
            lambda *args, **kwargs: {
                "result": {"pane_id": "w9:p1", "agent_status": "idle"}
            }
        )
        clock = {"now": 1000.0}
        with mock.patch.object(lch.time, "monotonic", side_effect=lambda: clock["now"]):
            first = launcher.poll(handle)
            self.assertEqual(first.state, lch.PollState.RUNNING)
            clock["now"] += 61.0
            second = launcher.poll(handle)
        self.assertEqual(second.state, lch.PollState.EXITED)
        self.assertEqual(second.exit_code, 1)
        self.assertEqual(second.detail, "NO_ENVELOPE")

    def test_route_admission_still_requires_transcript(self) -> None:
        from adw_modules import route_admission as ra

        with mock.patch.object(lch, "wait_for_agent_transcript", return_value=None):
            with self.assertRaises(ra.AdmissionError) as raised:
                ra._prompt_turn(
                    lambda *args, **kwargs: {},
                    {
                        "pane_id": "w9:p1",
                        "name": "admit-omp",
                        "transcript": "",
                    },
                    "Reply with exactly MARK",
                    "1000",
                    "MARK",
                )
        self.assertEqual(
            str(raised.exception),
            "AGENT_PROMPT_UNOBSERVED:admit-omp no transcript",
        )


if __name__ == "__main__":
    unittest.main()
