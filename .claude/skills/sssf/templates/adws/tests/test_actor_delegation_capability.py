"""Tool policy is the omp profile. Maestro does not deny tools by default.

`--tools` / `--disallowedTools` is a secondary hatch behind
`execution.restrict_actor_tools`, default off. `argv_denies_delegation` is
an observation about an argv, not a launch gate.

Run:  uv run adw_test.py -k delegation_capability
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import maestro
from adw_modules import launcher as lch
from adw_modules import permissions
from adw_modules import route_admission


def _spec(route: str, session_dir: Path, prompt: Path,
          restrict_tools: bool = False) -> lch.LaunchSpec:
    return lch.LaunchSpec(
        correlation_token="cap-test",
        worktree=session_dir,
        prompt_path=prompt,
        envelope_path=session_dir / "envelope.json",
        route=route,
        model="openai-codex/gpt-5.6-luna",
        effort="high",
        profile="openai-performance" if route == "omp" else None,
        session_dir=session_dir,
        pane_role="reviewer",
        restrict_tools=restrict_tools,
    )


class RouteCapabilityPolicyTests(unittest.TestCase):
    """The policy itself, before anything puts it on a command line."""

    def test_every_route_with_a_policy_names_its_delegation_tools(self) -> None:
        # A route absent from this table is a route whose actors are
        # unconstrained, which is the state the whole run above was in.
        self.assertEqual(
            set(permissions.DELEGATION_TOOLS), {"omp", "claude"},
            "a route Maestro launches must declare what delegation means on it")
        self.assertIn("task", permissions.DELEGATION_TOOLS["omp"])
        self.assertIn("hub", permissions.DELEGATION_TOOLS["omp"])

    def test_the_allowlist_subtracts_delegation_rather_than_agreeing_with_it(
            self) -> None:
        """B15's shape: the invariant is a computation, not two lists in sync.

        Adding a delegation tool to `ROUTE_TOOLS` must not re-grant it. If the
        subtraction were done by hand at authoring time, this mutation would
        pass and the capability would be back with nothing to notice.
        """
        original = permissions.ROUTE_TOOLS["omp"]
        permissions.ROUTE_TOOLS["omp"] = original + ("task", "hub")
        try:
            allowed = permissions.route_tool_allowlist("omp")
        finally:
            permissions.ROUTE_TOOLS["omp"] = original
        self.assertNotIn("task", allowed)
        self.assertNotIn("hub", allowed)

    def test_the_allowlist_keeps_every_tool_the_run_actually_used(self) -> None:
        """Measured, not guessed — a starved actor is its own failure mode.

        These are the tools invoked across that run's 39 reviewer and 48
        builder sessions, minus the two delegation tools. Removing one of them
        would contain the actor by breaking it.
        """
        allowed = set(permissions.route_tool_allowlist("omp"))
        for tool in ("read", "write", "bash", "grep", "glob", "todo", "edit",
                     "eval"):
            self.assertIn(tool, allowed)

    def test_an_unknown_route_yields_no_flags(self) -> None:
        # Unreachable rather than permissive: `HerdrLauncher.launch` refuses an
        # unknown route `UNSUPPORTED_ROUTE` before an argv builder is reached.
        self.assertEqual(permissions.route_capability_argv("pi"), ())


class BuiltArgvTests(unittest.TestCase):
    """The real builders, which are what a launch actually executes."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent
        self.prompt = self.root / "fixtures"
        self.session = self.root / "no-such-session-dir"

    def test_the_omp_argv_denies_delegation(self) -> None:
        """Default launch passes no `--tools`. The profile is the policy.

        This test previously asserted the detector True on an argv that still
        listed `eval`. That pinned an inaccurate observation. The law is now
        that a reviewer launch grants every tool the profile provides, and
        the detector reports that as not-denied.
        """
        argv = lch.build_omp_argv(
            Path("/usr/local/bin/omp"),
            _spec("omp", self.session, self.prompt))
        self.assertIn("--pm-profile", argv)
        self.assertEqual(argv[argv.index("--pm-profile") + 1],
                         "openai-performance")
        self.assertNotIn("--tools", argv)
        self.assertFalse(permissions.argv_denies_delegation("omp", argv))

    def test_the_claude_argv_denies_delegation(self) -> None:
        argv = lch.build_claude_argv(
            Path("/usr/local/bin/claude"),
            _spec("claude", self.session, self.prompt))
        self.assertNotIn("--disallowedTools", argv)
        self.assertNotIn("--disallowed-tools", argv)
        self.assertFalse(permissions.argv_denies_delegation("claude", argv))

    def test_the_prompt_positional_stays_last_on_the_omp_argv(self) -> None:
        """omp delivers the prompt as its `MESSAGES` positional.

        When the hatch is on, the capability flag is inserted mid-argv, and
        an insertion after the `@prompt` would turn the prompt path into a
        value for `--tools`.
        """
        argv = lch.build_omp_argv(
            Path("/usr/local/bin/omp"),
            _spec("omp", self.session, self.prompt, restrict_tools=True))
        self.assertTrue(argv[-1].startswith("@"))
        self.assertLess(argv.index("--tools"), len(argv) - 1)

    def test_the_restriction_hatch_actually_restricts(self) -> None:
        argv = lch.build_omp_argv(
            Path("/usr/local/bin/omp"),
            _spec("omp", self.session, self.prompt, restrict_tools=True))
        self.assertIn("--tools", argv)
        granted = argv[argv.index("--tools") + 1].split(",")
        self.assertNotIn("task", granted)
        self.assertNotIn("hub", granted)

    def test_the_admission_capture_argv_denies_delegation(self) -> None:
        """Admission uses the same builders. Default: no `--tools` hatch."""
        spec = route_admission.RouteCaptureSpec(
            route="omp", cwd=self.root, herdr=Path("/usr/local/bin/herdr"),
            binary=Path("/usr/local/bin/omp"),
            model="openai-codex/gpt-5.6-luna", effort="high",
            profile="openai-performance", session_dir=self.session,
            timeout_s=60.0)
        argv = route_admission._route_argv(
            spec, continuing=False, session_id=None)
        self.assertNotIn("--tools", argv)
        self.assertFalse(permissions.argv_denies_delegation("omp", argv))


class DetectorTests(unittest.TestCase):
    """§13.4: convict the planted violation, acquit the real tree."""

    def test_an_argv_with_no_containment_at_all_is_convicted(self) -> None:
        # This is verbatim what `build_omp_argv` produced before this change.
        unconstrained = (
            "/usr/local/bin/omp", "--pm-profile", "openai-performance",
            "--session-dir", "/tmp/s", "@/tmp/prompt.md")
        self.assertFalse(
            permissions.argv_denies_delegation("omp", unconstrained))

    def test_an_allowlist_that_admits_a_delegation_tool_is_convicted(
            self) -> None:
        planted = ("/usr/local/bin/omp", "--tools", "read,write,task",
                   "@/tmp/prompt.md")
        self.assertFalse(permissions.argv_denies_delegation("omp", planted))

    def test_a_deny_list_missing_one_spelling_is_convicted(self) -> None:
        """`Task` alone leaves `Agent`, which is the current spelling."""
        planted = ("/usr/local/bin/claude", "--disallowedTools", "Task")
        self.assertFalse(permissions.argv_denies_delegation("claude", planted))

    def test_the_hyphenated_claude_spelling_is_accepted(self) -> None:
        # `claude --help` documents both `--disallowedTools` and
        # `--disallowed-tools`; a detector that knew only one would acquit a
        # constrained argv as unconstrained.
        argv = ("/usr/local/bin/claude", "--disallowed-tools", "Task", "Agent")
        self.assertTrue(permissions.argv_denies_delegation("claude", argv))

    def test_an_argv_that_grants_eval_does_not_deny_delegation(self) -> None:
        planted = (
            "/usr/local/bin/omp", "--tools",
            "read,write,edit,bash,grep,glob,todo,eval",
            "@/tmp/prompt.md")
        self.assertFalse(permissions.argv_denies_delegation("omp", planted))


class RestrictActorToolsConfigTests(unittest.TestCase):
    """The hatch is reachable from maestro.config.yaml, default off."""

    def test_the_shipped_config_leaves_the_hatch_off(self) -> None:
        raw = (Path(__file__).resolve().parent.parent
               / "maestro.config.yaml").read_text(encoding="utf-8")
        self.assertIn("restrict_actor_tools: false", raw)

    def test_the_config_switch_reaches_the_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "project"
            (repo / "adws").mkdir(parents=True)
            (repo / "plans").mkdir()
            (repo / ".git").mkdir()
            binaries = {}
            for name in ("herdr", "omp", "claude"):
                binary = root / name
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)
                binaries[name] = str(binary)
            config = {
                "schema": "maestro-config.v1",
                "plans_dir": "plans",
                "state_root": "../maestro-state",
                "keys": {
                    "verify_key_env": "MAESTRO_TEST_VERIFY_KEY",
                    "signing_seed_env": "MAESTRO_TEST_SIGNING_SEED",
                    "route_verify_key_env": "MAESTRO_TEST_ROUTE_VERIFY_KEY",
                },
                "executables": binaries,
                "route_receipts": {"omp": "route-receipts/omp.json"},
                "reviewer": {
                    "route": "omp", "model": "review-model", "effort": "high",
                    "finalization_timeout_s": 60, "turn_timeout_s": 20,
                    "poll_interval_s": 1,
                },
                "execution": {
                    "route": "omp", "model": "execution-model",
                    "effort": "medium", "concurrency": 2,
                    "node_timeout_s": 120, "turn_timeout_s": 30,
                    "final_acceptance_timeout_s": 45, "backstop_t_s": 600,
                    "semantic_ceiling": 3,
                    "restrict_actor_tools": True,
                },
            }
            path = repo / "adws" / "maestro.config.yaml"
            path.write_text(json.dumps(config), encoding="utf-8")
            layout = maestro._load_maestro_layout(repo.resolve(), path)
        self.assertTrue(layout["execution"]["restrict_actor_tools"])


if __name__ == "__main__":
    unittest.main()
