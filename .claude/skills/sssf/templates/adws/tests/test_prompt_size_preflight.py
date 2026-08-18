"""B13 applied to every assembled prompt, and the bound on what one may carry.

Two failures on the same path, one downstream of the other:

* A permission failure's offending paths were rendered into the retry prompt as
  one `", ".join(...)` line. With a delta measured over a whole dependency tree
  that is a 1.1MB line, and `_fit`'s only move against a single oversized line
  is to drop it — so the prompt was simultaneously unbounded in the assembled
  string and empty of the one fact the agent needed. The bound is now a
  declared total plus a head sample plus an explicit elided count: the count is
  a structural fact and must survive whatever the sample elides.

* Nothing size-checked an assembled prompt against the route it was dispatched
  to, except the code reviewer. B13's rule is that an overflowing agent does
  not error — it compaction-loops and answers about something else — so the
  check must refuse before dispatch rather than truncate silently.

Run with:
    python -m pytest tests/test_prompt_size_preflight.py -o addopts= -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import agent_pi  # noqa: E402
from adw_modules import code_review as cr  # noqa: E402
from adw_modules import retry_policy as rp  # noqa: E402


class _Node:
    outputs = ("src/declared.py",)


def _paths(count: int) -> tuple:
    return tuple(f".venv/lib/python3.12/site-packages/pkg{i}/module{i}.py"
                 for i in range(count))


class BoundedOffendingPathRendering(unittest.TestCase):
    """The retry prompt names paths, bounded, without ever losing the count."""

    def _rendered(self, count: int) -> str:
        guidance = rp.VerificationGuidance(
            reason="the measured delta failed §8.3's permission check",
            offending_paths=_paths(count), failed_clause=4)
        rendered = rp.render_guidance(
            _Node(), rp.GuidanceLedger(verification=guidance))
        self.assertIsNotNone(rendered)
        return rendered

    def test_the_total_survives_the_elision(self):
        rendered = self._rendered(16090)
        self.assertIn("(16090 in total)", rendered)
        self.assertIn(f"and {16090 - rp.OFFENDING_PATH_SAMPLE} more", rendered)

    def test_the_sample_is_bounded_and_the_prompt_stays_inside_the_budget(self):
        rendered = self._rendered(16090)
        named = [line for line in rendered.splitlines()
                 if line.startswith("  .venv/")]
        self.assertEqual(len(named), rp.OFFENDING_PATH_SAMPLE)
        self.assertLessEqual(len(rendered), rp.GUIDANCE_CHAR_BUDGET)

    def test_the_agent_is_still_told_which_paths_it_wrote(self):
        """The regression the bound repairs: before it, `_fit` dropped the one
        oversized line whole and the prompt named no path at all."""
        rendered = self._rendered(16090)
        self.assertIn(".venv/lib/python3.12/site-packages/pkg0/module0.py", rendered)
        self.assertIn("Declared outputs: src/declared.py", rendered)

    def test_a_short_list_is_rendered_in_full_with_no_elision_marker(self):
        rendered = self._rendered(3)
        self.assertIn("(3 in total)", rendered)
        self.assertNotIn("more, elided here", rendered)
        for index in range(3):
            self.assertIn(f"pkg{index}/module{index}.py", rendered)

    def test_no_offending_paths_renders_no_paths_section(self):
        guidance = rp.VerificationGuidance(reason="clause 3", failed_clause=3)
        rendered = rp.render_guidance(
            _Node(), rp.GuidanceLedger(verification=guidance))
        self.assertNotIn("Paths written outside", rendered)


class _StubCatalog:
    """omp's merged catalog, stubbed, so no test shells out to a real binary."""

    ROWS = (("openrouter", "x-ai/grok-4.6", 500_000),
            ("openai-codex", "gpt-5.6-luna", 1_000_000),
            ("localvendor", "windowless-1", 0))

    def __enter__(self):
        self._real = agent_pi.catalog
        agent_pi.catalog = lambda: self.ROWS
        return self

    def __exit__(self, *exc):
        agent_pi.catalog = self._real
        return False


class PromptPreflight(unittest.TestCase):
    """`maestro._preflight_prompt` — the check, and what "fail closed" means.

    It lives at the CLI rather than in `code_review` because reading a route's
    catalog imports `agent_pi`, which `enforcement.py`'s `base-execution-import`
    check forbids any `adw_modules` policy module to do."""

    def test_an_oversized_prompt_is_refused_and_never_truncated(self):
        with _StubCatalog():
            with self.assertRaises(cr.HandoffTooLarge):
                maestro._preflight_prompt("x" * (3 * 500_000), "omp", "x-ai/grok-4.6")

    def test_a_prompt_that_fits_returns_its_estimate(self):
        with _StubCatalog():
            estimate = maestro._preflight_prompt("x" * 3_000, "omp", "x-ai/grok-4.6")
        self.assertEqual(estimate, cr.estimate_tokens("x" * 3_000))

    def test_a_model_the_catalog_does_not_carry_is_refused(self):
        """An unmeasured window on a measurable route is not a passing one."""
        with _StubCatalog():
            with self.assertRaises(cr.HandoffTooLarge):
                maestro._preflight_prompt("small", "omp", "no-such-model")

    def test_a_model_the_catalog_carries_with_no_window_is_refused(self):
        with _StubCatalog():
            with self.assertRaises(cr.HandoffTooLarge):
                maestro._preflight_prompt("small", "omp", "windowless-1")

    def test_a_pattern_is_resolved_through_the_catalog_not_split_on_a_slash(self):
        """`x-ai/grok-4.6` is a pattern whose provider is `openrouter`. Reading
        its halves as provider and id yields window 0, which a fail-closed check
        turns into a refusal of every launch of a correctly configured lane."""
        with _StubCatalog():
            self.assertEqual(agent_pi.context_window("x-ai", "grok-4.6"), 0)
            self.assertIsNotNone(
                maestro._preflight_prompt("small", "omp", "x-ai/grok-4.6"))

    def test_a_route_publishing_no_window_is_not_checked(self):
        """Refusing on a route with no catalog would be a route that no longer
        launches, not a size check. Stated as behaviour so it cannot drift into
        being assumed."""
        with _StubCatalog():
            self.assertIsNone(maestro._preflight_prompt("x" * 5_000_000, "claude", "opus"))


if __name__ == "__main__":
    unittest.main()
