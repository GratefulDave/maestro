"""A gate argv keeps its options paired with their values.

The shape these pin replaced split `gate.argv` into `-`-prefixed tokens and
everything else and concatenated the two groups. That detaches an option from
its value, and on FDAdb `lane-wp7-cookie-tests` it handed `--config` a test
file: vite loaded it as a config, evaluated `vitest` outside a worker, and
refused with `Vitest failed to access its internal state`. Four tester turns
were refused identically, because nothing the tester could write was wrong.

These assert on the argv itself. A stubbed `subprocess.run` records the argv
it was handed and replays scripted stdout; it cannot observe that a runner
reads one token as another's value.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from adw_modules import private_review as pr
from adw_modules import tests_chain as tc
from adw_modules.scheduler import _collect_gate

COOKIE_ARGV = (
    "--config",
    "tests/wp7-checkout/vitest.config.ts",
    "tests/wp7-checkout",
    "tests/wp7-checkout/entitlement-cookie.test.ts",
    "tests/wp7-checkout/checkout-success.render.test.ts",
)
COOKIE_FILES = {
    "tests/wp7-checkout/vitest.config.ts": "",
    "tests/wp7-checkout/entitlement-cookie.test.ts": "",
    "tests/wp7-checkout/checkout-success.render.test.ts": "",
}


def _value_after(argv, flag):
    tokens = list(argv)
    return tokens[tokens.index(flag) + 1]


class _Tree:
    """A collection tree holding the repository files a gate may also name."""

    def __init__(self, *present: str) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        for relative in present:
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")

    def close(self) -> None:
        self._tmp.cleanup()


class GateArgvSubstitution(unittest.TestCase):
    def test_option_keeps_its_own_value(self) -> None:
        argv, _ = pr.substituted_gate_argv(COOKIE_ARGV, COOKIE_FILES)
        self.assertEqual(
            _value_after(argv, "--config"), "tests/wp7-checkout/vitest.config.ts"
        )

    def test_option_value_is_never_a_test_file(self) -> None:
        argv, _ = pr.substituted_gate_argv(COOKIE_ARGV, COOKIE_FILES)
        self.assertFalse(_value_after(argv, "--config").endswith(".test.ts"))

    def test_a_planned_selector_the_draft_did_not_write_is_dropped(self) -> None:
        argv, selectors = pr.substituted_gate_argv(COOKIE_ARGV, COOKIE_FILES)
        self.assertNotIn("tests/wp7-checkout", argv)
        self.assertNotIn("tests/wp7-checkout", selectors)

    def test_every_written_file_is_named_exactly_once(self) -> None:
        argv, _ = pr.substituted_gate_argv(COOKIE_ARGV, COOKIE_FILES)
        for path in COOKIE_FILES:
            self.assertEqual(list(argv).count(path), 1, path)

    def test_a_written_file_the_plan_did_not_name_is_appended(self) -> None:
        files = dict(COOKIE_FILES)
        files["tests/wp7-checkout/extra.test.ts"] = ""
        argv, selectors = pr.substituted_gate_argv(COOKIE_ARGV, files)
        self.assertIn("tests/wp7-checkout/extra.test.ts", argv)
        self.assertIn("tests/wp7-checkout/extra.test.ts", selectors)

    def test_an_argv_of_bare_selectors_is_unchanged(self) -> None:
        planned = (
            "services/api-gateway/tests/test_faers_dpa_entitlement.py",
        )
        argv, selectors = pr.substituted_gate_argv(planned, dict.fromkeys(planned, ""))
        self.assertEqual(argv, planned)
        self.assertEqual(selectors, planned)

    def test_an_inline_option_value_is_not_read_as_a_selector(self) -> None:
        argv, selectors = pr.substituted_gate_argv(
            ("--config=vitest.config.ts",) + COOKIE_ARGV[2:], COOKIE_FILES
        )
        self.assertIn("--config=vitest.config.ts", argv)
        self.assertNotIn("--config=vitest.config.ts", selectors)


class CollectGateArgv(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = _Tree()
        self.addCleanup(self.tree.close)

    def test_collect_gate_pairs_config_with_the_config(self) -> None:
        gate = SimpleNamespace(
            runner="vitest", argv=COOKIE_ARGV, cwd=".", min_cases=6
        )
        collect = _collect_gate(gate, COOKIE_FILES, self.tree.path)
        self.assertEqual(
            _value_after(collect.argv, "--config"),
            "tests/wp7-checkout/vitest.config.ts",
        )


class SuiteSelectorsArgv(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = _Tree()
        self.addCleanup(self.tree.close)

    def test_sealed_suite_pairs_config_with_the_config(self) -> None:
        gate = SimpleNamespace(
            runner="vitest", argv=COOKIE_ARGV, cwd=".", min_cases=6
        )
        argv = tc._suite_selectors(gate, tuple(COOKIE_FILES), self.tree.path)
        self.assertEqual(
            _value_after(argv, "--config"), "tests/wp7-checkout/vitest.config.ts"
        )

    def test_pytest_keeps_its_own_flags_and_selectors(self) -> None:
        planned = ("services/api-gateway/tests/test_entitlement_issuance.py",)
        gate = SimpleNamespace(runner="pytest", argv=planned, cwd=".", min_cases=1)
        argv = tc._suite_selectors(gate, planned, self.tree.path)
        self.assertEqual(argv[-1], planned[0])
        self.assertIn("--tb=line", argv)


FIXTURE_ARGV = (
    "src/lib/api/dpa.test.ts",
    "src/lib/api/dpa-fixture-required.test.ts",
)
FIXTURE_WRITTEN = {"src/lib/api/dpa-fixture-required.test.ts": ""}


class AGateOperandAlreadyInTheTree(unittest.TestCase):
    """The shipped file a floor counts survives into the argv that measures it.

    FDAdb `lane-wp8r-fixture-tests` declared min_cases 16: six private cases
    plus the ten already in `src/lib/api/dpa.test.ts`, which the gate names as
    its first operand and no lane writes. Dropping that operand left draft
    collection measuring six against a floor of sixteen, so the run was
    refused `DRAFT_MIN_CASES: collected 6, min_cases 16` one turn after the
    test reviewer had correctly told the tester to stop padding the private
    file with `it.each`. Nothing the tester could write was the thing that was
    wrong (measured 2026-09-05, run a2ea7355).
    """

    def setUp(self) -> None:
        self.tree = _Tree("src/lib/api/dpa.test.ts")
        self.addCleanup(self.tree.close)

    def test_an_unwritten_operand_present_in_the_tree_is_kept(self) -> None:
        argv, selectors = pr.substituted_gate_argv(
            FIXTURE_ARGV, FIXTURE_WRITTEN, self.tree.path
        )
        self.assertIn("src/lib/api/dpa.test.ts", argv)
        self.assertIn("src/lib/api/dpa.test.ts", selectors)

    def test_it_keeps_its_authored_position(self) -> None:
        argv, _ = pr.substituted_gate_argv(
            FIXTURE_ARGV, FIXTURE_WRITTEN, self.tree.path
        )
        self.assertEqual(argv, FIXTURE_ARGV)

    def test_an_unwritten_operand_absent_from_the_tree_is_still_dropped(self) -> None:
        argv, selectors = pr.substituted_gate_argv(
            ("src/lib/api/never-written.test.ts",) + FIXTURE_ARGV[1:],
            FIXTURE_WRITTEN,
            self.tree.path,
        )
        self.assertNotIn("src/lib/api/never-written.test.ts", argv)
        self.assertNotIn("src/lib/api/never-written.test.ts", selectors)

    def test_a_directory_operand_is_dropped_even_when_it_exists(self) -> None:
        argv, _ = pr.substituted_gate_argv(
            ("src/lib/api",) + FIXTURE_ARGV[1:], FIXTURE_WRITTEN, self.tree.path
        )
        self.assertNotIn("src/lib/api", argv)

    def test_a_caller_with_no_tree_cannot_test_existence_and_drops(self) -> None:
        argv, _ = pr.substituted_gate_argv(FIXTURE_ARGV, FIXTURE_WRITTEN)
        self.assertNotIn("src/lib/api/dpa.test.ts", argv)

    def test_collect_gate_carries_the_shipped_operand(self) -> None:
        gate = SimpleNamespace(
            runner="vitest", argv=FIXTURE_ARGV, cwd=".", min_cases=16
        )
        collect = _collect_gate(gate, FIXTURE_WRITTEN, self.tree.path)
        self.assertIn("src/lib/api/dpa.test.ts", collect.argv)

    def test_the_sealed_suite_carries_the_shipped_operand(self) -> None:
        gate = SimpleNamespace(
            runner="vitest", argv=FIXTURE_ARGV, cwd=".", min_cases=16
        )
        argv = tc._suite_selectors(gate, tuple(FIXTURE_WRITTEN), self.tree.path)
        self.assertIn("src/lib/api/dpa.test.ts", argv)


if __name__ == "__main__":
    unittest.main()
