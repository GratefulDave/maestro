"""A redraft of an accepted suite changes only what a finding or a spec change names.

`suite_increment` is measured directly (reopen and amendment inputs, per-unit
AST hashes) and through the scheduler's real reopen path with a fake tester:
the first draft is a full draft, a reopen redraft that touches an uncited test
is refused before the test reviewer is asked, and a cited fix is accepted.
"""

from __future__ import annotations

import json
import unittest

from adw_modules import plan_compiler
from adw_modules import scheduler_types as st
from adw_modules import suite_increment as inc
from tests.test_suite_reopen import (
    ReopenActor,
    SUITE_PATH,
    SuiteReopenTests,
    _ASSERTS,
)

P = "tests/test_contract.py"

BASE = (
    "import os\n"
    "def helper():\n"
    "    return 1\n"
    "def test_alpha():\n"
    "    assert helper() == 1\n"
    "def test_beta():\n"
    "    assert os.sep == '/'\n"
)


def _out(draft, texts=(), **kw):
    return inc.out_of_scope({P: BASE}, {P: draft}, finding_texts=list(texts), **kw)


class ReopenScope(unittest.TestCase):
    def test_identical_redraft_is_in_scope(self) -> None:
        self.assertEqual(_out(BASE), [])

    def test_formatting_and_comments_do_not_count(self) -> None:
        self.assertEqual(_out("# note\n" + BASE.replace("== 1", "==   1")), [])

    def test_uncited_change_is_refused(self) -> None:
        found = _out(BASE.replace("== '/'", "!= '\\\\'"), [P + ":4"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["violated_requirement"], inc.OUT_OF_SCOPE)
        self.assertIn("test_beta", found[0]["observed_behavior"])

    def test_cited_change_by_line_is_accepted(self) -> None:
        self.assertEqual(_out(BASE.replace("helper() == 1", "helper() >= 1"), [P + ":5"]), [])

    def test_cited_change_by_node_id_is_accepted(self) -> None:
        draft = BASE.replace("== '/'", "!= '\\\\'")
        self.assertEqual(_out(draft, [P + "::test_beta fails"]), [])
        self.assertEqual(len(_out(draft, [P + "::test_alpha fails"])), 1)

    def test_removed_test_needs_a_citation(self) -> None:
        draft = BASE.split("def test_beta")[0]
        self.assertEqual(len(_out(draft, [P + ":5"])), 1)
        self.assertEqual(_out(draft, [P + ":7"]), [])

    def test_changed_helper_is_a_unit_of_its_own(self) -> None:
        draft = BASE.replace("return 1", "return 2")
        self.assertEqual(len(_out(draft, [P + ":5"])), 1)
        self.assertEqual(_out(draft, [P + ":3"]), [])

    def test_additions_are_allowed(self) -> None:
        self.assertEqual(_out(BASE + "def test_gamma():\n    assert True\n", [P + ":5"]), [])


OLD = {"acceptance_criteria": ["alpha returns one", "beta uses separator"], "interface": []}
NEW = {
    "acceptance_criteria": ["alpha returns one", "beta uses separator", "gamma rounds upward"],
    "interface": [],
}


class AmendmentScope(unittest.TestCase):
    def test_unchanged_claim_test_changed_is_refused(self) -> None:
        found = _out(
            BASE.replace("helper() == 1", "helper() >= 1") + "def test_gamma():\n    assert True\n",
            old_contract=OLD, new_contract=NEW, amendment=True,
        )
        self.assertEqual(len(found), 1)
        self.assertIn("test_alpha", found[0]["observed_behavior"])

    def test_added_test_for_a_new_example_is_accepted(self) -> None:
        self.assertEqual(
            _out(BASE + "def test_gamma_rounds_upward():\n    assert True\n",
                 old_contract=OLD, new_contract=NEW, amendment=True),
            [],
        )

    def test_test_bound_to_a_changed_claim_may_change(self) -> None:
        old = {"acceptance_criteria": ["alpha returns one", "beta uses separator"]}
        new = {"acceptance_criteria": ["alpha returns two", "beta uses separator"]}
        found = _out(
            BASE.replace("helper() == 1", "helper() == 2").replace("return 1", "return 2"),
            old_contract=old, new_contract=new, amendment=True,
        )
        # test_alpha mentions "alpha", a word only the changed criterion uses;
        # the helper is not a test and nothing cites it.
        self.assertEqual(len(found), 1)
        self.assertIn("helper", found[0]["observed_behavior"])

    def test_addition_without_a_spec_change_is_refused(self) -> None:
        same = {"acceptance_criteria": ["alpha returns one"]}
        found = _out(BASE + "def test_new():\n    assert True\n",
                     old_contract=same, new_contract=same, amendment=True)
        self.assertEqual(len(found), 1)

    def test_spec_diff_names_added_removed_changed(self) -> None:
        diff = inc.spec_diff(OLD, NEW)
        self.assertEqual(diff["added"], ["criterion:gamma rounds upward"])
        self.assertEqual((diff["removed"], diff["changed"]), ([], []))


class _Tester(ReopenActor):
    """A tester that returns one scripted suite per turn."""

    def __init__(self, *args, suites, **kw) -> None:
        super().__init__(*args, **kw)
        self.suites = list(suites)
        self.baselines: list = []
        self.reviewed: list[str] = []

    def write_tests(self, ctx):
        self.calls.append("write_tests")
        self.write_tests_lanes.append(ctx.lane.lane_id)
        self.tests_turns.append({"review": None, "contract": ""})
        self.baselines.append(ctx.redraft_baseline)
        body = self.suites.pop(0) if len(self.suites) > 1 else self.suites[0]
        return {"files": {path: body for path in ctx.lane.declared_outputs}}


SUITE_V1 = (
    "from pathlib import Path\n"
    "def test_product_is_ready():\n"
    "    assert Path('product.py').is_file()\n"
    "    assert Path('product.py').read_text().strip().endswith('ready')\n"
    "def test_other():\n"
    "    assert 1 + 1 == 2\n"
)
# Line 4 is inside test_product_is_ready, which the reviewer cites below.
SUITE_TOUCHES_OTHER = SUITE_V1.replace("1 + 1 == 2", "2 + 2 == 4").replace(
    "is_file()", "exists()"
)
SUITE_FIXES_CITED = SUITE_V1.replace("is_file()", "exists()")


class ReopenThroughTheScheduler(SuiteReopenTests):
    def _run(self, suites, name):
        actor = _Tester(
            self.repo, self.runtime.path / "worktrees",
            reviews=["suite", "pass"], suites=suites,
        )
        scheduler = self._start(self._compiled(), actor, name)
        return actor, scheduler.run()

    def test_uncited_edit_is_refused_then_cited_fix_is_accepted(self) -> None:
        actor, status = self._run(
            [SUITE_V1, SUITE_TOUCHES_OTHER, SUITE_FIXES_CITED], "run-incr"
        )
        self.assertEqual(status, st.RunStatus.COMPLETE)
        # A full draft first; the redraft starts from the accepted suite.
        self.assertIsNone(actor.baselines[0])
        self.assertEqual(sorted(actor.baselines[1]["suite_files"]), [SUITE_PATH])
        self.assertFalse(actor.baselines[1]["amendment"])
        # The refused redraft never reached the test reviewer.
        self.assertEqual(
            actor.calls,
            [
                "write_tests", "review_tests", "build", "review_code",
                "write_tests", "write_tests", "review_tests", "build", "review_code",
            ],
        )
        refusal = [
            json.loads(row[0])
            for row in self.store.conn.execute(
                "SELECT payload_json FROM lane_artifacts WHERE run_id=? AND lane_id=? "
                "AND artifact_kind=? ORDER BY sequence",
                ("run-incr", "lane-tests", st.ArtifactKind.TEST_REVIEW.value),
            )
        ]
        codes = [
            f["violated_requirement"]
            for review in refusal for f in review.get("findings") or ()
        ]
        self.assertEqual(codes.count(inc.OUT_OF_SCOPE), 1)
        # Its own text does not excuse the unit on the next round.
        self.assertEqual(actor.baselines[2]["finding_texts"].count(inc.OUT_OF_SCOPE), 0)
