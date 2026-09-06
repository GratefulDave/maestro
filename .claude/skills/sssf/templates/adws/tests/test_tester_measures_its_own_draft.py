"""A tester must run the gate's own collect against its draft before submitting.

Measured, FDAdb run `a2ea7355699c4dff93bc82ac89415475`, `lane-wp8r-route-tests`,
2026-09-05. The tester submitted a draft whose module deadlocks vitest at load;
collection enumerated nothing for the full 120s budget, twice, and ended the run.
Its transcript shows what it did instead of measuring: a grep-based self-check
reporting `it count 8 / describe 1`, and one bash call that returned no output in
0.12 seconds. Running the real listing by hand answers in 7.6s, so the omission
cost nothing to avoid -- the contract simply never asked for it.

These pin the obligation and the named anti-measurement in the materialized role
contract, for BOTH tester rules: a tests-lane draft and a hidden-validator draft
go through the same preflight (`scheduler._collect_private_draft`), so a rule
that lands on one lane kind only leaves the other blind.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import maestro
from adw_modules import launcher as lch
from adw_modules import scheduler_types as st


def _contract(lane_kind: str | None) -> str:
    actor = object.__new__(maestro.HerdrStageActor)
    with TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        path = maestro.HerdrStageActor._materialize_role_instructions(
            actor, cwd, "tester", "omp", lane_kind
        )
        text = path.read_text(encoding="utf-8")
        agents = lch.role_agent_dir(cwd) / "AGENTS.md"
        claude = lch.role_agent_dir(cwd) / "CLAUDE.md"
        assert agents.read_bytes() == claude.read_bytes()
    return text


class TesterMustEnumerateItsOwnDraft(unittest.TestCase):
    LANE_KINDS = (st.LANE_KIND_TESTS, None)

    def test_the_contract_requires_a_listing_before_the_envelope(self) -> None:
        for kind in self.LANE_KINDS:
            with self.subTest(lane_kind=kind):
                text = _contract(kind)
                self.assertIn("Before you return the envelope", text)
                self.assertIn("enumerate your own draft", text)

    def test_both_collect_invocations_are_spelled_out(self) -> None:
        # The tester is not handed the gate's argv, so a contract that says
        # "run the gate's collect" without saying what that is asks it to
        # guess. These are `runner_resolution.COLLECT_ARGS` verbatim.
        for kind in self.LANE_KINDS:
            with self.subTest(lane_kind=kind):
                text = _contract(kind)
                self.assertIn("list --run", text)
                self.assertIn("--collect-only -q -o addopts=", text)

    def test_a_static_scan_is_named_and_refused(self) -> None:
        for kind in self.LANE_KINDS:
            with self.subTest(lane_kind=kind):
                text = _contract(kind)
                self.assertIn("`it(`", text)
                self.assertIn("grep", text)
                self.assertIn("does not discharge it", text)

    def test_the_three_reasons_a_static_scan_is_a_different_question(self) -> None:
        for kind in self.LANE_KINDS:
            with self.subTest(lane_kind=kind):
                text = _contract(kind)
                self.assertIn("it.each", text)
                self.assertIn("never invoked registers nothing", text)
                self.assertIn("deadlocks at import greps exactly like", text)

    def test_a_listing_that_will_not_exit_is_not_read_as_a_failure(self) -> None:
        # FDAdb's own gate configs hold vitest open after a complete listing.
        # A contract that did not say so would train the tester to treat its
        # healthy draft as broken and rewrite it.
        for kind in self.LANE_KINDS:
            with self.subTest(lane_kind=kind):
                text = _contract(kind)
                self.assertIn("does not exit is fine", text)
                self.assertIn("Nothing printed at all", text)

    def test_the_lane_kind_rules_still_differ_where_they_should(self) -> None:
        tests_lane = _contract(st.LANE_KIND_TESTS)
        hidden = _contract(None)
        self.assertIn("Author files exactly at declared_outputs", tests_lane)
        self.assertNotIn("Author files exactly at declared_outputs", hidden)
        self.assertIn("hidden validator/meta-test files", hidden)
        self.assertNotIn("hidden validator/meta-test files", tests_lane)


if __name__ == "__main__":
    unittest.main()
