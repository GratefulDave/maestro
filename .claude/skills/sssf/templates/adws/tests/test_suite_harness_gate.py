"""A suite is never sealed while it fails inside its own harness.

The draft gate runs the tests lane's draft at the parent commit -- no
implementation -- and reads, per failing case, where the failure was raised.
A failure raised by the draft's own fixtures or helpers before any production
frame ran is red whatever the builder writes, so it is a typed REVISE to the
tester (`SUITE_HARNESS_DEFECT`) before the test reviewer is asked. A failure
raised by production, or a production stub's NotImplementedError, is the
expected red and passes.

A worked example the draft copies from the contract with an input key dropped
or added is refused the same way (`SUITE_EXAMPLE_MISMATCH`).

Every case here drives the real `FactoryScheduler` through WRITING_TESTS and
REVIEWING_TESTS against a real git repository and a real pytest.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adw_modules import git_publication as gitpub
from adw_modules import harness_probe_plugin
from adw_modules import plan_compiler
from adw_modules import runner_resolution as rr
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules.lifecycle import ArtifactStore
from adw_modules.runtime_state import RuntimeStateRoot
from test_draft_min_cases_preflight import (
    LANE_ID,
    PRIVATE,
    RUN_ID,
    DraftActor,
    _git,
    _init_repo,
    _plan_bytes,
)

#: Production at the parent commit: a stub, and a function that fails inside
#: production code. Neither is the tester's to fix.
PRODUCTION = '''\
def solve(inp):
    raise NotImplementedError("solve")


def lookup(inp):
    return inp["not-there-yet"]
'''

PRELUDE = '''\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import prod
'''

EXAMPLE = {"input": {"files": ["a.csv"], "minimum": 2}, "expect": 3}


def _padding(count: int) -> str:
    return "".join(
        "\ndef test_pad_{0}():\n    assert True\n".format(index) for index in range(count)
    )


class SuiteHarnessGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "product"
        state = root / "state"
        state.mkdir(mode=0o700)
        _init_repo(self.repo)
        (self.repo / "prod.py").write_text(PRODUCTION, encoding="utf-8")
        _git(self.repo, "add", "prod.py")
        _git(self.repo, "commit", "-m", "production stub")
        self.runtime = RuntimeStateRoot(state, overlap_paths=(self.repo,))
        self.runtime.ensure_layout()
        self.addCleanup(self.runtime.close)
        self.store = ArtifactStore(self.runtime.ledger_path())
        self.addCleanup(self.store.close)

    def _round(self, body: str) -> DraftActor:
        actor = DraftActor([body])
        document = json.loads(_plan_bytes())
        document["lanes"][0]["acceptance"] = [
            "a.txt is written",
            {
                "criterion": "claim-sum (positive): solve returns the count",
                "gating": False,
                "decided_by": [EXAMPLE],
            },
        ]
        compiled = plan_compiler.compile_plan(
            json.dumps(document, sort_keys=True).encode("utf-8"),
            plan_revision=1,
            plan_artifact_ref="plan:harness-gate",
        )
        target = gitpub.bind_target_worktree(self.repo, "refs/heads/main")
        sch.create_factory_run(
            store=self.store,
            run_id=RUN_ID,
            compiled=compiled,
            runtime=self.runtime,
            target=target,
        )
        scheduler = sch.FactoryScheduler(self.store, RUN_ID, actor, self.runtime, target)
        scheduler._planned(LANE_ID)
        scheduler._writing_tests(LANE_ID)
        scheduler._reviewing_tests(LANE_ID)
        return actor

    def _review(self) -> dict:
        rows = self.store.conn.execute(
            "SELECT payload_json FROM lane_artifacts WHERE run_id=? AND lane_id=? "
            "AND artifact_kind=? ORDER BY sequence",
            (RUN_ID, LANE_ID, st.ArtifactKind.TEST_REVIEW.value),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        return json.loads(rows[0][0])

    def test_harness_key_error_in_a_fixture_is_revised_before_review(self) -> None:
        body = PRELUDE + '''
@pytest.fixture
def example():
    inp = {"minimum": 2}
    return inp["files"]


def test_solves(example):
    assert prod.solve(example) == 3
''' + _padding(8)
        actor = self._round(body)
        self.assertEqual(actor.review_calls, 0)
        self.assertEqual(self.store.lane_stage(RUN_ID, LANE_ID), st.LaneStage.WRITING_TESTS)
        review = self._review()
        self.assertEqual(review["verdict"], "REVISE")
        (finding,) = review["findings"]
        self.assertEqual(finding["violated_requirement"], "SUITE_HARNESS_DEFECT")
        line = body.splitlines().index('    return inp["files"]') + 1
        self.assertIn("{0}:{1}:".format(PRIVATE, line), finding["observed_behavior"])
        self.assertIn("test_solves (setup): KeyError", finding["observed_behavior"])

    def test_production_red_passes_the_gate(self) -> None:
        # NotImplementedError from a stub, a KeyError raised inside production,
        # and an attribute production does not have yet: all the expected red.
        body = PRELUDE + '''
def test_stub():
    assert prod.solve({"files": ["a.csv"], "minimum": 2}) == 3


def test_inside_production():
    assert prod.lookup({}) == 1


def test_not_written_yet():
    assert prod.later() == 1
''' + _padding(6)
        actor = self._round(body)
        self.assertEqual(actor.review_calls, 1)
        self.assertEqual(self._review()["verdict"], "PASS")

    def test_example_input_key_mismatch_is_refused(self) -> None:
        body = PRELUDE + '''
EXAMPLES = [{"input": {"minimum": 2}, "expect": 3}]


@pytest.mark.parametrize("case", EXAMPLES)
def test_example(case):
    assert prod.solve(case["input"]) == case["expect"]
''' + _padding(8)
        actor = self._round(body)
        self.assertEqual(actor.review_calls, 0)
        (finding,) = self._review()["findings"]
        self.assertEqual(finding["violated_requirement"], "SUITE_EXAMPLE_MISMATCH")
        self.assertIn("claim-sum decided_by[0]", finding["observed_behavior"])
        self.assertIn("missing files", finding["observed_behavior"])

    def test_example_copied_exactly_passes(self) -> None:
        body = PRELUDE + '''
EXAMPLES = [{"input": {"files": ["a.csv"], "minimum": 2}, "expect": 3}]


@pytest.mark.parametrize("case", EXAMPLES)
def test_example(case):
    assert prod.solve(case["input"]) == case["expect"]
''' + _padding(8)
        actor = self._round(body)
        self.assertEqual(actor.review_calls, 1)


class HarnessPluginBindingTests(unittest.TestCase):
    def test_the_probe_copies_the_shipped_plugin(self) -> None:
        # The plugin runs in the tree's interpreter, loaded by `-p`; the file
        # copied is this module's own bytes.
        self.assertEqual(
            Path(rr._HARNESS_PLUGIN).resolve(),
            Path(harness_probe_plugin.__file__).resolve(),
        )


if __name__ == "__main__":
    unittest.main()
