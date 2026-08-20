"""No Maestro-launched actor may delegate the work its signature attests to.

§3.6 B12 says no actor signs off on its own output and review is cross-vendor
over the merged surface. §1.2 says no lifecycle transition may be caused by an
agent's claim about its own work. A reviewer that spawns a sub-task, blocks on
it, and relays its conclusion breaks both at once — and breaks them invisibly,
because the transcript, the receipt and the signature all name the actor
Maestro launched, not the model that produced the judgement.

`run-2a44d226e75a4be391a14f02b78a6d25` is the incident these tests exist for.
23 of its 39 launched reviews spawned a named sub-task through omp's
`hub`/`task` tools; 17 of those wrote a signed receipt attesting
`{"model": "openai-codex/gpt-5.6-luna", "route": "omp"}` over a verdict
produced by a `gpt-5.6-terra` sub-task, three of them PASS. The other six were
SIGHUPed still holding `hub op=wait`, wrote no receipt, and were absorbed as
environmental stalls that cancelled and rebuilt the builder.

The tests below assert over the **real** argv builders rather than over a
re-implementation of them, because the defect was never in a policy — there
was no policy — but in what the launcher actually put on the command line.

Run:  uv run adw_test.py -k delegation_capability
"""
from __future__ import annotations

import unittest
from pathlib import Path

from adw_modules import launcher as lch
from adw_modules import permissions
from adw_modules import route_admission


def _spec(route: str, session_dir: Path, prompt: Path) -> lch.LaunchSpec:
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
        argv = lch.build_omp_argv(
            Path("/usr/local/bin/omp"),
            _spec("omp", self.session, self.prompt))
        self.assertIn("--tools", argv)
        self.assertTrue(permissions.argv_denies_delegation("omp", argv))

    def test_the_claude_argv_denies_delegation(self) -> None:
        argv = lch.build_claude_argv(
            Path("/usr/local/bin/claude"),
            _spec("claude", self.session, self.prompt))
        self.assertIn("--disallowedTools", argv)
        self.assertTrue(permissions.argv_denies_delegation("claude", argv))

    def test_the_prompt_positional_stays_last_on_the_omp_argv(self) -> None:
        """omp delivers the prompt as its `MESSAGES` positional.

        The capability flag is inserted mid-argv, and an insertion after the
        `@prompt` would turn the prompt path into a value for `--tools` — the
        launch would start an agent with no prompt, which presents as the
        silent stall §9.6 removed by putting the prompt on the command line in
        the first place.
        """
        argv = lch.build_omp_argv(
            Path("/usr/local/bin/omp"),
            _spec("omp", self.session, self.prompt))
        self.assertTrue(argv[-1].startswith("@"))
        self.assertLess(argv.index("--tools"), len(argv) - 1)

    def test_the_admission_capture_argv_denies_delegation(self) -> None:
        """The second production caller, and the one nobody would think of.

        Route admission builds its own argv through the same builders. An
        admission agent that could delegate would prove a route's continuity
        under a capability set the run then does not use.
        """
        spec = route_admission.RouteCaptureSpec(
            route="omp", cwd=self.root, herdr=Path("/usr/local/bin/herdr"),
            binary=Path("/usr/local/bin/omp"),
            model="openai-codex/gpt-5.6-luna", effort="high",
            profile="openai-performance", session_dir=self.session,
            timeout_s=60.0)
        argv = route_admission._route_argv(
            spec, continuing=False, session_id=None)
        self.assertTrue(permissions.argv_denies_delegation("omp", argv))


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


if __name__ == "__main__":
    unittest.main()
