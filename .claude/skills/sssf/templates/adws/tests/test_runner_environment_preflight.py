"""A runner the plan names is proven usable before any agent is dispatched.

Two halves of one defect, measured 2026-09-03 on FDAdb `lane-wp7-gw-dpa-tests`:

`_collect_private_draft` resolved every runner against the *product repository*
while collecting in a throwaway vault tree. `tests_chain._sealed_suite` has
drawn the distinction since it was written -- vitest resolves against the
runtime root because `prepare_collect_tree` links its `node_modules` in, and
nothing bridges a Python environment, so pytest must resolve against the tree
it will run in. This call site did not. FDAdb has no `.venv`, rank 1 was empty,
resolution fell through to `uv run pytest`, and uv discovered a different
repository's environment.

And the collect tree was the one materialized tree never provisioned, so even
resolving against it would have found nothing.

The cost was paid by the wrong actor: the tester spent nine minutes writing a
correct suite, was refused seven seconds later, and was then handed a
correction turn asking it to fix an environment it does not own.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from adw_modules.scheduler import RunnerPreflightRefused, _collect_resolution_root


class CollectResolutionRoot(unittest.TestCase):
    def test_pytest_resolves_against_the_tree_it_will_run_in(self) -> None:
        gate = SimpleNamespace(runner="pytest", cwd=".")
        self.assertEqual(
            _collect_resolution_root(
                gate, Path("/tree"), Path("/repo"), provisioned=True
            ),
            Path("/tree"),
        )

    def test_vitest_resolves_against_the_runtime_root_it_links_from(self) -> None:
        gate = SimpleNamespace(runner="vitest", cwd=".")
        self.assertEqual(
            _collect_resolution_root(
                gate, Path("/tree"), Path("/repo"), provisioned=True
            ),
            Path("/repo"),
        )

    def test_without_provisioning_there_is_no_tree_environment_to_prefer(self) -> None:
        # A deployment declaring no provision_argv collected fine before this
        # rule existed; preferring an empty tree would refuse every pytest
        # draft in it.
        gate = SimpleNamespace(runner="pytest", cwd=".")
        self.assertEqual(
            _collect_resolution_root(
                gate, Path("/tree"), Path("/repo"), provisioned=False
            ),
            Path("/repo"),
        )

    def test_it_agrees_with_the_sealed_suite_rule(self) -> None:
        from adw_modules import tests_chain as tc

        source = Path(tc.__file__).read_text(encoding="utf-8")
        self.assertIn(
            'resolution_root = Path(tree) if bound.runner == "pytest" else root',
            source,
            "the sealed suite rule moved; _collect_resolution_root must follow it",
        )


class PreflightRefusal(unittest.TestCase):
    def test_the_refusal_is_typed_and_names_the_harness_not_a_lane(self) -> None:
        self.assertEqual(RunnerPreflightRefused.code, "RUNNER_PREFLIGHT_REFUSED")

    def test_it_carries_what_was_unusable(self) -> None:
        exc = RunnerPreflightRefused("pytest in .: no usable pytest was found")
        self.assertIn("pytest", str(exc))
        self.assertIn("no usable pytest", str(exc))


class PreflightRunsBeforeAnyLaneWork(unittest.TestCase):
    def test_run_calls_the_preflight_first(self) -> None:
        from adw_modules import scheduler as sch

        source = Path(sch.__file__).read_text(encoding="utf-8")
        run_body = source.split("    def run(self) -> st.RunStatus:", 1)[1]
        head = run_body.split("while True:", 1)[0]
        self.assertIn("self._assert_runners_usable()", head)
        # It must come before the first thing that can touch a lane.
        self.assertLess(
            head.index("self._assert_runners_usable()"),
            head.index("ensure_run_integration_ref"),
        )


class CollectTreeIsProvisioned(unittest.TestCase):
    def test_the_draft_collect_tree_is_provisioned_before_files_are_written(
        self,
    ) -> None:
        from adw_modules import scheduler as sch

        source = Path(sch.__file__).read_text(encoding="utf-8")
        body = source.split("    def _collect_private_draft(", 1)[1]
        body = body.split("    def _reviewing_tests(", 1)[0]
        self.assertIn("cr.provision_tree(", body)
        self.assertLess(
            body.index("cr.provision_tree("),
            body.index("prv.write_files("),
            "provisioning must precede any private byte reaching the tree",
        )


if __name__ == "__main__":
    unittest.main()
