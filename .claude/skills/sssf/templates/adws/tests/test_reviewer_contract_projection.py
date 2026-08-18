"""Executable proof of §3.6 B9 — the reviewer is handed the node's contract.

B9 says "the reviewer's input is a declared contract: goal, `produces`,
acceptance". Until this file existed it was none of those things. The chain,
end to end:

  `Plan.to_plan_nodes` projected node_id, needs, outputs and the gate, and
  dropped `instruction`. `st.PlanNode` had no field to receive it. So
  `code_review.build_handoff` read it through `getattr(node, "instruction",
  "")`, which could only ever answer `""`, and fell through to a goal derived
  from the node's own gate: *"Make the gate 'pytest …' pass over selector
  '…', changing only the declared outputs."* Every agent node, every run.

The cost is on the record. Node `lane-p1-canonical-object-key` of
`cmo-consolidation-l` was asked for "a pure derivation and policy module with
injected clients… no production migration, object mutation, or backfill
execution is authorized". Its builder wrote an executing S3 materializer. Its
reviewer — shown only "make your gate pass" — could not see that the code was
not the code the plan asked for, so it reviewed what it was given on its own
terms and reported real S3 concurrency hazards in a module that should not
have existed. Three attempts, three correct-but-irrelevant verdicts, retry
budget gone. §3.6 A9's unbounded loop, reached through B9's open door.

What is settled here:

  B9    an agent node's declared instruction reaches the rendered prompt
  B9    `produces` (declared outputs) reaches it
  B9    acceptance reaches it *including* §10.2's `min_cases` threshold —
        the number that separates "a test passes" from "seven tests pass"
  B15   a plan field with no projection is a raise, not a default: totality
        is checked against the model, so the next dropped field fails on the
        first projection instead of in production three modules away
  §6.2  a code node's goal is its command; an agent node's blank instruction
        is a refusal, because the plan model makes a blank one impossible

Run with:  uv run adws/adw_test.py -k reviewer_contract_projection
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ADWS = Path(__file__).resolve().parents[1]
if str(ADWS) not in sys.path:
    sys.path.insert(0, str(ADWS))

from adw_modules import code_review as cr  # noqa: E402
from adw_modules import finalization as fin  # noqa: E402
from adw_modules import plan_model as pm  # noqa: E402
from adw_modules import scheduler_types as st  # noqa: E402


#: The declared contract of the real node the incident happened on, taken
#: verbatim from the approved `cmo-consolidation-l` plan at digest
#: 898e6eab2cc9b97b1422c4808d7302595b9260795cc666f07cd8119833192f32. Held
#: here as a fixture rather than read from that repository so the proof
#: survives in a checkout that has only this one.
REAL_NODE_ID = "lane-p1-canonical-object-key"
REAL_INSTRUCTION = (
    "Add the canonical CMO object key derivation and the source-locator "
    "record that preserves the original bucket, key, version, ETag, size, "
    "MIME type, retrieval timestamp and URL")
REAL_OUTPUTS = [
    "src/lexgenius_pipeline/ingestion/judicial/cmo/canonical_object.py",
    "tests/unit/ingestion/test_cmo_canonical_object.py",
]
REAL_GATE_ARGV = ["tests/unit/ingestion/test_cmo_canonical_object.py"]
REAL_MIN_CASES = 7

#: The exact goal the reviewer was handed instead, before this fix.
DERIVED_GOAL_PREFIX = "Make the gate "


def plan_mapping() -> dict:
    """A plan carrying the real node's declared contract, and a code node."""
    return {
        "schema_version": "maestro-plan.v1",
        "plan_id": "cmo-consolidation-l",
        "repo": "example/pipeline",
        "base_commit": "0" * 40,
        "intent": "consolidate the CMO corpus",
        "evidence": [
            {"kind": "observed", "evidence_id": "src-pdf-fingerprint",
             "path": "README.md", "sha256": "a" * 64},
        ],
        "nodes": [
            {"kind": "agent", "node_id": REAL_NODE_ID, "needs": [],
             "reads": ["src-pdf-fingerprint"], "outputs": list(REAL_OUTPUTS),
             "instruction": REAL_INSTRUCTION,
             "gate": {"runner": "pytest", "cwd": ".",
                      "argv": list(REAL_GATE_ARGV),
                      "min_cases": REAL_MIN_CASES},
             "prompt_assets": []},
            {"kind": "code", "node_id": "n-suite", "needs": [REAL_NODE_ID],
             "reads": [], "outputs": [],
             "command": ["pytest", "-q"], "cwd": ".", "expects_changes": False},
        ],
        "merge_policy": {
            "integration_branch": "main",
            "integration_gate": {"runner": "pytest", "cwd": ".",
                                 "argv": ["tests"], "min_cases": 1},
        },
        "supersedes": None,
    }


def projected(node_id: str = REAL_NODE_ID) -> st.PlanNode:
    plan = pm.parse_mapping(plan_mapping())
    return {n.node_id: n for n in plan.to_plan_nodes()}[node_id]


def render_for(node: st.PlanNode) -> str:
    """The bytes a reviewer would actually be sent for this node."""
    base, output = "a" * 40, "b" * 40
    changed = tuple(node.outputs) or ("src/x.py",)
    objects = cr.review_objects(changed, output)
    digest = cr.review_digest(
        run_id="run-b9", node_id=node.node_id, base_sha=base,
        output_sha=output, rubric_version=cr.CODE_RUBRIC.version)
    matrix = fin.compute_matrix(cr.CODE_RUBRIC, digest, objects)
    handoff = cr.build_handoff(
        subject_digest=digest, run_id="run-b9", node=node, base_sha=base,
        output_sha=output, diff="--- a/x\n+++ b/x\n@@ -1 +1 @@\n-o\n+n\n",
        matrix=matrix, rubric=cr.CODE_RUBRIC,
        report_path=Path("/tmp/report.json"))
    return handoff.render()


# ── B9: the contract reaches the reviewer ───────────────────────────────────

class DeclaredContractReachesTheReviewerTest(unittest.TestCase):
    """The three fields B9 names, checked in the rendered bytes."""

    def test_the_projection_carries_the_declared_instruction(self):
        self.assertEqual(projected().instruction, REAL_INSTRUCTION)

    def test_the_goal_in_the_prompt_is_the_plans_goal(self):
        text = render_for(projected())
        heading = "## What this node was asked to do"
        self.assertIn(heading, text)
        goal = text.split(heading + "\n", 1)[1].split("\n", 1)[0]
        self.assertEqual(goal, REAL_INSTRUCTION)
        # The regression this file exists for: the derived goal is not the
        # goal, and must not appear anywhere in a node that declared one.
        self.assertNotIn(DERIVED_GOAL_PREFIX, text)

    def test_produces_reaches_the_reviewer(self):
        text = render_for(projected())
        for path in REAL_OUTPUTS:
            self.assertIn(path, text)

    def test_acceptance_reaches_the_reviewer_with_its_threshold(self):
        """§10.2's count is half of what a gate demands, so it is contract.

        A gate over one new module is green on one passing test and on seven.
        The plan asked for seven; a reviewer shown the command alone cannot
        tell a satisfied acceptance contract from a third of one.
        """
        node = projected()
        self.assertEqual(node.gate_min_cases, REAL_MIN_CASES)
        text = render_for(node)
        self.assertIn("at least {0} passing case(s)".format(REAL_MIN_CASES),
                      text)
        self.assertIn(" ".join(["pytest"] + REAL_GATE_ARGV), text)

    def test_the_handoff_carries_the_threshold_as_a_field(self):
        """B15's inverse: the number is read, not merely rendered nearby."""
        node = projected()
        text = render_for(node)
        self.assertIn("gate_min_cases", cr.ReviewHandoff.model_fields)
        self.assertNotIn("at least 1 passing case(s)", text)


# ── B15: a dropped field is a raise, not a default ──────────────────────────

class _AgentNodeDeclaringOneMoreField(pm.AgentNode):
    """A future plan-model field, standing in for the next one someone adds.

    The point of the guard is that it does not know about `instruction` or
    `min_cases` specifically. It knows only that the node model declares a
    field and the projection did not account for it — which is the whole
    class of defect, not the two instances of it already paid for.
    """

    contract_field_added_later: str = "unprojected"


class _GateDeclaringOneMoreField(pm.Gate):
    threshold_added_later: int = 3


class ProjectionTotalityTest(unittest.TestCase):
    """§3.6 B15 — a field with no reader is a build failure; here, a raise."""

    def node_and_projection(self):
        plan = pm.parse_mapping(plan_mapping())
        node = plan.node_by_id()[REAL_NODE_ID]
        return node, projected()

    def test_the_shipped_projection_is_total(self):
        """Every field of both node kinds is carried or exempted, today."""
        plan = pm.parse_mapping(plan_mapping())
        by_id = {n.node_id: n for n in plan.to_plan_nodes()}
        for node in plan.nodes:
            pm._assert_projection_is_total(node, by_id[node.node_id])

    def test_a_newly_declared_node_field_with_no_home_raises(self):
        node = _AgentNodeDeclaringOneMoreField(
            kind="agent", node_id=REAL_NODE_ID, needs=(),
            reads=("src-pdf-fingerprint",), outputs=tuple(REAL_OUTPUTS),
            instruction=REAL_INSTRUCTION,
            gate=pm.Gate(runner="pytest", argv=tuple(REAL_GATE_ARGV), cwd=".",
                         min_cases=REAL_MIN_CASES),
            prompt_assets=())
        with self.assertRaises(pm.ProjectionIncomplete) as caught:
            pm._assert_projection_is_total(node, projected())
        self.assertIn("contract_field_added_later", str(caught.exception))
        self.assertIn("_NODE_PROJECTION_EXEMPT", str(caught.exception))

    def test_a_field_carried_by_name_but_not_by_value_raises(self):
        """Stronger than a name check, which the `min_cases` drop would pass.

        A projection that declares the right field and never copies into it
        is the same production outcome as one that has no field at all.
        """
        node, _ = self.node_and_projection()
        wrong = st.PlanNode(
            node_id=REAL_NODE_ID, kind=st.NodeKind.AGENT, depth=0,
            outputs=("some/other/path.py",), instruction=REAL_INSTRUCTION,
            gate_command=("pytest",) + tuple(REAL_GATE_ARGV),
            gate_selector=REAL_GATE_ARGV[0], gate_min_cases=REAL_MIN_CASES)
        with self.assertRaises(pm.ProjectionIncomplete) as caught:
            pm._assert_projection_is_total(node, wrong)
        self.assertIn("outputs", str(caught.exception))

    def test_a_dropped_instruction_raises_rather_than_defaulting(self):
        """The instance this file was written for, as a value drop."""
        node, good = self.node_and_projection()
        blanked = st.PlanNode(
            node_id=good.node_id, kind=good.kind, depth=good.depth,
            outputs=good.outputs, gate_command=good.gate_command,
            gate_selector=good.gate_selector,
            gate_min_cases=good.gate_min_cases, instruction="dropped")
        with self.assertRaises(pm.ProjectionIncomplete) as caught:
            pm._assert_projection_is_total(node, blanked)
        self.assertIn("instruction", str(caught.exception))

    def test_a_newly_declared_gate_field_with_no_home_raises(self):
        """The gate is decomposed, so its fields need their own coverage."""
        node = pm.AgentNode.model_construct(
            kind="agent", node_id=REAL_NODE_ID, needs=(),
            reads=(), outputs=tuple(REAL_OUTPUTS),
            instruction=REAL_INSTRUCTION, prompt_assets=(),
            gate=_GateDeclaringOneMoreField(
                runner="pytest", argv=tuple(REAL_GATE_ARGV), cwd=".",
                min_cases=REAL_MIN_CASES))
        with self.assertRaises(pm.ProjectionIncomplete) as caught:
            pm._assert_projection_is_total(node, projected())
        self.assertIn("threshold_added_later", str(caught.exception))
        self.assertIn("_GATE_PROJECTION", str(caught.exception))

    def test_every_exemption_states_a_reason(self):
        """An empty set cannot distinguish 'decided' from 'forgot'."""
        for name, reason in pm._NODE_PROJECTION_EXEMPT.items():
            self.assertTrue(reason.strip(), name)
        for name in ("kind", "gate", "reads", "cwd", "prompt_assets"):
            self.assertIn(name, pm._NODE_PROJECTION_EXEMPT)
        self.assertNotIn("instruction", pm._NODE_PROJECTION_EXEMPT)


# ── a missing instruction is refused, not filled in ─────────────────────────

class MissingInstructionIsRefusedTest(unittest.TestCase):
    """The deliberate choice: refuse, do not fall back.

    A fallback here rebuilds the defect it is meant to catch. The only goal
    derivable from an agent node is "make your own gate pass", which is not
    independent of the artefact under review and is indistinguishable in the
    prompt from a terse real instruction. `AgentNode.instruction` is
    `min_length=1`, so a blank one is never a plan that omitted a goal — it
    is a projection that dropped one, and that is a defect in Maestro, which
    fails closed with the node named.
    """

    def test_an_agent_node_without_an_instruction_cannot_be_constructed(self):
        with self.assertRaises(ValueError) as caught:
            st.PlanNode(node_id="a", kind=st.NodeKind.AGENT, depth=0,
                        gate_command=("pytest",), gate_selector="k")
        self.assertIn("instruction", str(caught.exception))

    def test_a_code_node_carrying_an_instruction_is_refused(self):
        """§12.3 — a code node's goal is its command; a second one reads to
        nothing."""
        with self.assertRaises(ValueError) as caught:
            st.PlanNode(node_id="c", kind=st.NodeKind.CODE, depth=0,
                        command=("true",), instruction="do something else")
        self.assertIn("instruction", str(caught.exception))

    def test_a_blanked_agent_instruction_refuses_the_handoff(self):
        """The state the system actually shipped in, reproduced past the
        constructor: the reviewer launch is refused instead of starved."""
        node = projected()
        object.__setattr__(node, "instruction", "")
        with self.assertRaises(cr.InstructionNotCarried) as caught:
            cr.build_handoff(
                subject_digest="d" * 64, run_id="run-b9", node=node,
                base_sha="a" * 40, output_sha="b" * 40, diff="d",
                matrix=fin.compute_matrix(
                    cr.CODE_RUBRIC, "d" * 64,
                    cr.review_objects(tuple(node.outputs), "b" * 40)),
                rubric=cr.CODE_RUBRIC, report_path=Path("/tmp/r.json"))
        self.assertIn(REAL_NODE_ID, str(caught.exception))
        self.assertIsInstance(caught.exception, cr.HandoffIncomplete)

    def test_a_code_nodes_goal_is_its_command_and_is_not_a_refusal(self):
        node = projected("n-suite")
        self.assertEqual(node.instruction, "")
        text = render_for(node)
        self.assertIn("pytest -q", text)


# ── the same proof against the real approved plan, when it is present ───────

REAL_PLAN = Path(
    "/Users/davidandrews/PycharmProjects/lexgenius-pipeline/.maestro/plans"
    "/cmo-consolidation-l/maestro-plan.v1")


@unittest.skipUnless(REAL_PLAN.is_file(), "the approved plan is not checked out")
class TheRealApprovedPlanTest(unittest.TestCase):
    """A synthetic node would not have caught this defect, so the fixture
    above is checked against the bytes it was taken from."""

    def test_the_real_node_reaches_its_reviewer_with_its_own_goal(self):
        plan = pm.parse_mapping(json.loads(REAL_PLAN.read_text()))
        node = {n.node_id: n for n in plan.to_plan_nodes()}[REAL_NODE_ID]
        self.assertEqual(node.instruction, REAL_INSTRUCTION)
        self.assertEqual(node.gate_min_cases, REAL_MIN_CASES)
        self.assertEqual(list(node.outputs), REAL_OUTPUTS)
        text = render_for(node)
        self.assertIn(REAL_INSTRUCTION, text)
        self.assertNotIn(DERIVED_GOAL_PREFIX, text)


if __name__ == "__main__":
    unittest.main()
