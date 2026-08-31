"""Hard role-agent worktree boundary."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher
from adw_modules import workspace_isolation as isolation


class WorkspacePathBoundaryTest(unittest.TestCase):
    def test_accepts_checkout_paths_and_refuses_every_escape_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "checkout"
            root.mkdir()
            (root / "inside.txt").write_text("inside\n", encoding="utf-8")
            outside = base / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (root / "escape").symlink_to(outside)

            self.assertEqual(
                isolation.require_path(root, "inside.txt:1-2"),
                (root / "inside.txt").resolve(),
            )
            for raw in (
                str(outside),
                "../outside.txt",
                "escape",
                ".git/HEAD",
                "https://example.test/source",
                "memory://secret",
            ):
                with (
                    self.subTest(raw=raw),
                    self.assertRaises(isolation.IsolationRefused),
                ):
                    isolation.require_path(root, raw)

    def test_glob_refuses_a_wildcard_that_matches_an_external_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "checkout"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
            (root / "linked").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(isolation.IsolationRefused):
                isolation.check_tool_input(root, "glob", {"path": "*"})

    def test_file_tools_are_bounded_and_delegation_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            (root / "inside.py").write_text("x = 1\n", encoding="utf-8")
            isolation.check_tool_input(root, "bash", {"cwd": str(root)})
            isolation.check_tool_input(root, "bash", {"command": "echo hi"})
            isolation.check_tool_input(
                root, "bash", {"command": "echo hi", "cwd": None}
            )
            isolation.check_tool_input(root, "bash", {"command": "echo hi", "cwd": ""})
            isolation.check_tool_input(root, "read", {"path": "inside.py"})
            isolation.check_tool_input(root, "write", {"path": "inside.py"})
            isolation.check_tool_input(
                root, "edit", {"input": "[inside.py#A1B2]\nCUT 1.=1\n"}
            )
            isolation.check_tool_input(root, "glob", {"path": "inside.py"})
            isolation.check_tool_input(root, "grep", {"path": "inside.py"})
            isolation.check_tool_input(root, "lsp", {"file": "inside.py"})
            isolation.check_tool_input(root, "read", {"path": "skill://repo-skill"})
            isolation.check_tool_input(root, "read", {"path": "memory://note"})
            isolation.check_tool_input(
                root, "mcp__codemap_codemap", {"path": "inside.py"}
            )
            isolation.check_tool_input(
                root, "NotebookEdit", {"notebook_path": "inside.py"}
            )
            for tool, payload in (
                ("eval", {"code": "open('/tmp/secret').read()"}),
                ("task", {"prompt": "delegate"}),
                ("hub", {"op": "send"}),
                ("agent", {"prompt": "delegate"}),
                ("read", {"path": "https://example.test/source"}),
                ("read", {"path": "agent://other"}),
                ("mcp__codemap_codemap", {"path": str(root.parent / "outside.py")}),
                ("NotebookEdit", {"notebook_path": str(root.parent / "outside.ipynb")}),
                ("bash", {"cwd": str(root.parent)}),
            ):
                with (
                    self.subTest(tool=tool),
                    self.assertRaises(isolation.IsolationRefused),
                ):
                    isolation.check_tool_input(root, tool, payload)


class RouteIsolationConfigurationTest(unittest.TestCase):
    def _spec(self, root: Path, route: str) -> launcher.LaunchSpec:
        session = root / ".maestro-agent" / "session"
        session.mkdir(parents=True)
        system_prompt = root / ".maestro-agent" / "role-system.md"
        system_prompt.write_text("role contract\n", encoding="utf-8")
        return launcher.LaunchSpec(
            correlation_token="run:lane:role",
            worktree=root,
            prompt_path=root / "prompt.json",
            envelope_path=isolation.result_path(root, 1),
            route=route,
            model="opus",
            effort="high",
            profile="grok" if route == "omp" else None,
            session_dir=session,
            system_prompt_path=system_prompt,
        )

    def test_omp_argv_forces_hook_cwd_without_capability_suppressions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            argv = launcher.build_omp_argv(Path("omp"), self._spec(root, "omp"))
            self.assertIn("--cwd", argv)
            self.assertIn(str(root), argv)
            self.assertIn("--hook", argv)
            self.assertEqual(
                argv[argv.index("--append-system-prompt-file") + 1],
                str((root / ".maestro-agent" / "role-system.md").resolve()),
            )
            for flag in ("--no-extensions", "--no-skills", "--no-rules", "--no-lsp"):
                self.assertNotIn(flag, argv)
            self.assertNotIn("--tools", argv)

    def test_omp_hook_blocks_escape_and_wraps_shell(self) -> None:
        bun = shutil.which("bun")
        if not bun:
            self.skipTest("bun unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "checkout"
            root.mkdir()
            (root / "inside.txt").write_text("inside\n", encoding="utf-8")
            outside = base / "outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            (root / "escape").symlink_to(outside)
            script = """
const loaded = await import(__HOOK_URI__);
let handler;
loaded.default({on: (name, fn) => { if (name === "tool_call") handler = fn; }});
const blocked = await handler({toolName: "read", input: {path: "../secret"}});
const linked = await handler({toolName: "read", input: {path: "escape"}});
const admitted = await handler({toolName: "read", input: {path: "inside.txt"}});
const wrapped = await handler({toolName: "bash", input: {command: "pwd", cwd: "."}});
const omitted = await handler({toolName: "bash", input: {command: "echo hi"}});
const nulled = await handler({toolName: "bash", input: {command: "echo hi", cwd: null}});
console.log(JSON.stringify({blocked, linked, admitted, wrapped, omitted, nulled}));
""".replace("__HOOK_URI__", json.dumps(isolation.omp_hook_path().as_uri()))

            environment = dict(os.environ)
            environment.update(
                {
                    "MAESTRO_ROLE_ROOT": str(root),
                    "MAESTRO_ISOLATION_PYTHON": sys.executable,
                    "MAESTRO_ISOLATION_RUNNER": str(Path(isolation.__file__).resolve()),
                }
            )
            completed = subprocess.run(
                [bun, "-e", script],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["blocked"]["block"])
            self.assertTrue(payload["linked"]["block"])
            self.assertFalse(payload["admitted"].get("block"))
            wrapped = payload["wrapped"]["input"]
            self.assertEqual(wrapped["cwd"], str(root))
            self.assertIn(" run-bash ", wrapped["command"])
            for key in ("omitted", "nulled"):
                admitted = payload[key]["input"]
                self.assertEqual(admitted["cwd"], str(root))
                self.assertIn(" run-bash ", admitted["command"])

            unconfigured_environment = dict(os.environ)
            for key in launcher.ISOLATION_ENV_KEYS:
                unconfigured_environment.pop(key, None)
            unconfigured = subprocess.run(
                [bun, "-e", script],
                cwd=root,
                env=unconfigured_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(unconfigured.returncode, 0, unconfigured.stderr)
            denied = json.loads(unconfigured.stdout)["omitted"]
            self.assertTrue(denied["block"])
            self.assertIn("isolation configuration missing", denied["reason"])

    def test_claude_hook_disables_host_sandbox_and_wraps_bash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            argv = launcher.build_claude_argv(
                Path("claude"), self._spec(root, "claude")
            )
            self.assertIn("--dangerously-skip-permissions", argv)
            self.assertIn("--remote-control", argv)
            self.assertEqual(
                argv[argv.index("--append-system-prompt-file") + 1],
                str((root / ".maestro-agent" / "role-system.md").resolve()),
            )
            self.assertNotIn("--permission-mode", argv)
            self.assertNotIn("--strict-mcp-config", argv)
            self.assertNotIn("--setting-sources", argv)
            self.assertNotIn("--tools", argv)
            denied = argv.index("--disallowedTools") + 1
            self.assertEqual(argv[denied : denied + 2], ("Task", "Agent"))
            self.assertEqual(argv[-1], "--remote-control")
            settings = json.loads(argv[argv.index("--settings") + 1])
            self.assertFalse(settings["sandbox"]["enabled"])
            self.assertEqual(settings["permissions"]["deny"], ["Task", "Agent"])
            self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], "*")

            hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
            self.assertEqual(hook["args"][-1], str(root.resolve()))

            event = json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "pwd", "cwd": "."}}
            )
            with (
                mock.patch.dict(os.environ, {"MAESTRO_ROLE_ROOT": str(root)}),
                mock.patch("sys.stdin", new=__import__("io").StringIO(event)),
                mock.patch(
                    "sys.stdout", new_callable=__import__("io").StringIO
                ) as output,
            ):
                self.assertEqual(isolation._claude_hook(), 0)
            decision = json.loads(output.getvalue())["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "allow")
            self.assertEqual(decision["updatedInput"]["cwd"], str(root))
            self.assertIn(" run-bash ", decision["updatedInput"]["command"])

    def test_claude_hook_uses_explicit_root_without_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tester" / "checkout"
            root.mkdir(parents=True)
            event = json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("sys.stdin", new=__import__("io").StringIO(event)),
                mock.patch(
                    "sys.stdout", new_callable=__import__("io").StringIO
                ) as output,
            ):
                self.assertEqual(isolation._claude_hook(str(root)), 0)
            decision = json.loads(output.getvalue())["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "allow")
            self.assertEqual(decision["updatedInput"]["cwd"], str(root.resolve()))
            self.assertIn(" run-bash ", decision["updatedInput"]["command"])

    def test_claude_hook_recovers_pre_fix_pane_from_claude_project_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tester" / "checkout"
            root.mkdir(parents=True)
            event = json.dumps(
                {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
            )
            with (
                mock.patch.dict(
                    os.environ, {"CLAUDE_PROJECT_DIR": str(root)}, clear=True
                ),
                mock.patch("sys.stdin", new=__import__("io").StringIO(event)),
                mock.patch(
                    "sys.stdout", new_callable=__import__("io").StringIO
                ) as output,
            ):
                self.assertEqual(isolation._claude_hook(), 0)
            decision = json.loads(output.getvalue())["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "allow")
            self.assertEqual(decision["updatedInput"]["cwd"], str(root.resolve()))
            self.assertIn(" run-bash ", decision["updatedInput"]["command"])


class PaneIsolationEnvironmentTest(unittest.TestCase):
    def test_pane_env_flags_require_and_forward_isolation_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            env = isolation.scratch_environment(root)
            with self.assertRaises(launcher.LaunchRefused) as raised:
                launcher.pane_env_flags(env)
            self.assertEqual(
                raised.exception.refusal,
                launcher.LaunchRefusal.SCRATCH_REDIRECT_MISSING,
            )
            self.assertIn("MAESTRO_ROLE_ROOT", raised.exception.detail)
            env["MAESTRO_ROLE_ROOT"] = str(root.resolve())
            env["MAESTRO_ISOLATION_PYTHON"] = sys.executable
            env["MAESTRO_ISOLATION_RUNNER"] = str(Path(isolation.__file__).resolve())
            flags = launcher.pane_env_flags(env)
            forwarded: dict[str, str] = {}
            for index, item in enumerate(flags):
                if item == "--env":
                    key, _, value = flags[index + 1].partition("=")
                    forwarded[key] = value
            self.assertEqual(forwarded["MAESTRO_ROLE_ROOT"], str(root.resolve()))
            self.assertEqual(forwarded["MAESTRO_ISOLATION_PYTHON"], sys.executable)
            sibling = root.parent / "builder" / "checkout"
            sibling.mkdir(parents=True)
            bound = launcher.role_pane_environment(sibling, env)
            self.assertEqual(bound["MAESTRO_ROLE_ROOT"], str(sibling.resolve()))
            self.assertNotEqual(bound["TMPDIR"], env["TMPDIR"])
            self.assertTrue(bound["TMPDIR"].startswith(str(sibling.resolve())))


class ShellSandboxBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        docker = shutil.which("docker")
        if not docker:
            self.skipTest("docker unavailable")
        inspected = subprocess.run(
            [docker, "image", "inspect", isolation._SANDBOX_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if inspected.returncode:
            self.skipTest("role sandbox image unavailable")

    def test_shell_can_write_checkout_but_cannot_read_sibling_or_secret_env(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "checkout"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            encoded_write = base64.b64encode(b"printf 'ok\\n' > inside.txt").decode(
                "ascii"
            )
            self.assertEqual(isolation.run_bash(str(root), str(root), encoded_write), 0)
            self.assertEqual((root / "inside.txt").read_text(encoding="utf-8"), "ok\n")
            encoded_escape = base64.b64encode(
                f"cat {outside} >/dev/null".encode()
            ).decode("ascii")
            self.assertNotEqual(
                isolation.run_bash(str(root), str(root), encoded_escape), 0
            )
            encoded_env = base64.b64encode(
                b'test -z "$MAESTRO_TEST_SECRET_TOKEN"'
            ).decode("ascii")
            with mock.patch.dict(
                os.environ, {"MAESTRO_TEST_SECRET_TOKEN": "must-not-leak"}
            ):
                self.assertEqual(
                    isolation.run_bash(str(root), str(root), encoded_env), 0
                )

    def test_container_hides_git_host_root_and_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            (root / ".git").write_text(
                "gitdir: /host/repository.git\n", encoding="utf-8"
            )
            hidden_git = base64.b64encode(b"test ! -s .git").decode("ascii")
            self.assertEqual(isolation.run_bash(str(root), str(root), hidden_git), 0)
            for command in (
                "touch /host-write",
                "python -c 'import socket; socket.create_connection((\"1.1.1.1\", 53), 1)'",
            ):
                encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
                with self.subTest(command=command):
                    self.assertNotEqual(
                        isolation.run_bash(str(root), str(root), encoded), 0
                    )


if __name__ == "__main__":
    unittest.main()
