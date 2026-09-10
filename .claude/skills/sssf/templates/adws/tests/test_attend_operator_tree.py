"""What the operator agent is handed, and where it is handed it.

The operator agent's tree is the one place the sealed suite is written outside
the vault and outside code review's scratch. Two things have to hold: the
bytes land inside the tree the role contract confines the agent to, and the
handoff is files rather than prompt text. B13's size check is made against the
route's window at launch, and four rounds of findings plus a suite is a prompt
that can overflow -- an overflowing agent answers about a different lane.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADWS))

import maestro  # noqa: E402
from adw_modules import attend as att  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


def _request(tmp: Path, sealed: dict) -> att.OperatorRequest:
    ir = tmp / "current.ir.json"
    ir.write_text(json.dumps({"plan_id": "p-1"}), encoding="utf-8")
    return att.OperatorRequest(
        run_id="run-1",
        lane_id="lane-faq-build",
        stage=st.LaneStage.WAITING_FOR_USER.value,
        round_number=4,
        plan_revision=2,
        next_plan_revision=3,
        public_contract={"acceptance_criteria": ["emits FAQ records"]},
        reviews=({"kind": "CODE_REVIEW", "verdict": "REVISE"},),
        redacted_failures=("1 failed",),
        lane_gates="stage WAITING_FOR_USER",
        ir_path=str(ir),
        revision_out_path=str(tmp / "cwd" / "revisions" / "r3.ir.json"),
        sealed_files=sealed,
        amendment_rules="rules",
        allowed_lane_ids=("lane-faq-build", "lane-faq-tests"),
    )


class OperatorTree(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.cwd = self.tmp / "cwd"
        self.cwd.mkdir()
        self.actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)

    def _write(self, sealed: dict) -> None:
        self.actor._write_operator_tree(self.cwd, _request(self.tmp, sealed))

    def test_the_inputs_are_files_and_the_suite_is_on_disk(self) -> None:
        self._write({"tests/faq.spec.ts": "expect(record.disclaimer)"})
        inputs = self.cwd / "inputs"
        self.assertEqual(
            json.loads((inputs / "current.ir.json").read_text())["plan_id"], "p-1"
        )
        self.assertEqual((inputs / "lane_gates.txt").read_text(), "stage WAITING_FOR_USER")
        self.assertEqual((inputs / "amendment_rules.md").read_text(), "rules")
        reviews = json.loads((inputs / "reviews.json").read_text())
        self.assertEqual(reviews["reviews"][0]["verdict"], "REVISE")
        self.assertEqual(reviews["redacted_failures"], ["1 failed"])
        self.assertEqual(
            (self.cwd / "sealed" / "tests" / "faq.spec.ts").read_text(),
            "expect(record.disclaimer)",
        )
        self.assertTrue((self.cwd / "revisions").is_dir())

    def test_a_second_dispatch_replaces_the_previous_round(self) -> None:
        self._write({"tests/old.spec.ts": "stale"})
        self._write({"tests/new.spec.ts": "current"})
        self.assertFalse((self.cwd / "sealed" / "tests" / "old.spec.ts").exists())
        self.assertTrue((self.cwd / "sealed" / "tests" / "new.spec.ts").exists())

    def test_a_traversing_sealed_path_is_refused_rather_than_written(self) -> None:
        outside = self.tmp / "escaped.ts"
        with self.assertRaises(maestro.FactoryRefused) as caught:
            self._write({"../../escaped.ts": "leaked"})
        self.assertIn("OPERATOR_TREE_PATH_ESCAPE", str(caught.exception))
        self.assertFalse(outside.exists())


class ResolvedUnder(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def test_a_nested_relative_path_resolves_inside(self) -> None:
        self.assertEqual(
            maestro._resolved_under(self.root, "a/b/c.ts"),
            (self.root / "a" / "b" / "c.ts").resolve(),
        )

    def test_a_parent_component_is_refused(self) -> None:
        with self.assertRaises(maestro.FactoryRefused):
            maestro._resolved_under(self.root, "../out.ts")

    def test_an_absolute_path_is_refused(self) -> None:
        with self.assertRaises(maestro.FactoryRefused):
            maestro._resolved_under(self.root, "/etc/passwd")


class OperatorPrompt(unittest.TestCase):
    def test_the_envelope_schema_names_every_rationale_key(self) -> None:
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        schema = actor._schema("operator")
        self.assertEqual(sorted(schema["rationale"]), sorted(att.RATIONALE_KEYS))
        self.assertIn("revision_path", schema)

    def test_the_role_contract_states_the_privilege_and_its_limit(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        actor = maestro.HerdrStageActor.__new__(maestro.HerdrStageActor)
        actor.target = type("T", (), {"target_repository_root": holder.name})()
        written = actor._materialize_role_instructions(
            Path(holder.name), "operator", "claude"
        )
        text = written.read_text()
        self.assertIn("You may read the sealed acceptance suite", text)
        self.assertIn("allowed_lane_ids", text)
        self.assertIn("Do not run any `maestro` verb", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
