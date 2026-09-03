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

import unittest
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
    def test_collect_gate_pairs_config_with_the_config(self) -> None:
        gate = SimpleNamespace(
            runner="vitest", argv=COOKIE_ARGV, cwd=".", min_cases=6
        )
        collect = _collect_gate(gate, COOKIE_FILES)
        self.assertEqual(
            _value_after(collect.argv, "--config"),
            "tests/wp7-checkout/vitest.config.ts",
        )


class SuiteSelectorsArgv(unittest.TestCase):
    def test_sealed_suite_pairs_config_with_the_config(self) -> None:
        gate = SimpleNamespace(
            runner="vitest", argv=COOKIE_ARGV, cwd=".", min_cases=6
        )
        argv = tc._suite_selectors(gate, tuple(COOKIE_FILES))
        self.assertEqual(
            _value_after(argv, "--config"), "tests/wp7-checkout/vitest.config.ts"
        )

    def test_pytest_keeps_its_own_flags_and_selectors(self) -> None:
        planned = ("services/api-gateway/tests/test_entitlement_issuance.py",)
        gate = SimpleNamespace(runner="pytest", argv=planned, cwd=".", min_cases=1)
        argv = tc._suite_selectors(gate, planned)
        self.assertEqual(argv[-1], planned[0])
        self.assertIn("--tb=line", argv)


if __name__ == "__main__":
    unittest.main()
