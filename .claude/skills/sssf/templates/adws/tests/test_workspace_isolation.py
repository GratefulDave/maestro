"""Host-agent launch has no sandbox dependency; native Write stays on argv."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import launcher


class ZeroSandboxDependencyTest(unittest.TestCase):
    def test_sandbox_sources_are_absent(self) -> None:
        self.assertFalse((ADWS / "adw_modules" / "workspace_isolation.py").exists())
        self.assertFalse(
            (ADWS / "adw_modules" / "workspace_isolation_hook.ts").exists()
        )
        self.assertFalse((ADWS / "tools" / "role_sandbox.Dockerfile").exists())
        source = (ADWS / "adw_modules" / "launcher.py").read_text(encoding="utf-8")
        maestro = (ADWS / "maestro.py").read_text(encoding="utf-8")
        for blob in (source, maestro):
            self.assertNotIn("preflight_sandbox", blob)
            self.assertNotIn("MAESTRO_ISOLATION_", blob)
            self.assertNotIn("role_sandbox", blob)
            self.assertNotIn("workspace_isolation", blob)
        self.assertFalse(hasattr(launcher.LaunchRefusal, "ISOLATION_UNAVAILABLE"))
        self.assertFalse(hasattr(launcher, "ISOLATION_ENV_KEYS"))
        self.assertEqual(launcher.PANE_ENV_KEYS, launcher.SCRATCH_ENV_KEYS)


class HostAgentArgvTest(unittest.TestCase):
    def _spec(self, root: Path, route: str) -> launcher.LaunchSpec:
        session = root / ".maestro-agent" / "session"
        session.mkdir(parents=True)
        system_prompt = root / ".maestro-agent" / "CLAUDE.md"
        if route == "omp":
            system_prompt = root / ".maestro-agent" / "AGENTS.md"
        system_prompt.write_text("role contract\n", encoding="utf-8")
        return launcher.LaunchSpec(
            correlation_token="run:lane:role",
            worktree=root,
            prompt_path=root / "prompt.json",
            envelope_path=launcher.role_result_path(root, 1),
            route=route,
            model="opus",
            effort="high",
            profile="grok" if route == "omp" else None,
            session_dir=session,
            system_prompt_path=system_prompt,
        )

    def test_omp_argv_is_host_profile_without_sandbox_or_tools_hatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            argv = launcher.build_omp_argv(Path("omp"), self._spec(root, "omp"))
            self.assertEqual(argv[0], "omp")
            self.assertEqual(argv[argv.index("--profile") + 1], "grok")
            self.assertEqual(
                argv[argv.index("--append-system-prompt") + 1],
                str((root / ".maestro-agent" / "AGENTS.md").resolve()),
            )
            self.assertNotIn("--append-system-prompt-file", argv)
            for flag in (
                "--cwd",
                "--hook",
                "--tools",
                "--no-extensions",
                "--no-skills",
                "--no-rules",
                "--no-lsp",
                "--settings",
            ):
                self.assertNotIn(flag, argv)
            self.assertNotIn("Write", argv)
            self.assertTrue(any("AGENTS.md" in item for item in argv))

    def test_claude_argv_is_exact_remote_control_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            argv = launcher.build_claude_argv(
                Path("claude"), self._spec(root, "claude")
            )
            self.assertEqual(
                argv,
                (
                    "claude",
                    "--model",
                    "opus",
                    "--effort",
                    "high",
                    "--remote-control",
                ),
            )

    def test_pane_env_forwards_only_scratch_and_native_write_paths_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            env = launcher.scratch_environment(root)
            flags = launcher.pane_env_flags(env)
            forwarded: dict[str, str] = {}
            for index, item in enumerate(flags):
                if item == "--env":
                    key, _, value = flags[index + 1].partition("=")
                    forwarded[key] = value
            self.assertEqual(set(forwarded), set(launcher.SCRATCH_ENV_KEYS))
            self.assertTrue(forwarded["TMPDIR"].startswith(str(root.resolve())))
            sibling = root.parent / "builder" / "checkout"
            sibling.mkdir(parents=True)
            bound = launcher.role_pane_environment(sibling, env)
            self.assertNotEqual(bound["TMPDIR"], env["TMPDIR"])
            self.assertTrue(bound["TMPDIR"].startswith(str(sibling.resolve())))
            self.assertNotIn("MAESTRO_ROLE_ROOT", bound)
            self.assertNotIn("MAESTRO_ISOLATION_PYTHON", bound)
            self.assertNotIn("MAESTRO_ISOLATION_RUNNER", bound)


if __name__ == "__main__":
    unittest.main()
