"""#118 — a green gate does not refuse code nothing references.

`Gate.min_cases` is a floor with no ceiling. `lane-p5-gap-policy` shipped 20,
then 26, then 34 collected cases across three ACCEPTED attempts, and merged a
document-locator persistence layer — `_LOCATOR_NS`, `locator_row_id()` — that
no production path and no test ever called. Its reviewer named the surplus and
graded the finding non-blocking, so nothing refused it.

Two halves are tested here, and they are separable on purpose. The first is the
counted fact itself, over sources handed in as bytes, with no subprocess and no
repository: what the analyser calls unreachable, and — the half that decides
whether anyone can leave this switched on — what it must not. The second is the
refusal end to end, through a real scheduler over a real git repository, because
an adjudicator nothing dispatches is the dead seam this repository has shipped
before.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

from adw_modules import reachability as rc          # noqa: E402
from adw_modules import scheduler_types as st       # noqa: E402
from adw_modules import verification as vf          # noqa: E402

from test_scheduler import SchedulerFixture         # noqa: E402


def names(symbols) -> list:
    return [symbol.name for symbol in symbols]


# ── the counted fact ────────────────────────────────────────────────────────

class UnreferencedSymbolTests(unittest.TestCase):
    """What the analyser refuses, over sources given as bytes."""

    def test_a_produced_symbol_nothing_references_is_reported(self):
        produced = {"gap_policy.py": (
            "_LOCATOR_NS = 'cmo'\n"
            "\n"
            "def apply_gap_policy(rows):\n"
            "    return rows\n")}
        surface = dict(produced, **{"caller.py": (
            "from gap_policy import apply_gap_policy\n"
            "\n"
            "def run(rows):\n"
            "    return apply_gap_policy(rows)\n")})
        found = rc.unreferenced(produced, surface)
        self.assertEqual(names(found), ["_LOCATOR_NS"])
        self.assertEqual(found[0].path, "gap_policy.py")
        self.assertEqual(found[0].line, 1)

    def test_every_unreferenced_symbol_is_reported_not_the_first(self):
        """The issue's third acceptance criterion. One-per-attempt spends the
        semantic budget on a list the prompt could have carried at once."""
        produced = {"gap_policy.py": (
            "_LOCATOR_NS = 'cmo'\n"
            "\n"
            "def locator_row_id(doc):\n"
            "    return doc\n"
            "\n"
            "class CmoDocumentLocator:\n"
            "    pass\n"
            "\n"
            "def apply_gap_policy(rows):\n"
            "    return rows\n")}
        surface = dict(produced, **{
            "test_gap_policy.py": "from gap_policy import apply_gap_policy\n"
                                  "def test_applies():\n"
                                  "    assert apply_gap_policy([]) == []\n"})
        # Reported in location order, so a reader repairs the file top to
        # bottom rather than hunting the list.
        self.assertEqual(names(rc.unreferenced(produced, surface)),
                         ["_LOCATOR_NS", "locator_row_id", "CmoDocumentLocator"])

    def test_a_fully_referenced_module_passes_unchanged(self):
        """The issue's second acceptance criterion, production side."""
        produced = {"policy.py": (
            "LIMIT = 3\n"
            "\n"
            "def apply(rows):\n"
            "    return rows[:LIMIT]\n")}
        surface = dict(produced, **{"caller.py": (
            "import policy\n"
            "\n"
            "def run(rows):\n"
            "    return policy.apply(rows)\n")})
        self.assertEqual(rc.unreferenced(produced, surface), ())

    def test_a_reference_from_a_test_witnesses_the_symbol(self):
        """§7.3's evidence chain accepts either witness, so this check must."""
        produced = {"policy.py": "def apply(rows):\n    return rows\n"}
        surface = dict(produced, **{"tests/test_policy.py": (
            "from policy import apply\n"
            "\n"
            "def test_apply():\n"
            "    assert apply([]) == []\n")})
        self.assertEqual(rc.unreferenced(produced, surface), ())

    def test_an_attribute_reference_counts(self):
        """Resolved by AST: `mod.name` reaches the symbol without binding it."""
        produced = {"policy.py": "def apply(rows):\n    return rows\n"}
        surface = dict(produced, **{
            "caller.py": "import policy as p\n\ndef run(r):\n    return p.apply(r)\n"})
        self.assertEqual(rc.unreferenced(produced, surface), ())

    def test_a_mention_in_a_comment_or_string_does_not_count(self):
        """The issue forbids resolving by text match, and this is why."""
        produced = {"policy.py": "def apply(rows):\n    return rows\n"}
        surface = dict(produced, **{
            "caller.py": "# apply is called elsewhere\nNOTE = 'apply(rows)'\n"})
        self.assertEqual(names(rc.unreferenced(produced, surface)), ["apply"])

    def test_recursion_is_not_a_use(self):
        produced = {"policy.py": (
            "def walk(node):\n"
            "    return walk(node.parent) if node.parent else node\n")}
        self.assertEqual(names(rc.unreferenced(produced, dict(produced))), ["walk"])

    def test_a_symbol_used_only_by_a_sibling_in_the_same_file_is_referenced(self):
        produced = {"policy.py": (
            "LIMIT = 3\n"
            "\n"
            "def apply(rows):\n"
            "    return rows[:LIMIT]\n")}
        surface = dict(produced, **{"caller.py": "import policy\npolicy.apply([])\n"})
        self.assertEqual(rc.unreferenced(produced, surface), ())

    # ── the exemptions, each asserted so removing one is a red test ─────────

    def test_a_decorated_definition_is_reached_through_its_decorator(self):
        """E2 — a registry, a fixture, and a CLI verb are all named nowhere
        else by construction."""
        produced = {"routes.py": (
            "import registry\n"
            "\n"
            "@registry.route('/x')\n"
            "def handle(request):\n"
            "    return request\n")}
        self.assertEqual(rc.unreferenced(produced, dict(produced)), ())

    def test_what_the_runner_collects_is_exempt(self):
        """E3 — `min_cases` already counts these; nothing in source names them."""
        produced = {"tests/test_policy.py": (
            "import unittest\n"
            "\n"
            "def test_applies():\n"
            "    assert True\n"
            "\n"
            "class PolicyTest(unittest.TestCase):\n"
            "    def test_more(self):\n"
            "        assert True\n")}
        self.assertEqual(rc.unreferenced(produced, dict(produced)), ())

    def test_a_bare_helper_in_a_test_file_is_not_exempt(self):
        """E3 is scoped to what the runner collects. A test file is not an
        amnesty for machinery the tests never call."""
        produced = {"tests/test_policy.py": (
            "def _unused_helper():\n"
            "    return 1\n"
            "\n"
            "def test_applies():\n"
            "    assert True\n")}
        self.assertEqual(names(rc.unreferenced(produced, dict(produced))),
                         ["_unused_helper"])

    def test_dunder_module_metadata_is_exempt(self):
        produced = {"pkg.py": "__all__ = ['x']\n__version__ = '1.0'\n"}
        self.assertEqual(rc.unreferenced(produced, dict(produced)), ())

    def test_a_symbol_that_predates_the_attempt_is_not_this_nodes_bloat(self):
        """E4 — the false refusal this check must not manufacture: a node that
        touched one line of a module carrying legacy surplus."""
        base = {"policy.py": "def legacy():\n    return 1\n"}
        produced = {"policy.py": (
            "def legacy():\n"
            "    return 1\n"
            "\n"
            "def added(rows):\n"
            "    return rows\n")}
        surface = dict(produced, **{"caller.py": "import policy\npolicy.added([])\n"})
        self.assertEqual(rc.unreferenced(produced, surface, base), ())

    def test_a_symbol_this_attempt_added_to_an_existing_file_is_adjudicated(self):
        base = {"policy.py": "def legacy():\n    return 1\n"}
        produced = {"policy.py": (
            "def legacy():\n"
            "    return 1\n"
            "\n"
            "_SCRATCH = {}\n")}
        self.assertEqual(names(rc.unreferenced(produced, dict(produced), base)),
                         ["_SCRATCH"])

    def test_methods_are_not_adjudicated(self):
        """Module-level only: an override and a protocol implementation are
        defined-and-never-named by construction."""
        produced = {"policy.py": (
            "class Policy:\n"
            "    def apply(self, rows):\n"
            "        return rows\n")}
        surface = dict(produced, **{"caller.py": "from policy import Policy\nPolicy()\n"})
        self.assertEqual(rc.unreferenced(produced, surface), ())

    def test_a_non_python_output_is_not_adjudicated(self):
        produced = {"NOTES.md": "# locator_row_id\n"}
        self.assertEqual(rc.unreferenced(produced, dict(produced)), ())

    def test_source_that_does_not_parse_is_not_adjudicated(self):
        produced = {"broken.py": "def (:\n"}
        self.assertEqual(rc.unreferenced(produced, dict(produced)), ())


# ── the adjudicator ─────────────────────────────────────────────────────────

class AdjudicateReachabilityTests(unittest.TestCase):

    def symbol(self, name: str) -> rc.ProducedSymbol:
        return rc.ProducedSymbol(path="gap_policy.py", name=name, line=7)

    def test_no_unreachable_symbol_verifies(self):
        self.assertTrue(vf.adjudicate_reachability((), st.NodeKind.AGENT).verified)

    def test_an_agent_node_is_refused_semantically_with_the_symbols_named(self):
        verdict = vf.adjudicate_reachability(
            (self.symbol("locator_row_id"), self.symbol("_LOCATOR_NS")),
            st.NodeKind.AGENT)
        self.assertFalse(verdict.verified)
        self.assertIs(verdict.retry_class, st.RetryClass.SEMANTIC)
        self.assertIsNone(verdict.block_reason)
        self.assertEqual(verdict.unreferenced_symbols,
                         ("gap_policy.py:7:locator_row_id",
                          "gap_policy.py:7:_LOCATOR_NS"))

    def test_a_code_node_is_refused_terminally(self):
        """A deterministic command re-run against an unchanged base emits the
        same unreachable symbols, so retry cannot be the repair (§7.5)."""
        verdict = vf.adjudicate_reachability(
            (self.symbol("locator_row_id"),), st.NodeKind.CODE)
        self.assertIs(verdict.block_reason,
                      st.BlockReason.PRODUCED_SYMBOL_UNREFERENCED)
        self.assertIsNone(verdict.retry_class)
        self.assertIn(st.BlockReason.PRODUCED_SYMBOL_UNREFERENCED, st.NON_RETRYABLE)


# ── the refusal, end to end ─────────────────────────────────────────────────

class ReachabilityThroughScheduler(SchedulerFixture):
    """The same shape through a real scheduler over a real repository."""

    def test_an_unreferenced_produced_symbol_refuses_the_attempt(self):
        """The issue's first acceptance criterion. The node exhausts its
        semantic budget rather than merging, and the ledger names the symbol."""
        self.written["build"] = {"build.py": (
            "_LOCATOR_NS = 'cmo'\n"
            "\n"
            "def apply_gap_policy(rows):\n"
            "    return rows\n"),
            "use_build.py": "import build\nbuild.apply_gap_policy([])\n"}
        report = self.schedule(
            [self.agent("build", outputs=("*.py",))]).run()

        node = self.store.get_node("run1", "build")
        self.assertIs(node.block_reason, st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

        rows = [row for row in self.store.audit_transitions("run1", "build")
                if row.get("to_state") == st.NodeState.BLOCKED.value]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].get("detail", {}).get("unreferenced_symbols"),
                         ["build.py:1:_LOCATOR_NS"])

    def test_the_retry_prompt_names_the_symbol_to_remove(self):
        """A SEMANTIC retry whose prompt does not name the work is the same
        request repeated, which §7.5 says is not a retry at all."""
        self.written["build"] = {"build.py": (
            "_LOCATOR_NS = 'cmo'\n"
            "\n"
            "def apply_gap_policy(rows):\n"
            "    return rows\n"),
            "use_build.py": "import build\nbuild.apply_gap_policy([])\n"}
        self.schedule([self.agent("build", outputs=("*.py",))]).run()

        later = [p for p in self.prompts["build"][1:] if p]
        self.assertTrue(later, "a semantic retry must carry guidance")
        self.assertIn("build.py:1:_LOCATOR_NS", later[0])

    def test_a_node_whose_symbols_are_all_referenced_merges(self):
        """The issue's second acceptance criterion, end to end."""
        self.written["build"] = {"build.py": (
            "def apply_gap_policy(rows):\n"
            "    return rows\n"),
            "tests/test_build.py": (
                "from build import apply_gap_policy\n"
                "\n"
                "def test_applies():\n"
                "    assert apply_gap_policy([]) == []\n")}
        report = self.schedule(
            [self.agent("build", outputs=("*.py", "tests/*.py"))]).run()
        self.assertEqual(self.states(), {"build": "MERGED"})
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)


if __name__ == "__main__":            # pragma: no cover
    unittest.main()
