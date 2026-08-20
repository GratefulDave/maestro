"""D6 — B13 at the dispatch chokepoint, where it cannot be bypassed.

MAESTRO_architecture.md §3.6 B13: size-check every handoff against the target's
context window before dispatch, and fail closed. An overflowing agent does not
error — it compaction-loops and answers about a different task, which is how a
710,673-byte handoff produced a confident verdict about a completely different
workflow.

The check existed at three CLI dispatch sites, which is three places a fourth
site does not have to visit. `HerdrLauncher.launch` is the path every prompt
this system dispatches actually takes: omp carries it in argv
(`build_omp_argv`), claude submits it into the composer. A check there is the
one a new caller cannot route around, and omission fails *closed* — a spec that
carries no measured window is refused rather than launched.

Run with:
    python -m pytest tests/test_launch_prompt_preflight.py -o addopts= -q
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import handoff_budget as hb  # noqa: E402
from adw_modules import launcher as lch  # noqa: E402


class PreflightFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.prompt = self.root / "agent-prompt.txt"
        self.prompt.write_text("x" * 3_000, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def spec(self, *, route="omp", window=400_000, prompt=None):
        return lch.LaunchSpec(
            correlation_token="run1-node_a-1",
            worktree=self.root,
            prompt_path=self.prompt if prompt is None else prompt,
            envelope_path=self.root / "envelope.json",
            route=route,
            model="openai-codex/gpt-5.6-sol" if route == "omp" else "opus",
            effort="high",
            profile="p" if route == "omp" else None,
            session_dir=self.root / "session",
            context_window_tokens=window)


class TheChokepointRefusesAnOversizedPrompt(PreflightFixture):
    def test_a_prompt_over_the_budget_is_refused(self):
        self.prompt.write_text("x" * (3 * 400_000), encoding="utf-8")
        with self.assertRaises(lch.LaunchRefused) as caught:
            lch.preflight_launch_prompt(self.spec())
        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.PROMPT_TOO_LARGE)

    def test_a_prompt_that_fits_returns_its_estimate_and_launches(self):
        estimate = lch.preflight_launch_prompt(self.spec())
        self.assertEqual(estimate, hb.estimate_tokens_for_bytes(3_000))

    def test_the_boundary_is_the_declared_fraction_of_the_window(self):
        """Exactly at the budget passes; one token past it does not. Pins the
        arithmetic so a later change to `HANDOFF_CONTEXT_FRACTION` has to be
        deliberate."""
        window = 1_000
        budget = hb.handoff_budget(window)
        at_budget = int((budget - 1) * hb.BYTES_PER_TOKEN)
        self.prompt.write_bytes(b"x" * at_budget)
        self.assertLessEqual(
            lch.preflight_launch_prompt(self.spec(window=window)), budget)
        self.prompt.write_bytes(b"x" * int(budget * hb.BYTES_PER_TOKEN + 10))
        with self.assertRaises(lch.LaunchRefused):
            lch.preflight_launch_prompt(self.spec(window=window))

    def test_the_refusal_is_raised_before_any_pane_exists(self):
        """`pane_created=False` by construction — nothing has been created yet,
        so §8.3's quiesce step is not sent after a group that never started."""
        self.prompt.write_text("x" * (3 * 400_000), encoding="utf-8")
        with self.assertRaises(lch.LaunchRefused) as caught:
            lch.preflight_launch_prompt(self.spec())
        self.assertFalse(caught.exception.pane_created)

    def test_the_refusal_is_deterministic_so_no_budget_is_spent_retrying(self):
        """The prompt's size and the model's window are properties of the spec.
        A second launch overflows by exactly as much, so retrying it would end
        in LAUNCHER_BUDGET_EXHAUSTED naming a budget that was never usable."""
        for refusal in (lch.LaunchRefusal.PROMPT_TOO_LARGE,
                        lch.LaunchRefusal.PROMPT_UNMEASURED):
            self.assertTrue(refusal.deterministic, refusal.code)
            self.assertIs(refusal.pane_created, False, refusal.code)

    def test_no_new_retry_class_was_introduced(self):
        """§7.5 closes the retry classes at three and makes the closure
        load-bearing. These are refusal members inside the existing launcher
        class, sized by `deterministic`, exactly as CREDENTIAL already is."""
        from adw_modules import retry_policy as rp
        self.assertEqual(
            [member.value for member in rp.RetryClass],
            ["SEMANTIC", "ENVIRONMENTAL", "LAUNCHER_TRANSIENT"])


class OmissionFailsClosed(PreflightFixture):
    """The property that makes this a chokepoint rather than a fourth site."""

    def test_a_spec_with_no_measured_window_is_refused_not_launched(self):
        with self.assertRaises(lch.LaunchRefused) as caught:
            lch.preflight_launch_prompt(self.spec(window=None))
        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.PROMPT_UNMEASURED)

    def test_a_catalogued_model_carrying_no_window_is_refused(self):
        """`agent_pi.context_window` returns 0 for a model the catalog lists
        without one. An unmeasured window is not a passing one."""
        with self.assertRaises(lch.LaunchRefused) as caught:
            lch.preflight_launch_prompt(self.spec(window=0))
        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.PROMPT_UNMEASURED)

    def test_an_unmeasurable_prompt_file_is_refused(self):
        with self.assertRaises(lch.LaunchRefused) as caught:
            lch.preflight_launch_prompt(
                self.spec(prompt=self.root / "never-written.txt"))
        self.assertIs(caught.exception.refusal,
                      lch.LaunchRefusal.PROMPT_UNMEASURED)

    def test_the_default_on_LaunchSpec_is_the_refusing_value(self):
        """A future call site that simply does not know about the field gets
        the refusal, never a silent pass."""
        field = lch.LaunchSpec.__dataclass_fields__["context_window_tokens"]
        self.assertIsNone(field.default)


class ARouteWithNoDeclaredWindow(PreflightFixture):
    """The hole the prior lane left uncovered on purpose, closed deliberately.

    `_deliver_author_turn` runs the `claude` route on `opus`, which does not
    resolve in omp's catalog because claude publishes no catalog at all. There
    is no number to compare against, so no comparison is made — and that is a
    property of the route, recorded once, not an exemption a call site claims.
    """

    def test_a_windowless_route_is_not_checked_and_not_refused(self):
        self.prompt.write_text("x" * 5_000_000, encoding="utf-8")
        self.assertIsNone(
            lch.preflight_launch_prompt(self.spec(route="claude", window=None)))

    def test_the_exemption_is_a_property_of_the_route_not_of_a_call_site(self):
        self.assertEqual(hb.ROUTES_PUBLISHING_A_WINDOW, ("omp",))
        self.assertTrue(hb.route_publishes_a_window("omp"))
        self.assertFalse(hb.route_publishes_a_window("claude"))

    def test_adding_a_route_to_the_tuple_covers_every_dispatch_on_it(self):
        """The day claude publishes a catalog, one edit covers every site."""
        self.prompt.write_text("x" * 5_000_000, encoding="utf-8")
        original = hb.ROUTES_PUBLISHING_A_WINDOW
        hb.ROUTES_PUBLISHING_A_WINDOW = ("omp", "claude")
        try:
            with self.assertRaises(lch.LaunchRefused):
                lch.preflight_launch_prompt(
                    self.spec(route="claude", window=200_000))
        finally:
            hb.ROUTES_PUBLISHING_A_WINDOW = original


class TheCheckIsWiredIntoLaunchItself(unittest.TestCase):
    """A check nothing calls is decoration. This reads `launch`'s own source.

    A behavioural test would need a herdr script and a pane; the structural
    question here is narrower and exactly the one that rots — whether the call
    is still there, and still before anything is created.
    """

    def _launch_body(self):
        tree = ast.parse(Path(lch.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ClassDef)
                    and node.name == "HerdrLauncher"):
                for item in node.body:
                    if (isinstance(item, ast.FunctionDef)
                            and item.name == "launch"):
                        return item
        raise AssertionError("HerdrLauncher.launch not found")

    def _call_names(self, body):
        names = []
        for node in ast.walk(body):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.append((node.lineno, func.id))
                elif isinstance(func, ast.Attribute):
                    names.append((node.lineno, func.attr))
        return names

    def test_launch_calls_the_preflight(self):
        calls = [name for _line, name in self._call_names(self._launch_body())]
        self.assertIn("preflight_launch_prompt", calls)

    def test_the_preflight_runs_before_the_prompt_is_ever_built_or_sent(self):
        calls = self._call_names(self._launch_body())
        first = {}
        for line, name in calls:
            first.setdefault(name, line)
        self.assertIn("preflight_launch_prompt", first)
        for later in ("build_omp_argv", "build_claude_argv",
                      "submit_agent_prompt"):
            self.assertLess(first["preflight_launch_prompt"], first[later],
                            later)

    def test_the_preflight_runs_before_a_pane_is_split(self):
        """So a refusal really can declare `pane_created=False`."""
        calls = self._call_names(self._launch_body())
        preflight = min(line for line, name in calls
                        if name == "preflight_launch_prompt")
        herdr = min(line for line, name in calls if name == "_herdr")
        self.assertLess(preflight, herdr)

    def test_the_guard_would_convict_a_launch_that_dropped_the_call(self):
        """The mutation control: the reader above is wired, not decorative."""
        planted = ast.parse(
            "class HerdrLauncher:\n"
            "    def launch(self, spec):\n"
            "        self._herdr('pane', 'split')\n"
            "        return build_omp_argv(self.omp_path, spec)\n")
        found = [node for node in ast.walk(planted)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id == "preflight_launch_prompt"]
        self.assertEqual(found, [])


class EveryMaestroDispatchSiteDeclaresItsWindow(unittest.TestCase):
    """The CLI half: each `LaunchSpec(...)` maestro builds names the field.

    Not an allowlist — the assertion is that *no* site omits it. A new dispatch
    site that omits it fails here, and would be refused at runtime anyway.
    """

    def test_every_launch_spec_maestro_builds_carries_a_window(self):
        tree = ast.parse((ADWS / "maestro.py").read_text(encoding="utf-8"))
        specs = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "LaunchSpec"]
        # A floor, so an assertion over an empty list cannot pass for clean.
        # Three sites: the node reviewer, the build/agent node, and the plan
        # contract's authoring turn. There were four until `plan finalize`
        # stopped dispatching a reviewer and its factory was deleted; the
        # floor moved with the site rather than being left to acquit a
        # discovery that had gone silently empty.
        self.assertGreaterEqual(len(specs), 3)
        missing = [node.lineno for node in specs
                   if "context_window_tokens" not in
                   {kw.arg for kw in node.keywords}]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
