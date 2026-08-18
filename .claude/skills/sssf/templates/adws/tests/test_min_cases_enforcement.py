"""D11 — a gate is adjudicated at the number its plan declared.

`Plan.to_plan_nodes` copied a gate's runner, argv and selector and dropped its
`min_cases`, so all three adjudication sites read one per-run scalar that no
caller ever set and that was always its default of 1. A plan declaring 70 told
its agent 70 and verified it at 1; run-4ee9e079 reached ACCEPTED that way.

Every test here drives the production path. A test that adjudicated by calling
`verification.adjudicate_gate` with a hand-passed number would stay green with
the projection still dropping the value, which is the defect, not a test of it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maestro
from adw_modules import lifecycle as lc
from adw_modules import plan_model
from adw_modules import scheduler as sch
from adw_modules import scheduler_types as st
from adw_modules import worktree as wt


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, run_id: str):
    repo = root / ("repo-" + run_id)
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "maestro@example.invalid")
    git(repo, "config", "user.name", "Maestro MinCases")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    branch = "integration/{}".format(run_id)
    integration = root / ("integration-" + run_id)
    git(repo, "worktree", "add", "-q", "-b", branch, str(integration), "HEAD")
    return repo, integration, branch


def gate_result(passed: int, *, label: str = "node") -> "wt.GateResult":
    """A gate that ran cleanly and collected exactly `passed` cases."""
    return wt.GateResult(
        label=label, scope=label, selector="k", command=("pytest",),
        exit_code=0 if passed else 1, green=bool(passed),
        counts={"passed": passed, "failed": 0, "skipped": 0, "errored": 0})


DECLARED = 5
INTEGRATION_DECLARED = 70


def agent_node(node_id: str = "a", min_cases: int = DECLARED) -> st.PlanNode:
    return st.PlanNode(node_id=node_id, kind=st.NodeKind.AGENT, depth=0,
                       outputs=(node_id + ".py",), gate_command=("pytest",),
                       gate_selector="k", gate_min_cases=min_cases)


class MinCasesFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = lc.LifecycleStore(self.root / "lifecycle.db")
        self.addCleanup(self.store.close)

    def config(self, **over) -> st.SchedulerConfig:
        base = dict(concurrency=1, node_timeout_s=30, turn_timeout_s=10,
                    final_acceptance_timeout_s=30, backstop_t_s=120,
                    semantic_ceiling=1)
        base.update(over)
        return st.SchedulerConfig(**base)

    def run_plan(self, run_id: str, *, node: st.PlanNode, pre_passed: int,
                 post_passed: int, integration_passed: int = 999,
                 integration_min_cases: int = 1):
        repo, integration, branch = make_repo(self.root, run_id)

        def run_node(attempt, plan_node, record, retry_prompt, on_launch, cancelled):
            on_launch(None)
            return sch.NodeExecution(envelope_parsed=True, exit_code=0)

        def run_gate(attempt, plan_node, phase, cancelled):
            return gate_result(pre_passed if phase == "pre" else post_passed,
                               label=phase)

        deps = sch.SchedulerDeps(
            store=self.store, repo=repo, integration_path=integration,
            integration_branch=branch,
            worktrees_root=self.root / (run_id + "-wt"),
            scratch_root=self.root / (run_id + "-scratch"), run_node=run_node,
            run_gate=run_gate,
            run_integration_gate=lambda *a: gate_result(
                integration_passed, label="integration"),
            quiesce_attempt=lambda record, phase: None,
            kill_attempt=lambda *a: None,
            integration_min_cases=integration_min_cases)
        scheduler = sch.Scheduler(run_id, [node], self.config(), deps,
                                  plan_digest="min-cases-digest")
        self.addCleanup(scheduler.shutdown)
        return scheduler.run()

    def block_reason(self, run_id: str):
        return self.store.get_node(run_id, "a").block_reason


class PreGateCountsAtTheDeclaredNumberTest(MinCasesFixture):
    """Site 1 — §7.4's falsifiability check counts at the node's number."""

    def test_pre_gate_below_the_declared_number_is_not_falsifiable_green(self):
        """N-1 passing cases is RED for a gate declaring N, so the attempt runs.

        Under the defect the threshold was 1, so N-1 passing cases read as a
        green pre-gate and the node blocked GATE_NOT_FALSIFIABLE without the
        agent ever running.
        """
        self.run_plan("run-pre-below", node=agent_node(),
                      pre_passed=DECLARED - 1, post_passed=DECLARED)
        self.assertIsNot(self.block_reason("run-pre-below"),
                         st.BlockReason.GATE_NOT_FALSIFIABLE)

    def test_pre_gate_at_the_declared_number_is_green_and_blocks(self):
        """The control: N passing cases IS green, and §7.4 blocks on it."""
        self.run_plan("run-pre-at", node=agent_node(),
                      pre_passed=DECLARED, post_passed=DECLARED)
        self.assertIs(self.block_reason("run-pre-at"),
                      st.BlockReason.GATE_NOT_FALSIFIABLE)


class PostGateCountsAtTheDeclaredNumberTest(MinCasesFixture):
    """Site 2 — §7.3 clause 3 counts at the node's number."""

    def test_post_gate_below_the_declared_number_fails_verification(self):
        report = self.run_plan("run-post-below", node=agent_node(),
                               pre_passed=0, post_passed=DECLARED - 1)
        self.assertIs(self.block_reason("run-post-below"),
                      st.BlockReason.SEMANTIC_BUDGET_EXHAUSTED)
        self.assertIs(report.outcome, st.RunOutcome.BLOCKED)

    def test_post_gate_at_the_declared_number_verifies(self):
        report = self.run_plan("run-post-at", node=agent_node(),
                               pre_passed=0, post_passed=DECLARED,
                               integration_passed=1, integration_min_cases=1)
        self.assertIsNone(self.block_reason("run-post-at"))
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)


class IntegrationGateCountsAtTheDeclaredNumberTest(MinCasesFixture):
    """Site 3 — §8.8's final acceptance counts at the merge policy's number."""

    def test_integration_below_the_declared_number_is_not_accepted(self):
        report = self.run_plan(
            "run-int-below", node=agent_node(), pre_passed=0,
            post_passed=DECLARED,
            integration_passed=INTEGRATION_DECLARED - 1,
            integration_min_cases=INTEGRATION_DECLARED)
        self.assertIsNot(report.outcome, st.RunOutcome.ACCEPTED)

    def test_integration_at_the_declared_number_is_accepted(self):
        report = self.run_plan(
            "run-int-at", node=agent_node(), pre_passed=0,
            post_passed=DECLARED,
            integration_passed=INTEGRATION_DECLARED,
            integration_min_cases=INTEGRATION_DECLARED)
        self.assertIs(report.outcome, st.RunOutcome.ACCEPTED)


class DeclaredNumberSurvivesProjectionTest(unittest.TestCase):
    """Fact 3 — the value reaches the executable node from the plan artifact.

    `Gate.min_cases` was never lost at ingestion: `plan_model.Gate` carries it
    and `plan_validate` already checks a gate collects at least that many. It
    was dropped one step later, by `Plan.to_plan_nodes`, which is why the
    authoring path looked correct while every run adjudicated at 1.
    """

    def test_projection_carries_every_declared_gate_threshold(self):
        plan = _fixture_plan()
        by_id = plan.node_by_id()
        projected = {n.node_id: n for n in plan.to_plan_nodes()}
        for node_id, authored in by_id.items():
            gate = getattr(authored, "gate", None)
            if gate is None:
                continue
            self.assertEqual(projected[node_id].gate_min_cases, gate.min_cases,
                             "node {0} lost its declared threshold".format(node_id))

    def test_a_code_node_may_not_carry_a_threshold(self):
        with self.assertRaises(ValueError):
            st.PlanNode(node_id="c", kind=st.NodeKind.CODE, depth=0,
                        command=("true",), gate_min_cases=7)

    def test_a_gate_demanding_zero_cases_is_refused(self):
        with self.assertRaises(ValueError):
            st.PlanNode(node_id="a", kind=st.NodeKind.AGENT, depth=0,
                        gate_command=("pytest",), gate_selector="k",
                        gate_min_cases=0)


class ToldAndJudgedAreOneNumberTest(unittest.TestCase):
    """The actual invariant: the agent's number and the adjudicator's agree.

    Everything else here is plumbing. This is the property the defect broke —
    `maestro._agent_node_prompt` read the plan's gate while the adjudicator
    read an unrelated scalar, so the two could differ with nothing detecting
    it. They now derive from one field, and this asserts they still do.
    """

    def test_the_prompt_states_the_number_the_adjudicator_enforces(self):
        plan = _fixture_plan()
        by_id = plan.node_by_id()
        projected = {n.node_id: n for n in plan.to_plan_nodes()}
        checked = 0
        for node_id, authored in by_id.items():
            gate = getattr(authored, "gate", None)
            if gate is None:
                continue
            prompt = maestro._agent_node_prompt(
                authored, Path("/tmp/envelope.json"), None)
            enforced = projected[node_id].gate_min_cases
            self.assertIn(
                "collecting at least {0} case(s)".format(enforced), prompt,
                "node {0} is told a number the adjudicator does not "
                "enforce".format(node_id))
            checked += 1
        self.assertTrue(checked, "no agent node was checked")


def _fixture_plan() -> "plan_model.Plan":
    """The smallest real plan shape carrying a non-default threshold."""
    return plan_model.Plan.model_validate(json.loads(_PLAN_JSON))


_PLAN_JSON = json.dumps({
    "schema_version": "maestro-plan.v1",
    "plan_id": "min-cases-fixture",
    "repo": "fixture",
    "intent": "prove a declared gate threshold reaches the adjudicator",
    "base_commit": "0" * 40,
    "supersedes": None,
    "evidence": [
        {"evidence_id": "src-fixture", "kind": "observed",
         "path": "README.md", "sha256": "0" * 64},
        {"evidence_id": "produced-lane-threshold-0", "kind": "produced",
         "path": "tests/test_threshold.py", "base_sha256": None,
         "producer": "lane-threshold"},
    ],
    "nodes": [{
        "kind": "agent",
        "node_id": "lane-threshold",
        "needs": [],
        "reads": ["src-fixture"],
        "prompt_assets": [],
        "instruction": "add the cases the gate selects",
        "outputs": ["tests/test_threshold.py"],
        "gate": {
            "runner": "pytest",
            "argv": ["tests/test_threshold.py"],
            "cwd": ".",
            "min_cases": DECLARED,
        },
    }],
    "merge_policy": {
        "integration_branch": "main",
        "integration_gate": {
            "runner": "pytest",
            "argv": ["--strict-markers", "tests"],
            "cwd": ".",
            "min_cases": INTEGRATION_DECLARED,
        },
    },
})


if __name__ == "__main__":
    unittest.main()
