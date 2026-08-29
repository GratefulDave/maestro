"""Tests/build node split: hollow tests refused, genuine tests accepted.

The r7 reviewers found hollow tests in 3 of 5 lanes because one agent wrote
both the implementation and the tests that counted it. This file is the
measured case behind the `tests` node evidence chain (§16.3):

  1. a new case that passes at the parent commit is refused by name
  2. a new case that is red at the parent, then green after the build, is
     accepted
  3. the build node cannot edit the test files the tests node produced

Run with:  uv run adws/adw_test.py -k tests_build_split
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adw_modules import plan_canonical as pc  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import plan_validate as pv  # noqa: E402
from adw_modules import scheduler as sch  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402
from adw_modules import tests_chain as tc  # noqa: E402
from adw_modules import verification as vf  # noqa: E402
from adw_modules import worktree as wt  # noqa: E402

from test_scheduler import SchedulerFixture, green, red  # noqa: E402


HOLLOW = "def test_refund():\n    assert True\n"
GENUINE = (
    "def test_refund():\n"
    "    from refunds import refund\n"
    "    assert refund(600) == 100\n"
)
IMPLEMENTATION = "def refund(amount):\n    return 100\n"


def _gate_result(exit_code, **counts):
    return wt.GateResult(
        label="parent-red",
        scope="node",
        selector="t",
        command=("pytest",),
        exit_code=exit_code,
        green=exit_code == 0,
        counts=counts,
    )


def _ok_permission():
    return wt.PermissionVerdict(passes=True)


class AdjudicateParentRedTests(unittest.TestCase):
    def test_a_new_case_that_passes_at_parent_is_hollow(self):
        verdict = tc.adjudicate_parent_red(
            _gate_result(0, passed=1, failed=0, collected=1), 1
        )
        self.assertFalse(verdict.verified)
        self.assertIn(tc.TestsRefusal.HOLLOW_AT_PARENT.value, verdict.reason)

    def test_every_new_case_failed_is_the_witness(self):
        verdict = tc.adjudicate_parent_red(
            _gate_result(1, passed=0, failed=1, collected=1), 1
        )
        self.assertTrue(verdict.verified)

    def test_a_collection_error_is_not_red(self):
        verdict = tc.adjudicate_parent_red(_gate_result(2), 1)
        self.assertFalse(verdict.verified)
        self.assertIn(tc.TestsRefusal.COLLECTION_FAILED.value, verdict.reason)

    def test_an_import_crash_is_not_red(self):
        verdict = tc.adjudicate_parent_red(
            _gate_result(1, passed=0, failed=0, errored=1, collected=1), 1
        )
        self.assertFalse(verdict.verified)
        self.assertIn(tc.TestsRefusal.IMPORT_CRASH.value, verdict.reason)

    def test_no_new_case_is_refused(self):
        verdict = tc.adjudicate_parent_red(
            _gate_result(5, passed=0, failed=0, collected=0), 0
        )
        self.assertIn(tc.TestsRefusal.NO_NEW_CASES.value, verdict.reason)


class TestsNodePermissionTests(unittest.TestCase):
    def test_a_non_test_path_is_refused(self):
        permission = wt.PermissionVerdict(passes=True)
        verdict = tc.verify_tests_node(True, permission, ("src/refunds.py",), 1)
        self.assertFalse(verdict.verified)
        self.assertIn(tc.TestsRefusal.DIFF_NOT_TESTS_ONLY.value, verdict.reason)


class CollectAndRunTests(unittest.TestCase):
    """Real pytest, real files. The counts the evidence chain consumes."""

    def test_collect_counts_a_new_case_not_a_modified_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_refund.py").write_text(GENUINE)
            ids = tc.collect_nodeids(root, ("tests/test_refund.py",))
            self.assertEqual(1, len(ids))
            self.assertTrue(ids[0].endswith("::test_refund"))
            self.assertEqual(
                ("tests/test_refund.py::test_refund",), tc.new_nodeids((), ids)
            )

    def test_a_hollow_file_passes_at_parent_and_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_refund.py").write_text(HOLLOW)
            ids = tc.collect_nodeids(root, ("tests/test_refund.py",))
            result = tc.run_cases(root, ids)
            verdict = tc.adjudicate_parent_red(result, len(ids))
            self.assertFalse(verdict.verified)
            self.assertIn(tc.TestsRefusal.HOLLOW_AT_PARENT.value, verdict.reason)

    def test_a_genuine_file_is_red_at_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_refund.py").write_text(GENUINE)
            ids = tc.collect_nodeids(root, ("tests/test_refund.py",))
            result = tc.run_cases(root, ids)
            verdict = tc.adjudicate_parent_red(result, len(ids))
            self.assertTrue(verdict.verified, verdict.reason)
            # After the implementation exists, the same cases go green.
            (root / "refunds.py").write_text(IMPLEMENTATION)
            green_run = tc.run_cases(root, ids)
            counts = vf.GateCounts.parse(green_run.counts)
            self.assertIsNotNone(counts)
            self.assertGreaterEqual(counts.passed, 1)
            self.assertEqual(0, counts.failed)

    def test_a_modified_line_is_not_a_new_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                ["git", "init"], cwd=str(root), check=True, capture_output=True
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_refund.py").write_text(HOLLOW)
            subprocess.run(
                ["git", "add", "."], cwd=str(root), check=True, capture_output=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=t@t",
                    "-c",
                    "user.name=t",
                    "commit",
                    "-m",
                    "parent",
                ],
                cwd=str(root),
                check=True,
                capture_output=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            (root / "tests" / "test_refund.py").write_text(
                "def test_refund():\n    assert 1 == 1\n"
            )
            parent = tc.collect_parent_nodeids(root, sha, ("tests/test_refund.py",))
            current = tc.collect_nodeids(root, ("tests/test_refund.py",))
            self.assertEqual((), tc.new_nodeids(parent, current))
            verdict = tc.adjudicate_parent_red(
                tc.run_cases(root, tc.new_nodeids(parent, current)), 0
            )
            self.assertFalse(verdict.verified)
            self.assertIn(tc.TestsRefusal.NO_NEW_CASES.value, verdict.reason)


class SchedulerSplitTests(SchedulerFixture):
    def authored_tests_node(self, node_id="tests", outputs=None):
        return st.PlanNode(
            node_id=node_id,
            kind=st.NodeKind.TESTS,
            depth=0,
            outputs=tuple(outputs or ("tests/test_refund.py",)),
            instruction="Write tests for refund.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )

    def build_node(self, needs=("tests",)):
        return st.PlanNode(
            node_id="build",
            kind=st.NodeKind.AGENT,
            depth=1,
            needs=tuple(needs),
            outputs=("refunds.py",),
            instruction="Implement refund.",
            gate_command=("pytest", "tests/test_refund.py"),
            gate_selector="tests/test_refund.py",
        )

    def test_a_hollow_test_is_refused_and_does_not_merge(self):
        self.written["tests"] = {"tests/test_refund.py": HOLLOW}
        self.schedule([self.authored_tests_node()]).run()
        self.assertNotEqual(self.states()["tests"], st.NodeState.MERGED.value)
        details = self.store.conn.execute(
            "SELECT detail_json FROM transitions WHERE node_id=?", ("tests",)
        ).fetchall()
        blob = " ".join(row[0] or "" for row in details)
        self.assertIn(tc.TestsRefusal.HOLLOW_AT_PARENT.value, blob)

    def test_a_genuine_test_then_a_build_merges(self):
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.written["build"] = {"refunds.py": IMPLEMENTATION}
        self.gate_script[("build", "pre")] = [red()]
        self.gate_script[("build", "post")] = [green()]
        self.gate_script[("build", "falsify")] = [red()]
        self.schedule([self.authored_tests_node(), self.build_node()]).run()
        self.assertEqual(self.states()["tests"], st.NodeState.MERGED.value)
        self.assertEqual(self.states()["build"], st.NodeState.MERGED.value)

    def test_the_build_node_cannot_edit_the_tests_node_files(self):
        self.written["tests"] = {"tests/test_refund.py": GENUINE}
        self.written["build"] = {
            "refunds.py": IMPLEMENTATION,
            "tests/test_refund.py": HOLLOW,
        }
        self.gate_script[("build", "pre")] = [red()]
        self.gate_script[("build", "post")] = [green()]
        self.gate_script[("build", "falsify")] = [red()]
        self.schedule([self.authored_tests_node(), self.build_node()]).run()
        self.assertEqual(self.states()["tests"], st.NodeState.MERGED.value)
        self.assertNotEqual(self.states()["build"], st.NodeState.MERGED.value)


def _pair_mapping(base="0" * 40):
    gate = {
        "runner": "pytest",
        "argv": ["tests/test_refund.py"],
        "cwd": ".",
        "min_cases": 1,
    }
    return {
        "schema_version": pm.SCHEMA_V3,
        "plan_id": "split",
        "repo": "fixture",
        "base_commit": base,
        "intent": "split tests from build",
        "evidence": (),
        "nodes": [
            {
                "kind": "tests",
                "node_id": "tests",
                "instruction": "Write the refund tests.",
                "outputs": ["tests/test_refund.py"],
                "gate": gate,
            },
            {
                "kind": "agent",
                "node_id": "build",
                "needs": ["tests"],
                "instruction": "Implement refund.",
                "outputs": ["refunds.py"],
                "gate": gate,
            },
        ],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {
                "runner": "pytest",
                "argv": ["tests"],
                "cwd": ".",
                "min_cases": 1,
            },
        },
    }


class PlanV3PairTests(unittest.TestCase):
    def test_a_v3_pair_parses_and_projects(self):
        plan = pm.parse_mapping(_pair_mapping())
        self.assertEqual(pm.SCHEMA_V3, plan.schema_version)
        self.assertEqual(1, len(plan.tests_nodes))
        nodes = plan.to_plan_nodes()
        kinds = {n.node_id: n.kind for n in nodes}
        self.assertIs(kinds["tests"], st.NodeKind.TESTS)
        self.assertIs(kinds["build"], st.NodeKind.AGENT)
        build = next(n for n in nodes if n.node_id == "build")
        self.assertEqual(("tests",), build.needs)

    def test_a_v3_plan_with_tests_nodes_still_runs_unscoped_integration_argv(self):
        """Intersection of plan v3 and G1: tests nodes do not re-scope acceptance.

        Branch 5 strips paths and `-k` from the integration gate. Branch 6
        adds tests nodes whose files are the natural thing to name in that
        gate. Neither lane could see the other. A v3 plan that names the
        tests node's files (and a `-k` selector) still executes as
        whole-tree collection, and the lane-union `specs` argument is
        still ignored.
        """
        import maestro
        from adw_modules import runner_resolution as rr

        data = _pair_mapping()
        data["merge_policy"]["integration_gate"]["argv"] = [
            "-q",
            "-k",
            "refund",
            "tests/test_refund.py",
        ]
        plan = pm.parse_mapping(data)
        self.assertEqual(pm.SCHEMA_V3, plan.schema_version)
        self.assertEqual(1, len(plan.tests_nodes))
        self.assertEqual(
            ("-q",),
            pm.unscoped_argv(plan.merge_policy.integration_gate.argv),
        )

        runner = rr.ResolvedRunner(
            runner="pytest",
            executable="/abs/.venv/bin/pytest",
            origin="declared",
            probe_exit=5,
            version="stub",
        )
        captured = {}

        def fake_run(
            worktree_path,
            resolved,
            argv,
            scratch,
            cancel_requested,
            label="integration-gate",
        ):
            captured["argv"] = tuple(argv)
            captured["label"] = label
            return None

        lane_union = ("tests/test_refund.py", "tests/lane_only.py")
        with mock.patch.object(
            maestro.worktree, "run_integration_gate", side_effect=fake_run
        ):
            _, run_ig = maestro._scheduler_gate_deps(plan, {"pytest": runner})
            run_ig(Path("/tmp/integration"), lane_union, lambda: False)

        self.assertEqual(captured["argv"], ("-q",))
        self.assertNotIn("tests/test_refund.py", captured["argv"])
        self.assertNotIn("-k", captured["argv"])
        self.assertNotIn("refund", captured["argv"])
        self.assertNotIn("tests/lane_only.py", captured["argv"])
        self.assertEqual(captured["label"], "integration-gate")

    def test_v2_refuses_a_tests_kind(self):
        data = _pair_mapping()
        data["schema_version"] = pm.SCHEMA_V2
        with self.assertRaises(pm.PlanParseError):
            pm.parse_mapping(data)

    def test_a_tests_node_without_a_builder_is_blocked(self):
        data = _pair_mapping()
        data["nodes"] = [data["nodes"][0]]
        plan = pm.parse_mapping(data)
        blockers = pv._tests_build_paired(plan)
        self.assertTrue(
            any(b.obligation is pv.Obligation.TESTS_BUILD_PAIRED for b in blockers)
        )


class TesterConfigKeyTests(unittest.TestCase):
    def test_the_runtime_config_declares_tester(self):
        import yaml

        raw = yaml.safe_load((ADWS / "maestro.config.yaml").read_text(encoding="utf-8"))
        self.assertIn("tester", raw)
        tester = raw["tester"]
        self.assertIn(tester.get("route"), ("omp", "claude"))
        if tester["route"] == "omp":
            self.assertIsInstance(tester["profile"], str)
            self.assertTrue(tester["profile"].strip())
        else:
            self.assertIsInstance(tester.get("model"), str)
            self.assertTrue(str(tester["model"]).strip())
